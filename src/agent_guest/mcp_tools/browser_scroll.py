"""MCP tool ``browser_scroll`` (13-work-vm.md section 13.4, ADR-085 section 3; KOR-44).

Forwarded as ``workvm.call`` (``_forward.browser_tool``); the runner drives the platform browser of the session
(the first call opens it) and answers with the page after the action, checked against
``mcp/tools/browser_scroll.output.json``.
Input ``{target?, dy?}``.
"""

from __future__ import annotations

from agent_guest.mcp_tools import _forward

TOOL = _forward.browser_tool(
    "browser_scroll",
    "Scroll the active tab of the platform browser: target scrolls that element into view, else dy pixels "
    "down (default 600; negative scrolls up).",
)
