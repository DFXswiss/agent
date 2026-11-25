"""MCP tool ``browser_dialog`` (13-work-vm.md section 13.4, ADR-085 section 3; KOR-44).

Forwarded as ``workvm.call`` (``_forward.browser_tool``); the runner drives the platform browser of the session
(the first call opens it) and answers with the page after the action, checked against
``mcp/tools/browser_dialog.output.json``.
Input ``{action, text?}``.
"""

from __future__ import annotations

from agent_guest.mcp_tools import _forward

TOOL = _forward.browser_tool(
    "browser_dialog",
    "Choose how the platform browser answers the next dialogs (alert, confirm, prompt, beforeunload): accept "
    "(default) or dismiss, with text as the answer of a prompt; it stays until you change it. Every dialog is "
    "reported in notice.",
)
