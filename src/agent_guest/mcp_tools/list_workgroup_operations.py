"""MCP tool ``list_workgroup_operations`` (03-api.md §8.1, ADR-095 §3, read scope ADR-089).

A read tool: forwarded to the runner as ``platform.query`` with the handler limit of 10 s; the runner reads the hub's
device route of the workgroup and checks the scope and the rights of the task owner and of the instructing person at
call time (``workgroup_id``: another workgroup both may read). ``list_operations`` stays the read of this task.
"""

from __future__ import annotations

from agent_guest.mcp_tools._forward import read_tool

TOOL = read_tool(
    "list_workgroup_operations",
    "Return the operations of every task of this session's workgroup or another workgroup you may read "
    "(workgroup_id), newest first, optionally filtered by period, type or state, or with group_by type their counts "
    "per type, state and error code instead; page with before.",
)
