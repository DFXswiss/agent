"""U-HUB-tasks (transitions): `agent_hub.sessions.transitions.apply` as the outbox calls it - the state machine with
refusals, device and receive times, `ready_at`/`first_working_at` from the hub clock, the derived task state, audit,
frames and signals (02-data-model.md sections 2.4, 2.4.1; 03-api.md sections 4.6.1 step 5, 6.1)."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from agent_hub.errors import HubError
from agent_hub.sessions.effects import Effects
from agent_hub.sessions.transitions import SESSION_FAILURE_REASONS, TransitionRefused, apply
from agent_hub.stream import publish
from agent_hub.stream.publish import MemoryPublisher, Published
from tests.platform.contracts.harness import CONTRACTS
from tests.platform.hub.orgs.support import new_id
from tests.platform.hub.tasks.support import Desk, audit_rows, frames


def test_the_start_chain_drives_session_and_task(
    desk: Desk, published: MemoryPublisher, task_signals: list[dict[str, Any]]
) -> None:
    task, session = desk.started()
    created = desk.hub.clock()
    published.clear()
    task_signals.clear()
    chain = ["vm_assigning", "vm_ready", "sandbox_ready", "agent_starting", "prompt_delivering"]
    for state in chain:
        desk.hub.clock.advance(0.5)
        applied = desk.transition(session, state)
        assert (applied.changed, applied.state) == (True, state)
        assert desk.task(task)["state"] == "starting"
    ready = desk.hub.clock()
    desk.hub.clock.advance(0.7)
    device_time = desk.hub.clock() - timedelta(seconds=3)
    desk.transition(
        session, "working", at=device_time, agent_version="2.1.330", model_access="platform", model_id="deepseek/x"
    )
    row = desk.session(session)
    assert row["ready_at"] == ready.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    # first_working_at is the hub's receive time, never the device clock (03-api.md section 4.6.1 step 5)
    assert row["first_working_at"] == desk.hub.clock().isoformat(timespec="milliseconds").replace("+00:00", "Z")
    assert (row["agent_version"], row["model_access"], row["model_id"]) == ("2.1.330", "platform", "deepseek/x")
    assert desk.hub.clock() - created == timedelta(seconds=3.2)
    stored = desk.rows(
        "SELECT state, at, received_at, reported_by FROM hub.session_transition WHERE session_id = %s ORDER BY seq",
        session,
    )
    assert [item[0] for item in stored] == ["requested", *chain, "working"]
    assert stored[-1][1] == device_time
    assert stored[-1][2] == desk.hub.clock()
    assert stored[-1][3] == UUID(desk.scene.device.id)
    assert desk.task(task)["state"] == "working"
    assert task_signals == [{"task_id": UUID(task), "state": "working"}]
    session_frames = frames(published, "session")
    assert [item["transition"]["state"] for item in session_frames] == [*chain, "working"]
    assert frames(published, "task")[-1]["state"] == "working"
    working = audit_rows(desk, "session.working")
    assert [(row[0], row[1], row[3]) for row in working] == [
        ("device", UUID(desk.scene.device.id), {"reason": None, "model_access": "platform", "model_id": "deepseek/x"})
    ]
    # later working periods keep the first times
    desk.hub.clock.advance(10)
    desk.walk(session, "idle", "working")
    assert desk.session(session)["first_working_at"] == row["first_working_at"]
    assert desk.task(task)["state"] == "working"
    desk.walk(session, "idle", "suspended")
    assert desk.task(task)["state"] == "idle"
    desk.transition(session, "resuming")
    assert desk.task(task)["state"] == "starting"
    desk.walk(session, "working", "stopping", "stopped")
    final = desk.session(session)
    assert final["ended_at"] == desk.hub.clock().isoformat(timespec="milliseconds").replace("+00:00", "Z")
    assert desk.task(task)["state"] == "idle"


def test_failure_carries_its_reason_to_the_task(desk: Desk, published: MemoryPublisher) -> None:
    task, session = desk.started()
    desk.transition(session, "vm_assigning")
    desk.transition(session, "failed", "vm_start_failed", model_access="subscription")
    detail = desk.task(task)
    assert (detail["state"], detail["state_reason"]) == ("failed", "vm_start_failed")
    session_row = desk.session(session)
    assert (session_row["state"], session_row["state_reason"]) == ("failed", "vm_start_failed")
    assert session_row["ended_at"] is not None
    assert [row[3] for row in audit_rows(desk, "session.failed")] == [
        {"reason": "vm_start_failed", "model_access": "subscription"}
    ]


def test_a_working_child_makes_the_task_working(desk: Desk) -> None:
    task, main = desk.started()
    desk.walk(main, "prompt_delivering", "working", "idle")
    child = new_id()
    desk.execute(
        "INSERT INTO hub.session (id, task_id, org_id, wg_id, owner_id, device_id, parent_session_id, depth, state,"
        " created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, 1, 'requested', %s)",
        child,
        task,
        desk.org,
        desk.wg,
        desk.scene.writer.id,
        desk.scene.device.id,
        main,
        desk.hub.clock(),
    )
    desk.walk(child, "agent_starting", "working")
    assert desk.task(task)["state"] == "working"
    desk.transition(child, "stopped")
    assert desk.task(task)["state"] == "idle"


@pytest.mark.parametrize(
    ("path", "state", "reason"),
    [
        (["prompt_delivering"], "vm_assigning", None),
        (["prompt_delivering", "working"], "suspended", None),
        (["stopped"], "working", None),
        (["failed"], "stopped", None),
        (["stopping"], "idle", None),
    ],
)
def test_refused_transitions_change_nothing(desk: Desk, path: list[str], state: str, reason: str | None) -> None:
    _, session = desk.started()
    for step in path:
        desk.transition(session, step, "oom" if step == "failed" else None)
    before = desk.rows("SELECT count(*) FROM hub.session_transition WHERE session_id = %s", session)
    with pytest.raises(TransitionRefused) as raised:
        desk.transition(session, state, reason)
    assert raised.value.code == "request_invalid"
    assert desk.rows("SELECT count(*) FROM hub.session_transition WHERE session_id = %s", session) == before
    assert desk.session(session)["state"] == path[-1]


def test_the_current_state_again_is_no_change(desk: Desk, published: MemoryPublisher) -> None:
    _, session = desk.started()
    desk.transition(session, "vm_assigning")
    published.clear()
    applied = desk.transition(session, "vm_assigning")
    assert (applied.changed, applied.state) == (False, "vm_assigning")
    assert published.items() == []
    assert desk.rows("SELECT count(*) FROM hub.session_transition WHERE session_id = %s", session) == [(2,)]


@pytest.mark.parametrize(
    ("state", "reason", "details", "code"),
    [
        ("failed", None, {}, "request_invalid"),
        ("failed", "made_up", {}, "request_invalid"),
        ("stopped", "Not-A-Code", {}, "request_invalid"),
        ("stopped", 7, {}, "request_invalid"),
        ("dancing", None, {}, "request_invalid"),
        ("working", None, {"model_access": "auto"}, "request_invalid"),
    ],
)
def test_reasons_and_details_are_checked(
    desk: Desk, state: str, reason: Any, details: dict[str, str], code: str
) -> None:
    _, session = desk.started()
    with pytest.raises(HubError) as raised:
        desk.transition(session, state, reason, model_access=details.get("model_access"))
    assert raised.value.code == code


def test_only_the_session_device_reports(desk: Desk) -> None:
    _, session = desk.started()
    with pytest.raises(HubError) as raised:
        desk.transition(session, "vm_assigning", device=desk.scene.other_device)
    assert (raised.value.code, raised.value.status) == ("forbidden", 403)
    with pytest.raises(HubError) as missing:
        desk.transition(new_id(), "vm_assigning")
    assert missing.value.code == "not_found"


def test_a_hub_internal_caller_reports_without_device(desk: Desk) -> None:
    task, session = desk.started()
    now = desk.hub.clock()
    with desk.hub.pool.connection() as connection, connection.transaction():
        applied = apply(connection, session, "stopped", "unassigned", now, received_at=now, reported_by=None)
    applied.effects.flush()
    assert desk.session(session)["state_reason"] == "unassigned"
    assert desk.rows("SELECT reported_by FROM hub.session_transition WHERE session_id = %s AND seq = 2", session) == [
        (None,)
    ]
    assert audit_rows(desk, "session.stopped")[0][0] == "hub"
    assert desk.task(task)["state"] == "idle"


def test_failure_reasons_equal_the_contract() -> None:
    enum = json.loads(Path(CONTRACTS, "enums", "session-failure-reasons.json").read_text(encoding="utf-8"))
    assert set(enum["reasons"]) == SESSION_FAILURE_REASONS


class Failing:
    def publish(self, item: Published) -> None:
        raise RuntimeError("down")


def test_a_failing_publisher_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    previous = publish.set_publisher(Failing())
    try:
        effects = Effects()
        effects.hint(UUID(new_id()), "reconcile", {})
        other = Effects()
        other.signal("task.state", task_id=UUID(new_id()), state="idle")
        effects.extend(other)
        with caplog.at_level(logging.ERROR, logger="agent_hub.sessions"):
            effects.flush()
    finally:
        publish.set_publisher(previous)
    assert "could not be published" in caplog.text
    assert effects.frames == []
    assert effects.signals == []


def test_a_failed_resume_falls_back_to_suspended(desk: Desk) -> None:
    task, session = desk.started()
    desk.walk(session, "prompt_delivering", "working", "idle", "suspended", "resuming", "suspended")
    assert desk.session(session)["state"] == "suspended"
    assert desk.task(task)["state"] == "idle"
