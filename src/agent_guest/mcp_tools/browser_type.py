"""MCP tool ``browser_type`` (13-work-vm.md section 13.4, ADR-085 section 3; KOR-44).

Forwarded as ``workvm.call`` (``_forward.browser_tool``); the runner drives the platform browser of the session
(the first call opens it) and answers with the page after the action, checked against
``mcp/tools/browser_type.output.json``.
Input ``{target, text, submit?, clear?}``.
"""

from __future__ import annotations

from agent_guest.mcp_tools import _forward

TOOL = _forward.browser_tool(
    "browser_type",
    "Fill text into a field of the active tab of the platform browser at once: clear (default true) replaces "
    "the content, false appends; submit presses Enter afterwards (sends a form). Text typed into a password "
    "field is masked in the record.",
)
