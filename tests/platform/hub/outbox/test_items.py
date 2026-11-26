"""U-HUB-outbox (item kinds): the effect and the rights of every item kind of `contracts/enums/outbox-item-kinds.json`
(03-api.md section 6.1, 02-data-model.md sections 2.3-2.9) and the frames after the commit."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest

from agent_hub.crypto.encoding import format_time
from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.conftest import contracts_registry
from tests.platform.hub.orgs.support import new_id
from tests.platform.hub.outbox.support import NOT_CONTRACT, Outbox, item, new_vm, sample
from tests.platform.hub.tasks.support import audit_rows, frames, hints


def _published(published: MemoryPublisher, frame: str) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (entry.audience.kind, str(entry.audience.id), dict(entry.data))
        for entry in published.items()
        if entry.frame == frame
    ]


# --- event ------------------------------------------------------------------------------------------------------------


def test_event_items_are_stored_with_their_origin_and_announced(outbox: Outbox, published: MemoryPublisher) -> None:
    task, session = outbox.session()
    published.clear()
    first = outbox.output(1, session)
    assert outbox.statuses(first) == [(1, "ok", None)]
    event_id = first["body"]["event"]["header"]["id"]
    assert outbox.desk.rows(
        "SELECT origin_device_id::text, origin_seq, author_kind, task_id::text, session_id::text FROM hub.event"
        " WHERE id = %s",
        event_id,
    ) == [(outbox.device.id, 1, "device", task, session)]
    announced = _published(published, "event")
    assert [(kind, target, data["event"]["header"]["id"]) for kind, target, data in announced] == [
        ("org", outbox.desk.org, event_id)
    ]
    # the other device of the workgroup gets the content-free hint, the author does not
    assert [(kind, target) for kind, target, _data in _published(published, "events")] == [
        ("device", outbox.scene.other_device.id)
    ]
    # an E9 replay under a new origin_seq announces nothing
    published.clear()
    assert outbox.statuses(outbox.event(2, first["body"]["event"])) == [(2, "ok", None)]
    assert _published(published, "event") == []


# --- session_transition -----------------------------------------------------------------------------------------------


def test_session_transitions_drive_session_task_audit_and_frames(
    outbox: Outbox, published: MemoryPublisher, task_signals: list[dict[str, Any]]
) -> None:
    task, session = outbox.desk.started()
    published.clear()
    chain = ["vm_assigning", "vm_ready", "sandbox_ready", "agent_starting", "prompt_delivering"]
    items = [outbox.transition(seq, session, state) for seq, state in enumerate(chain, start=1)]
    working = outbox.transition(
        6, session, "working", agent_version="2.1.330", model_access="platform", model_id="deepseek/x"
    )
    assert {status for _seq, status, _error in outbox.statuses(*items, working)} == {"ok"}
    row = outbox.desk.session(session)
    assert (row["state"], row["agent_version"], row["model_access"], row["model_id"]) == (
        "working",
        "2.1.330",
        "platform",
        "deepseek/x",
    )
    assert outbox.desk.task(task)["state"] == "working"
    stored = outbox.desk.rows(
        "SELECT state, reported_by::text FROM hub.session_transition WHERE session_id = %s ORDER BY seq", session
    )
    assert stored == [("requested", None)] + [(state, outbox.device.id) for state in [*chain, "working"]]
    assert [entry["transition"]["state"] for entry in frames(published, "session")] == [*chain, "working"]
    assert frames(published, "task")[-1]["state"] == "working"
    assert {"task_id": UUID(task), "state": "working"} in task_signals
    assert len(audit_rows(outbox.desk, "session.working")) == 1
    # the same state again changes nothing; a failure needs a failure reason
    assert outbox.statuses(outbox.transition(7, session, "working")) == [(7, "ok", None)]
    assert outbox.statuses(outbox.transition(8, session, "failed", "agent_exited")) == [(8, "ok", None)]
    assert outbox.desk.session(session)["state"] == "failed"
    # a final state refuses every later step
    assert outbox.statuses(outbox.transition(9, session, "working")) == [(9, "rejected", "request_invalid")]


def test_session_transition_rights(outbox: Outbox) -> None:
    _task, foreign = outbox.session(outbox.scene.other_device)
    _task2, unplaced = outbox.scene.start_session(placed=False)
    assert outbox.statuses(
        outbox.transition(1, foreign, "idle"),
        outbox.transition(2, new_id(), "idle"),
        outbox.transition(3, unplaced, "idle"),
    ) == [(1, "rejected", "forbidden"), (2, "rejected", "not_found"), (3, "rejected", "forbidden")]
    # a failed state without a failure reason breaks the contract and is refused
    _task3, session = outbox.session()
    response = outbox.push(outbox.transition(4, session, "failed", "not_a_reason"), extensions=NOT_CONTRACT)
    assert response.json()["results"] == [{"origin_seq": 4, "status": "rejected", "error": "request_invalid"}]
    # an impossible device time
    bad_time = outbox.transition(5, session, "idle")
    bad_time["body"]["at"] = "2026-02-31T10:00:00.000Z"
    assert outbox.statuses(bad_time) == [(5, "rejected", "request_invalid")]


# --- vm_state ---------------------------------------------------------------------------------------------------------


def test_vm_state_rows(outbox: Outbox) -> None:
    vm, pool = new_vm(), new_vm()
    assert outbox.statuses(outbox.vm(1, vm, "assigning"), outbox.vm(2, pool, "pool", wg=None)) == [
        (1, "ok", None),
        (2, "ok", None),
    ]
    created = outbox.hub.clock()
    rows = outbox.desk.rows(
        "SELECT device_id::text, wg_id::text, state, golden_version, cpus, memory_mib, disk_gib, created_at FROM hub.vm"
        " WHERE id = %s",
        vm,
    )
    assert rows == [(outbox.device.id, outbox.desk.wg, "assigning", "golden-1", 4, 8192, 20, created)]
    later = outbox.vm(3, vm, "running", cpus=8, golden_version="golden-2")
    assert outbox.statuses(later) == [(3, "ok", None)]
    updated = outbox.desk.rows("SELECT state, cpus, golden_version, created_at, state_at FROM hub.vm WHERE id = %s", vm)
    assert updated[0][:4] == ("running", 8, "golden-2", created)
    assert format_time(updated[0][4]) == later["body"]["at"]  # the device time of the state
    assert outbox.statuses(outbox.vm(4, vm, "deleted")) == [(4, "ok", None)]
    assert outbox.desk.rows("SELECT state FROM hub.vm WHERE id = %s", vm) == [("deleted",)]


def test_vm_state_rights(outbox: Outbox) -> None:
    vm = new_vm()
    outbox.ok(outbox.vm(1, vm))
    other = outbox.scene.other_device
    # a VM of another device, a workgroup the device is not assigned to
    assert outbox.statuses(outbox.vm(1, vm, "stopped"), device=other) == [(1, "rejected", "forbidden")]
    assert outbox.desk.rows("SELECT device_id::text, state FROM hub.vm WHERE id = %s", vm) == [
        (outbox.device.id, "running")
    ]
    foreign_wg = outbox.desk.scene.draft.default_wg
    assert outbox.statuses(outbox.vm(2, new_vm(), wg=foreign_wg)) == [(2, "rejected", "forbidden")]
    huge = outbox.vm(3, new_vm(), memory_mib=2**31)
    response = outbox.push(huge)
    assert response.json()["results"] == [{"origin_seq": 3, "status": "rejected", "error": "request_invalid"}]


# --- operation_transition ---------------------------------------------------------------------------------------------


def _operation(outbox: Outbox, session: str, state: str = "accepted", *, slot: bool = True) -> str:
    task = outbox.scene.sessions[session][0]
    operation = new_id()
    now = outbox.hub.clock()
    outbox.desk.execute(
        "INSERT INTO hub.operation (id, org_id, wg_id, task_id, session_id, device_id, actor_id, type, state,"
        " dedupe_epoch, dedupe_tag, created_at, decided_at) VALUES (%s, %s, %s, %s, %s,"
        " (SELECT device_id FROM hub.session WHERE id = %s), %s, 'github.create_branch', %s, %s, %s, %s, %s)",
        operation,
        outbox.desk.org,
        outbox.desk.wg,
        task,
        session,
        session,
        outbox.scene.writer.id,
        state,
        1 if slot else None,
        operation if slot else None,
        now,
        now,
    )
    if slot:
        outbox.desk.execute(
            "INSERT INTO hub.dedupe_slot (wg_id, epoch, tag, operation_id, expires_at) VALUES (%s, 1, %s, %s, %s)",
            outbox.desk.wg,
            operation,
            operation,
            now + timedelta(hours=1),
        )
    return operation


def _result(outbox: Outbox, seq: int, operation: str, device: Any = None) -> dict[str, Any]:
    author = device or outbox.device
    return outbox.event(seq, outbox.scene.wg_event(author, "operation.result", ref=operation))


def _op(seq: int, operation: str, state: str, at: str, **extra: Any) -> dict[str, Any]:
    return item(seq, "operation_transition", {"operation_id": operation, "state": state, "at": at, **extra})


def test_operation_transitions(outbox: Outbox, published: MemoryPublisher) -> None:
    _task, session = outbox.session()
    operation = _operation(outbox, session)
    published.clear()
    executing = outbox.hub.clock() + timedelta(seconds=0.3)
    assert outbox.statuses(_op(1, operation, "executing", outbox.now(-0.5))) == [(1, "ok", None)]
    assert outbox.desk.rows("SELECT state, started_at, finished_at FROM hub.operation WHERE id = %s", operation) == [
        ("executing", executing, None)
    ]
    result = _result(outbox, 2, operation)
    result_id = result["body"]["event"]["header"]["id"]
    failed = _op(
        3, operation, "failed", outbox.now(), error_code="upstream_error", retryable=True, result_event_id=result_id
    )
    assert outbox.statuses(result, failed) == [(2, "ok", None), (3, "ok", None)]
    row = outbox.desk.rows(
        "SELECT state, error_code, retryable, result_event_id::text, finished_at FROM hub.operation WHERE id = %s",
        operation,
    )
    assert row == [("failed", "upstream_error", True, result_id, outbox.hub.clock())]
    assert outbox.desk.rows("SELECT count(*) FROM hub.dedupe_slot WHERE operation_id = %s", operation) == [(0,)]
    transitions = outbox.desk.rows(
        "SELECT seq, state, error_code, by_id::text FROM hub.operation_transition WHERE operation_id = %s ORDER BY seq",
        operation,
    )
    assert transitions == [(1, "executing", None, outbox.device.id), (2, "failed", "upstream_error", outbox.device.id)]
    audited = outbox.desk.rows(
        "SELECT action, actor_kind, target_id, detail FROM hub.audit WHERE action LIKE 'operation.%%' ORDER BY seq"
    )
    assert [(row[0], row[1], row[2]) for row in audited] == [
        ("operation.executing", "main_agent", operation),
        ("operation.failed", "main_agent", operation),
    ]
    for action, *_rest, detail in audited:
        contracts_registry().validate(f"audit/{action}.json", detail)
    assert audited[1][3] == {
        "type": "github.create_branch",
        "error_code": "upstream_error",
        "duplicate_of": None,
        "local_check": None,
        "github_identity": None,
        "triggered_by": None,
    }
    sent = _published(published, "operation")
    assert [(kind, target, data["state"]) for kind, target, data in sent] == [
        ("org", outbox.desk.org, "executing"),
        ("org", outbox.desk.org, "failed"),
    ]
    for _kind, _target, data in sent:
        contracts_registry().validate("ws/sse/operation.json", data)
    # the same state again is no change; a final state refuses later steps
    assert outbox.statuses(
        _op(
            4, operation, "failed", outbox.now(), error_code="upstream_error", retryable=True, result_event_id=result_id
        ),
        _op(5, operation, "executing", outbox.now()),
    ) == [(4, "ok", None), (5, "rejected", "request_invalid")]
    assert len(outbox.desk.rows("SELECT 1 FROM hub.operation_transition WHERE operation_id = %s", operation)) == 2


def test_operation_success_expiry_and_slots(outbox: Outbox) -> None:
    _task, session = outbox.session()
    done, received, kept = (
        _operation(outbox, session),
        _operation(outbox, session, "received"),
        _operation(outbox, session),
    )
    result = _result(outbox, 1, done)
    succeeded = _op(2, done, "succeeded", outbox.now(), result_event_id=result["body"]["event"]["header"]["id"])
    expired = _op(3, received, "expired", outbox.now(), error_code="hub_unreachable")
    assert outbox.statuses(result, succeeded, expired) == [(1, "ok", None), (2, "ok", None), (3, "ok", None)]
    assert outbox.desk.rows(
        "SELECT state, started_at IS NULL, finished_at IS NOT NULL FROM hub.operation WHERE id = ANY(%s::uuid[])"
        " ORDER BY state",
        [done, received],
    ) == [("expired", True, True), ("succeeded", True, True)]
    # success keeps the slot (it now blocks duplicates), expiry and failure release it
    slots = outbox.desk.rows("SELECT operation_id::text FROM hub.dedupe_slot ORDER BY operation_id")
    assert sorted(row[0] for row in slots) == sorted([done, kept])
    no_slot = _operation(outbox, session, slot=False)
    assert outbox.statuses(_op(4, no_slot, "expired", outbox.now(), error_code="timeout")) == [(4, "ok", None)]


def test_operation_transition_rights_and_result_checks(outbox: Outbox) -> None:
    _task, session = outbox.session()
    other = outbox.scene.other_device
    _task2, foreign_session = outbox.session(other)
    mine, foreign = _operation(outbox, session), _operation(outbox, foreign_session)
    stray = _result(outbox, 1, new_id())
    stray_id = stray["body"]["event"]["header"]["id"]
    statuses = outbox.statuses(
        stray,
        _op(2, new_id(), "executing", outbox.now()),
        _op(3, foreign, "executing", outbox.now()),
        _op(4, mine, "succeeded", outbox.now(), result_event_id=stray_id),
        _op(5, mine, "succeeded", outbox.now(), result_event_id=new_id()),
    )
    assert statuses == [
        (1, "ok", None),
        (2, "rejected", "not_found"),
        (3, "rejected", "forbidden"),
        (4, "rejected", "request_invalid"),
        (5, "rejected", "request_invalid"),
    ]
    # the result event of another device
    theirs = _result(outbox, 1, mine, device=other)
    outbox.ok(theirs, device=other)
    theirs_id = theirs["body"]["event"]["header"]["id"]
    assert outbox.statuses(_op(6, mine, "succeeded", outbox.now(), result_event_id=theirs_id)) == [
        (6, "rejected", "request_invalid")
    ]
    # a body whose fields do not fit the state breaks the contract
    for seq, body in enumerate(
        (
            {"state": "executing", "error_code": "timeout"},
            {"state": "succeeded"},
            {"state": "failed", "error_code": "timeout", "result_event_id": stray_id},
            {"state": "expired", "error_code": "upstream_error"},
        ),
        start=7,
    ):
        broken = item(seq, "operation_transition", {"operation_id": mine, "at": outbox.now(), **body})
        response = outbox.push(broken, extensions=NOT_CONTRACT)
        assert response.json()["results"] == [{"origin_seq": seq, "status": "rejected", "error": "request_invalid"}]
    assert outbox.desk.rows("SELECT state FROM hub.operation WHERE id = %s", mine) == [("accepted",)]


# --- usage, quota: base handlers of the usage area, tests in tests/platform/hub/usage/ --------------------------------


# --- metrics ----------------------------------------------------------------------------------------------------------


def test_metric_samples(outbox: Outbox) -> None:
    _task, session = outbox.session()
    vm = new_vm()
    outbox.ok(outbox.vm(1, vm))
    ts = outbox.now(-15)
    samples = [
        sample("device", outbox.device.id, ts, free_vm_slots=6, free_mem_bytes=2**40),
        sample("device", outbox.device.id, outbox.now()),
        sample("vm", vm, ts, cpu_ratio=1.5),
        sample("session", session, ts, cpu_ratio=None, state="working"),
    ]
    assert outbox.statuses(item(2, "metrics", {"samples": samples})) == [(2, "ok", None)]
    rows = outbox.desk.rows(
        "SELECT entity_kind, entity_id, device_id::text, cpu_ratio, free_vm_slots, state FROM hub.metric_sample"
        " ORDER BY entity_kind, ts"
    )
    assert rows == [
        ("device", outbox.device.id, outbox.device.id, 0.5, 6, "running"),
        ("device", outbox.device.id, outbox.device.id, 0.5, None, "running"),
        ("session", session, outbox.device.id, None, None, "working"),
        ("vm", vm, outbox.device.id, 1.5, None, "running"),
    ]
    # the same sample again is ignored
    assert outbox.statuses(item(3, "metrics", {"samples": samples[:1]})) == [(3, "ok", None)]
    assert outbox.desk.rows("SELECT count(*) FROM hub.metric_sample") == [(4,)]


def test_metric_sample_rights(outbox: Outbox) -> None:
    other = outbox.scene.other_device
    _task, foreign_session = outbox.session(other)
    foreign_vm = new_vm()
    outbox.ok(outbox.vm(1, foreign_vm), device=other)
    ts = outbox.now()
    mine = sample("device", outbox.device.id, ts)
    cases = [
        sample("device", other.id, ts),
        sample("vm", foreign_vm, ts),
        sample("vm", new_vm(), ts),
        sample("session", foreign_session, ts),
        sample("session", new_id(), ts),
    ]
    statuses = outbox.statuses(*[item(seq, "metrics", {"samples": [mine, bad]}) for seq, bad in enumerate(cases, 1)])
    assert statuses == [
        (1, "rejected", "forbidden"),
        (2, "rejected", "forbidden"),
        (3, "rejected", "not_found"),
        (4, "rejected", "forbidden"),
        (5, "rejected", "not_found"),
    ]
    assert outbox.desk.rows("SELECT count(*) FROM hub.metric_sample") == [(0,)]
    mismatch = item(6, "metrics", {"samples": [sample("vm", outbox.device.id, ts)]})
    response = outbox.push(mismatch, extensions=NOT_CONTRACT)
    assert response.json()["results"] == [{"origin_seq": 6, "status": "rejected", "error": "request_invalid"}]


# --- audit ------------------------------------------------------------------------------------------------------------


DETAILS: dict[str, dict[str, Any]] = {
    "hook.denied": {"tool": "Bash", "rule": 3},
    "read.session.get_task": {"result_count": 1},
    "session.prompt_refused": {"reason": "unregistered"},
    "model.provider_outside_allowlist": {"provider": "other", "model": "x/y"},
    "runner.inbound_rejected": {"source": "event", "code": "author_not_writer"},
    "runner.log_rollback": {"verified_seq": 4, "offered_seq": 3},
    "runner.log_fork": {"verified_seq": 4, "offered_seq": 4},
    "runner.alert": {"alert": "origin_seq_conflict", "value": None},
}


def test_audit_items(outbox: Outbox) -> None:
    task, session = outbox.session()
    items = [
        outbox.audit(seq, action, detail, session_id=session, task_id=task, result="denied")
        for seq, (action, detail) in enumerate(DETAILS.items(), start=1)
    ]
    plain = outbox.audit(9, "runner.alert", {"alert": "vm_budget_exceeded", "value": 2}, target_kind="device")
    task_only = outbox.audit(10, "read.session.list_tasks", {"result_count": 3}, task_id=task, target_kind="task")
    assert {status for _seq, status, _error in outbox.statuses(*items, plain, task_only)} == {"ok"}
    rows = outbox.desk.rows(
        "SELECT action, actor_kind, actor_id::text, device_id::text, org_id::text, wg_id::text, task_id::text,"
        " session_id::text, result, detail FROM hub.audit WHERE action = ANY(%s) ORDER BY seq",
        [*sorted(DETAILS), "read.session.list_tasks"],
    )
    device = outbox.device.id
    scoped = (outbox.desk.org, outbox.desk.wg, task, session)
    # an item that names a session is its agent's (ADR-095 section 4); the others stay the device's
    assert rows[0] == ("hook.denied", "main_agent", session, device, *scoped, "denied", {"tool": "Bash", "rule": 3})
    assert [row[1] for row in rows] == ["main_agent"] * len(DETAILS) + ["device", "device"]
    assert {row[2] for row in rows[8:]} == {device}
    assert [row[0] for row in rows] == [*DETAILS, "runner.alert", "read.session.list_tasks"]
    assert rows[8][4:8] == (None, None, None, None)
    assert rows[9][4:8] == (outbox.desk.org, outbox.desk.wg, task, None)
    for row in rows:
        contracts_registry().validate(f"audit/{row[0]}.json", row[9])


def test_ping_delivered_marks_the_ping(outbox: Outbox) -> None:
    _task, session = outbox.session()
    ping = new_id()
    created = outbox.hub.clock()
    outbox.desk.execute(
        "INSERT INTO hub.session_ping (id, session_id, kind, level, ref, created_at) VALUES"
        " (%s, %s, 'whiteboard.entry', 'workgroup', %s, %s)",
        ping,
        session,
        new_id(),
        created,
    )
    delivered = {"ping_id": ping, "kind": "whiteboard.entry", "via": "hook"}
    assert outbox.statuses(outbox.audit(1, "ping.delivered", delivered, session_id=session)) == [(1, "ok", None)]
    first = outbox.desk.rows("SELECT delivered_at FROM hub.session_ping WHERE id = %s", ping)
    assert first == [(outbox.hub.clock(),)]
    # delivered again (e.g. by the tool after the hook): the first time stays
    assert outbox.statuses(outbox.audit(2, "ping.delivered", delivered, session_id=session)) == [(2, "ok", None)]
    assert outbox.desk.rows("SELECT delivered_at FROM hub.session_ping WHERE id = %s", ping) == first
    assert len(audit_rows(outbox.desk, "ping.delivered")) == 2
    # without a session, an unknown ping, a ping of another kind
    other_kind = {**delivered, "kind": "subagent.finished"}
    assert outbox.statuses(
        outbox.audit(3, "ping.delivered", delivered),
        outbox.audit(4, "ping.delivered", {**delivered, "ping_id": new_id()}, session_id=session),
        outbox.audit(5, "ping.delivered", other_kind, session_id=session),
    ) == [(3, "rejected", "request_invalid"), (4, "rejected", "not_found"), (5, "rejected", "not_found")]


@pytest.mark.parametrize("kind", ["subagent.finished", "operation.finished"])
def test_ping_delivered_of_a_runner_ping_is_audited_without_a_hub_row(outbox: Outbox, kind: str) -> None:
    """05-runner.md section 9: the delivery of the runner's own pings is audited too (RUN-14 left it local)."""
    _task, session = outbox.session()
    ping = new_id()
    delivered = {"ping_id": ping, "kind": kind, "via": "tool"}
    assert outbox.statuses(outbox.audit(1, "ping.delivered", delivered, session_id=session)) == [(1, "ok", None)]
    assert outbox.desk.rows("SELECT 1 FROM hub.session_ping WHERE id = %s", ping) == []
    [row] = audit_rows(outbox.desk, "ping.delivered")
    assert (row[0], str(row[1]), row[3]) == ("main_agent", session, delivered)
    # without a session it stays refused
    assert outbox.statuses(outbox.audit(2, "ping.delivered", {**delivered, "ping_id": new_id()})) == [
        (2, "rejected", "request_invalid")
    ]
    # a hub ping of this session named with a runner kind is refused; one of another session is not looked at
    _other_task, other_session = outbox.session()
    hub_ping, foreign_ping = new_id(), new_id()
    for ping_id, owner in ((hub_ping, session), (foreign_ping, other_session)):
        outbox.desk.execute(
            "INSERT INTO hub.session_ping (id, session_id, kind, level, ref, created_at) VALUES"
            " (%s, %s, 'whiteboard.entry', 'workgroup', %s, %s)",
            ping_id,
            owner,
            new_id(),
            outbox.hub.clock(),
        )
    assert outbox.statuses(
        outbox.audit(3, "ping.delivered", {**delivered, "ping_id": hub_ping}, session_id=session),
        outbox.audit(4, "ping.delivered", {**delivered, "ping_id": foreign_ping}, session_id=session),
    ) == [(3, "rejected", "not_found"), (4, "ok", None)]
    assert outbox.desk.rows("SELECT delivered_at FROM hub.session_ping WHERE id = %s", foreign_ping) == [(None,)]


