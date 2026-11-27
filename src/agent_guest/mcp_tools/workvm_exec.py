"""MCP tool ``workvm_exec`` (13-work-vm.md §3; ADR-059; KOR-29).

Forwarded to the runner as journal record ``workvm.call`` (pending kind ``workvm``, ``_forward.workvm_tool``): the
runner obtains a standard task VM when the session has none (11 §7), mirrors the workspace into ``/workspace`` of the
session's work VM (``sync`` ``to_vm``, the default), starts ``/bin/bash -lc <command>`` as root and answers with the
job after ``wait_seconds`` (default 30) or its end; the guest waits that long plus 5 s (at most 33 s). While the
implicit start of the VM goes on (a cold start can take minutes) the runner answers in time with ``state``
``starting`` instead (nothing ran; 13-work-vm.md §3). The result is checked against
``mcp/tools/workvm_exec.output.json``.
"""

from __future__ import annotations

from agent_guest.mcp_tools import _forward

TOOL = _forward.workvm_tool(
    "workvm_exec",
    "Run a shell command as root in your own Ubuntu work VM: full internet through the platform, install anything "
    "(apt, pip, npm, ...), build, start services and containers, and run tests (lint, prettier, unit and "
    "integration tests): the command, its result, log and exit code are kept and visible to the user. Obtains a "
    "work VM automatically if you have none (it is temporary: torn down when released, after 30 minutes idle or "
    "when the session ends); while it is still starting, which can take minutes, the answer is state starting and "
    "nothing ran: call again in a minute or follow the start with workvm_status. The workspace is mirrored to "
    "/workspace before the command (sync to_vm; the default cwd), so your latest edits are always there. Returns "
    "the job with the tail of its output (stdout and stderr together); if it is still running after wait_seconds "
    "(default and at most 30), continue with workvm_output. Use wait_seconds 0 for long or background work; files "
    "the command creates come back with workvm_sync direction from_vm. For another size or an own VM of a subagent "
    "use request_operation type workvm.request.",
)
