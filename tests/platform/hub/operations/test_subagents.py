"""U-HUB-operations: `subagent.request` - limits of check H4 (depth, running, total, budget; ADR-017, R-13.8) and the
child session of an accepted request (R-10.6, R-13.2, R-13.5, R-13.7)."""

from __future__ import annotations

from typing import Any

import pytest

from agent_hub.operations import limits
from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.conftest import contracts_registry
from tests.platform.hub.operations.support import Ops
from tests.platform.hub.orgs.support import new_id
from tests.platform.hub.tasks.support import frames, hints

SUBAGENT = "subagent.request"


def _children(ops: Ops, task: str, session: str, states: list[str], depth: int = 1) -> list[str]:
    """Child sessions of `session` in the given states (as earlier accepted requests left them)."""
    created = []
    for state in states:
        child = new_id()
        ops.desk.execute(
            "INSERT INTO hub.session (id, task_id, org_id, wg_id, owner_id, device_id, parent_session_id, depth, state,"
            " created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())",
            child,
            task,
            ops.desk.org,
            ops.desk.wg,
            ops.scene.writer.id,
            ops.device.id,
            session,
            depth,
            state,
        )
        ops.scene.sessions[child] = (task, ops.desk.wg)
        created.append(child)
    return created


def test_accepted_request_creates_the_child_session(ops: Ops, published: MemoryPublisher) -> None:
    task, session = ops.session()
    vm = ops.desk.vm(ops.device, "running")
    ops.desk.execute("UPDATE hub.session SET vm_id = %s WHERE id = %s", vm, session)
    published.clear()
    operation, answer = ops.decide(session, SUBAGENT, dedupe="sub")
    child = answer["child_session_id"]
    assert answer == {"state": "accepted", "child_session_id": child}
    now = ops.hub.clock()
    assert ops.desk.rows(
        "SELECT task_id::text, org_id::text, wg_id::text, owner_id::text, device_id::text, vm_id,"
        " parent_session_id::text, origin_operation_id::text, depth, state, agent_kind, created_at FROM hub.session"
        " WHERE id = %s",
        child,
    ) == [
        (
            task,
            ops.desk.org,
            ops.desk.wg,
            ops.scene.writer.id,
            ops.device.id,
            vm,
            session,
            operation,
            1,
            "requested",
            "claude-code",
            now,
        )
    ]
    assert ops.desk.rows("SELECT seq, state, reported_by FROM hub.session_transition WHERE session_id = %s", child) == [
        (1, "requested", None)
    ]
    audit = ops.desk.rows(
        "SELECT action, actor_kind, actor_id::text, device_id::text FROM hub.audit WHERE session_id = %s"
        " AND target_kind = 'session'",
        child,
    )
    assert audit == [("session.requested", "device", ops.device.id, ops.device.id)]
    assert [action for (action,) in ops.desk.rows("SELECT action FROM hub.audit ORDER BY seq DESC LIMIT 2")] == [
        "session.requested",
        "operation.accepted",
    ]
    assert hints(published, ops.device, "session.start") == [{"type": "session.start", "session_id": child}]
    assert [frame["session"]["session_id"] for frame in frames(published, "session")] == [child]
    assert [frame["task_id"] for frame in frames(published, "task")] == [task]
    state = ops.fleet.client(ops.device).get("/v1/device/state")
    assert state.status_code == 200, state.text
    listed = {item["session_id"]: item for item in state.json()["sessions"]}
    assert listed[child]["parent_session_id"] == session
    assert listed[child]["depth"] == 1
    assert listed[child]["state"] == "requested"


def test_grandchild_and_depth_limit(ops: Ops) -> None:
    task, session = ops.session()
    child = ops.decide(session, SUBAGENT)[1]["child_session_id"]
    ops.scene.sessions[child] = (task, ops.desk.wg)
    ops.desk.execute("UPDATE hub.session SET state = 'working' WHERE id = %s", child)
    grandchild = ops.decide(child, SUBAGENT)[1]["child_session_id"]
    assert ops.desk.rows("SELECT depth, parent_session_id::text FROM hub.session WHERE id = %s", grandchild) == [
        (2, child)
    ]
    ops.scene.sessions[grandchild] = (task, ops.desk.wg)
    ops.desk.execute("UPDATE hub.session SET state = 'working' WHERE id = %s", grandchild)
    operation, answer = ops.decide(grandchild, SUBAGENT)
    assert answer == {"state": "denied", "error_code": "limit_exceeded"}
    assert ops.audit(operation)[0][:2] == ("operation.denied", "denied")
    assert ops.desk.rows("SELECT count(*) FROM hub.session WHERE origin_operation_id = %s", operation) == [(0,)]


