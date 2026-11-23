"""MCP tool ``get_whiteboard`` (03-api.md §8.1, read scope ADR-024).

``{level: "org" | "workgroup" | "task", task_id?}``: ``task_id`` exactly for level ``task``. The contract schema is a
plain object (the model API refuses a top-level ``oneOf`` in tool inputs, contract change GST-07), so this rule is
checked here: a call that breaks it is ``params_invalid`` and is not forwarded. Otherwise a read tool: forwarded to the
runner as ``platform.query`` (RUN-08) with the handler limit of 10 s; the runner checks that the task belongs to this
session's workgroup.
"""

from __future__ import annotations

from typing import Any

from agent_guest.mcp import Tool, ToolCall, ToolRefused
from agent_guest.mcp_tools import _forward


def get_whiteboard(call: ToolCall) -> Any:
    if (call.arguments["level"] == "task") != ("task_id" in call.arguments):
        raise ToolRefused()
    return _forward.read(call)


TOOL = Tool(
    name="get_whiteboard",
    description="Return the whiteboard entries of this organization, this session's workgroup or a task of it "
    "(task_id exactly for level task).",
    handler=get_whiteboard,
)
