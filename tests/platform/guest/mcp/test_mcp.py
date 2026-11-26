"""The platform MCP server on ``/mcp`` (03-api.md §8, contracts ``mcp/``): methods, errors, tool registry,
argument abuse (ATK-37) and the forwarding of read tools as ``platform.query`` with its time limit.
"""

from __future__ import annotations

import http.client
import json
import socket
import sys
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_guest import __version__, registry, toolsready
from agent_guest import mcp as mcp_core
from agent_guest.apisock import ApiClient, ApiSockError
from agent_guest.control import ControlContext
from agent_guest.mcp import McpServer, Tool, ToolCall, ToolError, dereference, forward_query, load_tools, query_tool
from agent_guest.mcp_tools.get_context import TOOL as GET_CONTEXT
from agent_guest.schema import SchemaError, SchemaSet
from agent_guest.state import Session
from tests.platform.guest.conftest import SESSION_ID, SESSION_TOKEN, make_session
from tests.platform.guest.mcp.conftest import CONTEXT_RESULT, FakeRunner, McpClient

Runner = Callable[..., FakeRunner]


def refusals(context: ControlContext) -> list[tuple[str | None, dict[str, Any]]]:
    """The ``platform.refused`` records of the journal as ``(session_id, body)`` (KOR-07)."""
    records = context.journal.read(0, 100)[0]
    return [(record["session_id"], record["body"]) for record in records if record["kind"] == "platform.refused"]


def ok(result: object) -> dict[str, Any]:
    return {"status": "ok", "result": result}


def tool_json(result: dict[str, Any]) -> Any:
    [item] = result["content"]
    assert item["type"] == "text"
    return json.loads(item["text"])


# ---------------------------------------------------------------------------------------------- transport


def test_initialize_echoes_the_protocol(
    mcp: McpClient, session: Session, schemas: SchemaSet, context: ControlContext
) -> None:
    params = {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "claude-code", "version": "2.1.330"},
    }
    reply = mcp.call("initialize", params)
    assert (reply.status, reply.headers["Content-Type"]) == (200, "application/json")
    answer = reply.json()
    assert schemas.errors("mcp/jsonrpc.response.json", answer) == []
    assert schemas.errors("mcp/initialize.result.json", answer["result"]) == []
    assert answer["result"] == {
        "protocolVersion": "2025-03-26",
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "agent-dfx-swiss-platform", "version": __version__},
    }
    assert mcp.call("initialize", {"capabilities": {}}).json()["error"] == {
        "code": -32602,
        "message": "Invalid params.",
    }
    assert refusals(context) == [(SESSION_ID, {"code": "params_invalid", "tool": None})]


def test_default_protocol_version() -> None:
    assert mcp_core.DEFAULT_PROTOCOL == "2025-06-18"


@pytest.mark.parametrize("method", ["notifications/initialized", "notifications/cancelled"])
def test_notifications_are_accepted_without_body(method: str, mcp: McpClient, session: Session) -> None:
    reply = mcp.post(json.dumps({"jsonrpc": "2.0", "method": method}).encode())
    assert (reply.status, reply.body) == (202, b"")


def test_ping_and_unknown_methods(
    mcp: McpClient, session: Session, schemas: SchemaSet, context: ControlContext
) -> None:
    ping = mcp.call("ping").json()
    assert ping["result"] == {}
    assert schemas.errors("mcp/ping.result.json", ping["result"]) == []
    assert refusals(context) == []
    for method in ("server/discover", "sampling/createMessage", "vendor/secret-name"):
        answer = mcp.call(method).json()
        assert answer["error"] == {"code": -32601, "message": "Method not found."}
        assert schemas.errors("mcp/jsonrpc.response.json", answer) == []
    # HOT-09: the log names the method when it is one of the protocol, a foreign name never reaches the journal
    assert refusals(context) == [
        (SESSION_ID, {"code": "method_not_found", "tool": None, "method": method})
        for method in ("server/discover", "sampling/createMessage", "other")
    ]


