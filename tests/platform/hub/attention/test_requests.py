"""U-HUB-attention (requests, KOR-49, ADR-078 section 4, rule E-05): a missing prerequisite asks the person who can
start the step - `operator.request` to the users with the platform role `operator` (credit used up, platform access
missing; account level, once per organization and occasion, not while an earlier request of the same reason is
unread), `device.needed` to the admins of the organization except the task owner (once per workgroup while unread)."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import pytest

from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.notifications import rules, signals
from tests.platform.hub.attention.support import Situation
from tests.platform.hub.orgs.support import new_id

pytestmark = pytest.mark.usefixtures("bound")


def _rows(situation: Situation, kind: str) -> list[tuple[Any, ...]]:
    signals.wait()
    return list(
        situation.desk.rows(
            "SELECT recipient_id::text, org_id::text, wg_id::text, ref FROM hub.notification WHERE kind = %s"
            " ORDER BY created_at, id",
            kind,
        )
    )


def _operator(situation: Situation, user: str, seq: int = 1) -> None:
    situation.desk.execute(
        "INSERT INTO hub.platform_role (user_id, role, granted_seq) VALUES (%s, 'operator', %s)", user, seq
    )


def _waiting(situation: Situation, reason: str) -> str:
    task = situation.insert_task(situation.desk.wg, situation.scene.writer.id, state="scheduling")
    situation.desk.execute("UPDATE hub.task SET state_reason = %s WHERE id = %s", reason, task)
    return task


def _ask(situation: Situation, task: str, reason: str, category: str = "needs_decision") -> list[rules.Notification]:
    occasion = rules.Occasion(UUID(task), category, reason, 1, "scheduling")
    with situation.hub.pool.connection() as connection, connection.transaction():
        return rules.request_rule(connection, occasion, situation.hub.clock())


def test_used_up_credit_asks_every_operator_once_per_occasion(situation: Situation) -> None:
    operator, other = situation.scene.stranger.id, situation.scene.reader.id
    _operator(situation, operator)
    _operator(situation, other, 2)
    task, session = situation.working()
    situation.step(session, "failed", "credit_exhausted")  # the attention record enters `credit_empty`
    ref = f"credit_exhausted:{situation.desk.org}"
    assert sorted(_rows(situation, rules.OPERATOR_REQUEST)) == sorted(
        [(operator, None, None, ref), (other, None, None, ref)]
    )
    assert _ask(situation, task, "credit_empty") == []  # the same organization again: asked already
    situation.desk.execute("UPDATE hub.notification SET read_at = now() WHERE kind = %s", rules.OPERATOR_REQUEST)
    assert _ask(situation, task, "credit_empty") == []  # read, but no credit since: still the same occasion
    situation.hub.clock.advance(5)
    with situation.hub.pool.connection() as connection, connection.transaction():
        row = AuditRow(
            at=situation.hub.clock(),
            actor_kind="operator",
            action="credit.changed",
            result="ok",
            org_id=UUID(situation.desk.org),
            target_kind="org",
            target_id=situation.desk.org,
            detail={"kind": "set", "amount_micro_usd": 5_000_000, "balance_micro_usd": 5_000_000, "actor": "operator"},
        )
        append_audit(connection, row)
    situation.hub.clock.advance(5)
    again = _ask(situation, task, "credit_empty")  # credit was set and is used up again: a new occasion
    assert sorted(row.recipient_id for row in again) == sorted(UUID(user) for user in (operator, other))
    assert all(row.org_id is None and row.ref == ref for row in again)


def test_an_unread_request_holds_back_further_ones_of_the_same_reason(situation: Situation) -> None:
    _operator(situation, situation.scene.stranger.id)
    first = _waiting(situation, "credit_exhausted")
    assert len(_ask(situation, first, "credit_empty")) == 1
    situation.desk.execute("UPDATE hub.notification SET ref = %s", f"credit_exhausted:{new_id()}")  # another org's
    assert _ask(situation, first, "credit_empty") == []  # unread request of the same reason: no flood


def test_missing_platform_access_asks_the_operators_until_answered(situation: Situation) -> None:
    _operator(situation, situation.scene.stranger.id)
    task = _waiting(situation, "platform_access_required")
    asked = _ask(situation, task, "device_missing")
    assert [(row.ref, row.org_id, row.kind) for row in asked] == [
        (f"platform_access_required:{situation.desk.org}", None, rules.OPERATOR_REQUEST)
    ]
    situation.desk.execute("UPDATE hub.notification SET read_at = now()")
    assert _ask(situation, task, "device_missing") == []
    assert _ask(situation, task, "credit_empty") != []  # another reason is another request


def test_without_an_operator_the_request_is_logged(situation: Situation, caplog: pytest.LogCaptureFixture) -> None:
    task = _waiting(situation, "credit_exhausted")
    with caplog.at_level(logging.WARNING, logger="agent_hub.notifications"):
        assert _ask(situation, task, "credit_empty") == []
    assert f"operator_request_unanswered reason=credit_exhausted org_id={situation.desk.org}" in caplog.text


def test_a_workgroup_without_a_device_asks_the_admins_except_the_owner(situation: Situation) -> None:
    task = _waiting(situation, "no_device")
    asked = _ask(situation, task, "device_missing")
    admin = situation.scene.admin.id
    assert [(str(row.recipient_id), str(row.wg_id), row.ref) for row in asked] == [
        (admin, situation.desk.wg, situation.desk.wg)
    ]
    assert _ask(situation, _waiting(situation, "no_device"), "device_missing") == []  # unread: once per workgroup
    situation.desk.execute("UPDATE hub.notification SET read_at = now() WHERE kind = %s", rules.DEVICE_NEEDED)
    assert len(_ask(situation, task, "device_missing")) == 1
    situation.desk.execute("UPDATE hub.task SET owner_id = %s WHERE id = %s", admin, task)
    situation.desk.execute("UPDATE hub.notification SET read_at = now()")
    assert _ask(situation, task, "device_missing") == []  # the owner is the only admin: `task.attention` covers it


def test_other_occasions_ask_nobody(situation: Situation) -> None:
    _operator(situation, situation.scene.stranger.id)
    task = _waiting(situation, "device_offline")
    assert _ask(situation, task, "device_missing") == []  # waiting for another reason
    assert _ask(situation, task, "question") == []
    assert _ask(situation, task, "device_unreachable", "blocked") == []
    assert _ask(situation, new_id(), "credit_empty") == []  # unknown task
    assert _rows(situation, rules.OPERATOR_REQUEST) == _rows(situation, rules.DEVICE_NEEDED) == []
