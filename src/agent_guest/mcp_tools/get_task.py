"""MCP tool ``get_task`` (03-api.md §8.1, read scope ADR-089).

A read tool: forwarded to the runner as ``platform.query`` (answered by RUN-08 from its local state) with the handler
limit of 10 s; the runner checks that the task belongs to a workgroup the session may read (else ``not_found``). An
answer about another task than the one asked for is ``unavailable``.
"""

from __future__ import annotations

from typing import Any

from agent_guest.mcp import Tool, ToolCall
from agent_guest.mcp_tools import _forward


def get_task(call: ToolCall) -> Any:
    return _forward.checked_read(call, _forward.READ_WAIT_SECONDS, id=call.arguments["task_id"])


TOOL = Tool(
    name="get_task",
    description="Return a task of this session's workgroup or of another workgroup you may read, with its sessions "
    "and operations.",
    handler=get_task,
)
