"""U-HUB-attention (store): the record follows every writer of its inputs in the writer's transaction - task and
session transitions, prompts, closing, operation registrations, outbox steps and the expiry job - with the rules of
`seq` and `since`, the `attention` frame for the workgroup's readers and the signal `task.attention` (ADR-064
sections 1, 2 and 4; KOR-39)."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator, Mapping
from typing import Any
from uuid import UUID

import pytest

from agent_hub import signals
from agent_hub.attention import store
from agent_hub.operations import jobs as operation_jobs
from agent_hub.sessions.effects import Effects
from agent_hub.stream.publish import Audience, MemoryPublisher, Published
from agent_hub.stream.sse import UNKNOWN, frame_wg
from tests.platform.hub.attention.support import Situation, attention_frames
from tests.platform.hub.orgs.support import new_id


@pytest.fixture
def occasions() -> Iterator[list[dict[str, Any]]]:
    """Every `task.attention` signal (the discovered subscribers stay)."""
    seen: list[dict[str, Any]] = []

    def record(_name: str, ids: Mapping[str, Any]) -> None:
        seen.append({key: str(value) for key, value in ids.items()})

    found = {name: list(items) for name, items in signals.configure().items()}
    found.setdefault(store.SIGNAL, []).append(record)
    signals.configure(found)
    yield seen
    signals.configure()


def _state_at(situation: Situation, task: str) -> Any:
    return situation.desk.rows("SELECT state_at FROM hub.task WHERE id = %s", task)[0][0]


def test_a_new_task_gets_its_record_and_a_phase_change_keeps_seq_and_since(
    situation: Situation, published: MemoryPublisher
) -> None:
    task, session = situation.started()
    created = situation.record(task)
    assert created is not None
    assert (created.kind, created.seq, created.since) == (("working", "starting"), 1, _state_at(situation, task))
    assert (created.ref_kind, created.ref_id, created.subagents_total, created.subagents_active) == (None, None, 0, 0)
    sent = attention_frames(published)
    assert sent[-1][0] == situation.desk.wg
    with situation.hub.pool.connection() as connection:
        assert sent[-1][1] == store.row_json(connection, task)
        assert store.row_json(connection, new_id()) is None
    situation.step(session, "working")
    working = situation.record(task)
    assert working is not None
    assert (working.kind, working.seq, working.since) == (("working", "working"), 1, created.since)
    situation.step(session, "idle")
    done = situation.record(task)
    assert done is not None
    assert (done.kind, done.seq, done.since) == (("done", "idle"), 2, situation.hub.clock())


def test_a_question_needs_a_decision_until_the_next_prompt(
    situation: Situation, published: MemoryPublisher, occasions: list[dict[str, Any]]
) -> None:
    task, session = situation.working()
    operation = situation.ask(session)
    assert situation.kind(task) == ("working", "working")  # the agent still works: not yet a question
    situation.step(session, "idle")
    asked = situation.record(task)
    assert asked is not None
    assert (asked.kind, asked.ref_kind, asked.ref_id, asked.seq) == (
        ("needs_decision", "question"),
        "operation",
        operation,
        2,
    )
    assert asked.since == situation.hub.clock()
    assert occasions == [
        {
            "task_id": task,
            "org_id": situation.desk.org,
            "wg_id": situation.desk.wg,
            "owner_id": situation.scene.writer.id,
            "category": "needs_decision",
            "reason": "question",
            "seq": "2",
            "task_state": "idle",
        }
    ]
    assert attention_frames(published)[-1][1]["reason"] == "question"
    situation.prompt(task)  # the answer, in the transaction that stores the prompt
    answered = situation.record(task)
    assert answered is not None
    assert (answered.kind, answered.seq, answered.ref_id) == (("done", "idle"), 3, None)
    assert len(occasions) == 1


def test_another_question_while_waiting_is_a_new_occasion_in_the_same_group(
    situation: Situation, occasions: list[dict[str, Any]]
) -> None:
    task, session = situation.working()
    situation.ask(session)
    situation.step(session, "idle")
    first = situation.record(task)
    approval = situation.ask(session, "human.approval")
    second = situation.record(task)
    assert first is not None
    assert second is not None
    assert (second.kind, second.ref_id, second.seq, second.since) == (
        ("needs_decision", "approval"),
        approval,
        first.seq + 1,
        first.since,
    )
    assert [item["reason"] for item in occasions] == ["question", "approval"]
    assert situation.refresh(task) is False  # the same derivation again changes nothing
    assert len(occasions) == 2


def test_an_agent_that_carries_on_after_asking_starts_a_new_turn(situation: Situation) -> None:
    task, session = situation.working()
    situation.ask(session)
    situation.step(session, "idle")
    assert situation.kind(task) == ("needs_decision", "question")
    situation.step(session, "working")  # e.g. woken by a subagent's finish
    assert situation.kind(task) == ("working", "working")
    situation.step(session, "idle")
    assert situation.kind(task) == ("done", "idle")


def test_a_refused_operation_counts_until_the_same_type_succeeds(situation: Situation) -> None:
    task, session = situation.working()
    refused = situation.refused(session, "github.push", "github_not_connected")
    situation.step(session, "idle")
    record = situation.record(task)
    assert record is not None
    assert (record.kind, record.ref_kind, record.ref_id) == (
        ("needs_decision", "connection_missing"),
        "operation",
        refused,
    )
    situation.prompt(task)
    situation.step(session, "working")
    situation.refused(session, "github.push", "secret_detected")
    situation.succeeded(session, "github.push")
    situation.step(session, "idle")
    assert situation.kind(task) == ("done", "idle")


def test_the_newest_operation_that_matters_decides(situation: Situation) -> None:
    task, session = situation.working()
    situation.ask(session)
    refused = situation.refused(session, "github.commit", "branch_protected")
    situation.refused(session, "github.commit", "no_changes")  # the agent's own business
    situation.step(session, "idle")
    record = situation.record(task)
    assert record is not None
    assert (record.kind, record.ref_id) == (("blocked", "operation_refused"), refused)


def test_running_operations_and_their_steps_refresh_the_record(situation: Situation) -> None:
    task, session = situation.working()
    situation.hub.clock.advance(1)
    operation, _answer = situation.ops.decide(session, "github.comment")
    assert situation.kind(task) == ("working", "operation")
    situation.ops.step(operation, "executing")
    assert situation.kind(task) == ("working", "operation")
    situation.ops.finish(operation)
    assert situation.kind(task) == ("working", "working")
    waiting, _answer = situation.ops.decide(session, "github.comment")
    assert situation.kind(task) == ("working", "operation")
    situation.hub.clock.advance(31 * 60)
    assert operation_jobs.expire(situation.hub.app.state.hub) == 1
    assert situation.ops.state(waiting) == "expired"
    assert situation.kind(task) == ("working", "working")


def test_subagents_are_counted_and_keep_the_task_running(situation: Situation) -> None:
    task, session = situation.working()
    situation.hub.clock.advance(1)
    operation, answer = situation.ops.decide(session, "subagent.request", dedupe="helper")
    child = answer["child_session_id"]
    situation.scene.sessions[child] = (task, situation.desk.wg)
    situation.ops.finish(operation)
    record = situation.record(task)
    assert record is not None
    assert (record.kind, record.subagents_total, record.subagents_active) == (("working", "working"), 1, 1)
    situation.step(child, "working")
    situation.step(session, "idle")
    assert situation.kind(task) == ("working", "subagents")
    situation.step(child, "stopped")
    record = situation.record(task)
    assert record is not None
    assert (record.kind, record.subagents_total, record.subagents_active) == (("done", "idle"), 1, 0)


def test_failed_tasks(situation: Situation, occasions: list[dict[str, Any]]) -> None:
    task, session = situation.working()
    situation.step(session, "failed", "budget_exceeded")
    record = situation.record(task)
    assert record is not None
    assert (record.kind, record.ref_kind, record.ref_id) == (("needs_decision", "credit_empty"), "session", session)
    other, other_session = situation.working()
    situation.step(other_session, "failed", "agent_exited")
    assert situation.kind(other) == ("blocked", "session_failed")
    assert [(item["reason"], item["task_state"]) for item in occasions] == [
        ("credit_empty", "failed"),
        ("session_failed", "failed"),
    ]


def test_a_credit_stop_of_an_idle_session_needs_a_decision(situation: Situation) -> None:
    task, session = situation.working()
    situation.step(session, "idle", "budget_exceeded")
    record = situation.record(task)
    assert record is not None
    assert (record.kind, record.ref_kind, record.ref_id) == (("needs_decision", "credit_empty"), "session", session)


def test_scheduling_without_a_reachable_or_assigned_device(situation: Situation) -> None:
    situation.desk.offline(situation.scene.device)
    task = situation.desk.create(situation.scene.writer).json()["task"]["task_id"]
    assert situation.kind(task) == ("blocked", "device_unreachable")
    situation.desk.execute(
        "UPDATE hub.wg_device SET unassigned_seq = assigned_seq + 1 WHERE wg_id = %s", situation.desk.wg
    )
    assert situation.refresh(task) is True
    record = situation.record(task)
    assert record is not None
    assert (record.kind, record.ref_kind, record.seq) == (("needs_decision", "device_missing"), None, 2)


def test_closing_and_reopening(situation: Situation, published: MemoryPublisher) -> None:
    task, session = situation.working()
    situation.hub.clock.advance(1)
    operation, answer = situation.ops.decide(session, "subagent.request", dedupe="helper")
    child = answer["child_session_id"]
    situation.scene.sessions[child] = (task, situation.desk.wg)
    situation.step(child, "working")
    situation.hub.clock.advance(1)
    response = situation.scene.writer.browser.post(f"/v1/tasks/{task}/close", json={})
    assert response.status_code == 200, response.text
    closed = situation.record(task)
    assert closed is not None
    assert (closed.kind, closed.seq, closed.subagents_active) == (("done", "closed"), 2, 1)
    published.clear()
    situation.step(session, "stopped")
    assert attention_frames(published) == []  # nothing of the record changed
    situation.step(child, "stopped")
    record = situation.record(task)
    assert record is not None
    assert (record.kind, record.seq, record.subagents_active) == (("done", "closed"), 2, 0)
    assert [data["subagents_active"] for _wg, data in attention_frames(published)] == [0]
    situation.prompt(task)
    reopened = situation.record(task)
    assert reopened is not None
    assert (reopened.category, reopened.seq) == ("working", 3)
    assert situation.ops.state(operation) == "accepted"


def test_an_unknown_task_has_no_record(situation: Situation) -> None:
    assert situation.refresh(new_id()) is False
    assert situation.desk.rows("SELECT count(*) FROM hub.task_attention") == [(0,)]


def test_a_lost_row_is_derived_again_with_its_task_state_time(situation: Situation) -> None:
    task, session = situation.working()
    situation.step(session, "idle")
    situation.desk.execute("DELETE FROM hub.task_attention WHERE task_id = %s", task)
    situation.hub.clock.advance(60)
    assert situation.refresh(task) is True
    record = situation.record(task)
    assert record is not None
    assert (record.kind, record.seq, record.since) == (("done", "idle"), 1, _state_at(situation, task))


def test_concurrent_refreshes_of_one_task_serialize_on_the_row(situation: Situation) -> None:
    task, session = situation.working()
    situation.desk.execute("UPDATE hub.session SET state = 'idle' WHERE id = %s", session)
    situation.desk.execute("UPDATE hub.task SET state = 'idle' WHERE id = %s", task)
    results: list[bool] = []
    hub = situation.hub
    with hub.pool.connection() as first, first.transaction():
        assert store.refresh(first, task, hub.clock(), Effects()) is True

        def second() -> None:
            with hub.pool.connection() as connection, connection.transaction():
                results.append(store.refresh(connection, task, hub.clock(), Effects()))

        thread = threading.Thread(target=second)
        thread.start()
        time.sleep(0.3)
        assert results == []  # waits for the first transaction
    thread.join(timeout=10)
    assert results == [False]  # derives from the committed state: nothing left to change
    record = situation.record(task)
    assert record is not None
    assert (record.kind, record.seq) == (("done", "idle"), 2)


def test_an_attention_frame_reaches_only_the_readers_of_its_workgroup() -> None:
    org, wg = UUID(int=5), UUID(int=6)
    with_wg = Published(Audience("org", org), store.FRAME, {"task_id": str(UUID(int=7))}, wg_id=wg)
    assert frame_wg(with_wg) == wg
    assert frame_wg(Published(Audience("org", org), store.FRAME, {"task_id": str(UUID(int=7))})) == UNKNOWN


@pytest.mark.parametrize("op_type", ["human.question", "human.approval"])
def test_a_subagent_cannot_ask_a_person_and_its_refusal_is_not_the_main_sessions(
    situation: Situation, op_type: str
) -> None:
    task, session = situation.working()
    situation.hub.clock.advance(1)
    operation, answer = situation.ops.decide(session, "subagent.request", dedupe="helper")
    child = answer["child_session_id"]
    situation.scene.sessions[child] = (task, situation.desk.wg)
    situation.ops.finish(operation)
    situation.step(child, "working")
    situation.hub.clock.advance(1)
    asked, refusal = situation.ops.decide(child, op_type)
    assert refusal == {"state": "denied", "error_code": "rights_denied"}
    assert situation.ops.audit(asked)[-1][:2] == ("operation.denied", "denied")
    situation.step(child, "stopped")
    situation.step(session, "idle")
    assert situation.kind(task) == ("done", "idle")
