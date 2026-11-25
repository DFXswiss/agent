"""The work VM tools of 13-work-vm.md section 3 (KOR-29) and the platform browser tools of section 13.4 (KOR-44):
forwarding as journal record ``workvm.call`` (pending kind ``workvm``), the answer checked against the tool's output
schema, the guest-side wait (``wait_seconds`` + 5 s, at most 33 s), the ``timeout`` that points to ``workvm_status``
(``browser_snapshot`` for a browser tool), runner errors and an unreachable control process.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agent_guest.apisock import ApiSockError
from agent_guest.control import ControlContext
from agent_guest.mcp import ToolCall, ToolError, load_tools
from agent_guest.mcp_tools import _forward
from agent_guest.schema import SchemaSet
from agent_guest.state import Session
from tests.platform.guest.conftest import SESSION_ID
from tests.platform.guest.mcp.conftest import McpClient
from tests.platform.guest.mcp_tools.conftest import FakeRunner, contract_fixtures, forwarded

Runner = Callable[..., FakeRunner]

WORKVM_TOOLS = ("workvm_exec", "workvm_output", "workvm_kill", "workvm_status", "workvm_sync", "workvm_browse")
BROWSER_TOOLS = (
    "browser_navigate",
    "browser_snapshot",
    "browser_click",
    "browser_hover",
    "browser_type",
    "browser_select",
    "browser_press",
    "browser_scroll",
    "browser_wait",
    "browser_evaluate",
    "browser_upload",
    "browser_dialog",
    "browser_tabs",
    "browser_download",
    "browser_screenshot",
)
FORWARDED_TOOLS = (*WORKVM_TOOLS, *BROWSER_TOOLS)
BROWSER_TIMEOUT = {
    "error": "timeout",
    "message": "The platform browser did not answer in time; the action may still have happened. Look at the page "
    "with browser_snapshot before you repeat it.",
}
JOB_ID = "j-0123456789ab"
PENDING_ID = "01a1ee56-5e00-7028-8f1b-340691d99f28"
TIMEOUT = {
    "error": "timeout",
    "message": "The work VM did not answer in time; the command or sync may still be running. Check workvm_status, "
    "then continue with workvm_output for a running job.",
}
UNAVAILABLE = {"error": "unavailable", "message": "The platform answered with an invalid result."}


def ok(result: object) -> dict[str, Any]:
    return {"status": "ok", "result": result}


def tool_json(result: dict[str, Any]) -> Any:
    [item] = result["content"]
    return json.loads(item["text"])


def valid(contracts_dir: Path, tool: str, kind: str) -> list[tuple[str, Any]]:
    return contract_fixtures(contracts_dir, f"{tool}.{kind}", "valid")


class RecordingControl:
    """Records the pending calls and waits the tools ask for and answers ``reply`` (``None``: nothing in time)."""

    def __init__(self, reply: dict[str, Any] | None = None) -> None:
        self.reply = reply
        self.created: list[tuple[str, str, dict[str, Any]]] = []
        self.waits: list[float] = []

    def pending_create(self, session_id: str, kind: str, body: dict[str, Any]) -> str:
        self.created.append((session_id, kind, body))
        return PENDING_ID

    def pending_wait(self, pending_id: str, session_id: str, timeout: float) -> dict[str, Any] | None:
        assert (pending_id, session_id) == (PENDING_ID, SESSION_ID)
        self.waits.append(timeout)
        return self.reply


class DeadControl(RecordingControl):
    def pending_create(self, session_id: str, kind: str, body: dict[str, Any]) -> str:
        raise ApiSockError("unavailable")


class DeadWait(RecordingControl):
    def pending_wait(self, pending_id: str, session_id: str, timeout: float) -> dict[str, Any] | None:
        raise ApiSockError("unavailable")


def run(schemas: SchemaSet, control: RecordingControl, tool: str, arguments: dict[str, Any]) -> Any:
    call = ToolCall(tool, SESSION_ID, arguments, control, schemas)  # type: ignore[arg-type]
    return load_tools()[tool].handler(call)


def test_the_wait_limits_follow_the_specification() -> None:
    assert _forward.WORKVM_WAIT_SECONDS == 30
    assert _forward.WORKVM_MARGIN_SECONDS == 5.0
    assert _forward.MAX_WAIT_SECONDS == 33.0


def test_every_work_vm_tool_is_registered_with_a_description() -> None:
    tools = load_tools()
    for name in WORKVM_TOOLS:
        assert tools[name].handler is _forward.workvm
        assert len(tools[name].description) >= 150, name
    assert "workvm.request" in tools["workvm_exec"].description
    assert "workvm_output" in tools["workvm_exec"].description
    assert "from_vm" in tools["workvm_sync"].description
    assert "automatically" in tools["workvm_exec"].description
    assert "kept and visible to the user" in tools["workvm_exec"].description
    assert "automatically" in tools["workvm_browse"].description
    assert ".workvm/screenshots/<job_id>.png" in tools["workvm_browse"].description
    assert "Read" in tools["workvm_browse"].description
    assert "Playwright script" in tools["workvm_browse"].description
    assert "workvm_exec" in tools["workvm_browse"].description
    assert "neither recorded nor watched" in tools["workvm_browse"].description
    assert "browser_*" in tools["workvm_browse"].description
    assert "request one" not in tools["workvm_status"].description


@pytest.mark.parametrize(("wait_seconds", "expected"), [(None, 33.0), (0, 5.0), (10, 15.0), (28, 33.0), (30, 33.0)])
@pytest.mark.parametrize(
    ("tool", "arguments"), [("workvm_exec", {"command": "make"}), ("workvm_output", {"job_id": JOB_ID})]
)
def test_job_tools_wait_for_wait_seconds_plus_the_margin(
    schemas: SchemaSet, tool: str, arguments: dict[str, Any], wait_seconds: int | None, expected: float
) -> None:
    control = RecordingControl()
    if wait_seconds is not None:
        arguments = {**arguments, "wait_seconds": wait_seconds}
    with pytest.raises(ToolError) as caught:
        run(schemas, control, tool, arguments)
    assert {"error": caught.value.code, "message": caught.value.message} == TIMEOUT
    assert control.waits == [expected]
    assert control.created == [(SESSION_ID, "workvm", {"tool": tool, "arguments": arguments})]


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("workvm_kill", {"job_id": JOB_ID, "signal": "KILL"}),
        ("workvm_status", {}),
        ("workvm_sync", {"direction": "from_vm", "path": "out"}),
        ("workvm_browse", {"url": "https://example.com/", "action": "screenshot"}),
    ],
)
def test_the_other_tools_wait_the_route_limit_and_time_out_with_a_hint(
    schemas: SchemaSet, tool: str, arguments: dict[str, Any]
) -> None:
    control = RecordingControl()
    with pytest.raises(_forward.WorkvmNoAnswer) as caught:
        run(schemas, control, tool, arguments)
    assert caught.value.code == "timeout"
    assert "workvm_status" in caught.value.message
    assert control.waits == [_forward.MAX_WAIT_SECONDS]
    assert control.created == [(SESSION_ID, "workvm", {"tool": tool, "arguments": arguments})]


def test_every_browser_tool_is_forwarded_with_the_shared_answer_text() -> None:
    tools = load_tools()
    assert sorted(name for name in tools if name.startswith("browser_")) == sorted(BROWSER_TOOLS)
    for name in BROWSER_TOOLS:
        description = tools[name].description
        assert tools[name].handler is _forward.workvm
        assert description.endswith(_forward.BROWSER_ANSWER), name
        assert len(description) > len(_forward.BROWSER_ANSWER) + 60, name
    shared = _forward.BROWSER_ANSWER
    for text in ("[ref=e12]", "f1e3", "Playwright selector", "snapshot false", "ok false", "browser.close"):
        assert text in shared, text
    assert "recorded and watched live" in shared
    navigate = tools["browser_navigate"].description
    for text in ("human.question", "workvm_browse", "browser.open", "cannot reach your work VM", "like a person"):
        assert text in navigate, text
    assert "masked" in tools["browser_type"].description
    assert ".browser/downloads/<file name>" in tools["browser_download"].description
    assert ".browser/screenshots/<browser_id>-<seq>.png" in tools["browser_screenshot"].description


@pytest.mark.parametrize("tool", BROWSER_TOOLS)
def test_an_unanswered_browser_call_times_out_with_the_snapshot_hint(
    schemas: SchemaSet, contracts_dir: Path, tool: str
) -> None:
    arguments = valid(contracts_dir, tool, "input")[0][1]
    control = RecordingControl()
    with pytest.raises(_forward.BrowserNoAnswer) as caught:
        run(schemas, control, tool, arguments)
    assert {"error": caught.value.code, "message": caught.value.message} == BROWSER_TIMEOUT
    assert control.waits == [_forward.MAX_WAIT_SECONDS]
    assert control.created == [(SESSION_ID, "workvm", {"tool": tool, "arguments": arguments})]


@pytest.mark.parametrize("tool", FORWARDED_TOOLS)
def test_every_valid_output_fixture_is_returned(schemas: SchemaSet, contracts_dir: Path, tool: str) -> None:
    arguments = valid(contracts_dir, tool, "input")[0][1]
    outputs = valid(contracts_dir, tool, "output")
    assert outputs
    for stem, document in outputs:
        assert run(schemas, RecordingControl(ok(copy.deepcopy(document))), tool, arguments) == document, stem


@pytest.mark.parametrize("tool", FORWARDED_TOOLS)
def test_an_invalid_or_foreign_result_is_unavailable(schemas: SchemaSet, contracts_dir: Path, tool: str) -> None:
    arguments = valid(contracts_dir, tool, "input")[0][1]
    other = "workvm_status" if tool != "workvm_status" else "workvm_sync"
    documents = [doc for _, doc in contract_fixtures(contracts_dir, f"{tool}.output", "invalid")]
    documents.append(valid(contracts_dir, other, "output")[0][1])
    for document in documents:
        with pytest.raises(ToolError) as caught:
            run(schemas, RecordingControl(ok(document)), tool, arguments)
        assert {"error": caught.value.code, "message": caught.value.message} == UNAVAILABLE


@pytest.mark.parametrize("code", ["not_found", "unavailable", "params_invalid", "timeout"])
def test_runner_errors_are_passed_on(schemas: SchemaSet, code: str) -> None:
    reply = {"status": "error", "error": {"error": code, "message": "No work VM: request one with workvm.request."}}
    with pytest.raises(ToolError) as caught:
        run(schemas, RecordingControl(reply), "workvm_exec", {"command": "true"})
    assert (caught.value.code, caught.value.message) == (code, "No work VM: request one with workvm.request.")


@pytest.mark.parametrize("control", [DeadControl(), DeadWait()])
@pytest.mark.parametrize("tool", ["workvm_exec", "workvm_status"])
def test_an_unreachable_control_process_is_unavailable(
    schemas: SchemaSet, control: RecordingControl, tool: str
) -> None:
    arguments = {"command": "true"} if tool == "workvm_exec" else {}
    with pytest.raises(ToolError) as caught:
        run(schemas, control, tool, arguments)
    assert (caught.value.code, caught.value.message) == (
        "unavailable",
        "The platform is not reachable right now. Try again later.",
    )


def test_unstorable_arguments_are_refused_before_forwarding(schemas: SchemaSet) -> None:
    control = RecordingControl()
    with pytest.raises(ToolError) as caught:
        run(schemas, control, "workvm_exec", {"command": "echo \x00"})
    assert caught.value.code == "params_invalid"
    assert control.created == []


def test_valid_calls_are_journaled_as_workvm_call_and_answered(
    *,
    mcp: McpClient,
    session: Session,
    context: ControlContext,
    contracts_dir: Path,
    schemas: SchemaSet,
    fake_runner: Runner,
) -> None:
    def answer(kind: str, body: dict[str, Any], session_id: str) -> dict[str, Any]:
        assert (kind, session_id) == ("workvm.call", SESSION_ID)
        return ok(valid(contracts_dir, body["tool"], "output")[0][1])

    runner = fake_runner(answer)
    cases = [(tool, document) for tool in FORWARDED_TOOLS for _, document in valid(contracts_dir, tool, "input")]
    for tool, document in cases:
        result = mcp.tool(tool, document)
        assert result["isError"] is False, (tool, result)
        assert tool_json(result) == valid(contracts_dir, tool, "output")[0][1]
    records = forwarded(context)
    assert [(record["body"]["tool"], record["body"]["arguments"]) for record in records] == cases
    for record in records:
        assert (record["kind"], record["session_id"]) == ("workvm.call", SESSION_ID)
        assert schemas.errors("guest/journal/workvm.call.json", record["body"]) == []
    assert len(runner.seen) == len(records)
    # not reads: no platform.read and no platform.query records
    kinds = {record["kind"] for record in context.journal.read(0, 1000)[0]}
    assert kinds.isdisjoint({"platform.read", "platform.query"})


def test_an_unanswered_call_times_out_with_the_status_hint(
    mcp: McpClient, session: Session, fake_runner: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_forward, "WORKVM_MARGIN_SECONDS", 0.3)
    fake_runner(lambda kind, body, session_id: None)
    result = mcp.tool("workvm_exec", {"command": "sleep 100", "wait_seconds": 0})
    assert (result["isError"], tool_json(result)) == (True, TIMEOUT)
