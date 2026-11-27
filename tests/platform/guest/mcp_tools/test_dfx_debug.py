"""U-GST-dfx_debug (ADR-102 section 5): the MCP tool `dfx_debug` is `request_operation` of the type `dfx.debug` with
the query DTO of the dfx-debug CLI as its arguments - checked against the operation's parameter schema, journaled as
`operation.request` for the runner, answered with the operation's outcome; it offers no write."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from agent_guest.control import ControlContext
from agent_guest.mcp import load_tools
from agent_guest.mcp_tools import dfx_debug, request_operation
from agent_guest.schema import SchemaSet
from agent_guest.state import Session
from tests.platform.guest.conftest import SESSION_ID
from tests.platform.guest.mcp.conftest import McpClient
from tests.platform.guest.mcp_tools.conftest import FakeRunner, forwarded

Runner = Callable[..., FakeRunner]
QUERY: dict[str, Any] = {
    "table": "buy_crypto",
    "select": [{"kind": "aggregate", "column": "id", "aggregate": "count", "as": "n"}],
    "where": {"kind": "leaf", "column": "amlCheck", "op": "=", "value": "Pass"},
    "limit": 1,
}
RESULT = {"table": "buy_crypto", "keys": ["n"], "rows": [[42]], "rows_total": 1, "truncated": False}


def _json(result: dict[str, Any]) -> Any:
    [item] = result["content"]
    return json.loads(item["text"])


def test_dfx_debug_runs_the_operation_with_the_dto_and_returns_its_result(
    mcp: McpClient, session: Session, context: ControlContext, schemas: SchemaSet, fake_runner: Runner
) -> None:
    def answer(kind: str, body: dict[str, Any], session_id: str) -> dict[str, Any]:
        assert (kind, session_id) == ("operation.request", SESSION_ID)
        return {
            "status": "ok",
            "result": {"operation_id": body["operation_id"], "status": "succeeded", "result": RESULT},
        }

    fake_runner(answer)
    result = mcp.tool("dfx_debug", QUERY)
    value = _json(result)
    assert result["isError"] is False
    assert schemas.errors("mcp/tools/dfx_debug.output.json", value) == []
    [record] = forwarded(context)
    body = record["body"]
    assert (record["kind"], body["type"], body["params"], body["local_check"]) == (
        "operation.request",
        "dfx.debug",
        QUERY,
        "passed",
    )
    assert schemas.errors("guest/journal/operation.request.json", body) == []
    assert value == {"operation_id": body["operation_id"], "status": "succeeded", "result": RESULT}


def test_a_dto_the_operation_refuses_is_journaled_rejected_and_never_waits(
    mcp: McpClient, session: Session, context: ControlContext, fake_runner: Runner
) -> None:
    fake_runner(lambda kind, body, session_id: None)
    deep: dict[str, Any] = {"kind": "leaf", "column": "id", "op": "=", "value": 1}
    deep = {"kind": "not", "child": {"kind": "or", "children": [deep, {"kind": "leaf", "column": "x", "op": "~"}]}}
    started = time.monotonic()
    value = _json(mcp.tool("dfx_debug", {**QUERY, "where": deep}))
    assert time.monotonic() - started < 5  # answered at once, no wait for the runner
    assert value["status"] == "rejected"
    assert value["error"]["code"] == "params_invalid"
    assert value["error"]["pointers"][0].startswith("/params/where")
    [record] = forwarded(context)
    assert (record["body"]["local_check"], record["body"]["local_error"]) == ("rejected", value["error"])


def test_without_an_answer_the_query_is_pending(
    mcp: McpClient, session: Session, context: ControlContext, fake_runner: Runner, monkeypatch: Any
) -> None:
    monkeypatch.setattr(request_operation, "WAIT_SECONDS", 0.2)
    fake_runner(lambda kind, body, session_id: None)
    started = time.monotonic()
    value = _json(mcp.tool("dfx_debug", QUERY))
    assert time.monotonic() - started < 5
    [record] = forwarded(context)
    assert value == {"operation_id": record["body"]["operation_id"], "status": "pending"}


def test_the_tool_offers_no_write(schemas: SchemaSet) -> None:
    """There is nothing like the CLI's `poll` (`POST /payIn/poll`): the arguments are the read DTO only and the
    description names no write."""
    tool = load_tools()["dfx_debug"]
    properties = schemas.load("mcp/objects.json")["$defs"]["input_dfx_debug"]["properties"]
    assert set(properties) == {"table", "select", "where", "groupBy", "orderBy", "limit", "offset"}
    assert dfx_debug.OPERATION_TYPE == "dfx.debug"
    assert "no write, no poll" in tool.description
    assert "payIn" not in tool.description
