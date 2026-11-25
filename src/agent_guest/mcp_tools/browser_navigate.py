"""MCP tool ``browser_navigate`` (13-work-vm.md section 13.4, ADR-085 section 3; KOR-44).

Forwarded as ``workvm.call`` (``_forward.browser_tool``); the runner drives the platform browser of the session
(the first call opens it) and answers with the page after the action, checked against
``mcp/tools/browser_navigate.output.json``.
Input ``{url?, history?, query?, wait_until?}``.
"""

from __future__ import annotations

from agent_guest.mcp_tools import _forward

TOOL = _forward.browser_tool(
    "browser_navigate",
    "Open a URL (http or https, any address on the internet) in the active tab of the platform browser, or go "
    "back, forward or reload with history; wait_until is load (default), domcontentloaded or networkidle. "
    "query (automated search) is switched off: open a search page with url, like a person. The platform "
    "browser runs in its own browser VM: every action is recorded with a picture and the user can watch it "
    "live. It cannot reach your work VM: look at an app running there with workvm_browse or a Playwright "
    "script in the work VM (neither is recorded nor watched). Use request_operation browser.open only for an "
    "explicit start with its own idle_seconds. For a captcha or a second factor that needs a person, ask with "
    "request_operation human.question.",
)
