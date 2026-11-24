"""U-HUB-attention (notifications): `task.attention` once per occasion to the task owner, workgroup-wide causes once
per owner, workgroup and reason while unread, a further occasion of a task only once the previous notification of
that task was read, nothing for a stuck failed task (`task.failed` covers it), no `task.failed` for a failed task that
needs a decision, and no `task.idle` for a task that needs a decision
(03-api.md section 4.9, ADR-076 section 5; KOR-39)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from agent_hub import signals as hub_signals
from agent_hub.notifications import rules, signals
from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.attention.support import Situation

pytestmark = pytest.mark.usefixtures("bound")


def _notifications(situation: Situation, kind: str = rules.TASK_ATTENTION) -> list[tuple[Any, ...]]:
    signals.wait()
    return list(
        situation.desk.rows(
            "SELECT recipient_id::text, wg_id::text, task_id::text, ref FROM hub.notification WHERE kind = %s"
            " ORDER BY created_at, id",
            kind,
        )
    )


def test_a_question_notifies_the_owner_once_and_no_idle_notification(
    situation: Situation, published: MemoryPublisher
) -> None:
    task, session = situation.working()
    situation.hub.clock.advance(30)  # long enough working for a task.idle
    situation.ask(session)
    situation.step(session, "idle")
    record = situation.record(task)
    assert record is not None
    expected = [(situation.scene.writer.id, situation.desk.wg, task, f"needs_decision:question:{record.seq}")]
    assert _notifications(situation) == expected
    assert _notifications(situation, rules.TASK_IDLE) == []
    sent = [item.data for item in published.items() if item.frame == "notification"]
    assert [data["kind"] for data in sent] == [rules.TASK_ATTENTION]
    ids: dict[str, UUID | int | str] = {
        "task_id": UUID(task),
        "org_id": UUID(situation.desk.org),
        "wg_id": UUID(situation.desk.wg),
        "owner_id": UUID(situation.scene.writer.id),
        "category": "needs_decision",
        "reason": "question",
        "seq": record.seq,
        "task_state": "idle",
    }
    hub_signals.emit("task.attention", **ids)  # the same occasion again
    assert _notifications(situation) == expected
    situation.ask(session, "human.approval")  # a new occasion, but the first notification is still unread
    assert _notifications(situation) == expected
    situation.desk.execute("UPDATE hub.notification SET read_at = now() WHERE kind = %s", rules.TASK_ATTENTION)
    situation.ask(session, "human.question")  # read: the next occasion notifies again
    assert [row[3] for row in _notifications(situation)] == [
        expected[0][3],
        f"needs_decision:question:{record.seq + 2}",
    ]


def test_an_idle_task_without_a_decision_still_notifies_idle(situation: Situation) -> None:
    task, session = situation.working()
    situation.hub.clock.advance(30)
    situation.step(session, "idle")
    assert [row[2] for row in _notifications(situation, rules.TASK_IDLE)] == [task]
    assert _notifications(situation) == []


def test_a_failed_task_notifies_once_either_failed_or_needing_a_decision(situation: Situation) -> None:
    stuck, stuck_session = situation.working()
    situation.step(stuck_session, "failed", "agent_exited")
    assert situation.kind(stuck) == ("blocked", "session_failed")
    credit, credit_session = situation.working()
    situation.step(credit_session, "failed", "budget_exceeded")
    record = situation.record(credit)
    assert record is not None
    assert _notifications(situation) == [
        (situation.scene.writer.id, situation.desk.wg, credit, f"needs_decision:credit_empty:{record.seq}")
    ]
    assert [row[2] for row in _notifications(situation, rules.TASK_FAILED)] == [stuck]


def test_workgroup_wide_causes_notify_once_while_unread(situation: Situation) -> None:
    situation.desk.offline(situation.scene.device)
    first = situation.desk.create(situation.scene.writer).json()["task"]["task_id"]
    second = situation.desk.create(situation.scene.writer).json()["task"]["task_id"]
    assert situation.kind(first) == situation.kind(second) == ("blocked", "device_unreachable")
    found = _notifications(situation)
    assert found == [(situation.scene.writer.id, situation.desk.wg, first, "blocked:device_unreachable:wg")]
    situation.desk.execute("UPDATE hub.notification SET read_at = now() WHERE kind = %s", rules.TASK_ATTENTION)
    third = situation.desk.create(situation.scene.writer).json()["task"]["task_id"]
    assert sorted(row[2] for row in _notifications(situation)) == sorted([first, third])


def test_no_notification_when_the_owner_lost_the_workgroup_or_the_occasion_passed(situation: Situation) -> None:
    task, session = situation.working()
    situation.ask(session)
    situation.step(session, "idle")
    record = situation.record(task)
    assert record is not None
    assert len(_notifications(situation)) == 1
    situation.desk.execute("DELETE FROM hub.notification")
    occasion = rules.Occasion(UUID(task), "needs_decision", "question", record.seq + 5, "idle")
    with situation.hub.pool.connection() as connection:
        assert rules.attention_rule(connection, occasion, situation.hub.clock()) == []  # not the current occasion
    situation.desk.execute(
        "UPDATE hub.wg_access SET revoked_seq = granted_seq + 1 WHERE wg_id = %s AND user_id = %s",
        situation.desk.wg,
        situation.scene.writer.id,
    )
    current = rules.Occasion(UUID(task), "needs_decision", "question", record.seq, "idle")
    with situation.hub.pool.connection() as connection:
        assert rules.attention_rule(connection, current, situation.hub.clock()) == []
