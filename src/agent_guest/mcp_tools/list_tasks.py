"""MCP tool ``list_tasks`` (03-api.md §8.1, read scope ADR-087).

A read tool: forwarded to the runner as ``platform.query`` (answered by RUN-08 from its local state) with the handler
limit of 10 s; the runner checks the scope and the rights of the task owner and of the instructing person at call
time (``workgroup_id``: another workgroup both may read).
"""

from __future__ import annotations

from agent_guest.mcp_tools._forward import read_tool

TOOL = read_tool(
    "list_tasks",
    "Return the tasks of this session's workgroup, or of another workgroup you may read (workgroup_id), newest "
    "first, optionally filtered by state; page with before.",
)
