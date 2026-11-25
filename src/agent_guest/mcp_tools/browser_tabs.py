"""MCP tool ``browser_tabs`` (13-work-vm.md section 13.4, ADR-085 section 3; KOR-44).

Forwarded as ``workvm.call`` (``_forward.browser_tool``); the runner drives the platform browser of the session
(the first call opens it) and answers with the page after the action, checked against
``mcp/tools/browser_tabs.output.json``.
Input ``{action, index?, url?}``.
"""

from __future__ import annotations

from agent_guest.mcp_tools import _forward

TOOL = _forward.browser_tool(
    "browser_tabs",
    "Manage the tabs of the platform browser: list, new (with url), select index (it becomes the active tab) "
    "or close index (default the active tab).",
)
