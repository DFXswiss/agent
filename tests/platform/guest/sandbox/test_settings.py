"""U-GST-sandbox: managed Claude Code settings (docs/platform/spec/05-runner.md section 4.2, 03-api.md section 8.1).

Every rule of 05 section 4.2 is checked on its own, and the rendered file is compared byte for byte with the golden
fixture `fixtures/managed-settings.json` (a change of the settings is a deliberate change of that fixture).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_guest.sandbox import settings

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "managed-settings.json"
REPO_ROOT = Path(__file__).resolve().parents[4]
HOOK_BASE = "http://127.0.0.1:7700/hooks/"

# 03-api.md section 8.1, in table order, then the work VM tools of 13-work-vm.md section 3 and the platform browser
# tools of section 13.4.
SPEC_MCP_TOOLS = [
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
    "get_usage",
    "list_audit",
    "list_workgroup_operations",
    "get_metrics",
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
    "dfx_debug",
]
# 05-runner.md section 5.2.
SPEC_EVENTS = [
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
]
SPEC_TOOL_EVENTS = {"PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionRequest", "PermissionDenied"}
SPEC_DENIED_TOOLS = [
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
]
SPEC_EDIT_DENIES = [
    "~/.claude/**",
    "~/.claude.json",
    "**/.claude/**",
    "**/.mcp.json",
    "**/CLAUDE.md",
    "**/CLAUDE.local.md",
    "**/AGENTS.md",
    "**/.git/**",
    "**/.gitconfig",
    "//etc/**",
    "//opt/**",
    "//usr/**",
    "//var/**",
    "//proc/**",
    "//run/**",
    "//srv/sessions/*/home/**",
]
SPEC_READ_DENIES = [
    "//etc/agent-guest/**",
    "//var/lib/agent-guest/**",
    "//proc/**",
    "//run/agent-guest/**",
    "//srv/sessions/*/home/**",
]


@pytest.fixture
def managed() -> dict[str, Any]:
    return settings.build_settings()


def test_rendered_file_matches_the_golden_fixture() -> None:
    assert settings.render() == FIXTURE.read_text(encoding="utf-8")


def test_render_is_canonical_json_with_trailing_newline() -> None:
    text = settings.render()
    assert text.endswith("}\n")
    assert json.loads(text) == settings.build_settings()
    assert text == json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n"


def test_build_returns_a_fresh_object_each_time(managed: dict[str, Any]) -> None:
    managed["permissions"]["allow"].append("Bash")
    assert "Bash" not in settings.build_settings()["permissions"]["allow"]


def test_default_mode_is_dont_ask(managed: dict[str, Any]) -> None:
    assert managed["permissions"]["defaultMode"] == "dontAsk"


def test_bypass_permissions_mode_is_disabled(managed: dict[str, Any]) -> None:
    assert managed["permissions"]["disableBypassPermissionsMode"] == "disable"


def test_auto_mode_is_disabled(managed: dict[str, Any]) -> None:
    assert managed["disableAutoMode"] == "disable"


def test_allow_list_is_the_file_tools_web_search_and_every_platform_tool(managed: dict[str, Any]) -> None:
    # 13-work-vm.md section 13.7: WebSearch is refused per session (command line, policy) for the platform access.
    expected = ["Read", "Glob", "Grep", "Edit", "Write", "WebSearch"]
    expected += [f"mcp__platform__{tool}" for tool in SPEC_MCP_TOOLS]
    # the main orchestrator's agent tool (05-runner.md section 17.5): only its session gets that server
    expected += ["mcp__orchestrator__search", "mcp__orchestrator__run"]
    assert managed["permissions"]["allow"] == expected


def test_mcp_tool_constant_is_the_api_tool_list() -> None:
    assert list(settings.MCP_TOOLS) == SPEC_MCP_TOOLS


def test_mcp_tool_constant_matches_the_contracts() -> None:
    tools_dir = REPO_ROOT / "contracts" / "mcp" / "tools"
    assert tools_dir.is_dir(), "contracts/mcp/tools is missing from the checkout"
    names = {path.name.split(".", 1)[0] for path in tools_dir.glob("*.input.json")}
    assert names == set(settings.MCP_TOOLS)


@pytest.mark.parametrize("tool", SPEC_DENIED_TOOLS)
def test_tool_is_denied(managed: dict[str, Any], tool: str) -> None:
    assert tool in managed["permissions"]["deny"]
    assert tool not in managed["permissions"]["allow"]


@pytest.mark.parametrize("pattern", SPEC_EDIT_DENIES)
def test_edit_deny(managed: dict[str, Any], pattern: str) -> None:
    assert f"Edit({pattern})" in managed["permissions"]["deny"]


@pytest.mark.parametrize("pattern", SPEC_READ_DENIES)
def test_read_deny(managed: dict[str, Any], pattern: str) -> None:
    assert f"Read({pattern})" in managed["permissions"]["deny"]


def test_deny_list_holds_exactly_the_specified_rules(managed: dict[str, Any]) -> None:
    expected = SPEC_DENIED_TOOLS + [f"Edit({p})" for p in SPEC_EDIT_DENIES] + [f"Read({p})" for p in SPEC_READ_DENIES]
    assert managed["permissions"]["deny"] == expected


def test_no_write_path_rules(managed: dict[str, Any]) -> None:
    # Claude Code 2.1.330 ignores Write(path) rules; Edit(path) covers every writing tool.
    rules = managed["permissions"]["allow"] + managed["permissions"]["deny"]
    assert not [rule for rule in rules if rule.startswith("Write(")]


@pytest.mark.parametrize(
    "key", ["allowManagedPermissionRulesOnly", "allowManagedHooksOnly", "allowManagedMcpServersOnly"]
)
def test_only_managed_rules_hooks_and_servers(managed: dict[str, Any], key: str) -> None:
    assert managed[key] is True


def test_only_the_platform_mcp_server_and_the_orchestrator_tool(managed: dict[str, Any]) -> None:
    assert managed["allowedMcpServers"] == [{"serverName": "platform"}, {"serverName": "orchestrator"}]


def test_no_known_marketplaces(managed: dict[str, Any]) -> None:
    assert managed["strictKnownMarketplaces"] == []


def test_skill_shell_execution_disabled(managed: dict[str, Any]) -> None:
    assert managed["disableSkillShellExecution"] is True


def test_one_http_hook_per_event(managed: dict[str, Any]) -> None:
    assert list(managed["hooks"]) == SPEC_EVENTS
    for event, entries in managed["hooks"].items():
        assert len(entries) == 1
        (hook,) = entries[0]["hooks"]
        assert hook == {
            "type": "http",
            "url": HOOK_BASE + event,
            "timeout": 15,
            "headers": {"Authorization": "Bearer $PLATFORM_SESSION_TOKEN"},
            "allowedEnvVars": ["PLATFORM_SESSION_TOKEN"],
        }


@pytest.mark.parametrize("event", SPEC_EVENTS)
def test_tool_events_match_every_tool(managed: dict[str, Any], event: str) -> None:
    entry = managed["hooks"][event][0]
    if event in SPEC_TOOL_EVENTS:
        assert entry["matcher"] == "*"
    else:
        assert "matcher" not in entry


def test_no_command_hooks_status_line_or_stdio_servers(managed: dict[str, Any]) -> None:
    hook_types = {hook["type"] for entries in managed["hooks"].values() for entry in entries for hook in entry["hooks"]}
    assert hook_types == {"http"}
    assert "statusLine" not in managed
    assert "mcpServers" not in managed
    assert "command" not in settings.render()


def test_hook_urls_and_env_are_restricted(managed: dict[str, Any]) -> None:
    assert managed["allowedHttpHookUrls"] == [HOOK_BASE + "*"]
    assert managed["httpHookAllowedEnvVars"] == ["PLATFORM_SESSION_TOKEN"]


def test_env(managed: dict[str, Any]) -> None:
    assert managed["env"] == {
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "1",
    }


def test_no_other_top_level_keys(managed: dict[str, Any]) -> None:
    assert set(managed) == {
        "permissions",
        "disableAutoMode",
        "allowManagedPermissionRulesOnly",
        "allowManagedHooksOnly",
        "allowManagedMcpServersOnly",
        "allowedMcpServers",
        "strictKnownMarketplaces",
        "disableSkillShellExecution",
        "allowedHttpHookUrls",
        "httpHookAllowedEnvVars",
        "hooks",
        "env",
    }
    assert set(managed["permissions"]) == {"defaultMode", "disableBypassPermissionsMode", "allow", "deny"}


def test_main_writes_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    assert settings.main([]) == 0
    assert capsys.readouterr().out == settings.render()


def test_main_writes_the_file_atomically_with_mode_0644(tmp_path: Path) -> None:
    target = tmp_path / "claude-code" / "managed-settings.json"
    target.parent.mkdir()
    target.write_text("old", encoding="utf-8")
    os.chmod(target, 0o600)
    assert settings.main(["--output", str(target)]) == 0
    assert target.read_text(encoding="utf-8") == settings.render()
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert [path.name for path in target.parent.iterdir()] == ["managed-settings.json"]


def test_main_removes_the_temporary_file_on_failure(tmp_path: Path) -> None:
    target = tmp_path / "managed-settings.json"
    target.mkdir()  # os.replace cannot overwrite a directory with a file
    with pytest.raises(OSError):  # noqa: PT011 - the platform decides between IsADirectoryError and OSError
        settings.main(["--output", str(target)])
    assert [path.name for path in tmp_path.iterdir()] == ["managed-settings.json"]


def test_module_entry_point() -> None:
    done = subprocess.run(
        [sys.executable, "-m", "agent_guest.sandbox.settings"], capture_output=True, text=True, check=True
    )
    assert done.stdout == settings.render()


def test_module_is_standard_library_only() -> None:
    source = Path(settings.__file__).read_text(encoding="utf-8")
    imports = {line.split()[1].split(".")[0] for line in source.splitlines() if line.startswith(("import ", "from "))}
    assert imports <= {"__future__", "argparse", "json", "os", "sys", "tempfile", "collections", "typing", "pathlib"}
