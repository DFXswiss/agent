"""MCP tool ``workvm_browse`` (13-work-vm.md §3; ADR-059 decision 6; KOR-29).

Forwarded as ``workvm.call`` (``_forward.workvm_tool``, no ``wait_seconds``: the guest waits up to 33 s). The runner
drives the preinstalled Chromium of the session's work VM (obtaining a standard task VM first when the session has
none) and answers with the page; a screenshot is brought into the workspace at ``path``. The result is checked against
``mcp/tools/workvm_browse.output.json``.
"""

from __future__ import annotations

from agent_guest.mcp_tools import _forward

TOOL = _forward.workvm_tool(
    "workvm_browse",
    "Open a web page (http or https, any address the work VM can reach) in the Chromium of your work VM. action read "
    "(default) returns the final URL, HTTP status, title and visible text; screenshot saves a full page PNG into "
    "your workspace at path (default .workvm/screenshots/<job_id>.png), view it with Read; click clicks selector, "
    "fill types text into selector, both then read the page. wait_ms waits after loading. Obtains a work VM "
    "automatically if you have none. For complex flows (logins, several steps, assertions) write a Playwright "
    "script and run it with workvm_exec.",
)
