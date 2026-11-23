"""MCP tool ``workvm_status`` (13-work-vm.md §3; ADR-059; KOR-29).

Forwarded as ``workvm.call`` (``_forward.workvm_tool``, no ``wait_seconds``: the guest waits up to 33 s). The result
is checked against ``mcp/tools/workvm_status.output.json``.
"""

from __future__ import annotations

from agent_guest.mcp_tools import _forward

TOOL = _forward.workvm_tool(
    "workvm_status",
    "Show your work VM (state, size, CPUs, memory, disk; null when you have none: work VMs are temporary, and "
    "workvm_exec or workvm_browse obtain a new one automatically) and its last 50 jobs with their state and exit "
    "code. Use it to find a job_id again, or after a call that timed out.",
)