def test_audit_item_rights_and_form(outbox: Outbox) -> None:
    task, session = outbox.session()
    other_task, foreign_session = outbox.session(outbox.scene.other_device)
    foreign_wg_task = new_id()
    now = outbox.hub.clock()
    outbox.desk.execute(
        "INSERT INTO hub.task (id, org_id, wg_id, owner_id, state, created_at, state_at, last_activity_at)"
        " VALUES (%s, %s, %s, %s, 'working', %s, %s, %s)",
        foreign_wg_task,
        outbox.desk.org,
        outbox.desk.scene.draft.default_wg,
        outbox.scene.admin.id,
        now,
        now,
        now,
    )
    count = {"result_count": 1}
    action = "read.session.get_task"
    assert outbox.statuses(
        outbox.audit(1, action, count, session_id=foreign_session),
        outbox.audit(2, action, count, session_id=new_id()),
        outbox.audit(3, action, count, session_id=session, task_id=other_task),
        outbox.audit(4, action, count, task_id=new_id()),
        outbox.audit(5, action, count, task_id=foreign_wg_task),
    ) == [
        (1, "rejected", "forbidden"),
        (2, "rejected", "not_found"),
        (3, "rejected", "request_invalid"),
        (4, "rejected", "not_found"),
        (5, "rejected", "forbidden"),
    ]
    assert task
    # actions outside the outbox list and details outside their schema
    invalid: list[tuple[str, dict[str, Any]]] = [
        ("task.create", {}),
        ("hook.denied", {"tool": "Bash", "rule": 9}),
        ("hook.denied", {"path": "/etc"}),
    ]
    for seq, (name, detail) in enumerate(invalid, start=6):
        response = outbox.push(outbox.audit(seq, name, detail), extensions=NOT_CONTRACT)
        assert response.json()["results"] == [{"origin_seq": seq, "status": "rejected", "error": "request_invalid"}]


