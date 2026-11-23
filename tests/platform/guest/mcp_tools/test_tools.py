"""The platform tools of 03-api.md §8.1 (GST-07): contract fixtures (valid and invalid arguments, valid and invalid
results), forwarding as ``platform.query``/``operation.request`` and the answer, time limits, foreign session tokens,
journal records, the local check of ``request_operation`` (05-runner.md §12 step 1) and the child check of
``get_subagent_result``.
"""

from __future__ import annotations

import copy
import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_guest import policy
from agent_guest.apisock import ApiClient, ApiSockError
from agent_guest.control import ControlContext
from agent_guest.mcp import Tool, ToolCall, ToolError, load_tools
from agent_guest.mcp_tools import _forward, request_operation
from agent_guest.sandbox import settings
from agent_guest.schema import SchemaSet
from agent_guest.state import Session
from tests.platform.guest.conftest import SESSION_ID, SESSION_TOKEN, ControlClient
from tests.platform.guest.mcp.conftest import McpClient
from tests.platform.guest.mcp_tools.conftest import (
    OTHER_SESSION_ID,
    OTHER_SESSION_TOKEN,
    FakeRunner,
    contract_fixtures,
    forwarded,
)

Runner = Callable[..., FakeRunner]

READ_TOOLS = (
    "list_workgroups",
    "list_members",
    "list_tasks",
    "get_task",
    "get_task_messages",
    "get_whiteboard",
    "list_operations",
)
WAITING_TOOLS = ("get_operation", "get_subagent_result")
QUERY_TOOLS = READ_TOOLS + WAITING_TOOLS
WORKVM_TOOLS = ("workvm_exec", "workvm_output", "workvm_kill", "workvm_status", "workvm_sync", "workvm_browse")
PACKAGE_TOOLS = (*QUERY_TOOLS, "request_operation", *WORKVM_TOOLS)
IDENTITY = {"get_task": ("task_id", "id"), "get_operation": ("operation_id",), "get_subagent_result": ("session_id",)}
CHILD_ID = "01a1ee56-5e00-7028-8f1b-340691d99f28"
TASK_ID = "01a1ee56-5e00-7003-8f1b-340691d99f12"
UUID7 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
COMMENT = {"repo": "example-org/example-repo", "number": 12, "body": "LGTM"}
COMMENT_RESULT = {
    "comment_id": 1,
    "url": "https://github.com/example-org/example-repo/pull/12#issuecomment-1",
}


def ok(result: object) -> dict[str, Any]:
    return {"status": "ok", "result": result}


def failure(code: str, message: str = "Not here.") -> dict[str, Any]:
    return {"status": "error", "error": {"error": code, "message": message}}


def tool_json(result: dict[str, Any]) -> Any:
    [item] = result["content"]
    assert item["type"] == "text"
    return json.loads(item["text"])


def first_output(contracts_dir: Path, tool: str) -> Any:
    return copy.deepcopy(contract_fixtures(contracts_dir, f"{tool}.output", "valid")[0][1])


def answer_for(contracts_dir: Path, tool: str, arguments: dict[str, Any]) -> Any:
    """A valid result of ``tool`` that names the object of ``arguments`` (identity fields of IDENTITY)."""
    result = first_output(contracts_dir, tool)
    if tool in IDENTITY:
        argument, *field = IDENTITY[tool]
        result[field[0] if field else argument] = arguments[argument]
    return result


