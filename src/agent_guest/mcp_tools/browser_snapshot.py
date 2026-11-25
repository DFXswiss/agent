"""MCP tool ``browser_snapshot`` (13-work-vm.md section 13.4, ADR-085 section 3; KOR-44).

Forwarded as ``workvm.call`` (``_forward.browser_tool``); the runner drives the platform browser of the session
(the first call opens it) and answers with the page after the action, checked against
``mcp/tools/browser_snapshot.output.json``.
Input ``{}``.
"""

from __future__ import annotations

from agent_guest.mcp_tools import _forward

TOOL = _forward.browser_tool(
    "browser_snapshot",
    "Read the active tab of the platform browser again: url, title, tabs and the snapshot of the page with "
    "fresh refs. Use it after a wait outside the browser or when a ref is no longer known.",
)
