"""MCP tool ``workvm_sync`` (13-work-vm.md §3; ADR-059; KOR-29).

Forwarded as ``workvm.call`` (``_forward.workvm_tool``, no ``wait_seconds``: the guest waits up to 33 s). A large sync
may take longer than that: no answer in time is the tool error ``timeout`` that points the session to
``workvm_status`` (``_forward.WorkvmNoAnswer``). The result is checked against ``mcp/tools/workvm_sync.output.json``.
"""

from __future__ import annotations

from agent_guest.mcp_tools import _forward

TOOL = _forward.workvm_tool(
    "workvm_sync",
    "Copy files between your workspace and /workspace of your work VM. direction to_vm mirrors the workspace (or "
    "path below it) into the VM; workvm_exec already does this before every command. direction from_vm brings the "
    "files of /workspace/<path> back into the same path of your workspace (path required, e.g. a build output or "
    "a screenshot directory), so you can Read them and commit them with the GitHub "
    "operations. .git directories, git-ignored files and protected files are not brought back. Returns the counts "
    "of changed, deleted and skipped files.",
)
