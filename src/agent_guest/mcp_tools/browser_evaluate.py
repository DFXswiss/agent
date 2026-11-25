"""MCP tool ``browser_evaluate`` (13-work-vm.md section 13.4, ADR-085 section 3; KOR-44).

Forwarded as ``workvm.call`` (``_forward.browser_tool``); the runner drives the platform browser of the session
(the first call opens it) and answers with the page after the action, checked against
``mcp/tools/browser_evaluate.output.json``.
Input ``{expression}``.
"""

from __future__ import annotations

from agent_guest.mcp_tools import _forward

TOOL = _forward.browser_tool(
    "browser_evaluate",
    "Run a JavaScript expression in the page of the active tab of the platform browser and return its JSON "
    "value in result (at most 24000 characters), for reading what the snapshot does not show. Scripted work "
    "belongs into a Playwright script in your work VM.",
)
