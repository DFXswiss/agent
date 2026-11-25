"""MCP tool ``browser_download`` (13-work-vm.md section 13.4, ADR-085 section 3; KOR-44).

Forwarded as ``workvm.call`` (``_forward.browser_tool``); the runner drives the platform browser of the session
(the first call opens it) and answers with the page after the action, checked against
``mcp/tools/browser_download.output.json``.
Input ``{target?, url?, path?}``.
"""

from __future__ import annotations

from agent_guest.mcp_tools import _forward

TOOL = _forward.browser_tool(
    "browser_download",
    "Download a file with the platform browser into your workspace: click target or open url (with the "
    "browser's cookies and sign-ins), and save the file at path (default .browser/downloads/<file name>); "
    "file names its path and size.",
)