def test_running_limit(ops: Ops) -> None:
    task, session = ops.session()
    _children(ops, task, session, ["working", "requested", "stopping", "stopped", "failed"])
    assert ops.decide(session, SUBAGENT)[1] == {"state": "denied", "error_code": "limit_exceeded"}
    ops.desk.execute("UPDATE hub.session SET state = 'stopped' WHERE task_id = %s AND state = 'stopping'", task)
    assert ops.decide(session, SUBAGENT)[1]["state"] == "accepted"


def test_total_limit(ops: Ops) -> None:
    task, session = ops.session()
    _children(ops, task, session, ["stopped"] * 8 + ["failed"])
    assert ops.decide(session, SUBAGENT)[1]["state"] == "accepted"
    assert ops.decide(session, SUBAGENT)[1] == {"state": "denied", "error_code": "limit_exceeded"}


def test_limits_are_per_task(ops: Ops) -> None:
    task, session = ops.session()
    _children(ops, task, session, ["working"] * 3)
    _other_task, other = ops.session()
    assert ops.decide(other, SUBAGENT)[1]["state"] == "accepted"


def test_budget_hook(ops: Ops) -> None:
    task, session = ops.session()
    asked: list[Any] = []

    def refuse(request: limits.BudgetRequest) -> bool:
        asked.append((str(request.org_id), str(request.session_id), request.now))
        return False

    limits.configure(refuse)
    assert ops.decide(session, SUBAGENT, dedupe="b")[1] == {"state": "denied", "error_code": "budget_exceeded"}
    assert asked == [(ops.desk.org, session, ops.hub.clock())]
    assert ops.slot_holder("b") is None
    assert ops.decide(session, "github.comment")[1] == {"state": "accepted"}
    _children(ops, task, session, ["working"] * 3)
    assert ops.decide(session, SUBAGENT)[1] == {"state": "denied", "error_code": "limit_exceeded"}
    assert len(asked) == 1
    limits.configure(lambda _request: True)
    ops.desk.execute("UPDATE hub.session SET state = 'stopped' WHERE parent_session_id = %s", session)
    assert ops.decide(session, SUBAGENT)[1]["state"] == "accepted"


def test_h4_before_h5(ops: Ops) -> None:
    task, session = ops.session()
    holder, _answer = ops.decide(session, SUBAGENT, dedupe="same")
    _children(ops, task, session, ["working"] * 2)
    assert ops.decide(session, SUBAGENT, dedupe="same")[1] == {"state": "denied", "error_code": "limit_exceeded"}
    assert ops.slot_holder("same") == holder


def test_duplicate_subagent_request_creates_no_child(ops: Ops) -> None:
    _task, session = ops.session()
    original, first = ops.decide(session, SUBAGENT, dedupe="d")
    duplicate, answer = ops.decide(session, SUBAGENT, dedupe="d")
    assert answer == {"state": "duplicate", "duplicate_of": original, "original": {"state": "accepted"}}
    assert ops.desk.rows("SELECT count(*) FROM hub.session WHERE origin_operation_id = %s", duplicate) == [(0,)]
    assert "child_session_id" in first


def test_repeated_accepted_request_answers_the_same_child(ops: Ops) -> None:
    _task, session = ops.session()
    body = ops.request(session, SUBAGENT)
    first = ops.register(body).json()
    again = ops.register(body).json()
    assert again == {**first, "replayed": True}
    assert ops.desk.rows("SELECT count(*) FROM hub.session WHERE parent_session_id = %s", session) == [(1,)]
    contracts_registry().validate("api/device-operations.response.json", again)


@pytest.mark.parametrize("state", ["stopped", "failed"])
def test_ended_child_cannot_request(ops: Ops, state: str) -> None:
    task, session = ops.session()
    (child,) = _children(ops, task, session, [state])
    assert ops.decide(child, SUBAGENT)[1] == {"state": "denied", "error_code": "rights_denied"}
