"""MCP tool ``list_members`` (03-api.md §8.1, read scope ADR-089).

``{scope: "org" | "workgroup", workgroup_id?}``: ``workgroup_id`` only for scope ``workgroup`` (the contract schema is
a plain object, so this rule is checked here: a call that breaks it is ``params_invalid`` and is not forwarded).
Otherwise a read tool: forwarded to the runner as ``platform.query`` (answered by RUN-08 from its local state) with
the handler limit of 10 s; the runner checks the scope and the rights of the task owner and of the instructing person
at call time.
"""

from __future__ import annotations

from typing import Any

from agent_guest.mcp import Tool, ToolCall, ToolRefused
from agent_guest.mcp_tools import _forward


def list_members(call: ToolCall) -> Any:
    if "workgroup_id" in call.arguments and call.arguments["scope"] != "workgroup":
        raise ToolRefused()
    return _forward.read(call)


TOOL = Tool(
    name="list_members",
    description="Return the members of this organization (scope org, with their role) or of this session's "
    "workgroup or another workgroup you may read (scope workgroup, workgroup_id, with their access).",
    handler=list_members,
)
