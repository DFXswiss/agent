"""MCP tool ``workvm_kill`` (13-work-vm.md §3; ADR-059; KOR-29).

Forwarded as ``workvm.call`` (``_forward.workvm_tool``, no ``wait_seconds``: the guest waits up to 33 s). The result
is checked against ``mcp/tools/workvm_kill.output.json``.
"""

from __future__ import annotations

from agent_guest.mcp_tools import _forward

TOOL = _forward.workvm_tool(
    "workvm_kill",
    "Stop a job of your work VM: sends signal (TERM by default, or INT, HUP, KILL) to every process of the job and "
    "returns the job with its final output. Use it for a command that hangs or that you no longer need; start the "
    "next one with workvm_exec.",
)
