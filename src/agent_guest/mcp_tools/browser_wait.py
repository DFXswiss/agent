"""MCP tool ``browser_wait`` (13-work-vm.md section 13.4, ADR-085 section 3; KOR-44).

Forwarded as ``workvm.call`` (``_forward.browser_tool``); the runner drives the platform browser of the session
(the first call opens it) and answers with the page after the action, checked against
``mcp/tools/browser_wait.output.json``.
Input ``{text?, gone?, url?, ms?, timeout_ms?}``.
"""

from __future__ import annotations

from agent_guest.mcp_tools import _forward

TOOL = _forward.browser_tool(
    "browser_wait",
    "Wait in the active tab of the platform browser until text appears, text is gone, the URL contains url, "
    "or simply ms milliseconds (at most 20000); timeout_ms (default 10000, at most 20000) ends the wait with "
    "ok false.",
)
