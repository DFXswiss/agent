"""Test runs of a session (ADR-080, KOR-43): `test list|show|log` against the hub fake with `test.run` events written
by the device and logs sealed like the runner's, the contract check of the `test.run` body, and the refusals of a log
that does not belong to its run."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_platform import testruns, workvm
from agent_runner.crypto import runlog
from agent_runner.crypto.ids import uuid7

from .conftest import Tool
from .fakes.runlogs import add_log, logs_of
from .fakes.sessions import add_session, device_event, state_of
from .hubfake import HubFake, User
from .seeding import assign_device, new_task
from .test_workvm import at, run_body

CB = runlog.CHUNK_BYTES


def body(run: str = "tr-000000000001", **changes: Any) -> dict[str, Any]:
    found = {
        "run_id": run,
        "operation_id": uuid7(),
        "kind": "unit",
        "command": "pytest -q",
        "cwd": "/workspace",
        "size": "standard",
        "state": "running",
        "exit_code": None,
        "started_at": at(30),
        "ended_at": None,
        "duration_ms": 0,
        "ready_ms": 0,
        "summary": None,
        "log": None,
        "tail": "",
        "reason": None,
    }
    return {**found, **changes}


def final(run: str, log: dict[str, Any] | None, **changes: Any) -> dict[str, Any]:
    summary = {"passed": 3, "failed": 1, "skipped": None, "errors": 0}
    return body(
        run,
        state="failed",
        exit_code=1,
        ended_at=at(1),
        duration_ms=29_000,
        ready_ms=4_000,
        summary=summary,
        log=log,
        tail="1 failed, 3 passed",
        **changes,
    )


Setup = tuple[str, str, str, str, dict[str, str]]


def setup(tool: Tool, hub: HubFake, account: User) -> Setup:
    """`(wg, device, task, session, place)`."""
    _org, wg, device = assign_device(tool, hub, account)
    task = new_task(tool, wg)
    session = add_session(hub, task, device=device)
    return wg, device, task, session, {"wg": wg, "task": task, "session": session}


def test_list_and_show(tool: Tool, hub: HubFake, account: User) -> None:
    wg, device, task, session, place = setup(tool, hub, account)
    assert tool.run("test", "list", "--session", session)[1]["runs"] == []
    first = body("tr-000000000001", started_at=at(60))
    second = body("tr-000000000002", started_at=at(20), kind="lint")
    same = body("tr-000000000003", started_at=at(20))
    log = add_log(hub, account, device, ref="tr-000000000001", output=b"collected 4 items\n", place=place)
    for item in (first, second, same, final("tr-000000000001", log, started_at=at(60))):
        device_event(hub, account, device, "test.run", item, **place)
    device_event(hub, account, device, "test.run", {**first, "extra": 1}, **place)  # invalid
    other = add_session(hub, task, device=device)
    device_event(hub, account, device, "test.run", body("tr-000000000009"), wg=wg, task=task, session=other)
    code, listed = tool.run("test", "list", "--session", session)
    assert code == 0, listed
    ids = [run["run_id"] for run in listed["runs"]]
    assert ids == ["tr-000000000003", "tr-000000000002", "tr-000000000001"]
    assert (listed["task_id"], listed["session_id"], listed["invalid"], listed["locked"]) == (task, session, 1, 0)
    done = listed["runs"][2]
    assert (done["state"], done["summary"]["failed"], done["log"]["bytes"], done["session_id"]) == (
        "failed",
        1,
        18,
        session,
    )
    assert "tail" not in done
    whole = tool.run("test", "list", "--task", task)[1]
    assert len(whole["runs"]) == 4
    assert whole["session_id"] is None
    trimmed = tool.run("test", "list", "--task", task, "--limit", "1", "--fields", "run_id,state")[1]
    assert trimmed["runs"] == [{"run_id": "tr-000000000003", "state": "running"}]
    code, shown = tool.run("test", "show", "tr-000000000001", "--session", session)
    assert code == 0, shown
    assert (shown["run"]["tail"], shown["run"]["log"], shown["task_id"]) == ("1 failed, 3 passed", log, task)
    assert tool.run("test", "show", "tr-000000000009", "--session", session)[1]["error"] == "not_found"
    assert tool.run("test", "show", "tr-000000000009", "--task", task)[1]["run"]["session_id"] == other
    # an event of another task or kind that the hub slips in is left out; without the key all are locked
    stranger = new_task(tool, wg, "Other")
    slipped = device_event(hub, account, device, "test.run", body(), wg=wg, task=stranger, session=session)
    kind = device_event(hub, account, device, "workvm.run", run_body(), **place)
    hub.hooks[("GET", f"/v1/scopes/{wg}/events")] = lambda _body, _request: (
        200,
        {"events": [slipped, kind], "next_after": None},
    )
    assert tool.run("test", "list", "--session", session)[1]["unverified"] == 2
    del hub.hooks[("GET", f"/v1/scopes/{wg}/events")]
    hub.wraps[:] = [wrap for wrap in hub.wraps if wrap["scope"] != "wg" or wrap["recipient_id"] != account.id]
    locked = tool.run("test", "list", "--session", session)[1]
    assert (locked["runs"], locked["locked"]) == ([], 6)


def test_place_refusals(tool: Tool, hub: HubFake, account: User) -> None:
    _wg, _device, task, session, _place = setup(tool, hub, account)
    assert tool.run("test", "list")[1]["reason"] == "session_or_task"
    assert tool.run("test", "list", "--session", session, "--task", task)[1]["reason"] == "session_or_task"
    assert tool.run("test", "list", "--session", "x")[1]["error"] == "invalid_argument"
    assert tool.run("test", "show", "tr-1", "--session", session)[1]["reason"] == "run_id"
    assert tool.run("test", "log", "nope", "--session", session)[1]["reason"] == "run_id"
    state_of(hub).rows[session]["task_id"] = None
    assert tool.run("test", "list", "--session", session)[1]["reason"] == "response_invalid"


def test_log_window_and_file(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    _wg, device, _task, session, place = setup(tool, hub, account)
    output = bytes(range(97, 123)) * 12_000 + "é".encode()  # two chunks, ends in a two-byte character
    log = add_log(hub, account, device, ref="tr-0000000000aa", output=output, total=len(output) + 1000, place=place)
    device_event(hub, account, device, "test.run", final("tr-0000000000aa", log), **place)
    run = ("test", "log", "tr-0000000000aa", "--session", session)
    code, first = tool.run(*run)
    assert code == 0, first
    assert (first["offset"], first["end"], first["next_offset"]) == (0, testruns.WINDOW, testruns.WINDOW)
    assert first["text"] == output[: testruns.WINDOW].decode()
    assert (first["bytes"], first["stored_bytes"], first["truncated"]) == (len(output) + 1000, len(output), True)
    across = tool.run(*run, "--offset", str(CB - 10), "--limit", "20")[1]
    assert across["text"] == output[CB - 10 : CB + 10].decode()
    last = tool.run(*run, "--offset", str(len(output) - 3), "--limit", "100")[1]
    assert (last["text"], last["next_offset"], last["end"]) == ("zé", None, len(output))
    assert tool.run(*run, "--offset", str(len(output)))[1]["text"] == ""
    assert tool.run(*run, "--offset", str(len(output) - 1), "--limit", "1")[1]["text"] == "�"
    assert tool.run(*run, "--offset", str(len(output) + 1))[1]["reason"] == "offset"
    for bad in (("--offset", "-1"), ("--limit", "0"), ("--limit", str(testruns.MAX_WINDOW + 1))):
        assert tool.run(*run, *bad)[1]["error"] == "invalid_argument", bad
    assert tool.run(*run, "--output", "x", "--limit", "5")[1]["reason"] == "output_whole_log"
    assert tool.run(*run, "--output", "x", "--offset", "5")[1]["reason"] == "output_whole_log"
    target = tmp_path / "run.log"
    target.write_bytes(b"old content that is replaced")
    code, written = tool.run(*run, "--output", str(target))
    assert code == 0, written
    assert target.read_bytes() == output
    assert (written["written_bytes"], written["output"], written["object_id"]) == (
        len(output),
        str(target),
        log["object_id"],
    )
    assert tool.run(*run, "--output", str(tmp_path / "no" / "dir"))[1]["reason"] == "output_unwritable"


def test_log_of_a_work_vm_job(tool: Tool, hub: HubFake, account: User) -> None:
    _wg, device, _task, session, place = setup(tool, hub, account)
    log = add_log(hub, account, device, ref="j-0000000000bb", output=b"long build output\n", place=place)
    job = run_body("j-0000000000bb", state="exited", exit_code=0, ended_at=at(0), output="output\n", output_bytes=18)
    device_event(hub, account, device, "workvm.run", {**job, "log": log}, **place)
    code, shown = tool.run("test", "log", "j-0000000000bb", "--session", session)
    assert code == 0, shown
    assert (shown["text"], shown["run_id"]) == ("long build output\n", "j-0000000000bb")
    without = run_body("j-0000000000cc")
    device_event(hub, account, device, "workvm.run", without, **place)
    refused = tool.run("test", "log", "j-0000000000cc", "--session", session)[1]
    assert (refused["error"], refused["reason"]) == ("not_found", "no_log")
    assert tool.run("test", "log", "j-0000000000dd", "--session", session)[1]["error"] == "not_found"
    # `session workvm` keeps a job with its log
    assert [run["log"] for run in tool.run("session", "workvm", "--session", session)[1]["runs"] if "log" in run] == [
        log
    ]


def test_log_refusals(tool: Tool, hub: HubFake, account: User) -> None:
    _wg, device, task, session, place = setup(tool, hub, account)

    def run_with(run: str, **changes: Any) -> tuple[str, dict[str, Any]]:
        sealed = {"ref": run, "output": b"out\n", **changes}
        log = add_log(hub, account, device, **sealed, place=place)
        device_event(hub, account, device, "test.run", final(run, log), **place)
        return run, log

    def reason(run: str) -> str:
        found = tool.run("test", "log", run, "--session", session)[1]
        assert found["error"] == "hub_refused", found
        return str(found["reason"])

    # the log of another run, of another session, or sizes that differ from the event
    swapped, _log = run_with("tr-000000000001", ref="tr-000000000002")
    assert reason(swapped) == "log_mismatch"
    other, other_log = run_with("tr-000000000003", changes={"session": uuid7()})
    assert reason(other) == "log_mismatch"
    # a manifest that does not verify, a key the user does not hold
    forged, forged_log = run_with("tr-000000000004")
    logs_of(hub)[forged_log["object_id"]].sig = logs_of(hub)[other_log["object_id"]].sig
    assert reason(forged) == "signature_invalid"
    later, _log = run_with("tr-000000000005", changes={"epoch": 99})
    assert reason(later) == "log_locked"
    # chunks: not base64, of another position, of the wrong length
    garbled, garbled_log = run_with("tr-000000000006")
    path = f"/v1/tasks/{task}/logs/{garbled_log['object_id']}/chunks/0"
    hub.hooks[("GET", path)] = lambda _body, _request: (200, {"ct": "*"})
    assert reason(garbled) == "header_invalid"
    del hub.hooks[("GET", path)]
    moved, moved_log = run_with("tr-000000000007", output=b"x" * (CB + 5))
    stored = logs_of(hub)[moved_log["object_id"]]
    stored.chunks.reverse()
    assert reason(moved) == "decrypt_failed"
    short, _log = run_with("tr-000000000008", pieces=[b"a" * 100, b"b" * CB])
    assert reason(short) == "header_invalid"
    # a file whose chunks do not give the signed digest is removed
    wrong, wrong_log = run_with("tr-000000000009", changes={"digest": stored.manifest["digest"]})
    target = Path(tool.root) / "wrong.log"
    refused = tool.run("test", "log", wrong, "--session", session, "--output", str(target))[1]
    assert (refused["reason"], target.exists()) == ("header_invalid", False)
    assert wrong_log["stored_bytes"] == 4
    # no log, unknown run, a log the hub does not have
    device_event(hub, account, device, "test.run", body("tr-00000000000a"), **place)
    assert tool.run("test", "log", "tr-00000000000a", "--session", session)[1]["reason"] == "no_log"
    assert tool.run("test", "log", "tr-00000000000b", "--session", session)[1]["error"] == "not_found"
    del logs_of(hub)[forged_log["object_id"]]
    assert tool.run("test", "log", forged, "--session", session)[1]["hub_error"] == "not_found"


def test_contract_check_of_the_body() -> None:
    assert testruns.parse_run(body()) is not None
    log = {"object_id": uuid7(), "bytes": 10, "stored_bytes": 4, "truncated": True}
    assert testruns.parse_run(final("tr-000000000001", log)) is not None
    for bad in (
        body(run_id="tr-1"),
        body(kind="perf"),
        body(state="done"),
        body(summary={"passed": 1}),
        body(summary={"passed": -1, "failed": None, "skipped": None, "errors": None}),
        body(summary="x"),
        body(log={**log, "stored_bytes": 11}),
        body(log={**log, "object_id": "x"}),
        body(log={**log, "truncated": 1}),
        body(reason="gone"),
        body(tail="x" * 8193),
        {k: v for k, v in body().items() if k != "tail"},
        "x",
    ):
        assert testruns.parse_run(bad) is None, bad
    assert workvm.parse_run({**run_body(), "log": log}) is not None
    assert workvm.parse_run({**run_body(), "log": None}) is not None
    assert workvm.parse_run({**run_body(), "log": {**log, "bytes": 1}}) is None
