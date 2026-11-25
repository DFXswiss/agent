"""MCP tool ``browser_click`` (13-work-vm.md section 13.4, ADR-085 section 3; KOR-44).

Forwarded as ``workvm.call`` (``_forward.browser_tool``); the runner drives the platform browser of the session
(the first call opens it) and answers with the page after the action, checked against
``mcp/tools/browser_click.output.json``.
Input ``{target?, x?, y?, double?, button?}``.
"""

from __future__ import annotations

from agent_guest.mcp_tools import _forward

TOOL = _forward.browser_tool(
    "browser_click",
    "Click an element of the active tab of the platform browser: target, or a point of the 1280x800 viewport "
    "(x, y) when no element fits; double for a double click, button left (default), right or middle. Links "
    "that open a new tab make it the active tab.",
)
