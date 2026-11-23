"""MCP tool ``workvm_output`` (13-work-vm.md §3; ADR-059; KOR-29).

Forwarded as ``workvm.call`` (``_forward.workvm_tool``): the runner answers with the job after ``wait_seconds``
(default 30) or its end, with the output tail or the bytes from ``offset``; the guest waits that long plus 5 s (at most
33 s). The result is checked against ``mcp/tools/workvm_output.output.json``.
"""

from __future__ import annotations

from agent_guest.mcp_tools import _forward

TOOL = _forward.workvm_tool(
    "workvm_output",
    "Follow a job of your work VM started with workvm_exec: waits up to wait_seconds (default and at most 30) for new "
    "output or the end of the job and returns its state, exit_code and output. Without offset you get the tail of "
    "the output; with offset (and limit) the bytes from that position, so you can page through long logs "
    "(output_start and output_bytes tell you where you are). Call it again as long as the job is running.",
)
