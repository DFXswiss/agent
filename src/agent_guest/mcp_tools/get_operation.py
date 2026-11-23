"""MCP tool ``get_operation`` (03-api.md §8.1, §12; 05-runner.md §12).

Forwarded to the runner as ``platform.query`` (answered by RUN-07 from its operation store): the runner answers once
the operation is final or after ``wait_seconds`` (≤ 30, default 0) with ``status:"pending"``; the guest waits that
long plus the read limit (``_forward.waiting_read``). No answer in that time is ``{operation_id, status:"pending"}``,
as for ``request_operation`` (the operation is not known to be final). Operations outside this session's task scope
are ``not_found`` (runner). An answer about another operation than the one asked for is ``unavailable``.
"""

from __future__ import annotations

from typing import Any

from agent_guest.mcp import Tool, ToolCall
from agent_guest.mcp_tools import _forward


def get_operation(call: ToolCall) -> Any:
    operation_id = call.arguments["operation_id"]
    try:
        return _forward.waiting_read(call, operation_id=operation_id)
    except _forward.NoAnswer:
        return {"operation_id": operation_id, "status": "pending"}


TOOL = Tool(
    name="get_operation",
    description="Return the state and result of an operation of this task; wait_seconds (at most 30) waits for a "
    "final state.",
    handler=get_operation,
)
