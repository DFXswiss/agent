"""MCP tool ``get_metrics`` (03-api.md §8.1, ADR-095 §3, read scope ADR-089).

A read tool: forwarded to the runner as ``platform.query`` with the handler limit of 10 s; the runner reads the hub's
device route of the workgroup and checks the scope and the rights of the task owner and of the instructing person at
call time (``workgroup_id``: another workgroup both may read).
"""

from __future__ import annotations

from agent_guest.mcp_tools._forward import read_tool

TOOL = read_tool(
    "get_metrics",
    "Return the monitoring series of one metric of a session, workgroup VM or work VM of this session's workgroup "
    "or another workgroup you may read (workgroup_id) for a period at a step in seconds, at most 1000 points.",
)
