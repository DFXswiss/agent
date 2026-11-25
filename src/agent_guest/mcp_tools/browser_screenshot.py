"""MCP tool ``browser_screenshot`` (13-work-vm.md section 13.4, ADR-085 section 3; KOR-44).

Forwarded as ``workvm.call`` (``_forward.browser_tool``); the runner drives the platform browser of the session
(the first call opens it) and answers with the page after the action, checked against
``mcp/tools/browser_screenshot.output.json``.
Input ``{path?, full_page?}``.
"""

from __future__ import annotations

from agent_guest.mcp_tools import _forward

TOOL = _forward.browser_tool(
    "browser_screenshot",
    "Save a PNG of the active tab of the platform browser into your workspace at path (default "
    ".browser/screenshots/<browser_id>-<seq>.png; full_page for the whole page) and look at it with Read.",
)
