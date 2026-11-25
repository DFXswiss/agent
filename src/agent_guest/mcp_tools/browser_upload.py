"""MCP tool ``browser_upload`` (13-work-vm.md section 13.4, ADR-085 section 3; KOR-44).

Forwarded as ``workvm.call`` (``_forward.browser_tool``); the runner drives the platform browser of the session
(the first call opens it) and answers with the page after the action, checked against
``mcp/tools/browser_upload.output.json``.
Input ``{target, paths}``.
"""

from __future__ import annotations

from agent_guest.mcp_tools import _forward

TOOL = _forward.browser_tool(
    "browser_upload",
    "Put files of your workspace (paths relative to the workspace, at most 16) into a file input of the "
    "active tab of the platform browser.",
)
