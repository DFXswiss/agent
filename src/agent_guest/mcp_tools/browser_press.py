"""MCP tool ``browser_press`` (13-work-vm.md section 13.4, ADR-085 section 3; KOR-44).

Forwarded as ``workvm.call`` (``_forward.browser_tool``); the runner drives the platform browser of the session
(the first call opens it) and answers with the page after the action, checked against
``mcp/tools/browser_press.output.json``.
Input ``{key}``.
"""

from __future__ import annotations

from agent_guest.mcp_tools import _forward

TOOL = _forward.browser_tool(
    "browser_press",
    "Press a key or chord on the focused element of the active tab of the platform browser: Enter, Escape, "
    "Tab, ArrowDown, Control+A and so on.",
)