def wait_for(condition: Callable[[], bool], limit: float = 5.0) -> None:
    deadline = time.monotonic() + limit
    while not condition():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.01)


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    """Short guest-side waits so that unanswered calls end quickly."""
    monkeypatch.setattr(_forward, "READ_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(request_operation, "WAIT_SECONDS", 0.3)


# ---------------------------------------------------------------------------------------------- registry


def test_registry_holds_every_tool_of_the_contract(schemas: SchemaSet) -> None:
    tools = load_tools()
    assert set(PACKAGE_TOOLS) <= set(tools)
    assert set(tools) | {"get_notifications"} == set(settings.MCP_TOOLS) == policy.PLATFORM_TOOLS
    for name in PACKAGE_TOOLS:
        tool = tools[name]
        assert isinstance(tool, Tool)
        assert tool.description.endswith(".")
        assert schemas.load(tool.input_schema)["$id"].endswith(f"/mcp/tools/{name}.input.json")
        assert schemas.load(tool.output_schema)["$id"].endswith(f"/mcp/tools/{name}.output.json")


def test_tools_list_names_the_managed_settings_tools(mcp: McpClient, session: Session, schemas: SchemaSet) -> None:
    listed = mcp.call("tools/list").json()["result"]["tools"]
    allowed = {entry.removeprefix("mcp__platform__") for entry in settings.build_settings()["permissions"]["allow"]}
    assert {tool["name"] for tool in listed} <= allowed
    for tool in listed:
        assert schemas.errors("mcp/tools-list.result.json#/properties/tools/items", tool) == []
        assert tool["inputSchema"]["additionalProperties"] is False or "oneOf" in tool["inputSchema"]


# ---------------------------------------------------------------------------------------------- arguments


def invalid_cases(contracts_dir: Path) -> list[tuple[str, str, Any]]:
    root = contracts_dir
    return [
        (tool, stem, document)
        for tool in PACKAGE_TOOLS
        for stem, document in contract_fixtures(root, f"{tool}.input", "invalid")
    ]


def valid_cases(contracts_dir: Path) -> list[tuple[str, str, Any]]:
    return [
        (tool, stem, document)
        for tool in QUERY_TOOLS
        for stem, document in contract_fixtures(contracts_dir, f"{tool}.input", "valid")
    ]


def test_every_tool_has_contract_fixtures(contracts_dir: Path) -> None:
    for tool in PACKAGE_TOOLS:
        for kind in ("input", "output"):
            for validity in ("valid", "invalid"):
                assert contract_fixtures(contracts_dir, f"{tool}.{kind}", validity), (tool, kind, validity)


def test_invalid_arguments_are_params_invalid_and_not_forwarded(
    mcp: McpClient, session: Session, context: ControlContext, contracts_dir: Path, fake_runner: Runner
) -> None:
    runner = fake_runner(lambda kind, body, session_id: None)
    cases = invalid_cases(contracts_dir)
    extra = [
        ("list_tasks", "limit-zero", {"limit": 0}),
        ("get_task_messages", "after-bool", {"task_id": TASK_ID, "after": False}),
        ("get_operation", "wait-31", {"operation_id": CHILD_ID, "wait_seconds": 31}),
        ("get_subagent_result", "session-in-arguments", {"session_id": CHILD_ID, "caller": SESSION_ID}),
        ("request_operation", "params-array", {"type": "task.set_title", "params": []}),
        ("request_operation", "no-params", {"type": "task.set_title"}),
        ("get_whiteboard", "org-with-id", {"level": "org", "task_id": TASK_ID}),
        ("get_whiteboard", "workgroup-with-id", {"level": "workgroup", "task_id": TASK_ID}),
        ("get_whiteboard", "task-without-id", {"level": "task"}),
    ]
    assert len(cases) >= len(PACKAGE_TOOLS)
    for tool, stem, document in cases + extra:
        result = mcp.tool(tool, document)
        assert result["isError"] is True, (tool, stem)
        assert tool_json(result) == {"error": "params_invalid", "message": "The arguments do not match the tool."}
    time.sleep(0.1)
    assert forwarded(context) == []
    assert runner.seen == []


def test_valid_arguments_are_forwarded_and_answered(
    *,
    mcp: McpClient,
    session: Session,
    context: ControlContext,
    contracts_dir: Path,
    schemas: SchemaSet,
    fake_runner: Runner,
) -> None:
    def answer(kind: str, body: dict[str, Any], session_id: str) -> dict[str, Any]:
        assert (kind, session_id) == ("platform.query", SESSION_ID)
        return ok(answer_for(contracts_dir, body["tool"], body["arguments"]))

    runner = fake_runner(answer)
    cases = valid_cases(contracts_dir)
    for tool, stem, document in cases:
        if tool == "get_subagent_result":
            assert document["session_id"] != SESSION_ID
        result = mcp.tool(tool, document)
        assert result["isError"] is False, (tool, stem, result)
        value = tool_json(result)
        assert value == answer_for(contracts_dir, tool, document)
        assert schemas.errors(f"mcp/tools/{tool}.output.json", value) == []
    records = forwarded(context)
    assert [(record["body"]["tool"], record["body"]["arguments"]) for record in records] == [
        (tool, document) for tool, _, document in cases
    ]
    for record in records:
        assert record["kind"] == "platform.query"
        assert record["session_id"] == SESSION_ID
        assert schemas.errors("guest/journal/platform.query.json", record["body"]) == []
        assert UUID7.match(record["body"]["pending_id"])
    assert len({record["body"]["pending_id"] for record in records}) == len(records)
    assert len(runner.seen) == len(records)


def test_every_valid_output_fixture_passes_and_every_invalid_one_is_refused(
    mcp: McpClient, session: Session, contracts_dir: Path, schemas: SchemaSet, fake_runner: Runner
) -> None:
    """Valid results reach the session unchanged. An invalid result is refused by the answer route when it is no
    tool's result (``tool_result``); one that is another tool's result is ``unavailable`` for the asking tool."""
    replies: list[Any] = []
    fake_runner(lambda kind, body, session_id: ok(replies.pop(0)))
    other = first_output(contracts_dir, "list_workgroups")
    unavailable = {"error": "unavailable", "message": "The platform answered with an invalid result."}
    for tool in QUERY_TOOLS:
        arguments = contract_fixtures(contracts_dir, f"{tool}.input", "valid")[0][1]
        for stem, document in contract_fixtures(contracts_dir, f"{tool}.output", "valid"):
            argument, *field = IDENTITY.get(tool, ("",))
            if argument:
                arguments = {**arguments, argument: document[field[0] if field else argument]}
            replies.append(document)
            result = mcp.tool(tool, arguments)
            assert (result["isError"], tool_json(result)) == (False, document), (tool, stem)
        for stem, document in contract_fixtures(contracts_dir, f"{tool}.output", "invalid"):
            assert schemas.errors(f"mcp/tools/{tool}.output.json", document), (tool, stem)
            if schemas.is_valid("mcp/objects.json#/$defs/tool_result", document):
                replies.append(document)
                result = mcp.tool(tool, arguments)
                assert (result["isError"], tool_json(result)) == (True, unavailable), (tool, stem)
            else:
                assert schemas.errors("guest/pending-answer.request.json", ok(document)), (tool, stem)
        replies.append(other if tool != "list_workgroups" else first_output(contracts_dir, "list_tasks"))
        result = mcp.tool(tool, arguments)
        assert (result["isError"], tool_json(result)) == (True, unavailable), tool


def test_the_answer_route_refuses_a_result_of_no_tool(
    session: Session, context: ControlContext, control_client: ControlClient, api_client: ApiClient
) -> None:
    pending_id = api_client.pending_create(SESSION_ID, "query", {"tool": "list_tasks", "arguments": {}})
    reply = control_client.request("POST", f"/v1/pending/{pending_id}/answer", ok({"tasks": "none"}))
    assert reply.status == 400
    assert api_client.pending_wait(pending_id, SESSION_ID, 0) is None


@pytest.mark.parametrize(
    ("tool", "arguments", "key"),
    [
        ("get_task", {"task_id": TASK_ID}, "id"),
        ("get_operation", {"operation_id": CHILD_ID}, "operation_id"),
        ("get_subagent_result", {"session_id": CHILD_ID}, "session_id"),
    ],
)
def test_an_answer_about_another_object_is_unavailable(
    *,
    mcp: McpClient,
    session: Session,
    contracts_dir: Path,
    fake_runner: Runner,
    tool: str,
    arguments: dict[str, Any],
    key: str,
) -> None:
    def answer(kind: str, body: dict[str, Any], session_id: str) -> dict[str, Any]:
        result = answer_for(contracts_dir, tool, arguments)
        result[key] = "01a1ee56-5e00-7003-8f1b-340691d99fff"
        return ok(result)

    fake_runner(answer)
    result = mcp.tool(tool, arguments)
    assert result["isError"] is True
    assert tool_json(result)["error"] == "unavailable"


@pytest.mark.parametrize("code", ["not_found", "forbidden", "unavailable", "timeout", "params_invalid"])
def test_runner_errors_are_passed_on(mcp: McpClient, session: Session, fake_runner: Runner, code: str) -> None:
    fake_runner(lambda kind, body, session_id: failure(code, "The runner says no."))
    for tool, arguments in [("list_members", {"scope": "org"}), ("get_operation", {"operation_id": CHILD_ID})]:
        result = mcp.tool(tool, arguments)
        assert (result["isError"], tool_json(result)) == (True, {"error": code, "message": "The runner says no."})


# ---------------------------------------------------------------------------------------------- time limits


def test_unanswered_reads_time_out_after_the_handler_limit(
    mcp: McpClient, session: Session, context: ControlContext, fake_runner: Runner, quick: None
) -> None:
    fake_runner(lambda kind, body, session_id: None)
    for tool in (*READ_TOOLS, "get_subagent_result", "get_operation"):
        arguments = {
            "list_members": {"scope": "workgroup"},
            "get_task": {"task_id": TASK_ID},
            "get_task_messages": {"task_id": TASK_ID},
            "get_whiteboard": {"level": "org"},
            "get_operation": {"operation_id": CHILD_ID},
            "get_subagent_result": {"session_id": CHILD_ID},
        }.get(tool, {})
        started = time.monotonic()
        result = mcp.tool(tool, arguments)
        assert 0.25 <= time.monotonic() - started < 5
        if tool == "get_operation":
            assert (result["isError"], tool_json(result)) == (False, {"operation_id": CHILD_ID, "status": "pending"})
            continue
        assert (result["isError"], tool_json(result)) == (
            True,
            {"error": "timeout", "message": "The platform did not answer in time. Try again later."},
        )
    assert len(forwarded(context)) == len(READ_TOOLS + WAITING_TOOLS)


def test_read_limits_follow_the_specification() -> None:
    assert _forward.READ_WAIT_SECONDS == 10.0
    assert request_operation.WAIT_SECONDS == 20.0
    assert _forward.MAX_WAIT_SECONDS == 33.0


class RecordingControl:
    """Records the waits the tools ask the control process for and answers nothing."""

    def __init__(self) -> None:
        self.waits: list[float] = []
        self.created: list[tuple[str, str, dict[str, Any]]] = []

    def pending_create(self, session_id: str, kind: str, body: dict[str, Any]) -> str:
        self.created.append((session_id, kind, body))
        return CHILD_ID

    def pending_wait(self, pending_id: str, session_id: str, timeout: float) -> dict[str, Any] | None:
        self.waits.append(timeout)
        return None


@pytest.mark.parametrize(("wait_seconds", "expected"), [(None, 10.0), (0, 10.0), (5, 15.0), (23, 33.0), (30, 33.0)])
@pytest.mark.parametrize("tool", WAITING_TOOLS)
def test_waiting_reads_wait_for_the_runner_up_to_the_route_limit(
    schemas: SchemaSet, tool: str, wait_seconds: int | None, expected: float
) -> None:
    control = RecordingControl()
    key = "operation_id" if tool == "get_operation" else "session_id"
    arguments: dict[str, Any] = {key: CHILD_ID}
    if wait_seconds is not None:
        arguments["wait_seconds"] = wait_seconds
    call = ToolCall(tool, SESSION_ID, arguments, control, schemas)  # type: ignore[arg-type]
    if tool == "get_operation":
        assert load_tools()[tool].handler(call) == {"operation_id": CHILD_ID, "status": "pending"}
    else:
        with pytest.raises(ToolError) as caught:
            load_tools()[tool].handler(call)
        assert caught.value.code == "timeout"
    assert control.waits == [expected]
    assert control.created == [(SESSION_ID, "query", {"tool": tool, "arguments": arguments})]


def test_reads_wait_ten_seconds(schemas: SchemaSet) -> None:
    for tool in READ_TOOLS:
        control = RecordingControl()
        arguments = {"get_task": {"task_id": TASK_ID}, "get_whiteboard": {"level": "org"}}.get(tool, {})
        with pytest.raises(ToolError):
            load_tools()[tool].handler(ToolCall(tool, SESSION_ID, arguments, control, schemas))  # type: ignore[arg-type]
        assert control.waits == [10.0], tool


def test_waiting_read_answered_late_by_the_runner(
    mcp: McpClient, session: Session, contracts_dir: Path, fake_runner: Runner, quick: None
) -> None:
    result_value = answer_for(contracts_dir, "get_operation", {"operation_id": CHILD_ID})

    def answer(kind: str, body: dict[str, Any], session_id: str) -> dict[str, Any]:
        time.sleep(0.6)
        return ok(result_value)

    fake_runner(answer)
    # an answer after 0.6 s inside a window of 10 s (a window of 1 s left a loaded device 0.4 s)
    result = mcp.tool("get_operation", {"operation_id": CHILD_ID, "wait_seconds": 10})
    assert (result["isError"], tool_json(result)) == (False, result_value)


# ---------------------------------------------------------------------------------------------- sessions


def test_foreign_or_missing_session_tokens_are_refused_before_anything_is_journaled(
    mcp: McpClient, session: Session, context: ControlContext
) -> None:
    params = {"name": "request_operation", "arguments": {"type": "github.comment", "params": COMMENT}}
    for token in (None, "Zm9yZWlnbi10b2tlbi1vZi1hbm90aGVyLXZtLTAwMDAw", SESSION_TOKEN + "x", OTHER_SESSION_TOKEN):
        reply = mcp.call("tools/call", params, token=token)
        assert (reply.status, reply.json()) == (401, {"error": "unauthorized"}), token
    assert forwarded(context) == []


def test_a_stopped_session_token_is_refused(mcp: McpClient, context: ControlContext, session: Session) -> None:
    context.state.save_session(replace(session, state="stopped"))
    reply = mcp.call("tools/call", {"name": "list_workgroups", "arguments": {}})
    assert reply.status == 401
    assert forwarded(context) == []


def test_the_session_comes_from_the_token_only(
    *,
    mcp: McpClient,
    session: Session,
    other_session: Session,
    context: ControlContext,
    contracts_dir: Path,
    fake_runner: Runner,
) -> None:
    def answer(kind: str, body: dict[str, Any], session_id: str) -> dict[str, Any]:
        if kind == "operation.request":
            return ok({"operation_id": body["operation_id"], "status": "succeeded", "result": COMMENT_RESULT})
        if body["tool"] == "get_operation" and session_id != SESSION_ID:
            return failure("not_found", "The operation is not known.")
        return ok(answer_for(contracts_dir, body["tool"], body["arguments"]))

    fake_runner(answer)
    created = tool_json(mcp.tool("request_operation", {"type": "github.comment", "params": COMMENT}))
    operation = {"operation_id": created["operation_id"]}
    assert created["status"] == "succeeded"
    assert tool_json(mcp.tool("get_operation", operation))["operation_id"] == created["operation_id"]
    other = mcp.call("tools/call", {"name": "get_operation", "arguments": operation}, token=OTHER_SESSION_TOKEN)
    assert tool_json(other.json()["result"]) == {"error": "not_found", "message": "The operation is not known."}
    child = mcp.call(
        "tools/call",
        {"name": "get_subagent_result", "arguments": {"session_id": SESSION_ID}},
        token=OTHER_SESSION_TOKEN,
    )
    assert tool_json(child.json()["result"])["session_id"] == SESSION_ID
    assert [(record["kind"], record["session_id"]) for record in forwarded(context)] == [
        ("operation.request", SESSION_ID),
        ("platform.query", SESSION_ID),
        ("platform.query", OTHER_SESSION_ID),
        ("platform.query", OTHER_SESSION_ID),
    ]


def test_get_subagent_result_refuses_the_own_session(
    mcp: McpClient, session: Session, context: ControlContext, fake_runner: Runner
) -> None:
    runner = fake_runner(lambda kind, body, session_id: None)
    for wait in ({}, {"wait_seconds": 30}):
        result = mcp.tool("get_subagent_result", {"session_id": SESSION_ID, **wait})
        assert (result["isError"], tool_json(result)) == (
            True,
            {"error": "not_found", "message": "Only child sessions of this session can be read."},
        )
    time.sleep(0.1)
    assert forwarded(context) == []
    assert runner.seen == []
    # a refused read is journaled like every call (KOR-10, AUD-02 NB-08); the runner audits it as rejected
    records = context.journal.read(0, 100)[0]
    refused = [(r["session_id"], r["body"]) for r in records if r["kind"] == "platform.refused"]
    assert refused == [(SESSION_ID, {"code": "not_found", "tool": "get_subagent_result"})] * 2


def test_get_subagent_result_child_check_is_the_runners(
    mcp: McpClient, session: Session, context: ControlContext, contracts_dir: Path, fake_runner: Runner
) -> None:
    children = {CHILD_ID}

    def answer(kind: str, body: dict[str, Any], session_id: str) -> dict[str, Any]:
        child = body["arguments"]["session_id"]
        if session_id != SESSION_ID or child not in children:
            return failure("not_found", "The session is not a child of this session.")
        return ok({"session_id": child, "state": "working"})

    fake_runner(answer)
    assert tool_json(mcp.tool("get_subagent_result", {"session_id": CHILD_ID})) == {
        "session_id": CHILD_ID,
        "state": "working",
    }
    stranger = mcp.tool("get_subagent_result", {"session_id": OTHER_SESSION_ID, "wait_seconds": 0})
    assert tool_json(stranger) == {"error": "not_found", "message": "The session is not a child of this session."}
    bodies = [record["body"] for record in forwarded(context)]
    assert [body["arguments"] for body in bodies] == [
        {"session_id": CHILD_ID},
        {"session_id": OTHER_SESSION_ID, "wait_seconds": 0},
    ]


# ---------------------------------------------------------------------------------------------- request_operation


def test_request_operation_forwards_and_returns_the_final_state(
    mcp: McpClient, session: Session, context: ControlContext, schemas: SchemaSet, fake_runner: Runner
) -> None:
    def answer(kind: str, body: dict[str, Any], session_id: str) -> dict[str, Any]:
        assert kind == "operation.request"
        return ok({"operation_id": body["operation_id"], "status": "succeeded", "result": COMMENT_RESULT})

    fake_runner(answer)
    before = time.time()
    result = mcp.tool("request_operation", {"type": "github.comment", "params": COMMENT, "idempotency_key": "r-12"})
    value = tool_json(result)
    assert result["isError"] is False
    assert schemas.errors("mcp/tools/request_operation.output.json", value) == []
    [record] = forwarded(context)
    body = record["body"]
    assert (record["kind"], record["session_id"]) == ("operation.request", SESSION_ID)
    assert schemas.errors("guest/journal/operation.request.json", body) == []
    assert value == {"operation_id": body["operation_id"], "status": "succeeded", "result": COMMENT_RESULT}
    assert (body["type"], body["params"], body["idempotency_key"]) == ("github.comment", COMMENT, "r-12")
    assert body["local_check"] == "passed"
    assert "local_error" not in body
    assert UUID7.match(body["operation_id"])
    assert UUID7.match(body["pending_id"])
    assert body["operation_id"] != body["pending_id"]
    millis = uuid.UUID(body["operation_id"]).int >> 80
    assert before * 1000 - 1 <= millis <= time.time() * 1000
    assert body["requested_at"].endswith("Z")


@pytest.mark.parametrize(
    "reply",
    [
        {"status": "failed", "error": {"code": "upstream_error", "retryable": True}},
        {"status": "denied", "error": {"code": "rights_denied"}},
        {"status": "rejected", "error": {"code": "repo_not_allowed", "message": "The repository is not allowed."}},
        {"status": "duplicate", "duplicate_of": CHILD_ID, "result": COMMENT_RESULT},
        {"status": "pending", "duplicate_of": CHILD_ID},
    ],
)
def test_request_operation_returns_every_outcome_of_the_runner(
    mcp: McpClient, session: Session, schemas: SchemaSet, fake_runner: Runner, reply: dict[str, Any]
) -> None:
    fake_runner(lambda kind, body, session_id: ok({"operation_id": body["operation_id"], **reply}))
    value = tool_json(mcp.tool("request_operation", {"type": "github.comment", "params": COMMENT}))
    assert value == {"operation_id": value["operation_id"], **reply}
    assert schemas.errors("mcp/tools/request_operation.output.json", value) == []


def test_request_operation_without_an_answer_is_pending(
    mcp: McpClient, session: Session, context: ControlContext, fake_runner: Runner, quick: None
) -> None:
    fake_runner(lambda kind, body, session_id: None)
    started = time.monotonic()
    value = tool_json(mcp.tool("request_operation", {"type": "task.set_title", "params": {"title": "Notes"}}))
    assert 0.25 <= time.monotonic() - started < 5
    [record] = forwarded(context)
    assert value == {"operation_id": record["body"]["operation_id"], "status": "pending"}
    assert "idempotency_key" not in record["body"]


def test_request_operation_answer_for_another_operation_is_unavailable(
    mcp: McpClient, session: Session, fake_runner: Runner
) -> None:
    fake_runner(lambda kind, body, session_id: ok({"operation_id": CHILD_ID, "status": "pending"}))
    result = mcp.tool("request_operation", {"type": "github.comment", "params": COMMENT})
    assert (result["isError"], tool_json(result)["error"]) == (True, "unavailable")


def test_request_operation_invalid_result_and_runner_error(
    mcp: McpClient, session: Session, fake_runner: Runner
) -> None:
    replies = [ok({"workgroups": []}), failure("forbidden", "The task owner may not write.")]
    fake_runner(lambda kind, body, session_id: replies.pop(0))
    first = tool_json(mcp.tool("request_operation", {"type": "github.comment", "params": COMMENT}))
    assert first == {"error": "unavailable", "message": "The platform answered with an invalid result."}
    second = tool_json(mcp.tool("request_operation", {"type": "github.comment", "params": COMMENT}))
    assert second == {"error": "forbidden", "message": "The task owner may not write."}


@pytest.mark.parametrize(
    ("kind", "params", "error"),
    [
        ("github.delete_repository", {}, {"code": "unknown_type", "message": "The operation type is not known."}),
        ("Shell", {"cmd": "id"}, {"code": "unknown_type", "message": "The operation type is not known."}),
        ("../guest/assign.request", {}, {"code": "unknown_type", "message": "The operation type is not known."}),
        (
            "github.comment",
            {"repo": "example-org/example-repo", "number": True, "body": "x"},
            {
                "code": "params_invalid",
                "message": "The parameters do not match the operation type.",
                "pointers": ["/params/number"],
            },
        ),
        (
            "github.comment",
            {**COMMENT, "zz_unexpected": 1},
            {
                "code": "params_invalid",
                "message": "The parameters do not match the operation type.",
                "pointers": ["/params/zz_unexpected"],
            },
        ),
        (
            "task.set_title",
            {},
            {
                "code": "params_invalid",
                "message": "The parameters do not match the operation type.",
                "pointers": ["/params/title"],
            },
        ),
    ],
)
def test_request_operation_local_check_forwards_and_rejects(
    *,
    mcp: McpClient,
    session: Session,
    context: ControlContext,
    schemas: SchemaSet,
    fake_runner: Runner,
    kind: str,
    params: dict[str, Any],
    error: dict[str, Any],
) -> None:
    runner = fake_runner(lambda kind, body, session_id: None)
    started = time.monotonic()
    result = mcp.tool("request_operation", {"type": kind, "params": params, "idempotency_key": "k"})
    # far below the 20 s wait for the runner, which never answers here (2 s left a loaded device little room)
    assert time.monotonic() - started < 10
    value = tool_json(result)
    assert result["isError"] is False
    assert schemas.errors("mcp/tools/request_operation.output.json", value) == []
    [record] = forwarded(context)
    assert value == {"operation_id": record["body"]["operation_id"], "status": "rejected", "error": error}
    assert record["kind"] == "operation.request"
    assert (record["body"]["type"], record["body"]["params"], record["body"]["idempotency_key"]) == (kind, params, "k")
    assert (record["body"]["local_check"], record["body"]["local_error"]) == ("rejected", error)
    assert schemas.errors("guest/journal/operation.request.json", record["body"]) == []
    wait_for(lambda: len(runner.seen) == 1)


def test_local_check_drops_overlong_pointers(schemas: SchemaSet) -> None:
    long_key = "k" * 300
    error = request_operation.local_check(schemas, "github.comment", {**COMMENT, long_key: 1})
    assert error == {"code": "params_invalid", "message": "The parameters do not match the operation type."}
    error = request_operation.local_check(
        schemas, "github.comment", {"repo": "example-org/example-repo", "number": "1", "body": 2}
    )
    assert error is not None
    assert error["pointers"] == ["/params/number", "/params/body"]
    assert request_operation.local_check(schemas, "github.comment", COMMENT) is None


def test_every_operation_type_of_the_index_is_known(schemas: SchemaSet, contracts_dir: Path) -> None:
    index = json.loads((contracts_dir / "ops" / "INDEX.json").read_text(encoding="utf-8"))
    types = request_operation.known_types(schemas)
    assert types == {entry["type"] for entry in index["types"]}
    assert request_operation.known_types(schemas) is types
    for kind in types:
        schemas.load(f"ops/{kind}.params.json")


# ---------------------------------------------------------------------------------------------- control failures


class DeadControl:
    def pending_create(self, session_id: str, kind: str, body: dict[str, Any]) -> str:
        raise ApiSockError("unavailable")


class DeadWait(RecordingControl):
    def pending_wait(self, pending_id: str, session_id: str, timeout: float) -> dict[str, Any] | None:
        raise ApiSockError("unavailable")


@pytest.mark.parametrize("control", [DeadControl(), DeadWait()])
@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("request_operation", {"type": "github.comment", "params": COMMENT}),
        ("get_operation", {"operation_id": CHILD_ID}),
        ("get_subagent_result", {"session_id": CHILD_ID}),
        ("get_task", {"task_id": TASK_ID}),
        ("list_tasks", {}),
    ],
)
def test_an_unreachable_control_process_is_unavailable(
    schemas: SchemaSet, control: object, tool: str, arguments: dict[str, Any]
) -> None:
    call = ToolCall(tool, SESSION_ID, arguments, control, schemas)  # type: ignore[arg-type]
    with pytest.raises(ToolError) as caught:
        load_tools()[tool].handler(call)
    assert (caught.value.code, caught.value.message) == (
        "unavailable",
        "The platform is not reachable right now. Try again later.",
    )


def test_a_locally_rejected_operation_needs_the_control_process(schemas: SchemaSet) -> None:
    call = ToolCall("request_operation", SESSION_ID, {"type": "x.y", "params": {}}, DeadControl(), schemas)  # type: ignore[arg-type]
    with pytest.raises(ToolError) as caught:
        request_operation.request_operation(call)
    assert caught.value.code == "unavailable"


def test_forwarding_for_an_inactive_session_is_unavailable(api_client: ApiClient, schemas: SchemaSet) -> None:
    call = ToolCall("request_operation", SESSION_ID, {"type": "github.comment", "params": COMMENT}, api_client, schemas)
    with pytest.raises(ToolError) as caught:
        request_operation.request_operation(call)
    assert caught.value.code == "unavailable"
