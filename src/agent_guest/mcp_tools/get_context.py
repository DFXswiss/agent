"""MCP tool ``get_context`` (03-api.md §8.1): organization, workgroup, task, session, rights, subagent limits and, since
KOR-52 (ADR-095 §3), the session's name and handle, its main agent and its helpers.

A read tool: forwarded to the runner as ``platform.query`` (answered by RUN-08 from its local state).
"""

from __future__ import annotations

from agent_guest.mcp import query_tool

TOOL = query_tool(
    "get_context",
    "Return the organization, workgroup, task and session of this session, the workgroup rights of the task owner, "
    "the subagent limits, your name and handle, your main agent and your helpers (your own subagents).",
)
