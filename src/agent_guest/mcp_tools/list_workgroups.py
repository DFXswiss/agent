"""MCP tool ``list_workgroups`` (03-api.md §8.1, read scope ADR-087).

A read tool: forwarded to the runner as ``platform.query`` (answered by RUN-08 from its local state) with the handler
limit of 10 s; the runner checks the scope and the rights of the task owner at call time.
"""

from __future__ import annotations

from agent_guest.mcp_tools._forward import read_tool

TOOL = read_tool(
    "list_workgroups",
    "Return the workgroups of this organization with this session's access to each (null: not readable; another "
    "workgroup is readable when the task owner and the person instructing you may both read it).",
)