# --- wrap -------------------------------------------------------------------------------------------------------------


def test_wrap_items(outbox: Outbox, published: MemoryPublisher) -> None:
    draft, device = outbox.scene.draft, outbox.device
    published.clear()
    wrap = draft.wrap(device, "wg", outbox.desk.wg, 1, outbox.scene.other_device)
    assert outbox.statuses(item(1, "wrap", {"wrap": wrap})) == [(1, "ok", None)]
    assert outbox.desk.rows(
        "SELECT recipient_id::text, wrapped_by::text FROM hub.key_wrap WHERE wrapped_by = %s", device.id
    ) == [(outbox.scene.other_device.id, device.id)]
    assert hints_of(published, "wraps") == [("device", outbox.scene.other_device.id)]
    # stored before: ignored; a stranger is not entitled
    stranger = draft.wrap(device, "wg", outbox.desk.wg, 1, outbox.scene.stranger)
    assert outbox.statuses(item(2, "wrap", {"wrap": wrap}), item(3, "wrap", {"wrap": stranger})) == [
        (2, "ok", None),
        (3, "rejected", "wrap_unauthorized"),
    ]
    assert hints(published) == []


def hints_of(published: MemoryPublisher, frame: str) -> list[tuple[str, str]]:
    return [(entry.audience.kind, str(entry.audience.id)) for entry in published.items() if entry.frame == frame]
