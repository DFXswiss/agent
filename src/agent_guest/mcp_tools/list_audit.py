"""MCP tool ``list_audit`` (03-api.md §8.1, ADR-095 §3, read scope ADR-089).

A read tool: forwarded to the runner as ``platform.query`` with the handler limit of 10 s; the runner reads the hub's
device route of the workgroup, checks the scope and the rights of the task owner and of the instructing person at
call time (``workgroup_id``: another workgroup both may read) and passes an audit row's ``detail`` on as flat tokens
only.
"""

from __future__ import annotations

from agent_guest.mcp_tools._forward import read_tool

TOOL = read_tool(
    "list_audit",
    "Return the audit rows of this session's workgroup or another workgroup you may read (workgroup_id), newest "
    "first, optionally filtered by period, action (or a dotted group), task or session, or with group_by action "
    "their counts per action and result instead; page with before.",
)
