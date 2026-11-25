"""Managed Claude Code settings of every session sandbox (docs/platform/spec/05-runner.md section 4.2, ADR-008).

Layer 1 of the sandbox: `/etc/claude-code/managed-settings.json` (root 0644) is generated once at golden build by

    python3 -E -s -m agent_guest.sandbox.settings --output /etc/claude-code/managed-settings.json

and is identical in every VM of that golden image. The settings allow only the file tools, the platform MCP tools and
Claude Code's own web search (refused per session for the platform access), deny every other tool, protect
configuration and system paths, accept only managed rules, hooks and MCP servers, and send every hook event to the
guest API (`http://127.0.0.1:7700/hooks/<Event>`). There are no command hooks, no stdio MCP servers and no command
status line: each would need an exec exception in the OS sandbox.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Sequence
from typing import Any

DEFAULT_OUTPUT = "/etc/claude-code/managed-settings.json"
HOOK_BASE_URL = "http://127.0.0.1:7700/hooks/"
HOOK_TIMEOUT_SECONDS = 15
SESSION_TOKEN_ENV = "PLATFORM_SESSION_TOKEN"
MCP_SERVER = "platform"
# The main orchestrator's agent tool (05-runner.md section 17.5): a second server only the orchestrator session gets,
# through ``/mcp-orchestrator`` of the guest API; its two tools are those of ``agent_platform.connector``.
ORCHESTRATOR_SERVER = "orchestrator"
ORCHESTRATOR_TOOLS: tuple[str, ...] = ("search", "run")

# Platform API tools of the session (03-api.md section 8.1); the same list is served by GST-04/GST-07.
MCP_TOOLS: tuple[str, ...] = (
    "get_context",
    "list_workgroups",
    "list_members",
    "list_tasks",
    "get_task",
    "get_task_messages",
    "get_whiteboard",
    "get_notifications",
    "request_operation",
    "get_operation",
    "list_operations",
    "get_subagent_result",
    "workvm_exec",
    "workvm_output",
    "workvm_kill",
    "workvm_status",
    "workvm_sync",
    "workvm_browse",
    "browser_navigate",
    "browser_snapshot",
    "browser_click",
    "browser_hover",
    "browser_type",
    "browser_select",
    "browser_press",
    "browser_scroll",
    "browser_wait",
    "browser_evaluate",
    "browser_upload",
    "browser_dialog",
    "browser_tabs",
    "browser_download",
    "browser_screenshot",
)
FILE_TOOLS: tuple[str, ...] = ("Read", "Glob", "Grep", "Edit", "Write")
# Claude Code's own web search runs at the model provider (13-work-vm.md section 13.7). These settings are shared by
# every session of the VM, so they allow it; the command line (``driver``) and the PreToolUse policy refuse it per
# session for the platform access.
ALLOWED_TOOLS: tuple[str, ...] = ("WebSearch",)
DENIED_TOOLS: tuple[str, ...] = (
    "Bash",
    "PowerShell",
    "Agent",
    "Task",
    "TaskOutput",
    "TaskStop",
    "KillShell",
    "BashOutput",
    "Monitor",
    "WebFetch",
    "NotebookEdit",
    "Skill",
    "Workflow",
    "CronCreate",
    "CronDelete",
    "RemoteTrigger",
    "EnterWorktree",
    "ExitWorktree",
    "SendMessage",
    "TeamCreate",
    "LSP",
)
# Edit(path) rules cover every writing tool; Write(path) rules are ignored by Claude Code 2.1.330.
EDIT_DENIED_PATHS: tuple[str, ...] = (
    "~/.claude/**",
    "~/.claude.json",
    "**/.claude/**",
    "**/.mcp.json",
    "**/CLAUDE.md",
    "**/CLAUDE.local.md",  # loaded as instructions like CLAUDE.md (ADR-045)
    "**/AGENTS.md",  # loaded as instructions like CLAUDE.md (ADR-045)
    "**/.git/**",
    "**/.gitconfig",
    "//etc/**",
    "//opt/**",
    "//usr/**",
    "//var/**",
    "//proc/**",
    "//run/**",
    "//srv/sessions/*/home/**",
)
# The session's own harness under /run/agent-guest/harness/ is read by Claude Code at start through
# --append-system-prompt-file, not through the Read tool, so the whole guest runtime directory is denied.
READ_DENIED_PATHS: tuple[str, ...] = (
    "//etc/agent-guest/**",
    "//var/lib/agent-guest/**",
    "//proc/**",
    "//run/agent-guest/**",
    "//srv/sessions/*/home/**",
)
# Hook events of 05-runner.md section 5.2; the tool events match every tool.
HOOK_EVENTS: tuple[str, ...] = (
    "SessionStart",
    "SessionEnd",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PermissionRequest",
    "PermissionDenied",
    "Notification",
    "Stop",
    "StopFailure",
    "SubagentStart",
    "SubagentStop",
    "PreCompact",
    "PostCompact",
    "ConfigChange",
    "InstructionsLoaded",
    "CwdChanged",
)
TOOL_EVENTS = frozenset({"PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionRequest", "PermissionDenied"})
ENV: tuple[tuple[str, str], ...] = (
    ("DISABLE_AUTOUPDATER", "1"),
    ("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1"),
    ("DISABLE_TELEMETRY", "1"),
    ("DISABLE_ERROR_REPORTING", "1"),
    ("CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY", "1"),
)


def _hook_entry(event: str) -> dict[str, Any]:
    hook = {
        "type": "http",
        "url": HOOK_BASE_URL + event,
        "timeout": HOOK_TIMEOUT_SECONDS,
        "headers": {"Authorization": f"Bearer ${SESSION_TOKEN_ENV}"},
        "allowedEnvVars": [SESSION_TOKEN_ENV],
    }
    entry: dict[str, Any] = {"hooks": [hook]}
    if event in TOOL_EVENTS:
        entry["matcher"] = "*"
    return entry


def build_settings() -> dict[str, Any]:
    """The managed settings as a new JSON-compatible object."""
    deny = list(DENIED_TOOLS)
    deny += [f"Edit({path})" for path in EDIT_DENIED_PATHS]
    deny += [f"Read({path})" for path in READ_DENIED_PATHS]
    return {
        "permissions": {
            "defaultMode": "dontAsk",
            "disableBypassPermissionsMode": "disable",
            "allow": list(FILE_TOOLS)
            + list(ALLOWED_TOOLS)
            + [f"mcp__{MCP_SERVER}__{tool}" for tool in MCP_TOOLS]
            + [f"mcp__{ORCHESTRATOR_SERVER}__{tool}" for tool in ORCHESTRATOR_TOOLS],
            "deny": deny,
        },
        "disableAutoMode": "disable",
        "allowManagedPermissionRulesOnly": True,
        "allowManagedHooksOnly": True,
        "allowManagedMcpServersOnly": True,
        "allowedMcpServers": [{"serverName": MCP_SERVER}, {"serverName": ORCHESTRATOR_SERVER}],
        "strictKnownMarketplaces": [],
        "disableSkillShellExecution": True,
        "allowedHttpHookUrls": [HOOK_BASE_URL + "*"],
        "httpHookAllowedEnvVars": [SESSION_TOKEN_ENV],
        "hooks": {event: [_hook_entry(event)] for event in HOOK_EVENTS},
        "env": dict(ENV),
    }


def render() -> str:
    """The file content: deterministic JSON (sorted keys, two-space indent) with a trailing newline."""
    return json.dumps(build_settings(), indent=2, sort_keys=True) + "\n"


def write(path: str) -> None:
    """Replace `path` atomically with the rendered settings, mode 0644."""
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".managed-settings.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(render())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m agent_guest.sandbox.settings", description="Write the managed Claude Code settings."
    )
    parser.add_argument("--output", help=f"target file (golden build: {DEFAULT_OUTPUT}); default: standard output")
    args = parser.parse_args(argv)
    if args.output is None:
        sys.stdout.write(render())
    else:
        write(args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
