"""The work VM of a session (13-work-vm.md sections 8 and 10, ADR-059): `session workvm` against the hub double with
the web's panel (phase, countdown to the 10 s target, facts, runtime of the session and of the task, runs latest per
job), the contract checks of the `workvm.*` bodies, and the work VM items of the task chat."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from agent_platform import workvm
from agent_runner.crypto.encoding import format_time
from agent_runner.crypto.ids import uuid7

from .conftest import Tool
from .fakes.sessions import add_operation, add_session, device_event, state_of
from .hubfake import HubFake, User
from .seeding import assign_device, new_task

NOW = datetime(2026, 11, 24, 8, 0, 0, tzinfo=UTC)
NOW_MS = int(NOW.timestamp() * 1000)


def at(seconds: float) -> str:
    """The time `seconds` before NOW."""
    return format_time(NOW - timedelta(seconds=seconds))


def state_body(vm: str = "wvm-0a1b2c3d", **changes: Any) -> dict[str, Any]:
    body = {
        "workvm_id": vm,
        "state": "created",
        "scope": "session",
        "size": "standard",
        "cpus": 2,
        "memory_mib": 4096,
        "disk_gib": 20,
        "implicit": False,
        "reason": None,
        "started_at": at(60),
        "ended_at": None,
        "runtime_ms": 0,
    }
    return {**body, **changes}


def run_body(job: str = "j-000000000001", vm: str = "wvm-0a1b2c3d", **changes: Any) -> dict[str, Any]:
    body = {
        "workvm_id": vm,
        "job_id": job,
        "kind": "exec",
        "command": "make test",
        "state": "running",
        "exit_code": None,
        "started_at": at(30),
        "ended_at": None,
        "duration_ms": 0,
        "output": "",
        "output_bytes": 0,
    }
    return {**body, **changes}


@pytest.fixture
def fixed_now(monkeypatch: pytest.MonkeyPatch) -> None:
    assert workvm.now_ms() > 1_700_000_000_000
    monkeypatch.setattr(workvm, "now_ms", lambda: NOW_MS)


def request(hub: HubFake, task: str, session: str, state: str, seconds: float) -> str:
    operation = add_operation(hub, task, session, "workvm.request")
    state_of(hub).operations[operation]["operation"] |= {"state": state, "created_at": at(seconds)}
    return operation


@pytest.mark.usefixtures("fixed_now")
def test_the_panel_of_a_session(tool: Tool, hub: HubFake, account: User) -> None:
    _org, wg, device = assign_device(tool, hub, account)
    task = new_task(tool, wg)
    session = add_session(hub, task, device=device)
    place = {"wg": wg, "task": task, "session": session}
    code, shown = tool.run("session", "workvm", "--session", session)
    assert code == 0, shown
    assert (shown["phase"], shown["vm"], shown["runs"], shown["task_id"]) == ("none", None, [], task)
    assert shown["runtime"] == {"session_ms": 0, "task_ms": 0, "vms": 0, "running": False}
    # the session asked for a VM: starting with the countdown, then overdue; a finished request counts no more
    request(hub, task, session, "succeeded", 30)
    add_operation(hub, task, session, "github.create_pr")
    request(hub, task, add_session(hub, task, device=device), "requested", 1)  # another session's request
    operation = request(hub, task, session, "requested", 3)
    starting = tool.run("session", "workvm", "--session", session)[1]
    assert (starting["phase"], starting["elapsed_ms"], starting["remaining_ms"]) == ("starting", 3000, 7000)
    assert starting["overdue"] is False
    state_of(hub).operations[operation]["operation"]["created_at"] = at(12)
    overdue = tool.run("session", "workvm", "--session", session)[1]
    assert (overdue["remaining_ms"], overdue["overdue"]) == (0, True)
    state_of(hub).operations[operation]["operation"]["created_at"] = "yesterday"
    assert tool.run("session", "workvm", "--session", session)[1]["phase"] == "none"
    # running: the VM's facts and the runtime of the session and of the task
    device_event(hub, account, device, "workvm.state", state_body(), **place)
    running = tool.run("session", "workvm", "--session", session)[1]
    assert (running["phase"], running["vm"]["size"], running["elapsed_ms"]) == ("running", "standard", 0)
    assert running["runtime"] == {"session_ms": 60_000, "task_ms": 60_000, "vms": 1, "running": True}
    # deleted after it ran 90 s; another session's VM counts for the task only
    deleted = state_body(state="deleted", reason="idle", ended_at=at(0), runtime_ms=90_000)
    device_event(hub, account, device, "workvm.state", deleted, **place)
    other = add_session(hub, task, device=device)
    later = state_body("wvm-0000000b", started_at=at(10))
    device_event(hub, account, device, "workvm.state", later, wg=wg, task=task, session=other)
    gone = tool.run("session", "workvm", "--session", session)[1]
    assert (gone["phase"], gone["vm"]["reason"]) == ("deleted", "idle")
    assert gone["runtime"] == {"session_ms": 90_000, "task_ms": 100_000, "vms": 2, "running": True}
    # the session ran a job in the other session's (task) VM: that VM is the session's too, the newest by start
    run = run_body(vm="wvm-0000000b", started_at=at(5))
    device_event(hub, account, device, "workvm.run", run, **place)
    shared = tool.run("session", "workvm", "--session", session)[1]
    assert (shared["phase"], shared["vm"]["workvm_id"]) == ("running", "wvm-0000000b")
    assert shared["runtime"]["session_ms"] == 100_000


@pytest.mark.usefixtures("fixed_now")
def test_runs_latest_per_job_newest_first(tool: Tool, hub: HubFake, account: User) -> None:
    _org, wg, device = assign_device(tool, hub, account)
    task = new_task(tool, wg)
    session = add_session(hub, task, device=device)
    place = {"wg": wg, "task": task, "session": session}
    first = run_body("j-000000000001", started_at=at(30))
    final = {**first, "state": "exited", "exit_code": 0, "ended_at": at(20), "duration_ms": 10_000}
    second = run_body("j-000000000002", started_at=at(10), output="tail", output_bytes=4096)
    same_start = run_body("j-000000000003", started_at=at(10), kind="browse")
    for body in (first, second, final, same_start):
        device_event(hub, account, device, "workvm.run", body, **place)
    device_event(hub, account, device, "workvm.run", {**first, "extra": 1}, **place)
    device_event(hub, account, device, "workvm.state", {"workvm_id": "x"}, **place)
    other = add_session(hub, task, device=device)
    device_event(hub, account, device, "workvm.run", run_body("j-000000000009"), wg=wg, task=task, session=other)
    code, shown = tool.run("session", "workvm", "--session", session)
    assert code == 0, shown
    assert [run["job_id"] for run in shown["runs"]] == ["j-000000000002", "j-000000000003", "j-000000000001"]
    assert [run["output_truncated"] for run in shown["runs"]] == [True, False, False]
    assert (shown["runs"][2]["state"], shown["invalid"], shown["unverified"]) == ("exited", 2, 0)
    # the task chat keeps only the latest run of a job, as the web's timeline
    chat = tool.run("task", "show", "--task", task)[1]["events"]
    jobs = [event["body"].get("job_id") for event in chat if event["kind"] == "workvm.run"]
    assert jobs == ["j-000000000002", "j-000000000001", "j-000000000003", "j-000000000001", "j-000000000009"]
    assert [event["body"]["state"] for event in chat if event["kind"] == "workvm.run"][1] == "exited"
    followed = tool.run("task", "follow", "--task", task)[1]["events"]
    assert len([event for event in followed if event["kind"] == "workvm.run"]) == 6
    # an event of another task the hub slips in, and a forged one, are left out; without the key all are locked
    stranger = new_task(tool, wg, "Other")
    slipped = device_event(hub, account, device, "workvm.run", run_body(), wg=wg, task=stranger, session=session)
    forged = {**slipped, "event": {**slipped["event"], "ct": slipped["event"]["ct"][:-4] + "AAAA"}}
    everything = [*hub.events[wg], forged]
    other_kinds = [item for item in everything if not item["event"]["header"]["kind"].startswith("workvm.")]
    assert other_kinds
    hub.hooks[("GET", f"/v1/scopes/{wg}/events")] = lambda body, request: (
        200,
        {"events": everything, "next_after": None},
    )
    assert tool.run("session", "workvm", "--session", session)[1]["unverified"] == 2 + len(other_kinds)
    del hub.hooks[("GET", f"/v1/scopes/{wg}/events")]
    hub.wraps[:] = [wrap for wrap in hub.wraps if wrap["scope"] != "wg" or wrap["recipient_id"] != account.id]
    locked = tool.run("session", "workvm", "--session", session)[1]
    assert (locked["runs"], locked["locked"], locked["unverified"]) == ([], 7, 0)


def test_session_workvm_refusals(tool: Tool, hub: HubFake, account: User) -> None:
    _org, wg, device = assign_device(tool, hub, account)
    task = new_task(tool, wg)
    session = add_session(hub, task, device=device)
    assert tool.run("session", "workvm", "--session", "x")[1]["error"] == "invalid_argument"
    assert tool.run("session", "workvm", "--session", uuid7())[1]["hub_error"] == "not_found"
    state_of(hub).rows[session]["task_id"] = None
    assert tool.run("session", "workvm", "--session", session)[1]["reason"] == "response_invalid"
    state_of(hub).rows[session]["task_id"] = task
    hub.hooks[("GET", f"/v1/tasks/{task}/operations")] = lambda body, request: (200, {"operations": None})
    assert tool.run("session", "workvm", "--session", session)[1]["reason"] == "response_invalid"


def test_contract_checks_of_the_bodies() -> None:
    assert workvm.parse_state(state_body()) is not None
    assert workvm.parse_run(run_body(exit_code=0, ended_at=at(0))) is not None
    assert workvm.parse_run(run_body(duration_ms=5.0)) is not None  # a JSON 5.0 is the number 5
    for body in (
        state_body(cpus=True),
        state_body(cpus=2.5),
        state_body(cpus=65),
        state_body(implicit=0),
        state_body(reason="gone"),
        state_body(started_at="2026-11-24T08:00:00Z"),
        state_body(workvm_id="wvm-0A1B2C3D"),
        {k: v for k, v in state_body().items() if k != "reason"},
        {**state_body(), "extra": None},
        "x",
    ):
        assert workvm.parse_state(body) is None, body
    for body in (
        run_body(command="x" * 4097),
        run_body(job_id="j-1"),
        run_body(exit_code=256),
        run_body(ended_at="x"),
        run_body(state="done"),
    ):
        assert workvm.parse_run(body) is None, body
    # maxLength counts code points, the truncation UTF-8 bytes
    assert workvm.parse_run(run_body(command="ä" * 4096)) is not None
    assert workvm.output_truncated(run_body(output="ä", output_bytes=2)) is False
    assert workvm.output_truncated(run_body(output="ä", output_bytes=3)) is True


def test_panel_and_runtime_rules() -> None:
    newer = state_body("wvm-0000000a", started_at=at(10), state="deleted", reason="released", runtime_ms=5000)
    older = state_body("wvm-0000000b", started_at=at(60), state="deleted", reason=None, runtime_ms=1000)
    repeated = state_body("wvm-0000000c", started_at=at(30))
    states = [newer, older, repeated, {**repeated, "started_at": at(5)}]
    # the last VM by start, not by order; a repeated `created` of a VM does not replace its first one
    panel = workvm.workvm_panel(states[:2], [], NOW_MS)
    assert (panel["phase"], panel["vm"]) == ("deleted", newer)
    assert workvm.workvm_runtime(states, NOW_MS) == {"ms": 36_000, "vms": 3, "running": True}