def test_what_the_server_does_not_offer_is_an_empty_list(
    mcp: McpClient, session: Session, schemas: SchemaSet, context: ControlContext
) -> None:
    """HOT-09: every list a client may ask for while it connects is answered, empty where there is nothing."""
    answers = {method: mcp.call(method, {"cursor": "x"}).json() for method in mcp_core.EMPTY_LISTS}
    assert {method: answer["result"] for method, answer in answers.items()} == {
        "resources/list": {"resources": []},
        "resources/templates/list": {"resourceTemplates": []},
        "prompts/list": {"prompts": []},
        "logging/setLevel": {},
    }
    assert all(schemas.errors("mcp/jsonrpc.response.json", answer) == [] for answer in answers.values())
    assert refusals(context) == []
    assert mcp_core.EMPTY_LISTS["prompts/list"] == {"prompts": []}  # answers are copies


def test_the_method_names_are_those_of_the_contract() -> None:
    record = json.loads((Path(mcp_core.__file__).parents[2] / "contracts/guest/journal/record.json").read_text())
    [branch] = [item for item in record["$defs"]["body_platform_refused"]["oneOf"] if "method" in item["properties"]]
    assert set(branch["properties"]["method"]["enum"]) == mcp_core.METHODS | {"other"}


@pytest.mark.parametrize(
    ("raw", "expected_id", "code"),
    [
        (b"{", None, -32700),
        (b"\xff", None, -32700),
        (b'[{"jsonrpc":"2.0","id":1,"method":"ping"}]', None, -32600),
        (b'{"jsonrpc":"1.0","id":7,"method":"ping"}', 7, -32600),
        (b'{"jsonrpc":"2.0","id":"x","method":"ping","extra":1}', "x", -32600),
        (b'{"jsonrpc":"2.0","id":true,"method":"ping"}', None, -32600),
        (b'{"jsonrpc":"2.0","id":null,"method":"ping"}', None, -32600),
        (b'{"jsonrpc":"2.0","id":1,"method":"ping","params":[]}', 1, -32600),
    ],
)
def test_malformed_messages(
    raw: bytes, expected_id: object, code: int, *, mcp: McpClient, session: Session, context: ControlContext
) -> None:
    reply = mcp.post(raw)
    assert reply.status == 400
    answer = reply.json()
    assert (answer["id"], answer["error"]["code"]) == (expected_id, code)
    refusal = "parse_error" if code == -32700 else "invalid_request"
    assert refusals(context) == [(SESSION_ID, {"code": refusal, "tool": None})]


