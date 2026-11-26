"""The platform browser (docs/platform/spec/13-work-vm.md section 13, ADR-085) against a migrated store, a fake Lima, a
fake job helper, a fake `workvm-browser` and a fake guest: open and close, implicit open, the tools, the action
record, file transfer, failure and restart, and the teardown on every path."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from agent_runner.errors import RunnerError
from agent_runner.github import snapshot
from agent_runner.guestlink.errors import GuestRefused, GuestUnreachable
from agent_runner.operations.extensions import OperationContext, OperationFailed
from agent_runner.store.migrate import migrate
from agent_runner.vm.lima import EXIT_TIMEOUT
from agent_runner.workvm import browser, sync
from agent_runner.workvm import browser_records as rows
from agent_runner.workvm.browser import Browsers, BrowserSettings, describe, picture
from agent_runner.workvm.browser_helper import Answer, Image
from agent_runner.workvm.jobs import ShellResult
from agent_runner.workvm.manager import CallFailed, WorkVms
from tests.platform.contracts.harness import ID_BASE, Contracts, StrictValidator

from .kit import CHILD, JPEG, ORG, PAGE_URL, SESSION, TASK, TASK_B, World, browser_answer, session

OP = "0193000a-0000-7000-8000-0000000000f1"
OP_B = "0193000a-0000-7000-8000-0000000000f2"


CONTRACTS = Contracts.load()


def conforms(kind: str, instance: Any) -> None:
    """`instance` matches the contract (`events/<kind>.json`, or a `$defs` entry of `mcp/objects.json`)."""
    if kind.startswith("#"):
        schema: Any = {"$ref": ID_BASE + "mcp/objects.json" + kind}
        StrictValidator(schema, registry=CONTRACTS.registry).validate(instance)
    else:
        CONTRACTS.validate(kind, instance)


@pytest.fixture
def world(runner_dsn: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    assert migrate(runner_dsn).status == "applied"
    created = World(dsn=runner_dsn, tmp=tmp_path)
    created.workspace = {"docs/a.txt": b"A", "b.txt": b"BB"}

    async def guest_for(env: Any, local: Any) -> Any:
        return created.guest()

    monkeypatch.setattr(snapshot, "guest_for", guest_for)
    yield created
    for _identity, kind, body in created.events:  # every record a test wrote follows the contracts
        if kind.startswith("browser."):
            conforms(f"events/{kind}.json", body)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 0.01
        return self.now


class FakeLive:
    def __init__(self) -> None:
        self.stopped: list[str] = []

    def stop(self, session_id: str) -> None:
        self.stopped.append(session_id)


def make(vms: WorkVms, **settings: Any) -> Browsers:
    return Browsers(workvms=vms, settings=BrowserSettings(**settings), clock=Clock(), live=FakeLive())


def ctx(params: dict[str, Any], operation: str = OP, local: Any = None) -> OperationContext:
    op: Any = SimpleNamespace(id=operation)
    return OperationContext(
        context=None, operation=op, session=local or session(), params=params, data={}, attempt=1, env="env"
    )


def browser_rows(world: World) -> list[dict[str, Any]]:
    with psycopg.connect(world.dsn, autocommit=True, row_factory=dict_row) as conn:
        return list(conn.execute("SELECT * FROM runner.browser_session ORDER BY opened_at, browser_id"))


def vm_rows(world: World) -> list[dict[str, Any]]:
    with psycopg.connect(world.dsn, autocommit=True, row_factory=dict_row) as conn:
        return list(conn.execute("SELECT id, scope, state, delete_reason FROM runner.workvm ORDER BY created_at, id"))


def old_and_fresh(world: World) -> tuple[dict[str, Any], dict[str, Any]]:
    """The deleted and the running VM of a browser session that got a fresh VM."""
    found = sorted(vm_rows(world), key=lambda vm: vm["state"] == "running")
    assert [vm["state"] for vm in found] == ["deleted", "running"]
    return found[0], found[1]


def sql(world: World, query: str, params: tuple[Any, ...] = ()) -> None:
    with psycopg.connect(world.dsn, autocommit=True) as conn:
        conn.execute(query, params)


def events(world: World, kind: str) -> list[dict[str, Any]]:
    return [body for _identity, found, body in world.events if found == kind]


def states(world: World) -> list[tuple[str, str | None]]:
    return [(body["state"], body["reason"]) for body in events(world, "browser.state")]


# -- open and close --------------------------------------------------------------------------------------------


def test_open_starts_a_browser_vm_and_records_the_browser_session(world: World) -> None:
    async def body(vms: WorkVms) -> dict[str, Any]:
        return await make(vms).open(ctx({}))

    result = world.run(body)
    CONTRACTS.validate("ops/browser.open.result.json", result)
    [row] = browser_rows(world)
    [vm] = vm_rows(world)
    assert result == {
        "browser_id": row["browser_id"],
        "created": True,
        "state": "open",
        "workvm_id": vm["id"],
        "ready_ms": result["ready_ms"],
        "idle_seconds": 600,
        "url": "about:blank",
        "title": "",
    }
    assert result["ready_ms"] > 0
    assert (vm["scope"], vm["state"]) == ("browser", "running")
    assert (row["state"], row["implicit"], str(row["operation_id"]), row["workvm_id"]) == ("open", False, OP, vm["id"])
    assert states(world) == [("starting", None), ("open", None)]
    starting, opened = events(world, "browser.state")
    assert starting["workvm_id"] == opened["workvm_id"] == vm["id"]
    assert (opened["operation_id"], opened["implicit"], opened["closed_at"]) == (OP, False, None)
    assert events(world, "workvm.state") == []  # a browser VM writes none, like a run VM
    assert (vm["id"], "browser.selftest") in world.shell_calls
    assert any(
        line.startswith(f"runner.browser opened browser_id={row['browser_id']} ready_ms=") for line in world.logs
    )
    identity = world.events[0][0]
    assert (identity.session_id, identity.task_id, identity.org_id) == (SESSION, TASK, ORG)


def test_open_is_idempotent_per_session_and_opens_the_url(world: World) -> None:
    async def body(vms: WorkVms) -> list[dict[str, Any]]:
        browsers = make(vms)
        first = await browsers.open(ctx({"url": "https://a.test/", "idle_seconds": 120}))
        second = await browsers.open(ctx({}, OP_B))
        third = await browsers.open(ctx({"url": "https://b.test/", "idle_seconds": 3600}, OP_B))
        return [first, second, third]

    first, second, third = world.run(body)
    assert (first["created"], first["url"], first["title"], first["idle_seconds"]) == (True, PAGE_URL, "Example", 120)
    assert (second["created"], second["browser_id"], second["url"], second["title"]) == (
        False,
        first["browser_id"],
        PAGE_URL,
        "",
    )
    assert (third["created"], third["idle_seconds"]) == (False, 3600)
    assert [request for _vm, request in world.browser.requests] == [
        {"tool": "browser_navigate", "arguments": {"url": "https://a.test/", "snapshot": False}},
        {"tool": "browser_navigate", "arguments": {"url": "https://b.test/", "snapshot": False}},
    ]
    [row] = browser_rows(world)
    assert (row["seq"], row["idle_seconds"], row["url"]) == (2, 3600, PAGE_URL)
    assert [body["seq"] for body in events(world, "browser.action")] == [1, 2]
    assert len(vm_rows(world)) == 1


def test_close_tears_the_vm_down_and_names_how_the_last_one_ended(world: World) -> None:
    async def body(vms: WorkVms) -> list[dict[str, Any]]:
        browsers = make(vms)
        never = await browsers.close(ctx({}))
        opened = await browsers.open(ctx({"url": "https://a.test/"}))
        closed = await browsers.close(ctx({}))
        again = await browsers.close(ctx({}))
        assert browsers.live.stopped == [SESSION]
        return [never, opened, closed, again]

    never, opened, closed, again = world.run(body)
    for result in (never, closed, again):
        CONTRACTS.validate("ops/browser.close.result.json", result)
    assert never == {"closed": False, "browser_id": None, "actions": 0, "reason": None}
    assert closed == {"closed": True, "browser_id": opened["browser_id"], "actions": 1, "reason": "browser_closed"}
    assert again == {**closed, "closed": False}
    [vm] = vm_rows(world)
    assert (vm["state"], vm["delete_reason"]) == ("deleted", "browser_closed")
    [row] = browser_rows(world)
    assert (row["state"], row["close_reason"]) == ("closed", "browser_closed")
    closing = events(world, "browser.state")[-1]
    assert (closing["state"], closing["reason"], closing["actions"]) == ("closed", "browser_closed", 1)
    assert closing["closed_at"] is not None
    assert f"runner.browser closed browser_id={opened['browser_id']} reason=browser_closed actions=1" in world.logs


@pytest.mark.parametrize(
    ("change", "code"),
    [("credit", "credit_exhausted"), ("boundary", "workvm_unavailable")],
)
def test_open_fails_formally_before_anything_starts(world: World, change: str, code: str) -> None:
    if change == "credit":
        world.exhausted.add(ORG)
    else:
        world.boundary = False

    async def body(vms: WorkVms) -> None:
        await make(vms).open(ctx({}))

    with pytest.raises(OperationFailed) as caught:
        world.run(body)
    assert caught.value.code == code
    assert events(world, "browser.state") == []
    assert browser_rows(world) == []


def test_a_browser_vm_beside_a_task_vm_needs_only_free_resources(world: World) -> None:
    """No fixed number of browser or work VMs (ADR-075 addendum 2)."""

    async def body(vms: WorkVms) -> dict[str, Any]:
        await vms.request(session(), {})
        return await make(vms).open(ctx({}))

    assert world.run(body)["created"] is True


def test_a_vm_that_does_not_start_fails_the_open_and_ends_the_countdown(world: World) -> None:
    world.fail.add("clone")

    async def body(vms: WorkVms) -> None:
        await make(vms).open(ctx({}))

    with pytest.raises(OperationFailed) as caught:
        world.run(body)
    assert (caught.value.code, caught.value.retryable) == ("upstream_error", True)
    assert states(world) == [("starting", None), ("closed", "failed")]
    assert browser_rows(world) == []


def test_a_browser_that_answers_its_second_selftest_opens(world: World) -> None:
    world.browser.probes.append(ShellResult(1, b'{"ok":false,"error":"the browser daemon did not start\x07"}\n', False))

    async def body(vms: WorkVms) -> None:
        await make(vms).open(ctx({}))

    world.run(body)
    [vm] = vm_rows(world)
    assert vm["state"] == "running"
    assert [call for _vm, call in world.shell_calls].count("browser.selftest") == 2
    assert states(world) == [("starting", None), ("open", None)]
    [line] = [line for line in world.logs if line.startswith("runner.browser probe_failed ")]
    assert " rc=1 ms=" in line
    assert line.endswith(' said="{\\"ok\\":false,\\"error\\":\\"the browser daemon did not start \\"}"')


def test_a_browser_that_does_not_answer_its_selftest_fails_the_open_and_its_vm_goes(world: World) -> None:
    world.browser.probes += [ShellResult(0, b'{"ok": 1}', False), ShellResult(0, b'{"ok": 1}', False)]

    async def body(vms: WorkVms) -> None:
        await make(vms).open(ctx({}))

    with pytest.raises(OperationFailed) as caught:
        world.run(body)
    assert caught.value.code == "upstream_error"
    [vm] = vm_rows(world)
    assert (vm["state"], vm["delete_reason"]) == ("deleted", "failed")
    assert states(world) == [("starting", None), ("closed", "failed")]


@pytest.mark.parametrize("step", ["probe", "insert"])
def test_the_vm_goes_when_the_open_breaks_after_it_started(
    world: World, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    async def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("broken")

    if step == "probe":
        monkeypatch.setattr(Browsers, "_probe", broken)
    else:
        monkeypatch.setattr(rows, "insert", broken)

    async def body(vms: WorkVms) -> None:
        await make(vms).open(ctx({}))

    with pytest.raises(RuntimeError):
        world.run(body)
    [vm] = vm_rows(world)
    assert (vm["state"], vm["delete_reason"]) == ("deleted", "failed")


# -- tool calls ----------------------------------------------------------------------------------------------------


def call(world: World, tool: str, arguments: dict[str, Any], *, before: Any = None, **settings: Any) -> dict[str, Any]:
    async def body(vms: WorkVms) -> dict[str, Any]:
        browsers = make(vms, **settings)
        if before is not None:
            await before(browsers)
        return await browsers.call("env", session(), tool, arguments)

    answer = world.run(body)
    conforms("#/$defs/output_browser", answer)
    return answer


def test_a_call_without_a_browser_opens_one_implicitly(world: World) -> None:
    answer = call(world, "browser_snapshot", {})
    [row] = browser_rows(world)
    assert answer == {
        "browser_id": row["browser_id"],
        "seq": 1,
        "ok": True,
        "error": None,
        "notice": browser.IMPLICIT_NOTICE,
        "url": PAGE_URL,
        "title": "Example",
        "tab": 0,
        "tabs": [{"index": 0, "url": PAGE_URL, "title": "Example", "active": True}],
        "snapshot": '- heading "Example" [ref=e1]',
        "result": None,
        "file": None,
    }
    assert (row["implicit"], row["operation_id"], row["seq"], row["url"]) == (True, None, 1, PAGE_URL)
    opened = events(world, "browser.state")[1]
    assert (opened["state"], opened["implicit"], opened["operation_id"]) == ("open", True, None)
    [action] = events(world, "browser.action")
    assert action == {
        "browser_id": row["browser_id"],
        "seq": 1,
        "tool": "browser_snapshot",
        "action": "snapshot",
        "target": None,
        "text": None,
        "url": PAGE_URL,
        "title": "Example",
        "tab": 0,
        "ok": True,
        "error": None,
        "notice": browser.IMPLICIT_NOTICE,
        "at": "2026-11-23T10:00:00.000Z",
        "duration_ms": action["duration_ms"],
        "image": {
            "type": "image/jpeg",
            "width": 960,
            "height": 600,
            "data": base64.urlsafe_b64encode(JPEG).decode().rstrip("="),
        },
        "image_omitted": None,
    }
    assert world.browser.requests[0][1] == {"tool": "browser_snapshot", "arguments": {}}


def test_an_implicit_open_that_fails_is_the_tool_error_unavailable(world: World) -> None:
    world.exhausted.add(ORG)
    with pytest.raises(CallFailed) as caught:
        call(world, "browser_snapshot", {})
    assert caught.value.error == "unavailable"
    assert caught.value.message.startswith("No browser could be opened: The organization's credit is used up")


def test_a_query_needs_the_search_switch(world: World) -> None:
    off = call(world, "browser_navigate", {"query": "agent platform"})
    assert (off["ok"], off["error"], off["url"], off["snapshot"]) == (False, browser.SEARCH_OFF, "about:blank", "")
    assert world.browser.requests == []
    action = events(world, "browser.action")[-1]
    assert (action["action"], action["text"], action["image"], action["image_omitted"]) == (
        "search",
        "agent platform",
        None,
        "unavailable",
    )
    on = call(world, "browser_navigate", {"query": "a&b c"}, search_url="https://search.test/?q={q}&l=en")
    assert on["ok"] is True
    assert world.browser.requests[-1][1] == {
        "tool": "browser_navigate",
        "arguments": {"url": "https://search.test/?q=a%26b+c&l=en"},
    }


def test_snapshot_false_leaves_the_snapshot_empty(world: World) -> None:
    answer = call(world, "browser_click", {"target": "e1", "snapshot": False})
    assert answer["snapshot"] == ""
    assert world.browser.requests[-1][1]["arguments"] == {"target": "e1", "snapshot": False}


def test_typed_text_is_masked_for_a_password_field_and_cut_in_the_record(world: World) -> None:
    world.browser.script.append(browser_answer(masked=True))
    call(world, "browser_type", {"target": "e2", "text": "secret"})
    world.browser.script.append(browser_answer())
    call(world, "browser_type", {"target": "t" * 1024, "text": "x" * 5000})
    masked, plain = events(world, "browser.action")
    assert (masked["action"], masked["target"], masked["text"]) == ("type", "e2", "••••")
    assert (plain["target"], plain["text"]) == ("t" * 1024, "x" * 1024)
    assert all("secret" not in json.dumps(body) for body in events(world, "browser.action"))


def test_the_picture_is_kept_only_when_it_fits(world: World) -> None:
    big = base64.b64encode(b"x" * (browser.IMAGE_BYTES + 1)).decode()
    world.browser.script += [
        browser_answer(image={"width": 960, "height": 600, "jpeg": big}),
        browser_answer(image={"width": 961, "height": 600, "jpeg": "AAAA"}),
        browser_answer(image=None),
    ]

    async def body(vms: WorkVms) -> None:
        browsers = make(vms)
        for _ in range(3):
            await browsers.call("env", session(), "browser_snapshot", {})

    world.run(body)
    assert [(b["image"], b["image_omitted"]) for b in events(world, "browser.action")] == [
        (None, "too_large"),
        (None, "too_large"),
        (None, "unavailable"),
    ]


def test_a_failed_action_is_ok_false_with_the_page(world: World) -> None:
    world.browser.script.append(browser_answer(ok=False, error="No element e9.", notice="A dialog was accepted."))
    answer = call(world, "browser_click", {"x": 10, "y": 20})
    assert (answer["ok"], answer["error"]) == (False, "No element e9.")
    assert answer["notice"] == browser.IMPLICIT_NOTICE + " A dialog was accepted."
    action = events(world, "browser.action")[-1]
    assert (action["action"], action["target"], action["ok"]) == ("click", "10,20", False)


# -- files -----------------------------------------------------------------------------------------------------


def test_upload_copies_the_workspace_files_into_the_vm(world: World) -> None:
    answer = call(world, "browser_upload", {"target": "#file", "paths": ["docs/a.txt", "b.txt", "docs/a.txt"]})
    assert answer["ok"] is True
    [(destination, files)] = world.browser.uploads
    assert destination.startswith("/var/lib/workvm-browser/uploads/")
    assert files == {"0/a.txt": b"A", "1/b.txt": b"BB", "2/a.txt": b"A"}
    request = world.browser.requests[-1][1]
    assert request == {
        "tool": "browser_upload",
        "arguments": {
            "target": "#file",
            "files": [f"{destination}/0/a.txt", f"{destination}/1/b.txt", f"{destination}/2/a.txt"],
        },
    }
    assert sorted(world.exports) == [".", "docs"]
    action = events(world, "browser.action")[-1]
    assert action["text"] == "docs/a.txt, b.txt, docs/a.txt"


@pytest.mark.parametrize("case", ["missing", "unreadable", "invalid", "too_large", "copy"])
def test_an_upload_that_cannot_copy_is_ok_false(world: World, monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    paths = ["docs/a.txt"]
    if case == "missing":
        paths = ["docs/none.txt"]
    elif case == "unreadable":
        world.export_error = GuestUnreachable("files")
    elif case == "invalid":

        def invalid(_path: Any) -> Any:
            raise RunnerError(sync.INVALID, "archive_invalid")

        monkeypatch.setattr(sync, "read_export", invalid)
    elif case == "too_large":
        monkeypatch.setattr(browser, "UPLOAD_BYTES", 0)
    else:
        world.browser.upload_result = ShellResult(2, b"", False)
    answer = call(world, "browser_upload", {"target": "#file", "paths": paths})
    assert answer["ok"] is False
    expected = {
        "missing": "There is no file docs/none.txt in the workspace.",
        "unreadable": "The files could not be read from the workspace: The workspace could not be read. Try again.",
        "invalid": "The files could not be read from the workspace.",
        "too_large": "The files are larger than the browser accepts (100 MiB).",
        "copy": "The files could not be copied into the browser VM.",
    }
    assert answer["error"] == expected[case]
    assert world.browser.requests == []


INVALID_DIRECTORY = "The path is not a valid directory of the workspace."
DOWNLOAD = {"path": "/var/lib/workvm-browser/downloads/r.pdf", "name": "r.pdf", "bytes": 4}


def test_download_and_screenshot_come_into_the_workspace(world: World) -> None:
    world.browser.script += [
        browser_answer(file=DOWNLOAD),
        browser_answer(file=DOWNLOAD),
        browser_answer(file={"path": "/var/lib/workvm-browser/shot.png", "name": "shot.png", "bytes": 4}),
        browser_answer(file=DOWNLOAD),
        browser_answer(ok=False, error="no", file=DOWNLOAD),
    ]

    async def body(vms: WorkVms) -> list[dict[str, Any]]:
        browsers = make(vms)
        found = [await browsers.call("env", session(), "browser_download", {"url": "https://a.test/r.pdf"})]
        found.append(await browsers.call("env", session(), "browser_download", {"target": "e3", "path": "out/x.pdf"}))
        found.append(await browsers.call("env", session(), "browser_screenshot", {"full_page": True}))
        found.append(await browsers.call("env", session(), "browser_click", {"target": "e3"}))
        found.append(await browsers.call("env", session(), "browser_download", {"target": "e3"}))
        return found

    first, second, shot, click, failed = world.run(body)
    browser_id = first["browser_id"]
    assert first["file"] == {"path": ".browser/downloads/r.pdf", "name": "r.pdf", "bytes": 4}
    assert second["file"] == {"path": "out/x.pdf", "name": "r.pdf", "bytes": 4}
    assert shot["file"] == {"path": f".browser/screenshots/{browser_id}-3.png", "name": "shot.png", "bytes": 4}
    assert (click["file"], failed["file"], failed["ok"]) == (None, None, False)
    assert world.packed == [DOWNLOAD["path"], DOWNLOAD["path"], "/var/lib/workvm-browser/shot.png"]
    assert [directory for directory, _data, _replace in world.imports] == [
        ".browser/downloads",
        "out",
        ".browser/screenshots",
    ]
    assert "path" not in world.browser.requests[1][1]["arguments"]
    actions = events(world, "browser.action")
    assert (actions[0]["target"], actions[1]["target"]) == ("https://a.test/r.pdf", "e3")


@pytest.mark.parametrize("case", ["import", "pack"])
def test_a_file_that_cannot_come_into_the_workspace_is_ok_false(world: World, case: str) -> None:
    world.browser.script.append(browser_answer(file=DOWNLOAD))
    if case == "import":
        world.import_error = GuestRefused("files", 400, "path_invalid")
    else:
        world.helper_fail["packfile"] = ShellResult(1, b"", False)
    answer = call(world, "browser_download", {"target": "e3"})
    assert (answer["ok"], answer["file"]) == (False, None)
    assert (
        answer["error"]
        == {
            "import": "The file could not be saved in the workspace: " + INVALID_DIRECTORY,
            "pack": "The file could not be brought into the workspace.",
        }[case]
    )
    assert answer["url"] == PAGE_URL


# -- failure and restart ---------------------------------------------------------------------------------------


def run_calls(world: World, steps: list[tuple[str, dict[str, Any]]], before: Any = None) -> list[dict[str, Any]]:
    async def body(vms: WorkVms) -> list[dict[str, Any]]:
        browsers = make(vms)
        found = []
        for tool, arguments in steps:
            if tool == "hook":
                await arguments["run"](vms, browsers)
                continue
            found.append(await browsers.call("env", session(), tool, arguments))
        return found

    answers = world.run(body)
    for answer in answers:
        conforms("#/$defs/output_browser", answer)
    return answers


def test_a_timed_out_call_keeps_the_vm(world: World) -> None:
    world.browser.script += [browser_answer(), ShellResult(EXIT_TIMEOUT, b"", False), ShellResult(1, b"", False)]
    _first, slow, failed = run_calls(
        world, [("browser_snapshot", {}), ("browser_navigate", {"url": "https://slow.test/"}), ("browser_snapshot", {})]
    )
    assert (slow["ok"], slow["error"], slow["url"]) == (False, "timeout", PAGE_URL)
    assert (failed["ok"], failed["error"]) == (False, browser.FAILED_ERROR)
    [vm] = vm_rows(world)
    assert vm["state"] == "running"
    assert [call for _vm, call in world.shell_calls].count("browser.selftest") == 3  # start and two probes


@pytest.mark.parametrize("failure", ["probe", "invalid", "too_large", "dead"])
def test_a_lost_vm_goes_and_the_next_call_gets_a_fresh_one(world: World, failure: str) -> None:
    def kill(vms: WorkVms, _browsers: Browsers) -> Any:
        async def run() -> None:
            world.dead.add(vm_rows(world)[0]["id"])

        return run()

    if failure == "probe":
        world.browser.script += [browser_answer(), ShellResult(EXIT_TIMEOUT, b"", False)]
        world.browser.probes += [ShellResult(0, b'{"ok": true}', False), ShellResult(EXIT_TIMEOUT, b"", False)]
    elif failure == "invalid":
        world.browser.script += [browser_answer(), browser_answer(extra=1)]
    elif failure == "too_large":
        world.browser.script += [browser_answer(), ShellResult(-9, b"{", True)]
    else:
        world.browser.script += [browser_answer()]
    steps: list[tuple[str, dict[str, Any]]] = [("browser_snapshot", {})]
    if failure == "dead":
        steps.append(("hook", {"run": kill}))
    steps += [("browser_click", {"target": "e1"}), ("browser_click", {"target": "e1"})]
    steps += [("browser_navigate", {"url": "https://again.test/"})]
    _first, lost, refused, navigated = run_calls(world, steps)
    assert (lost["ok"], lost["error"]) == (False, browser.LOST_ERROR)
    old, fresh = old_and_fresh(world)
    assert (old["state"], old["delete_reason"]) == ("deleted", "vm_lost")
    assert fresh["state"] == "running"
    assert refused == {
        "browser_id": lost["browser_id"],
        "seq": 3,
        "ok": False,
        "error": browser.RESTARTED_ERROR,
        "notice": browser.RESTART_NOTICE,
        "url": "about:blank",
        "title": "",
        "tab": 0,
        "tabs": [],
        "snapshot": "",
        "result": None,
        "file": None,
    }
    assert (navigated["ok"], navigated["seq"], navigated["notice"]) == (True, 4, None)
    assert ("restarted", "vm_lost") in states(world)
    [row] = browser_rows(world)
    assert (row["state"], row["workvm_id"], row["seq"]) == ("open", fresh["id"], 4)
    assert any(line.startswith("runner.browser lost browser_id=") for line in world.logs)
    assert any(line.startswith("runner.browser restarted browser_id=") for line in world.logs)


def test_after_a_restart_the_browser_session_continues_on_a_fresh_vm(world: World) -> None:
    async def restart(vms: WorkVms, _browsers: Browsers) -> None:
        await vms.reconcile()

    first, navigated = run_calls(
        world,
        [("browser_snapshot", {}), ("hook", {"run": restart}), ("browser_navigate", {"url": "https://again.test/"})],
    )
    assert (navigated["browser_id"], navigated["seq"], navigated["ok"]) == (first["browser_id"], 2, True)
    assert navigated["notice"] == browser.RESTART_NOTICE
    assert ("restarted", "restart") in states(world)
    old, _fresh = old_and_fresh(world)
    assert old["delete_reason"] == "restart"


def test_a_vm_neither_running_nor_gone_is_replaced(world: World) -> None:
    async def stop(vms: WorkVms, _browsers: Browsers) -> None:
        sql(world, "UPDATE runner.workvm SET state = 'stopped'")

    run_calls(world, [("browser_snapshot", {}), ("hook", {"run": stop}), ("browser_snapshot", {})])
    old, fresh = old_and_fresh(world)
    assert (old["state"], old["delete_reason"], fresh["state"]) == ("deleted", "vm_lost", "running")


def test_a_restart_that_cannot_get_a_vm_is_unavailable(world: World) -> None:
    async def lose(vms: WorkVms, _browsers: Browsers) -> None:
        await vms.reconcile()
        world.exhausted.add(ORG)

    with pytest.raises(CallFailed) as caught:
        run_calls(world, [("browser_snapshot", {}), ("hook", {"run": lose}), ("browser_snapshot", {})])
    assert caught.value.error == "unavailable"
    assert caught.value.message.startswith("The browser VM could not be started again: ")


def test_open_of_a_browser_without_vm_restarts_it(world: World) -> None:
    async def body(vms: WorkVms) -> list[dict[str, Any]]:
        browsers = make(vms)
        first = await browsers.open(ctx({}))
        await vms.reconcile()
        second = await browsers.open(ctx({"url": "https://a.test/"}, OP_B))
        await vms.reconcile()
        third = await browsers.open(ctx({}, OP_B))
        return [first, second, third]

    first, second, third = world.run(body)
    assert first["browser_id"] == second["browser_id"] == third["browser_id"]
    assert (second["created"], second["url"]) == (False, PAGE_URL)
    assert events(world, "browser.action")[-1]["notice"] == browser.RESTART_NOTICE
    assert states(world).count(("restarted", "restart")) == 2


def test_a_call_on_a_browser_closed_meanwhile_is_unavailable(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    async def closed(*_args: Any) -> None:
        return None

    monkeypatch.setattr(rows, "next_seq", closed)
    with pytest.raises(CallFailed) as caught:
        call(world, "browser_snapshot", {})
    assert (caught.value.error, caught.value.message) == ("unavailable", browser.CLOSED_TEXT)


def test_lose_and_running_for_the_live_picture(world: World) -> None:
    async def body(vms: WorkVms) -> None:
        browsers = make(vms)
        assert await browsers.running(SESSION) is None
        await browsers.open(ctx({}))
        found = await browsers.running(SESSION)
        assert found is not None
        row, vm = found
        await browsers.lose(row.browser_id, "wvm-00000000")  # another VM: nothing
        await browsers.lose("br-000000000000", vm.id)  # another browser session: nothing
        assert (await vm_records_state(vms, vm.id)) == "running"
        await browsers.lose(row.browser_id, vm.id)
        assert (await vm_records_state(vms, vm.id)) == "deleted"
        assert await browsers.running(SESSION) is None
        assert browsers.live.stopped == [SESSION]

    world.run(body)


async def vm_records_state(vms: WorkVms, workvm_id: str) -> str:
    from agent_runner.workvm import records  # noqa: PLC0415

    found = await records.get(vms.deps.db, workvm_id)
    assert found is not None
    return found.state


# -- teardown --------------------------------------------------------------------------------------------------


def test_the_idle_pass_closes_a_browser_without_calls(world: World) -> None:
    async def body(vms: WorkVms) -> list[list[str]]:
        browsers = make(vms)
        await browsers.open(ctx({"idle_seconds": 60}))
        world.now += timedelta(seconds=1)
        await browsers.open(ctx({}, local=session(CHILD, task_id=TASK_B)))
        world.now = world.now.replace(minute=1)
        async with browsers._lock(SESSION):
            skipped = await browsers.idle_pass()
        closed = await browsers.idle_pass()
        world.now = world.now.replace(minute=20)
        later = await browsers.idle_pass()
        return [skipped, closed, later]

    skipped, closed, later = world.run(body)
    first, second = browser_rows(world)
    assert (skipped, closed, later) == ([], [first["browser_id"]], [second["browser_id"]])
    assert (first["close_reason"], second["close_reason"]) == ("browser_idle", "browser_idle")
    assert all(vm["delete_reason"] == "browser_idle" for vm in vm_rows(world))


def test_session_end_task_close_and_credit_close_the_browser(world: World) -> None:
    other = session(CHILD, task_id=TASK_B)

    async def body(vms: WorkVms) -> list[list[str]]:
        browsers = make(vms)
        await browsers.open(ctx({}))
        world.now += timedelta(seconds=1)
        await browsers.open(ctx({}, local=other))
        world.now += timedelta(seconds=1)
        none = await browsers.session_ended(OP)  # no browser session of that session
        ended = await browsers.session_ended(SESSION, reason="task_closed")
        again = await browsers.session_ended(SESSION)
        nothing = await browsers.credit_pass()
        world.exhausted.add(ORG)
        credit = await browsers.credit_pass()
        world.exhausted.clear()
        await browsers.open(ctx({}, local=other))
        closed = await browsers.close_task(TASK_B)
        twice = await browsers.close_task(TASK_B)
        return [none, ended, again, nothing, credit, closed, twice]

    none, ended, again, nothing, credit, closed, twice = world.run(body)
    first, second, third = browser_rows(world)
    assert (none, again, nothing, twice) == ([], [], [], [])
    assert (ended, credit, closed) == ([first["browser_id"]], [second["browser_id"]], [third["browser_id"]])
    assert [row["close_reason"] for row in (first, second, third)] == ["task_closed", "credit_exhausted", "task_closed"]
    assert [vm["delete_reason"] for vm in vm_rows(world)] == ["task_closed", "credit_exhausted", "task_closed"]


def test_closing_a_row_twice_or_without_a_live_vm(world: World) -> None:
    async def body(vms: WorkVms) -> None:
        browsers = make(vms)
        await browsers.open(ctx({}))
        row = await rows.open_of(vms.deps.db, SESSION)
        assert row is not None
        await vms.reconcile()  # its VM is gone
        assert await browsers._close(row, "session_ended") is not None
        assert await browsers._close(row, "session_ended") is None
        sql(
            world,
            "INSERT INTO runner.browser_session (browser_id, session_id, task_id, org_id, wg_id, state, idle_seconds,"
            " implicit, opened_at, last_call_at) SELECT 'br-00000000000b', session_id, task_id, org_id, wg_id, 'open',"
            " 600, true, opened_at, last_call_at FROM runner.browser_session",
        )
        assert await browsers.session_ended(SESSION) == ["br-00000000000b"]

    world.run(body)
    assert [row["close_reason"] for row in browser_rows(world)] == ["session_ended", "session_ended"]


def test_an_event_that_cannot_be_written_is_logged(world: World) -> None:
    world.emit_error = RuntimeError("down")

    async def body(vms: WorkVms) -> dict[str, Any]:
        return await make(vms).open(ctx({}))

    assert world.run(body)["created"] is True
    assert "runner.browser event_failed kind=browser.state error=RuntimeError" in world.logs


def test_without_live_picture_nothing_is_stopped(world: World) -> None:
    async def body(vms: WorkVms) -> dict[str, Any]:
        browsers = Browsers(workvms=vms)
        await browsers.open(ctx({}))
        return await browsers.close(ctx({}))

    assert world.run(body)["closed"] is True


# -- the store -------------------------------------------------------------------------------------------------


def test_one_open_browser_session_per_session(world: World) -> None:
    insert = (
        "INSERT INTO runner.browser_session (browser_id, session_id, task_id, org_id, wg_id, state, idle_seconds,"
        " implicit, opened_at, last_call_at, closed_at, close_reason, operation_id)"
        " VALUES (%s, %s, %s, %s, %s, %s, 600, %s, now(), now(), %s, %s, %s)"
    )
    sql(world, insert, ("br-000000000001", SESSION, TASK, ORG, ORG, "open", True, None, None, None))
    sql(world, insert, ("br-000000000002", SESSION, TASK, ORG, ORG, "closed", False, "now", "browser_idle", OP))
    for values in [
        ("br-000000000003", SESSION, TASK, ORG, ORG, "open", True, None, None, None),  # a second open one
        ("br-00000000000X", CHILD, TASK, ORG, ORG, "open", True, None, None, None),  # the ID format
        ("br-000000000004", CHILD, TASK, ORG, ORG, "closed", True, None, None, None),  # closed without reason
        ("br-000000000005", CHILD, TASK, ORG, ORG, "open", False, None, None, None),  # explicit without operation
        ("br-000000000006", CHILD, TASK, ORG, ORG, "closed", True, "now", "restart", None),  # not a close reason
    ]:
        with pytest.raises(psycopg.errors.IntegrityError):
            sql(world, insert, values)
    for number, reason in enumerate(("browser_closed", "browser_idle", "vm_lost")):
        sql(
            world,
            "INSERT INTO runner.workvm (id, lima_name, slot, scope, task_id, session_id, requested_by, org_id, wg_id,"
            " size, cpus, memory_mib, disk_gib, image, state, created_at, last_used_at, delete_reason)"
            " VALUES (%s, %s, 0, 'browser', %s, %s, %s, %s, %s, 'standard', 4, 8192, 50, 'img', 'deleted', now(),"
            " now(), %s)",
            (f"wvm-0000000{number}", "n-" + reason, TASK, SESSION, SESSION, ORG, ORG, reason),
        )
    with pytest.raises(psycopg.errors.IntegrityError):
        sql(
            world,
            "INSERT INTO runner.workvm (id, lima_name, slot, scope, task_id, requested_by, org_id, wg_id, size, cpus,"
            " memory_mib, disk_gib, image, state, created_at, last_used_at) VALUES ('wvm-0000000f', 'n-x', 1,"
            " 'browser', %s, %s, %s, %s, 'standard', 4, 8192, 50, 'img', 'running', now(), now())",
            (TASK, SESSION, ORG, ORG),
        )


# -- record helpers --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "arguments", "described"),
    [
        ("browser_navigate", {"url": "https://a.test/"}, ("navigate", "https://a.test/", None)),
        ("browser_navigate", {"history": "back"}, ("back", None, None)),
        ("browser_navigate", {"query": "q"}, ("search", None, "q")),
        ("browser_click", {"target": "e1", "double": True}, ("double_click", "e1", None)),
        ("browser_click", {"x": 1, "y": 2}, ("click", "1,2", None)),
        ("browser_dialog", {"action": "accept", "text": "yes"}, ("dialog.accept", None, "yes")),
        ("browser_tabs", {"action": "new", "url": "https://a.test/"}, ("tabs.new", None, "https://a.test/")),
        ("browser_tabs", {"action": "select", "index": 2}, ("tabs.select", None, 2)),
        ("browser_tabs", {"action": "list"}, ("tabs.list", None, None)),
        ("browser_download", {"url": "https://a.test/f"}, ("download", "https://a.test/f", None)),
        ("browser_type", {"target": "e1", "text": "hi"}, ("type", "e1", "hi")),
        ("browser_select", {"target": "e1", "values": ["a", "b"]}, ("select", "e1", "a, b")),
        ("browser_press", {"key": "Enter"}, ("press", None, "Enter")),
        ("browser_scroll", {"dy": -5}, ("scroll", None, -5)),
        ("browser_evaluate", {"expression": "1"}, ("evaluate", None, "1")),
        ("browser_upload", {"target": "e1", "paths": ["a"]}, ("upload", "e1", "a")),
        ("browser_wait", {"gone": "x"}, ("wait", None, "x")),
        ("browser_wait", {"ms": 5}, ("wait", None, "5 ms")),
        ("browser_wait", {"timeout_ms": 5}, ("wait", None, None)),
        ("browser_hover", {"target": "e1"}, ("hover", "e1", None)),
        ("browser_screenshot", {}, ("screenshot", None, None)),
    ],
)
def test_describe(tool: str, arguments: dict[str, Any], described: tuple[Any, ...]) -> None:
    assert describe(tool, arguments) == described


def test_picture() -> None:
    base = Answer(True, None, None, "u", "t", 0, [], "", False, None, Image(960, 10, b"j"), None)
    assert picture(base) == ({"type": "image/jpeg", "width": 960, "height": 10, "data": "ag"}, None)
    assert picture(dataclasses.replace(base, image=None)) == (None, "unavailable")


def test_calls_of_one_session_are_serialised(world: World) -> None:
    async def body(vms: WorkVms) -> list[int]:
        browsers = make(vms)
        found = await asyncio.gather(*(browsers.call("env", session(), "browser_snapshot", {}) for _ in range(3)))
        return sorted(answer["seq"] for answer in found)

    assert world.run(body) == [1, 2, 3]
    assert len(browser_rows(world)) == 1


def test_an_open_row_without_a_vm_gets_one_on_the_next_call(world: World) -> None:
    sql(
        world,
        "INSERT INTO runner.browser_session (browser_id, session_id, task_id, org_id, wg_id, state, idle_seconds,"
        " implicit, opened_at, last_call_at) VALUES ('br-00000000000c', %s, %s, %s, %s, 'open', 600, true, now(),"
        " now())",
        (SESSION, TASK, ORG, ORG),
    )
    answer = call(world, "browser_navigate", {"url": "https://a.test/"})
    assert (answer["browser_id"], answer["ok"], answer["notice"]) == ("br-00000000000c", True, browser.RESTART_NOTICE)
    assert ("restarted", "vm_lost") in states(world)


# -- races and gaps (review) -----------------------------------------------------------------------------------


def slow_start(monkeypatch: pytest.MonkeyPatch, gate: asyncio.Event, started: asyncio.Event) -> None:
    original = Browsers._start_vm

    async def start(self: Browsers, *args: Any, **kwargs: Any) -> Any:
        started.set()
        await gate.wait()
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(Browsers, "_start_vm", start)


@pytest.mark.parametrize("path", ["restart", "create"])
def test_a_teardown_waits_for_a_starting_vm_and_leaves_none_running(
    world: World, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    async def body(vms: WorkVms) -> dict[str, Any]:
        browsers = make(vms)
        if path == "restart":
            await browsers.open(ctx({}))
            await vms.reconcile()
        gate, started = asyncio.Event(), asyncio.Event()
        slow_start(monkeypatch, gate, started)
        calling = asyncio.ensure_future(browsers.call("env", session(), "browser_navigate", {"url": PAGE_URL}))
        await started.wait()
        ending = asyncio.ensure_future(browsers.session_ended(SESSION))
        await asyncio.sleep(0.01)
        assert not ending.done()  # it waits for the call that starts the VM
        gate.set()
        answer = await calling
        assert len(await ending) == 1
        assert SESSION not in browsers.locks
        return answer

    assert world.run(body)["ok"] is True
    assert all(vm["state"] == "deleted" for vm in vm_rows(world))
    assert browser_rows(world)[-1]["close_reason"] == "session_ended"


def test_a_vm_started_for_a_browser_closed_meanwhile_goes(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    original = Browsers._start_vm

    async def start(self: Browsers, *args: Any, **kwargs: Any) -> Any:
        vm = await original(self, *args, **kwargs)
        sql(
            world, "UPDATE runner.browser_session SET state = 'closed', closed_at = now(), close_reason = 'task_closed'"
        )
        return vm

    async def body(vms: WorkVms) -> None:
        browsers = make(vms)
        await browsers.open(ctx({}))
        await vms.reconcile()
        monkeypatch.setattr(Browsers, "_start_vm", start)
        await browsers.call("env", session(), "browser_snapshot", {})

    with pytest.raises(CallFailed) as caught:
        world.run(body)
    assert caught.value.error == "unavailable"
    assert caught.value.message.endswith(browser.CLOSED_TEXT)
    assert [vm["delete_reason"] for vm in vm_rows(world)].count("browser_closed") == 1
    assert all(vm["state"] == "deleted" for vm in vm_rows(world))


def test_the_idle_pass_sweeps_browser_vms_no_open_browser_refers_to(world: World) -> None:
    other = session(CHILD, task_id=TASK_B)
    world.settings = dataclasses.replace(world.settings, slots=3)

    async def body(vms: WorkVms) -> list[str]:
        browsers = make(vms)
        await browsers.open(ctx({}))  # referenced: kept
        orphan = await vms.obtain(other, scope="browser", size="standard")
        await vms.request(session(), {})  # not a browser VM: kept
        async with browsers._lock(CHILD):  # its session starts one now: kept
            assert await browsers.idle_pass() == []
            assert orphan.id not in [vm["id"] for vm in vm_rows(world) if vm["state"] == "deleted"]
        await browsers.idle_pass()
        return [orphan.id]

    [orphan] = world.run(body)
    found = {vm["id"]: (vm["scope"], vm["state"], vm["delete_reason"]) for vm in vm_rows(world)}
    assert found[orphan] == ("browser", "deleted", "failed")
    assert sorted(state for _scope, state, _reason in found.values()) == ["deleted", "running", "running"]
    assert f"runner.browser orphan_deleted workvm_id={orphan}" in world.logs


def test_a_teardown_rechecks_the_row_under_the_lock(world: World) -> None:
    async def body(vms: WorkVms) -> list[list[str]]:
        browsers = make(vms)
        await browsers.open(ctx({}))
        seen: list[int] = []

        def once(_row: Any) -> bool:
            seen.append(1)
            return len(seen) == 1  # chosen when listed, no longer under the lock

        def closing(_row: Any) -> bool:
            sql(
                world,
                "UPDATE runner.browser_session SET state = 'closed', closed_at = now(), close_reason = 'browser_idle'",
            )
            return True

        return [await browsers._close_all("browser_idle", once), await browsers._close_all("browser_idle", closing)]

    assert world.run(body) == [[], []]


def test_a_start_that_breaks_ends_the_countdown(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("broken")

    monkeypatch.setattr(Browsers, "_start_vm", broken)

    async def body(vms: WorkVms) -> None:
        await make(vms).open(ctx({}))

    with pytest.raises(RuntimeError):
        world.run(body)
    assert states(world) == [("starting", None), ("closed", "failed")]


def test_a_broken_action_still_gets_its_entry(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("broken")

    async def body(vms: WorkVms) -> None:
        browsers = make(vms)
        await browsers.call("env", session(), "browser_snapshot", {})
        monkeypatch.setattr(Browsers, "_run", broken)
        await browsers.call("env", session(), "browser_click", {"target": "e1"})

    with pytest.raises(RuntimeError):
        world.run(body)
    first, second = events(world, "browser.action")
    assert (first["seq"], second["seq"], second["ok"], second["error"]) == (1, 2, False, browser.BROKEN_ERROR)


def test_locks_of_closed_browsers_are_forgotten(world: World) -> None:
    async def body(vms: WorkVms) -> None:
        browsers = make(vms)
        await browsers.open(ctx({}))
        await browsers.close(ctx({}))
        assert SESSION not in browsers.locks
        browsers._prune(SESSION)  # nothing to forget
        lock = browsers._lock(SESSION)
        async with lock:
            browsers._prune(SESSION)  # held: kept
            assert browsers.locks[SESSION] is lock
        browsers._prune(SESSION)
        assert SESSION not in browsers.locks

    world.run(body)
