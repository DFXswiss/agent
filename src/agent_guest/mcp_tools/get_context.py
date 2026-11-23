"""MCP tool ``get_context`` (03-api.md §8.1): organization, workgroup, task, session, rights and subagent limits.

A read tool: forwarded to the runner as ``platform.query`` (answered by RUN-08 from its local state).
"""

from __future__ import annotations

from agent_guest.mcp import query_tool

TOOL = query_tool(
    "get_context",
    "Return the organization, workgroup, task and session of this session, the workgroup rights of the task owner "
    "and the subagent limits.",
)