def test_a_refusal_is_answered_when_the_journal_cannot_be_reached(
    schemas: SchemaSet, api_client: ApiClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreachable(*_args: object) -> int:
        raise ApiSockError("unavailable")

    monkeypatch.setattr(api_client, "append", unreachable)
    server = McpServer(schemas, api_client, load_tools())
    status, answer = server.handle(SESSION_ID, b"{")
    assert (status, answer) == (
        400,
        {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error."}},
    )


@pytest.mark.parametrize(("token", "scheme"), [(None, "Bearer"), ("wrong-token-0123456789", "Bearer"), ("x", "Basic")])
def test_authentication(token: str | None, scheme: str, mcp: McpClient, session: Session) -> None:
    reply = mcp.call("ping", token=token, scheme=scheme)
    assert (reply.status, reply.json()) == (401, {"error": "unauthorized"})


def test_stopped_session_is_refused(mcp: McpClient, context: ControlContext) -> None:
    context.state.save_session(make_session(state="failed"))
    assert mcp.call("ping").status == 401


@pytest.mark.parametrize("method", ["GET", "DELETE"])
def test_no_sse_stream_and_no_mcp_session(method: str, api_port: int) -> None:
    connection = http.client.HTTPConnection("127.0.0.1", api_port, timeout=10)
    connection.request(method, "/mcp", headers={"Accept": "text/event-stream"})
    response = connection.getresponse()
    assert (response.status, response.getheader("Allow")) == (405, "POST")
    connection.close()


# ---------------------------------------------------------------------------------------------- tools/list


def test_tools_list_dereferences_the_input_schema(mcp: McpClient, session: Session, schemas: SchemaSet) -> None:
    answer = mcp.call("tools/list", {"cursor": "ignored"}).json()
    tools = answer["result"]["tools"]
    assert [tool["name"] for tool in tools] == list(load_tools())
    [tool] = [tool for tool in tools if tool["name"] == "get_context"]
    assert schemas.errors("mcp/tools-list.result.json#/properties/tools/items", tool) == []
    assert tool["inputSchema"]["type"] == "object"
    assert tool["inputSchema"]["additionalProperties"] is False
    assert "$ref" not in json.dumps(tool["inputSchema"])
    assert mcp.call("tools/list").json()["result"] == answer["result"]
    assert mcp.call("tools/list", {"page": 2}).json()["error"]["code"] == -32602


def test_tools_list_names_exactly_the_tools_of_the_contract(
    mcp: McpClient, session: Session, contracts_dir: Path
) -> None:
    """The listed tools are the enum of ``tools-list.result.json``, the workgroup reads of ADR-097 section 3
    included."""
    document = json.loads((contracts_dir / "mcp" / "tools-list.result.json").read_text(encoding="utf-8"))
    names = document["properties"]["tools"]["items"]["properties"]["name"]["enum"]
    listed = [tool["name"] for tool in mcp.call("tools/list").json()["result"]["tools"]]
    assert set(listed) == set(names)
    assert {"get_usage", "list_audit", "list_workgroup_operations", "get_metrics"} <= set(listed)


def tools_ready_records(context: ControlContext) -> list[tuple[str | None, dict[str, Any]]]:
    records = context.journal.read(0, 100)[0]
    return [(record["session_id"], record["body"]) for record in records if record["kind"] == "session.tools_ready"]


def test_tools_list_reports_that_the_tools_reached_the_agent(
    mcp: McpClient, session: Session, context: ControlContext
) -> None:
    """HOT-09: the agent's ``tools/list`` is the proof that it has the platform tools; only the first listing of a run
    is journaled, and a new run (new token) has to list them again."""
    assert not toolsready.ready(context, SESSION_ID)
    count = len(mcp.call("tools/list").json()["result"]["tools"])
    assert toolsready.ready(context, SESSION_ID)
    mcp.call("tools/list")
    [(sid, body)] = tools_ready_records(context)
    assert (sid, body["tools"]) == (SESSION_ID, count)
    context.state.save_session(replace(session, token_sha256="0" * 64))
    assert not toolsready.ready(context, SESSION_ID)


TOKEN = "2" * 64


def test_tools_list_without_a_token_reports_nothing(
    schemas: SchemaSet, api_client: ApiClient, session: Session, context: ControlContext
) -> None:
    status, answer = McpServer(schemas, api_client, load_tools()).handle(
        SESSION_ID, b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
    )
    assert status == 200
    assert answer is not None
    assert answer["result"]["tools"]
    assert tools_ready_records(context) == []


def test_a_report_the_control_process_cannot_take_is_repeated(
    schemas: SchemaSet,
    api_client: ApiClient,
    session: Session,
    context: ControlContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude Code lists the tools once per run: a report lost while the control process restarts is repeated in the
    background until it is taken (review finding 2)."""
    server = McpServer(schemas, api_client, load_tools())
    real = api_client.call
    answers: list[str] = ["unavailable", "unavailable"]
    pauses: list[float] = []
    spawned: list[Callable[[], None]] = []

    def flaky(op: str, args: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        if answers:
            raise ApiSockError(answers.pop(0))
        return real(op, args, timeout)

    monkeypatch.setattr(api_client, "call", flaky)
    server.pause = pauses.append
    server.spawn = spawned.append
    status, answer = server.handle(SESSION_ID, b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}', session.token_sha256)
    assert status == 200
    assert answer is not None
    assert answer["result"]["tools"]  # the answer never waits for the report
    assert tools_ready_records(context) == []
    (again,) = spawned
    again()
    assert pauses == [mcp_core.REPORT_PAUSE] * 2
    assert toolsready.ready(context, SESSION_ID)


def test_a_report_refused_for_good_or_never_taken_ends(
    schemas: SchemaSet, api_client: ApiClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = McpServer(schemas, api_client, load_tools())
    calls: list[str] = []

    def refused(op: str, args: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        calls.append(op)
        raise ApiSockError("not_found")

    monkeypatch.setattr(api_client, "call", refused)
    server.tools_listed(SESSION_ID, TOKEN)
    assert calls == [mcp_core.TOOLS_LISTED]  # refused for good: no repetition

    def unavailable(op: str, args: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        calls.append(op)
        raise ApiSockError("unavailable")

    monkeypatch.setattr(api_client, "call", unavailable)
    server.pause = lambda seconds: None
    server.spawn = lambda work: work()
    calls.clear()
    server.tools_listed(SESSION_ID, TOKEN)
    assert len(calls) == 1 + mcp_core.REPORT_ATTEMPTS


def test_the_report_thread_runs_in_the_background() -> None:
    done = threading.Event()
    mcp_core._thread(done.set)
    assert done.wait(5)


def test_registry_holds_the_tools_of_this_package() -> None:
    assert load_tools()["get_context"] is GET_CONTEXT
    assert (GET_CONTEXT.input_schema, GET_CONTEXT.output_schema) == (
        "mcp/tools/get_context.input.json",
        "mcp/tools/get_context.output.json",
    )


@pytest.mark.parametrize(
    "body", ['TOOL = "not a tool"\n', "from agent_guest.mcp import query_tool\nTOOL = query_tool('other', 'x')\n"]
)
def test_registry_refuses_foreign_tool_objects(body: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = tmp_path / "gst04_tools"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "list_tasks.py").write_text(body)
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        with pytest.raises(registry.RegistryError) as caught:
            load_tools("gst04_tools")
        assert caught.value.code == "registry_invalid"
    finally:
        for name in [name for name in sys.modules if name.startswith("gst04_tools")]:
            del sys.modules[name]


def test_dereference_inlines_every_reference(tmp_path: Path) -> None:
    root = tmp_path / "contracts"
    root.mkdir()
    head = {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "x"}
    (root / "defs.json").write_text(json.dumps({**head, "$defs": {"n": {"type": "integer", "$comment": "c"}}}))
    (root / "a.json").write_text(
        json.dumps(
            {
                **head,
                "title": "t",
                "type": "object",
                "properties": {
                    "list": {"type": "array", "items": {"$ref": "defs.json#/$defs/n"}},
                    "map": {"type": "object", "additionalProperties": {"$ref": "defs.json#/$defs/n"}},
                    "closed": {"type": "object", "additionalProperties": False},
                    "either": {"oneOf": [{"$ref": "defs.json#/$defs/n"}], "anyOf": [{"type": "null"}]},
                },
            }
        )
    )
    (root / "loop.json").write_text(json.dumps({**head, "$ref": "loop.json"}))
    schemas = SchemaSet(root)
    assert dereference(schemas, "a.json") == {
        "type": "object",
        "properties": {
            "list": {"type": "array", "items": {"type": "integer"}},
            "map": {"type": "object", "additionalProperties": {"type": "integer"}},
            "closed": {"type": "object", "additionalProperties": False},
            "either": {"oneOf": [{"type": "integer"}], "anyOf": [{"type": "null"}]},
        },
    }
    with pytest.raises(SchemaError):
        dereference(schemas, "loop.json")


# ---------------------------------------------------------------------------------------------- tools/call


def test_get_context_is_forwarded_and_answered(
    mcp: McpClient, session: Session, fake_runner: Runner, schemas: SchemaSet, context: ControlContext
) -> None:
    runner = fake_runner(lambda body: ok(CONTEXT_RESULT))
    result = mcp.tool("get_context", {})
    assert schemas.errors("mcp/tools-call.result.json", result) == []
    assert (result["isError"], tool_json(result)) == (False, CONTEXT_RESULT)
    [query] = runner.seen
    assert (query["session_id"], query["tool"], query["arguments"]) == (SESSION_ID, "get_context", {})
    records, _ = context.journal.read(0, 100)
    assert [record["kind"] for record in records] == ["platform.query"]


def test_missing_arguments_count_as_empty(mcp: McpClient, session: Session, fake_runner: Runner) -> None:
    fake_runner(lambda body: ok(CONTEXT_RESULT))
    assert mcp.tool("get_context")["isError"] is False


@pytest.mark.parametrize(
    ("answer", "code"),
    [
        ({"status": "error", "error": {"error": "forbidden", "message": "Not in the read scope."}}, "forbidden"),
        (
            ok({"workgroups": [{"id": SESSION_ID, "name": "Other", "is_default": False, "my_access": None}]}),
            "unavailable",
        ),
    ],
)
def test_runner_errors_and_foreign_results(
    answer: dict[str, Any], code: str, mcp: McpClient, session: Session, fake_runner: Runner
) -> None:
    fake_runner(lambda body: answer)
    result = mcp.tool("get_context", {})
    assert result["isError"] is True
    assert tool_json(result)["error"] == code


def test_pending_call_times_out(
    mcp: McpClient, session: Session, fake_runner: Runner, monkeypatch: pytest.MonkeyPatch, schemas: SchemaSet
) -> None:
    monkeypatch.setattr(mcp_core, "QUERY_WAIT_SECONDS", 0.3)
    runner = fake_runner(lambda body: None)
    result = mcp.tool("get_context", {})
    assert tool_json(result) == {"error": "timeout", "message": "The platform did not answer in time. Try again later."}
    assert schemas.errors("mcp/tool-error.json", tool_json(result)) == []
    assert len(runner.seen) == 1


@pytest.mark.parametrize(
    "params",
    [
        {"name": "delete_task", "arguments": {}},
        {"name": 5},
        {"arguments": {}},
        {"name": "Bash", "arguments": {}},
        {"name": "get_context", "arguments": {}, "extra": 1},
    ],
)
def test_unknown_tools_are_rpc_errors(
    params: dict[str, Any], mcp: McpClient, session: Session, context: ControlContext
) -> None:
    answer = mcp.call("tools/call", params).json()
    assert answer["error"] == {"code": -32602, "message": "Unknown tool."}
    assert refusals(context) == [(SESSION_ID, {"code": "unknown_tool", "tool": None})]


@pytest.mark.parametrize("arguments", [{"x": 1}, [], "text", 5, True])
def test_get_context_takes_no_arguments(
    arguments: object, mcp: McpClient, session: Session, context: ControlContext
) -> None:
    result = mcp.tool("get_context", arguments)
    assert result["isError"] is True
    assert tool_json(result) == {"error": "params_invalid", "message": "The arguments do not match the tool."}
    assert refusals(context) == [(SESSION_ID, {"code": "params_invalid", "tool": "get_context"})]
    assert len(context.journal.read(0, 10)[0]) == 1  # nothing forwarded


# ---------------------------------------------------------------------------------------------- ATK-37 units


@pytest.fixture
def abuse_server(schemas: SchemaSet, api_client: ApiClient, session: Session) -> McpServer:
    """A server with read tools that take arguments; ``get_whiteboard`` is GST-07's tool (it checks the task_id rule
    that its plain-object schema leaves open); the runner never answers."""
    tools = {name: query_tool(name, "test") for name in ("list_tasks", "get_task")}
    return McpServer(schemas, api_client, {**tools, "get_whiteboard": load_tools()["get_whiteboard"]})


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("list_tasks", {"limit": True}),
        ("list_tasks", {"limit": 5.0}),
        ("list_tasks", {"limit": 101}),
        ("list_tasks", {"limit": "5"}),
        ("list_tasks", {"state": "x" * 100_000}),
        ("list_tasks", {"extra": 1}),
        ("get_task", {}),
        ("get_task", {"task_id": 1}),
        ("get_task", {"task_id": "../../etc/passwd"}),
        ("get_whiteboard", {"level": "task"}),
        ("get_whiteboard", {"level": "org", "task_id": None}),
    ],
)
def test_argument_abuse_is_refused_before_forwarding(
    tool: str, arguments: dict[str, Any], abuse_server: McpServer, context: ControlContext
) -> None:
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
    status, answer = abuse_server.handle(SESSION_ID, json.dumps(message).encode())
    assert status == 200
    assert answer is not None
    assert tool_json(answer["result"])["error"] == "params_invalid"
    assert refusals(context) == [(SESSION_ID, {"code": "params_invalid", "tool": tool})]
    assert len(context.journal.read(0, 10)[0]) == 1  # nothing forwarded


def test_valid_arguments_are_forwarded(
    abuse_server: McpServer, context: ControlContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp_core, "QUERY_WAIT_SECONDS", 0.05)
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "list_tasks", "arguments": {"limit": 5}},
    }
    _, answer = abuse_server.handle(SESSION_ID, json.dumps(message).encode())
    assert answer is not None
    assert tool_json(answer["result"])["error"] == "timeout"
    records, _ = context.journal.read(0, 10)
    assert records[0]["body"]["arguments"] == {"limit": 5}


# ---------------------------------------------------------------------------------------------- forwarding units


class DeadControl:
    def pending_create(self, session_id: str, kind: str, body: dict[str, Any]) -> str:
        raise ApiSockError("unavailable")


def test_forwarding_without_control_is_unavailable(schemas: SchemaSet) -> None:
    call = ToolCall("get_context", SESSION_ID, {}, DeadControl(), schemas)  # type: ignore[arg-type]
    with pytest.raises(ToolError) as caught:
        forward_query(call)
    assert (caught.value.code, caught.value.message) == (
        "unavailable",
        "The platform is not reachable right now. Try again later.",
    )


def test_forwarding_for_an_inactive_session_is_unavailable(api_client: ApiClient, schemas: SchemaSet) -> None:
    call = ToolCall("get_context", SESSION_ID, {}, api_client, schemas)
    with pytest.raises(ToolError) as caught:
        forward_query(call, wait=0.01)
    assert caught.value.code == "unavailable"


def test_tool_objects() -> None:
    tool = Tool("get_task", "d", forward_query)
    assert replace(tool, name="list_tasks").input_schema == "mcp/tools/list_tasks.input.json"


# ---------------------------------------------------------------------------------------------- read limit, KOR-10


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_the_read_limiter_is_a_token_bucket_per_session() -> None:
    clock = Clock()
    limiter = mcp_core.ReadLimiter(burst=3, rate=2.0, clock=clock)
    assert [limiter.take("a") for _ in range(5)] == ["ok", "ok", "ok", "refused", "refused_again"]
    assert limiter.take("b") == "ok"  # every session has its own bucket
    clock.now += 0.5  # one token back
    assert [limiter.take("a"), limiter.take("a")] == ["ok", "refused"]  # a new stretch is journaled again
    clock.now += 60
    assert [limiter.take("a") for _ in range(4)] == ["ok", "ok", "ok", "refused"]  # never more than the burst


def test_the_read_limiter_is_one_bucket_for_parallel_calls() -> None:
    limiter = mcp_core.ReadLimiter(burst=50, rate=0.0)
    results: list[str] = []
    lock = threading.Lock()

    def calls() -> None:
        found = [limiter.take("a") for _ in range(100)]
        with lock:
            results.extend(found)

    threads = [threading.Thread(target=calls) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert (results.count("ok"), results.count("refused"), results.count("refused_again")) == (50, 1, 749)


def test_refusals_of_a_session_are_journaled_up_to_their_limit(
    schemas: SchemaSet, api_client: ApiClient, session: Session, context: ControlContext
) -> None:
    """KOR-10 (review of NB-07): refusals that are no reads (an unknown tool, a protocol error, an oversized body) are
    journaled under their own bucket, so a session sending them in a loop cannot grow its records without bound."""
    clock = Clock()
    server = McpServer(schemas, api_client, load_tools(), refusals=mcp_core.ReadLimiter(burst=2, rate=1.0, clock=clock))
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "read_secrets"}}
    for _ in range(10):
        assert server.handle(SESSION_ID, json.dumps(call).encode())[1]["error"]["code"] == -32602  # type: ignore[index]
    assert refusals(context) == [(SESSION_ID, {"code": "unknown_tool", "tool": None})] * 3
    clock.now += 1.0
    server.handle(SESSION_ID, b"{")
    assert refusals(context)[-1] == (SESSION_ID, {"code": "parse_error", "tool": None})


def test_the_read_limiter_forgets_full_buckets(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    monkeypatch.setattr(mcp_core, "MAX_BUCKETS", 2)
    limiter = mcp_core.ReadLimiter(burst=2, rate=1.0, clock=clock)
    limiter.take("a")
    limiter.take("a")
    limiter.take("b")
    clock.now += 1.5  # b is full again, a is not
    limiter.take("c")
    assert sorted(limiter._buckets) == ["a", "c"]


def test_reads_over_the_limit_are_refused_and_journaled_once_per_stretch(
    schemas: SchemaSet, api_client: ApiClient, session: Session, context: ControlContext, fake_runner: Runner
) -> None:
    """KOR-10 (AUD-02 NB-07): a session reading in a loop cannot grow its audit trail without bound."""
    runner = fake_runner(lambda body: ok(CONTEXT_RESULT))
    clock = Clock()
    server = McpServer(schemas, api_client, load_tools(), mcp_core.ReadLimiter(burst=1, rate=1.0, clock=clock))
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_context", "arguments": {}}}
    body = json.dumps(call).encode()
    assert tool_json(server.handle(SESSION_ID, body)[1]["result"]) == CONTEXT_RESULT  # type: ignore[index]
    refused = [server.handle(SESSION_ID, body)[1]["result"] for _ in range(50)]  # type: ignore[index]
    assert all(answer["isError"] for answer in refused)
    assert tool_json(refused[0]) == {
        "error": "rate_limited",
        "message": "Too many platform reads of this session; try again in a few seconds.",
    }
    assert refusals(context) == [(SESSION_ID, {"code": "rate_limited", "tool": "get_context"})]
    assert len(runner.seen) == 1  # a refused read is never forwarded
    # operations are not reads: the operation budget limits them, the read bucket does not
    operation = {**call, "params": {"name": "request_operation", "arguments": {"type": 5}}}
    answer = server.handle(SESSION_ID, json.dumps(operation).encode())[1]
    assert tool_json(answer["result"])["error"] == "params_invalid"  # type: ignore[index]


def _raw_post(port: int, length: int, token: str | None) -> bytes:
    """Only the head of a request with ``Content-Length: length``: the server answers without reading a body."""
    auth = f"Authorization: Bearer {token}\r\n" if token else ""
    head = f"POST /mcp HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: {length}\r\n{auth}\r\n"
    answer = b""
    with socket.create_connection(("127.0.0.1", port), timeout=10) as connection:
        connection.sendall(head.encode())
        while chunk := connection.recv(65536):
            answer += chunk
    return answer


def test_an_oversized_request_is_journaled_after_the_authentication(
    mcp: McpClient, session: Session, context: ControlContext
) -> None:
    """KOR-10 (AUD-02 NB-08): the body limit is checked once the session is known, so its refusal is journaled; an
    unauthenticated oversized request is `401` and journals nothing. The body is never read."""
    from agent_guest.api_routes.mcp import MAX_BODY  # noqa: PLC0415

    assert _raw_post(mcp.port, MAX_BODY + 1, "wrong-token-0123456789").startswith(b"HTTP/1.1 401")
    assert refusals(context) == []
    answer = _raw_post(mcp.port, MAX_BODY + 1, SESSION_TOKEN)
    assert answer.startswith(b"HTTP/1.1 413")
    assert b"Connection: close" in answer
    assert answer.endswith(b'{"error":"size_exceeded"}')
    assert refusals(context) == [(SESSION_ID, {"code": "size_exceeded", "tool": None})]
    # a body at the limit is read and answered
    assert mcp.post(b" " * (MAX_BODY - 2) + b"{}").status == 400
