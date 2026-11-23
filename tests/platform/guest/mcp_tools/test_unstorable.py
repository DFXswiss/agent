"""U-GST-mcp: arguments the runner's store cannot hold are refused at once (KOR-09, AUD-02 NA-11; 03-api.md section 8).

A NUL character or a lone surrogate in a string, or a nesting deeper than the runner accepts, makes the runner skip
the journal record (PostgreSQL ``jsonb``, ``agent_runner.operations.journal.storable``): the session got no answer and
the call ended only after ``PENDING_SECONDS`` as ``pending_expired``. The guest answers such a call with the formal
refusal ``params_invalid`` before anything is forwarded and journals ``platform.refused {code, tool}`` without the
arguments.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent_guest.control import ControlContext
from agent_guest.mcp import ToolCall, ToolRefused
from agent_guest.mcp_tools import _forward
from agent_guest.state import Session
from agent_runner.operations import journal
from tests.platform.guest.mcp.conftest import McpClient
from tests.platform.guest.mcp_tools.conftest import forwarded

COMMENT = {"repo": "example-org/example-repo", "number": 12, "body": "LGTM"}
REFUSED = {"error": "params_invalid", "message": "The arguments do not match the tool."}


def tool_json(result: dict[str, Any]) -> Any:
    return json.loads(result["content"][0]["text"])


def refusals(context: ControlContext) -> list[dict[str, Any]]:
    records, _ = context.journal.read(0, 1000)
    return [record["body"] for record in records if record["kind"] == "platform.refused"]


@pytest.mark.parametrize(
    "body", ["LGTM\x00", "a\udcc2\udc9bb", "\ud800"], ids=["nul", "lone-surrogates", "lone-high-surrogate"]
)
def test_an_operation_with_unstorable_params_is_refused_at_once(
    mcp: McpClient, session: Session, context: ControlContext, body: str
) -> None:
    result = mcp.tool("request_operation", {"type": "github.comment", "params": {**COMMENT, "body": body}})
    assert result["isError"] is True
    assert tool_json(result) == REFUSED
    assert forwarded(context) == []
    assert refusals(context) == [{"code": "params_invalid", "tool": "request_operation"}]


def test_a_locally_invalid_operation_with_a_nul_is_refused_as_well(
    mcp: McpClient, session: Session, context: ControlContext
) -> None:
    result = mcp.tool("request_operation", {"type": "no.such_type", "params": {"x\x00": 1}})
    assert tool_json(result) == REFUSED
    assert forwarded(context) == []
    assert refusals(context) == [{"code": "params_invalid", "tool": "request_operation"}]


def test_a_nul_in_the_idempotency_key_or_too_deep_params_are_refused(
    mcp: McpClient, session: Session, context: ControlContext
) -> None:
    deep: Any = "x"
    for _ in range(_forward.MAX_DEPTH):
        deep = {"d": deep}
    for arguments in (
        {"type": "github.comment", "params": COMMENT, "idempotency_key": "k\x00"},
        {"type": "github.comment", "params": {**COMMENT, "extra": deep}},
    ):
        assert tool_json(mcp.tool("request_operation", arguments)) == REFUSED
    assert forwarded(context) == []
    assert len(refusals(context)) == 2


def test_storable_follows_the_runner_rule() -> None:
    assert _forward.storable({"a": ["b", {"c": 1, "d": None, "e": 1.5, "f": True}]})
    assert not _forward.storable({"a\x00": 1})
    assert not _forward.storable(["\udfff"])
    deep: Any = "x"
    for _ in range(_forward.MAX_DEPTH):
        deep = [deep]
    assert _forward.storable(deep)
    assert not _forward.storable([deep])


def test_forwarding_helpers_refuse_unstorable_arguments() -> None:
    call = ToolCall("get_task", "s", {"task_id": "\x00"}, None, None)  # type: ignore[arg-type]
    with pytest.raises(ToolRefused):
        _forward.read(call)
    with pytest.raises(ToolRefused):
        _forward.forward(call, "query", {"tool": "get_task", "arguments": call.arguments})


def test_the_guest_rule_is_the_runners() -> None:
    samples: list[Any] = [{"a": "b"}, {"a\x00": 1}, ["\udfff"], "\ud800x", {"k": [1, None, "ok"]}]
    assert [_forward.storable(value) for value in samples] == [journal.storable(value) for value in samples]
    assert _forward.MAX_DEPTH == journal.MAX_DEPTH
