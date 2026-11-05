from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace

import pytest

from agent_cli.ai_accounts import LANE_EXECUTION_ERROR, AccountError
from agent_cli.lane import (
    LANE_STRIP_ENV,
    LANE_VENDORS,
    OPENROUTER_BASE_URL,
    LaneResult,
    _OPENROUTER_WRAPPER,
    _run_in_tmux,
    claude_argv,
    codex_argv,
    finalize_lane_output,
    grok_argv,
    launch,
    openrouter_argv,
    hide_config_dir,
    parse_status,
    record_lane_run,
    tmux_wrap_argv,
)
from agent_cli.main import _sanitize_lane_output, main
from agent_cli.model_slots import MODELS_NOT_CONFIGURED

pytestmark = pytest.mark.no_pg

OPERATOR_GROK_MODEL = "operator-lane-grok-model"
OPERATOR_CODEX_MODEL = "operator-lane-codex-model"
OPERATOR_GROK_HOME = "/operator/path/grok-lane-a"
OPERATOR_CODEX_HOME = "/operator/path/codex-lane-a"
OPERATOR_CLAUDE_HOME = "/operator/path/claude-lane-a"
OPERATOR_CLAUDE_MODEL = "operator-lane-claude-model"
OPERATOR_OPENROUTER_HOME = "/operator/path/openrouter-lane-a"
OPERATOR_OPENROUTER_MODEL = "deepseek/example"
OPERATOR_GROK_HOME_B = "/operator/path/grok-lane-b"
OPERATOR_GROK_MODEL_B = "operator-lane-grok-model-b"
DEFAULT_SESSION = "sess-1"
ALT_SESSION = "sess-2"


def write_operator_ai_accounts(
    home: Path,
    *,
    sessions: dict | None = None,
) -> None:
    """Write an operator-supplied ai-accounts.json for positive lane scenarios.

    Installation defaults stay empty. Callers must invoke this explicitly for
    positive cases; negative tests leave the home unconfigured.
    """
    data = {
        "lane_execution": {"mode": "this-device-is-lane-host"},
        "models": {"first": "grok", "second": "codex"},
        "accounts": {
            "grok-a": {"provider": "grok", "config_dir": OPERATOR_GROK_HOME},
            "codex-a": {"provider": "codex", "config_dir": OPERATOR_CODEX_HOME},
            "claude-a": {"provider": "claude", "config_dir": OPERATOR_CLAUDE_HOME},
            "grok-b": {"provider": "grok", "config_dir": OPERATOR_GROK_HOME_B},
            "openrouter-a": {
                "provider": "openrouter", "config_dir": OPERATOR_OPENROUTER_HOME
            },
        },
        "roles": {
            "lane-builder": {
                "account": "grok-a",
                "model": OPERATOR_GROK_MODEL,
                "access": "workspace-write",
            },
            "lane-reader": {
                "account": "grok-a",
                "model": OPERATOR_GROK_MODEL,
                "access": "read-only",
            },
            "codex-builder": {
                "account": "codex-a",
                "model": OPERATOR_CODEX_MODEL,
                "access": "workspace-write",
            },
            "codex-reader": {
                "account": "codex-a",
                "model": OPERATOR_CODEX_MODEL,
                "access": "read-only",
            },
            "claude-builder": {
                "account": "claude-a",
                "model": OPERATOR_CLAUDE_MODEL,
                "access": "workspace-write",
            },
            "claude-reader": {
                "account": "claude-a",
                "model": OPERATOR_CLAUDE_MODEL,
                "access": "read-only",
            },
            "lane-builder-b": {
                "account": "grok-b",
                "model": OPERATOR_GROK_MODEL_B,
                "access": "workspace-write",
            },
            "openrouter-builder": {
                "account": "openrouter-a",
                "model": OPERATOR_OPENROUTER_MODEL,
                "access": "workspace-write",
            },
            "openrouter-reader": {
                "account": "openrouter-a",
                "model": OPERATOR_OPENROUTER_MODEL,
                "access": "read-only",
            },
        },
        "sessions": sessions
        or {
            DEFAULT_SESSION: {
                "interactive": "lane-builder",
                "lanes": {
                    "grok:implementer": "lane-builder",
                    "grok:reviewer": "lane-reader",
                    "grok:pr-reviewer-quality": "lane-reader",
                    "grok:pr-reviewer-logic": "lane-reader",
                    "codex:implementer": "codex-builder",
                    "codex:reviewer": "codex-reader",
                    "codex:pr-reviewer-quality": "codex-reader",
                    "codex:pr-reviewer-logic": "codex-reader",
                    "claude:implementer": "claude-builder",
                    "claude:reviewer": "claude-reader",
                    "claude:pr-reviewer-quality": "claude-reader",
                    "claude:pr-reviewer-logic": "claude-reader",
                    "openrouter:implementer": "openrouter-builder",
                    "openrouter:reviewer": "openrouter-reader",
                    "openrouter:pr-reviewer-quality": "openrouter-reader",
                    "openrouter:pr-reviewer-logic": "openrouter-reader",
                },
            },
            ALT_SESSION: {
                "interactive": "lane-builder-b",
                "lanes": {
                    "grok:implementer": "lane-builder-b",
                },
            },
        },
    }
    (home / "ai-accounts.json").write_text(json.dumps(data), encoding="utf-8")


def run(argv: list[str]) -> None:
    main(argv)


def _lane_inner(argv: list[str]) -> list[str]:
    if "--" in argv:
        return argv[argv.index("--") + 1 :]
    return list(argv)


def _assert_nested_role_env_prefix(
    argv: list[str], *, config_dir: str, provider: str
) -> None:
    inner = _lane_inner(argv)
    assert inner[0] == "env"
    home_key = "GROK_HOME" if provider == "grok" else "CODEX_HOME"
    assert f"{home_key}={config_dir}" in inner
    for key in (
        "XAI_API_KEY",
        "GROK_API_KEY",
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "ANTHROPIC_API_KEY",
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "GROK_HOME",
        "CODEX_HOME",
    ):
        assert key in inner
        assert inner[inner.index(key) - 1] == "-u"
    # Role env_prefix precedes the existing provider argv (which itself starts with env).
    second_env = inner.index("env", 1)
    assert second_env > inner.index(f"{home_key}={config_dir}")
    provider_bin = "grok" if provider == "grok" else "codex"
    assert provider_bin in inner[second_env:]


def test_grok_implementer_argv_requires_explicit_model() -> None:
    argv = grok_argv(
        spec_file="/tmp/spec.md", cwd="/work", write=True, model="explicit-grok-model"
    )
    assert "--session-id" not in argv
    assert "--always-approve" not in argv
    assert "reasoning-effort" not in argv
    assert "grok-4.5" not in argv
    assert "grok-4.6" not in argv
    assert argv[0] == "env"
    for key in LANE_STRIP_ENV:
        assert "-u" in argv
        assert key in argv
    assert argv[argv.index("grok") :] == [
        "grok",
        "--prompt-file",
        "/tmp/spec.md",
        "-m",
        "explicit-grok-model",
        "--permission-mode",
        "acceptEdits",
        "--allow",
        "Write",
        "--allow",
        "Edit",
        "--output-format",
        "plain",
        "--cwd",
        "/work",
    ]
    strip_idx = [argv.index(k) for k in LANE_STRIP_ENV]
    assert strip_idx == sorted(strip_idx)


def test_grok_reviewer_argv_requires_explicit_model() -> None:
    argv = grok_argv(
        spec_file="/tmp/spec.md", cwd="/work", write=False, model="explicit-readonly-model"
    )
    assert "--permission-mode" not in argv
    assert "acceptEdits" not in argv
    assert "--always-approve" not in argv
    assert "--session-id" not in argv
    assert "reasoning-effort" not in argv
    assert argv[argv.index("grok") :] == [
        "grok",
        "--prompt-file",
        "/tmp/spec.md",
        "-m",
        "explicit-readonly-model",
        "--allow",
        "Read",
        "--allow",
        "Grep",
        "--allow",
        "Glob",
        "--deny",
        "Write",
        "--deny",
        "Edit",
        "--deny",
        "Bash",
        "--no-subagents",
        "--disable-web-search",
        "--output-format",
        "plain",
        "--cwd",
        "/work",
    ]


def test_pr_reviewer_quality_uses_readonly_grok_argv(tmp_path: Path) -> None:
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("review this\n", encoding="utf-8")
    result = launch(
        role="pr-reviewer-quality",
        vendor="grok",
        spec_file=str(spec),
        cwd=str(tmp_path),
        dry_run=True,
        tmux=False,
        config_home=tmp_path,
        session_id=DEFAULT_SESSION,
    )
    assert "--deny" in result.argv
    assert "Write" in result.argv
    assert "acceptEdits" not in result.argv
    assert "--permission-mode" not in result.argv
    assert OPERATOR_GROK_MODEL in result.argv
    _assert_nested_role_env_prefix(
        result.argv, config_dir=OPERATOR_GROK_HOME, provider="grok"
    )


def test_codex_implementer_argv_requires_explicit_model() -> None:
    argv = codex_argv(
        cwd="/work", write=True, output_file="/tmp/out.txt", model="explicit-codex-model"
    )
    assert "workspace-write" in argv
    assert "explicit-codex-model" in argv
    assert "gpt-5.6-sol" not in argv
    assert "reasoning-effort" not in argv
    assert argv[-1] == "-"
    assert argv[0] == "env"
    for key in LANE_STRIP_ENV:
        assert key in argv
    assert argv[argv.index("--model") + 1] == "explicit-codex-model"


def test_codex_reviewer_argv_requires_explicit_model() -> None:
    argv = codex_argv(
        cwd="/work", write=False, output_file="/tmp/out.txt", model="explicit-codex-ro"
    )
    assert "read-only" in argv
    assert "workspace-write" not in argv
    assert argv[argv.index("--model") + 1] == "explicit-codex-ro"
    assert argv[-1] == "-"


def _strip_prefix() -> list[str]:
    prefix = ["env"]
    for key in LANE_STRIP_ENV:
        prefix.extend(["-u", key])
    return prefix


def test_claude_argv_is_exactly_restricted_to_file_tools() -> None:
    assert claude_argv(
        cwd="/work", write=True, model="model-a"
    ) == [
        *_strip_prefix(),
        "claude", "--print", "--verbose", "--output-format", "stream-json",
        "--model", "model-a", "--restricted", "--strict-mcp-config",
        "--disable-slash-commands", "--tools", "Read,Grep,Glob,Edit,Write",
        "--permission-mode", "acceptEdits",
    ]
    assert claude_argv(
        cwd="/work", write=False, model="model-b", read_dirs=("/control", "/evidence")
    ) == [
        *_strip_prefix(),
        "claude", "--print", "--verbose", "--output-format", "stream-json",
        "--model", "model-b", "--restricted", "--strict-mcp-config",
        "--disable-slash-commands", "--tools", "Read,Grep,Glob",
        "--permission-mode", "default", "--add-dir", "/control",
        "--add-dir", "/evidence",
    ]
    with pytest.raises(SystemExit, match="write lane takes no extra directories"):
        claude_argv(cwd="/work", write=True, model="m", read_dirs=("/control",))


def test_openrouter_argv_has_wrapper_and_never_has_key() -> None:
    argv = openrouter_argv(
        cwd="/work",
        write=False,
        model="deepseek/example",
        output_file="/tmp/output.jsonl",
        read_dirs=("/control",),
    )
    assert argv == [
        *_strip_prefix(),
        f"ANTHROPIC_BASE_URL={OPENROUTER_BASE_URL}",
        "ANTHROPIC_API_KEY=",
        "sh", "-c", _OPENROUTER_WRAPPER, "sh", "/tmp/output.jsonl",
        "claude", "--print", "--verbose", "--output-format", "stream-json",
        "--model", "deepseek/example", "--restricted", "--strict-mcp-config",
        "--disable-slash-commands", "--tools", "Read,Grep,Glob",
        "--permission-mode", "default", "--add-dir", "/control",
    ]
    assert "secret-test-key" not in " ".join(argv)


def test_openrouter_wrapper_missing_and_present_key(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "record.txt"
    fake = bin_dir / "claude"
    fake.write_text(
        "#!/bin/sh\n"
        "printf '%s|%s|%s\\n' \"${ANTHROPIC_AUTH_TOKEN:+set}\" \"$ANTHROPIC_BASE_URL\" \"$*\" > \"$FAKE_RECORD\"\n"
        "printf '%s\\n' '{\"type\":\"result\",\"result\":\"STATUS: complete\",\"usage\":{}}'\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    output = tmp_path / "stream.jsonl"
    argv = openrouter_argv(
        cwd=str(tmp_path), write=False, model="vendor/model", output_file=str(output)
    )
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["FAKE_RECORD"] = str(record)
    env["CLAUDE_CONFIG_DIR"] = str(config)
    missing = subprocess.run(argv, capture_output=True, text=True, check=False, env=env)
    assert missing.returncode == 2
    assert missing.stdout == ""
    assert "STATUS: unavailable" in missing.stderr
    assert "openrouter key missing" in missing.stderr
    assert not output.exists()

    key = "secret-test-key"
    (config / "openrouter-api-key").write_text(key, encoding="utf-8")
    present = subprocess.run(argv, capture_output=True, text=True, check=False, env=env)
    assert present.returncode == 0
    assert present.stdout == ""
    assert '"type":"result"' in output.read_text(encoding="utf-8")
    recorded = record.read_text(encoding="utf-8")
    assert recorded.startswith(f"set|{OPENROUTER_BASE_URL}|")
    assert key not in recorded
    assert key not in " ".join(argv)


def test_finalize_lane_output_parses_usage_and_tool_errors() -> None:
    events = [
        {"type": "system", "subtype": "init", "model": "fallback-model"},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "working"},
            {"type": "tool_use", "id": "tool-1", "name": "Read", "input": {"file_path": "/work/a.py"}},
            {"type": "tool_use", "id": "tool-2", "name": "Grep", "input": {"pattern": "secret=abc"}},
            {"type": "tool_use", "id": "tool-3", "name": "Grep", "input": {"pattern": "/work/a.py"}},
            {"type": "tool_use", "id": "tool-4", "name": "Read", "input": {}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tool-2", "is_error": True, "content": "denied"}
        ]}},
        {"type": "result", "result": "done\nSTATUS: complete", "num_turns": 3,
         "duration_ms": 42, "usage": {"input_tokens": 10, "output_tokens": 4,
         "cache_read_input_tokens": 2, "cache_creation_input_tokens": 1},
         "modelUsage": {"vendor/model": {}}},
    ]
    stream = "\n".join(json.dumps(event) for event in events)
    out = finalize_lane_output("claude", stream, "", cwd="/work")
    assert out.text == "done\nSTATUS: complete"
    assert out.usage is not None
    assert out.usage.model == "vendor/model"
    assert out.usage.input_tokens == 10
    assert out.usage.num_turns == 3
    assert out.tools == (
        {"tool": "Read", "target": "a.py", "error": False},
        {"tool": "Grep", "target": "[redacted]", "error": True},
        {"tool": "Grep", "target": "/work/a.py", "error": False},
        {"tool": "Read", "target": "", "error": False},
    )
    assert out.tools_truncated is False


@pytest.mark.parametrize(
    "result_fields",
    [{"is_error": True, "subtype": "success"}, {"is_error": False, "subtype": "error_max_turns"}],
)
def test_finalize_lane_output_drops_text_of_a_failed_result(result_fields) -> None:
    event = {"type": "result", "result": "partial work\nSTATUS: complete",
             "usage": {"input_tokens": 5, "output_tokens": 1}, **result_fields}
    out = finalize_lane_output("claude", json.dumps(event), "", cwd="/work")
    assert out.text == ""
    assert parse_status(out.text, 0) == "partial"
    assert out.usage is not None and out.usage.input_tokens == 5


@pytest.mark.parametrize("vendor", ["openrouter", "codex", "grok"])
def test_finalize_lane_output_hides_the_account_directory_in_the_final_text(vendor: str) -> None:
    text = "Unable to use /opt/profiles/router/settings.json\nSTATUS: complete"
    if vendor == "openrouter":
        event = {"type": "result", "subtype": "success", "is_error": False, "result": text}
        out = finalize_lane_output(
            vendor, "", json.dumps(event), cwd="/work", config_dir="/opt/profiles/router"
        )
    else:
        out = finalize_lane_output(
            vendor, text, "", cwd="/work", config_dir="/opt/profiles/router"
        )
    assert out.text == "Unable to use <config_dir>/settings.json\nSTATUS: complete"


def test_hide_config_dir_replaces_raw_and_resolved_paths(tmp_path: Path) -> None:
    real = tmp_path / "real-profile"
    real.mkdir()
    link = tmp_path / "linked-profile"
    link.symlink_to(real)
    text = f"error reading {link}/settings.json and {real}/key"
    assert hide_config_dir(text, str(link)) == (
        "error reading <config_dir>/settings.json and <config_dir>/key"
    )
    assert hide_config_dir("unchanged", None) == "unchanged"


def test_finalize_lane_output_keeps_text_of_a_successful_result() -> None:
    event = {"type": "result", "subtype": "success", "is_error": False, "result": "STATUS: complete"}
    out = finalize_lane_output("openrouter", "", json.dumps(event), cwd="/work")
    assert out.text == "STATUS: complete"


def test_finalize_lane_output_fail_closed_and_bounds_tools() -> None:
    no_result = json.dumps({"type": "assistant", "message": {"content": []}})
    assert finalize_lane_output("claude", no_result, "", cwd="/work").text == ""
    assert finalize_lane_output("claude", "not json", "", cwd="/work").usage is None
    blocks = [
        {"type": "tool_use", "id": f"t-{i}", "name": "Read", "input": {"path": "x" * 500}}
        for i in range(201)
    ]
    stream = "\n".join(
        [
            json.dumps({"type": "system", "subtype": "init", "model": "fallback"}),
            json.dumps({"type": "assistant", "message": {"content": blocks}}),
            json.dumps({"type": "result", "result": "STATUS: complete", "usage": {},
                        "modelUsage": {"a": {}, "b": {}}}),
        ]
    )
    out = finalize_lane_output(
        "openrouter", "pane noise", stream, cwd="/work", stream_from_output_file=True
    )
    assert len(out.tools) == 200
    assert out.tools_truncated is True
    assert len(out.tools[0]["target"]) <= 300
    assert out.usage is not None and out.usage.model == "fallback"
    assert finalize_lane_output("codex", "stdout", "file", cwd="/work").text == "file"
    assert finalize_lane_output("grok", "stdout", "file", cwd="/work").text == "stdout"


def test_finalize_lane_output_keeps_worktree_targets_and_redacts_outside(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    inside = worktree / "src" / "module.py"
    events = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "inside",
                        "name": "Read",
                        "input": {"file_path": str(inside)},
                    },
                    {
                        "type": "tool_use",
                        "id": "outside",
                        "name": "Read",
                        "input": {"file_path": "/Users/someone/private.py"},
                    },
                    {
                        "type": "tool_use",
                        "id": "outside-other-prefix",
                        "name": "Read",
                        "input": {"file_path": "/opt/openrouter-profile/openrouter-api-key"},
                    },
                    {
                        "type": "tool_use",
                        "id": "escape",
                        "name": "Read",
                        "input": {"path": "../outside.txt"},
                    },
                ]
            },
        },
        {"type": "result", "result": "STATUS: complete", "usage": {}},
    ]
    out = finalize_lane_output(
        "claude",
        "\n".join(json.dumps(event) for event in events),
        "",
        cwd=str(worktree),
    )
    assert out.tools == (
        {"tool": "Read", "target": "src/module.py", "error": False},
        {"tool": "Read", "target": "[redacted]", "error": False},
        {"tool": "Read", "target": "[redacted]", "error": False},
        {"tool": "Read", "target": "[redacted]", "error": False},
    )


@pytest.mark.parametrize("config_inside_worktree", [False, True])
def test_launch_refuses_openrouter_config_and_worktree_overlap(
    tmp_path: Path, config_inside_worktree: bool
) -> None:
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("implement\n", encoding="utf-8")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    config_dir = worktree / "config" if config_inside_worktree else tmp_path
    raw = json.loads((tmp_path / "ai-accounts.json").read_text(encoding="utf-8"))
    raw["accounts"]["openrouter-a"]["config_dir"] = str(config_dir)
    (tmp_path / "ai-accounts.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(SystemExit, match="overlaps"):
        launch(
            role="implementer",
            vendor="openrouter",
            spec_file=str(spec),
            cwd=str(worktree),
            dry_run=True,
            tmux=False,
            config_home=tmp_path,
            session_id=DEFAULT_SESSION,
        )


def test_launch_refuses_openrouter_read_directory_inside_config(tmp_path: Path) -> None:
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("review\n", encoding="utf-8")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    config_dir = tmp_path / "router-config"
    config_dir.mkdir()
    exposed = config_dir / "evidence"
    exposed.mkdir()
    raw = json.loads((tmp_path / "ai-accounts.json").read_text(encoding="utf-8"))
    raw["accounts"]["openrouter-a"]["config_dir"] = str(config_dir)
    (tmp_path / "ai-accounts.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(SystemExit, match="read directory overlaps"):
        launch(
            role="reviewer",
            vendor="openrouter",
            spec_file=str(spec),
            cwd=str(worktree),
            read_dirs=(str(exposed),),
            dry_run=True,
            tmux=False,
            config_home=tmp_path,
            session_id=DEFAULT_SESSION,
        )


def test_launch_openrouter_reads_stream_from_output_file(tmp_path: Path) -> None:
    write_operator_ai_accounts(tmp_path)
    config_dir = tmp_path / "router-config"
    config_dir.mkdir()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    raw = json.loads((tmp_path / "ai-accounts.json").read_text(encoding="utf-8"))
    raw["accounts"]["openrouter-a"]["config_dir"] = str(config_dir)
    (tmp_path / "ai-accounts.json").write_text(json.dumps(raw), encoding="utf-8")
    spec = tmp_path / "spec.md"
    spec.write_text("implement\n", encoding="utf-8")

    def fake(argv: list[str], stdin_text: str | None) -> object:
        wrapper_index = next(i for i, value in enumerate(argv) if "openrouter-api-key" in value)
        output_file = Path(argv[wrapper_index + 2])
        output_file.write_text(
            json.dumps(
                {
                    "type": "result",
                    "result": "STATUS: complete",
                    "usage": {"input_tokens": 7, "output_tokens": 2},
                    "modelUsage": {OPERATOR_OPENROUTER_MODEL: {}},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        assert stdin_text == "implement\n"
        return SimpleNamespace(returncode=0, stdout="pane output is not the result", stderr="")

    result = launch(
        role="implementer",
        vendor="openrouter",
        spec_file=str(spec),
        cwd=str(worktree),
        runner=fake,
        tmux=False,
        config_home=tmp_path,
        session_id=DEFAULT_SESSION,
    )
    assert result.stdout == "STATUS: complete"
    assert result.status == "complete"
    assert result.usage is not None
    assert result.usage.model == OPERATOR_OPENROUTER_MODEL
    assert result.usage.input_tokens == 7


def test_launch_openrouter_in_tmux_keeps_pane_diagnostic_as_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_operator_ai_accounts(tmp_path)
    config_dir = tmp_path / "router-config"
    config_dir.mkdir()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    raw = json.loads((tmp_path / "ai-accounts.json").read_text(encoding="utf-8"))
    raw["accounts"]["openrouter-a"]["config_dir"] = str(config_dir)
    (tmp_path / "ai-accounts.json").write_text(json.dumps(raw), encoding="utf-8")
    spec = tmp_path / "spec.md"
    spec.write_text("implement\n", encoding="utf-8")
    reason = "STATUS: unavailable\nopenrouter key missing\n"
    monkeypatch.setattr(
        "agent_cli.lane._run_in_tmux",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=2, stdout=reason, stderr=""),
    )

    result = launch(
        role="implementer",
        vendor="openrouter",
        spec_file=str(spec),
        cwd=str(worktree),
        tmux=True,
        config_home=tmp_path,
        session_id=DEFAULT_SESSION,
    )

    assert result.status == "unavailable"
    assert result.stdout == ""
    assert "openrouter key missing" in result.stderr


def test_launch_openrouter_missing_key_records_stderr_reason(tmp_path: Path) -> None:
    write_operator_ai_accounts(tmp_path)
    config_dir = tmp_path / "router-config"
    config_dir.mkdir()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    raw = json.loads((tmp_path / "ai-accounts.json").read_text(encoding="utf-8"))
    raw["accounts"]["openrouter-a"]["config_dir"] = str(config_dir)
    (tmp_path / "ai-accounts.json").write_text(json.dumps(raw), encoding="utf-8")
    spec = tmp_path / "spec.md"
    spec.write_text("implement\n", encoding="utf-8")
    reason = "STATUS: unavailable\nopenrouter key missing\n"

    def missing_key(argv: list[str], stdin_text: str | None) -> object:
        assert any("openrouter-api-key" in value for value in argv)
        assert stdin_text == "implement\n"
        return SimpleNamespace(returncode=2, stdout="", stderr=reason)

    result = launch(
        role="implementer",
        vendor="openrouter",
        spec_file=str(spec),
        cwd=str(worktree),
        runner=missing_key,
        tmux=False,
        config_home=tmp_path,
        session_id=DEFAULT_SESSION,
    )
    assert result.stdout == ""
    assert result.status == "unavailable"

    class CaptureStore:
        row: dict | None = None

        def write(
            self, table: str, operation: str, row_id: str, row: dict
        ) -> None:
            assert (table, operation, row_id) == ("activity", "insert", row["id"])
            self.row = row

    store = CaptureStore()
    record_lane_run(
        store,
        session_id=DEFAULT_SESSION,
        result=result,
    )
    assert store.row is not None
    assert store.row["payload"]["final_text"] == reason


def test_parse_status_complete() -> None:
    assert parse_status("hello\nSTATUS: complete\n", 0) == "complete"


def test_parse_status_last_line_wins() -> None:
    assert parse_status("STATUS: complete\nSTATUS: partial\n", 0) == "partial"


def test_parse_status_schema_line_not_complete() -> None:
    assert (
        parse_status("STATUS: complete | partial | timeout | unavailable", 0) == "partial"
    )


def test_parse_status_completed_suffix_not_complete() -> None:
    assert parse_status("STATUS: completed\n", 0) == "partial"


def test_parse_status_newline_after_colon_not_complete() -> None:
    assert parse_status("STATUS:\ncomplete\n", 0) == "partial"


def test_parse_status_rc_124_timeout() -> None:
    assert parse_status("no status here", 124) == "timeout"


def test_parse_status_rc_nonzero_unavailable() -> None:
    assert parse_status("no status here", 1) == "unavailable"


def test_parse_status_rc_zero_partial() -> None:
    assert parse_status("no status here", 0) == "partial"


def test_launch_requires_config_home_and_session_id(tmp_path: Path) -> None:
    spec = tmp_path / "spec.md"
    spec.write_text("do the thing\n", encoding="utf-8")

    def boom(argv: list[str], stdin_text: str | None) -> object:
        raise AssertionError("runner must not be called without config")

    with pytest.raises(SystemExit, match="explicit session and AI configuration home"):
        launch(
            role="implementer",
            vendor="grok",
            spec_file=str(spec),
            cwd=str(tmp_path),
            runner=boom,
            dry_run=True,
            tmux=False,
        )
    with pytest.raises(SystemExit, match="explicit session and AI configuration home"):
        launch(
            role="implementer",
            vendor="grok",
            spec_file=str(spec),
            cwd=str(tmp_path),
            runner=boom,
            dry_run=True,
            tmux=False,
            config_home=tmp_path,
            session_id=None,
        )
    with pytest.raises(SystemExit, match="explicit session and AI configuration home"):
        launch(
            role="implementer",
            vendor="grok",
            spec_file=str(spec),
            cwd=str(tmp_path),
            runner=boom,
            dry_run=True,
            tmux=False,
            config_home=None,
            session_id=DEFAULT_SESSION,
        )


def test_launch_unconfigured_lane_fails_before_runner(tmp_path: Path) -> None:
    """Configured accounts without lane execution must not start the runner."""
    write_operator_ai_accounts(tmp_path)
    raw = json.loads((tmp_path / "ai-accounts.json").read_text(encoding="utf-8"))
    raw.pop("lane_execution")
    (tmp_path / "ai-accounts.json").write_text(json.dumps(raw), encoding="utf-8")
    spec = tmp_path / "spec.md"
    spec.write_text("do the thing\n", encoding="utf-8")
    called = {"n": 0}

    def boom(argv: list[str], stdin_text: str | None) -> object:
        called["n"] += 1
        raise AssertionError("runner must not be called for unconfigured lane")

    with pytest.raises(AccountError, match=re.escape(LANE_EXECUTION_ERROR)):
        launch(
            role="implementer",
            vendor="grok",
            spec_file=str(spec),
            cwd=str(tmp_path),
            runner=boom,
            dry_run=False,
            tmux=False,
            config_home=tmp_path,
            session_id=DEFAULT_SESSION,
        )
    assert called["n"] == 0


def test_launch_dry_run_does_not_require_lane_execution(tmp_path: Path) -> None:
    write_operator_ai_accounts(tmp_path)
    raw = json.loads((tmp_path / "ai-accounts.json").read_text(encoding="utf-8"))
    raw.pop("lane_execution")
    (tmp_path / "ai-accounts.json").write_text(json.dumps(raw), encoding="utf-8")
    spec = tmp_path / "spec.md"
    spec.write_text("inspect the lane command\n", encoding="utf-8")

    def boom(_argv: list[str], _stdin_text: str | None) -> object:
        raise AssertionError("runner must not be called on dry_run")

    result = launch(
        role="implementer",
        vendor="grok",
        spec_file=str(spec),
        cwd=str(tmp_path),
        runner=boom,
        dry_run=True,
        tmux=False,
        config_home=tmp_path,
        session_id=DEFAULT_SESSION,
    )
    assert result.returncode == 0
    assert OPERATOR_GROK_MODEL in result.argv


def test_launch_unconfigured_models_fail_before_runner(tmp_path: Path) -> None:
    write_operator_ai_accounts(tmp_path)
    raw = json.loads((tmp_path / "ai-accounts.json").read_text(encoding="utf-8"))
    raw.pop("models")
    (tmp_path / "ai-accounts.json").write_text(json.dumps(raw), encoding="utf-8")
    spec = tmp_path / "spec.md"
    spec.write_text("do the thing\n", encoding="utf-8")
    called = {"n": 0}

    def boom(argv: list[str], stdin_text: str | None) -> object:
        called["n"] += 1
        raise AssertionError("runner must not be called for unconfigured models")

    with pytest.raises(AccountError, match=re.escape(MODELS_NOT_CONFIGURED)):
        launch(
            role="implementer",
            vendor="grok",
            spec_file=str(spec),
            cwd=str(tmp_path),
            runner=boom,
            dry_run=False,
            tmux=False,
            config_home=tmp_path,
            session_id=DEFAULT_SESSION,
        )
    assert called["n"] == 0


def test_launch_missing_binding_precedes_missing_models(tmp_path: Path) -> None:
    write_operator_ai_accounts(tmp_path)
    raw = json.loads((tmp_path / "ai-accounts.json").read_text(encoding="utf-8"))
    raw.pop("models")
    raw["sessions"][DEFAULT_SESSION]["lanes"].pop("grok:implementer")
    (tmp_path / "ai-accounts.json").write_text(json.dumps(raw), encoding="utf-8")
    spec = tmp_path / "spec.md"
    spec.write_text("do the thing\n", encoding="utf-8")

    with pytest.raises(
        AccountError,
        match=f"No AI lane configured for session {DEFAULT_SESSION} slot grok:implementer",
    ):
        launch(
            role="implementer",
            vendor="grok",
            spec_file=str(spec),
            cwd=str(tmp_path),
            runner=lambda *_args: (_ for _ in ()).throw(AssertionError("must not run")),
            dry_run=False,
            tmux=False,
            config_home=tmp_path,
            session_id=DEFAULT_SESSION,
        )


def test_launch_provider_mismatch_refused_before_runner(tmp_path: Path) -> None:
    write_operator_ai_accounts(tmp_path)
    raw = json.loads((tmp_path / "ai-accounts.json").read_text(encoding="utf-8"))
    raw["sessions"] = {
        DEFAULT_SESSION: {"lanes": {"grok:implementer": "codex-builder"}},
    }
    (tmp_path / "ai-accounts.json").write_text(json.dumps(raw), encoding="utf-8")
    spec = tmp_path / "spec.md"
    spec.write_text("implement\n", encoding="utf-8")
    called = {"n": 0}

    def boom(argv: list[str], stdin_text: str | None) -> object:
        called["n"] += 1
        raise AssertionError("runner must not start on provider mismatch")

    with pytest.raises(AccountError, match="provider does not match"):
        launch(
            role="implementer",
            vendor="grok",
            spec_file=str(spec),
            cwd=str(tmp_path),
            runner=boom,
            tmux=False,
            config_home=tmp_path,
            session_id=DEFAULT_SESSION,
        )
    assert called["n"] == 0


@pytest.mark.parametrize(
    "lane_execution",
    [
        None,
        {},
        {"mode": "remote"},
        {"mode": "local"},
        {"mode": "remote", "host": ""},
        {"mode": "remote", "host": "lane;local"},
        {"mode": "this-device-is-lane-host", "host": "unused"},
    ],
)
def test_launch_invalid_lane_execution_fails_closed(
    tmp_path: Path, lane_execution: object
) -> None:
    write_operator_ai_accounts(tmp_path)
    raw = json.loads((tmp_path / "ai-accounts.json").read_text(encoding="utf-8"))
    if lane_execution is None:
        raw.pop("lane_execution")
    else:
        raw["lane_execution"] = lane_execution
    (tmp_path / "ai-accounts.json").write_text(json.dumps(raw), encoding="utf-8")
    spec = tmp_path / "spec.md"
    spec.write_text("implement\n", encoding="utf-8")

    with pytest.raises(AccountError, match=re.escape(LANE_EXECUTION_ERROR)):
        launch(
            role="implementer",
            vendor="grok",
            spec_file=str(spec),
            cwd=str(tmp_path),
            runner=lambda *_args: (_ for _ in ()).throw(AssertionError("must not run")),
            tmux=False,
            config_home=tmp_path,
            session_id=DEFAULT_SESSION,
        )


def test_launch_remote_mode_is_rejected_before_runner(tmp_path: Path) -> None:
    write_operator_ai_accounts(tmp_path)
    raw = json.loads((tmp_path / "ai-accounts.json").read_text(encoding="utf-8"))
    raw["lane_execution"] = {"mode": "remote", "host": "lane.example"}
    (tmp_path / "ai-accounts.json").write_text(json.dumps(raw), encoding="utf-8")
    spec = tmp_path / "spec.md"
    spec.write_text("implement\n", encoding="utf-8")
    called = {"n": 0}

    def runner(_argv: list[str], _stdin_text: str | None) -> object:
        called["n"] += 1
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with pytest.raises(AccountError, match="remote.*not supported"):
        launch(
            role="implementer",
            vendor="grok",
            spec_file=str(spec),
            cwd=str(tmp_path),
            runner=runner,
            tmux=False,
            config_home=tmp_path,
            session_id=DEFAULT_SESSION,
        )
    assert called["n"] == 0


def test_launch_writable_reviewer_binding_refused_before_runner(tmp_path: Path) -> None:
    write_operator_ai_accounts(tmp_path)
    raw = json.loads((tmp_path / "ai-accounts.json").read_text(encoding="utf-8"))
    raw["sessions"][DEFAULT_SESSION]["lanes"]["grok:reviewer"] = "lane-builder"
    (tmp_path / "ai-accounts.json").write_text(json.dumps(raw), encoding="utf-8")
    spec = tmp_path / "spec.md"
    spec.write_text("review\n", encoding="utf-8")
    called = {"n": 0}

    def boom(argv: list[str], stdin_text: str | None) -> object:
        called["n"] += 1
        raise AssertionError("runner must not start on writable reviewer binding")

    with pytest.raises(AccountError, match="requires read-only access"):
        launch(
            role="reviewer",
            vendor="grok",
            spec_file=str(spec),
            cwd=str(tmp_path),
            runner=boom,
            tmux=False,
            config_home=tmp_path,
            session_id=DEFAULT_SESSION,
        )
    assert called["n"] == 0


def test_two_sessions_select_different_config_homes_and_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("implement\n", encoding="utf-8")
    monkeypatch.setenv("XAI_API_KEY", "ambient-xai-token")
    monkeypatch.setenv("GROK_API_KEY", "ambient-grok-token")
    monkeypatch.setenv("GROK_HOME", "/ambient/should-not-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-openai")

    result_a = launch(
        role="implementer",
        vendor="grok",
        spec_file=str(spec),
        cwd=str(tmp_path),
        dry_run=True,
        tmux=False,
        config_home=tmp_path,
        session_id=DEFAULT_SESSION,
    )
    result_b = launch(
        role="implementer",
        vendor="grok",
        spec_file=str(spec),
        cwd=str(tmp_path),
        dry_run=True,
        tmux=False,
        config_home=tmp_path,
        session_id=ALT_SESSION,
    )

    assert OPERATOR_GROK_MODEL in result_a.argv
    assert OPERATOR_GROK_MODEL_B in result_b.argv
    assert OPERATOR_GROK_MODEL_B not in result_a.argv
    assert OPERATOR_GROK_MODEL not in result_b.argv
    assert f"GROK_HOME={OPERATOR_GROK_HOME}" in result_a.argv
    assert f"GROK_HOME={OPERATOR_GROK_HOME_B}" in result_b.argv
    assert "/ambient/should-not-leak" not in result_a.argv
    assert "/ambient/should-not-leak" not in result_b.argv
    assert "ambient-xai-token" not in " ".join(result_a.argv)
    assert "ambient-grok-token" not in " ".join(result_b.argv)
    assert os.environ["XAI_API_KEY"] == "ambient-xai-token"
    assert os.environ["GROK_HOME"] == "/ambient/should-not-leak"
    _assert_nested_role_env_prefix(
        result_a.argv, config_dir=OPERATOR_GROK_HOME, provider="grok"
    )
    _assert_nested_role_env_prefix(
        result_b.argv, config_dir=OPERATOR_GROK_HOME_B, provider="grok"
    )


def test_launch_dry_run_does_not_call_runner(tmp_path: Path) -> None:
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("do the thing\n", encoding="utf-8")

    def boom(argv: list[str], stdin_text: str | None) -> object:
        raise AssertionError("runner must not be called on dry_run")

    result = launch(
        role="implementer",
        vendor="grok",
        spec_file=str(spec),
        cwd=str(tmp_path),
        runner=boom,
        dry_run=True,
        tmux=False,
        config_home=tmp_path,
        session_id=DEFAULT_SESSION,
    )
    assert result.status == ""
    assert result.returncode == 0
    assert result.model == OPERATOR_GROK_MODEL
    assert OPERATOR_GROK_MODEL in result.argv
    _assert_nested_role_env_prefix(
        result.argv, config_dir=OPERATOR_GROK_HOME, provider="grok"
    )


@pytest.mark.parametrize("vendor", LANE_VENDORS)
def test_every_provider_unsets_ambient_openrouter_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, vendor: str
) -> None:
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("implement\n", encoding="utf-8")
    monkeypatch.setenv("OPENROUTER_API_KEY", "ambient-openrouter-token")

    result = launch(
        role="implementer",
        vendor=vendor,
        spec_file=str(spec),
        cwd=str(tmp_path),
        dry_run=True,
        tmux=False,
        config_home=tmp_path,
        session_id=DEFAULT_SESSION,
    )

    key_index = result.argv.index("OPENROUTER_API_KEY")
    assert result.argv[key_index - 1] == "-u"
    assert "ambient-openrouter-token" not in result.argv
    assert os.environ["OPENROUTER_API_KEY"] == "ambient-openrouter-token"


def test_launch_codex_dry_run_skips_mkstemp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("codex dry run\n", encoding="utf-8")

    def boom_mkstemp(*_args: object, **_kwargs: object) -> tuple[int, str]:
        raise AssertionError("tempfile.mkstemp must not be called on dry_run")

    monkeypatch.setattr("agent_cli.lane.tempfile.mkstemp", boom_mkstemp)
    result = launch(
        role="implementer",
        vendor="codex",
        spec_file=str(spec),
        cwd=str(tmp_path),
        dry_run=True,
        tmux=False,
        config_home=tmp_path,
        session_id=DEFAULT_SESSION,
    )
    assert "--output-last-message" in result.argv
    assert OPERATOR_CODEX_MODEL in result.argv
    _assert_nested_role_env_prefix(
        result.argv, config_dir=OPERATOR_CODEX_HOME, provider="codex"
    )


def test_launch_fake_runner_codex_stdin(tmp_path: Path) -> None:
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    contents = "codex please implement\n"
    spec.write_text(contents, encoding="utf-8")
    seen: list[tuple[list[str], str | None]] = []
    output_paths: list[str] = []

    def fake(argv: list[str], stdin_text: str | None) -> object:
        seen.append((argv, stdin_text))
        out_path = argv[argv.index("--output-last-message") + 1]
        output_paths.append(out_path)
        Path(out_path).write_text("STATUS: complete\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="STATUS: partial\n", stderr="")

    result = launch(
        role="implementer",
        vendor="codex",
        spec_file=str(spec),
        cwd=str(tmp_path),
        runner=fake,
        tmux=False,
        config_home=tmp_path,
        session_id=DEFAULT_SESSION,
    )
    assert len(seen) == 1
    assert seen[0][1] == contents
    assert "codex" in seen[0][0]
    assert OPERATOR_CODEX_MODEL in seen[0][0]
    assert result.status == "complete"
    assert result.returncode == 0
    assert output_paths
    assert not Path(output_paths[0]).exists()


def test_launch_no_tmux_claude_keeps_stream_json_on_stdout(tmp_path: Path) -> None:
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    contents = "claude please review\n"
    spec.write_text(contents, encoding="utf-8")
    stream = json.dumps(
        {"type": "result", "result": "reviewed\nSTATUS: complete", "usage": {}}
    )
    seen_stdin: list[str | None] = []

    def fake(argv: list[str], stdin_text: str | None) -> object:
        seen_stdin.append(stdin_text)
        return SimpleNamespace(returncode=0, stdout=stream, stderr="")

    result = launch(
        role="reviewer",
        vendor="claude",
        spec_file=str(spec),
        cwd=str(tmp_path),
        runner=fake,
        tmux=False,
        config_home=tmp_path,
        session_id=DEFAULT_SESSION,
    )
    assert seen_stdin == [contents]
    assert result.status == "complete"
    assert result.stdout == "reviewed\nSTATUS: complete"


def test_launch_codex_unlinks_output_file_on_runner_exception(tmp_path: Path) -> None:
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("codex please fail\n", encoding="utf-8")
    out_path: str | None = None

    def fake(argv: list[str], stdin_text: str | None) -> object:
        nonlocal out_path
        out_path = argv[argv.index("--output-last-message") + 1]
        Path(out_path).write_text("STATUS: complete\n", encoding="utf-8")
        raise RuntimeError("codex runner failed")

    with pytest.raises(RuntimeError, match="codex runner failed"):
        launch(
            role="implementer",
            vendor="codex",
            spec_file=str(spec),
            cwd=str(tmp_path),
            runner=fake,
            tmux=False,
            config_home=tmp_path,
            session_id=DEFAULT_SESSION,
        )
    assert out_path is not None
    assert not Path(out_path).exists()


def test_launch_fake_runner_grok_stdin_none_or_empty(tmp_path: Path) -> None:
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("grok please implement\n", encoding="utf-8")
    seen: list[str | None] = []

    @dataclass
    class FakeDone:
        returncode: int
        stdout: str
        stderr: str

    def fake(argv: list[str], stdin_text: str | None) -> object:
        seen.append(stdin_text)
        return FakeDone(0, "STATUS: complete\n", "")

    result = launch(
        role="implementer",
        vendor="grok",
        spec_file=str(spec),
        cwd=str(tmp_path),
        runner=fake,
        tmux=False,
        config_home=tmp_path,
        session_id=DEFAULT_SESSION,
    )
    assert seen == [None] or seen == [""]
    assert result.status == "complete"


def test_tmux_wrap_argv_shape() -> None:
    inner = ["env", "-u", "ANTHROPIC_API_KEY", "grok", "--prompt-file", "s"]
    argv = tmux_wrap_argv(inner, name="agent-lane-grok-implementer", cwd="/work")
    assert argv[:6] == [
        "tmux",
        "new-session",
        "-d",
        "-s",
        "agent-lane-grok-implementer",
        "-c",
    ]
    assert argv[6] == "/work"
    assert argv[7] == "--"
    assert argv[8:] == inner


def test_launch_default_wraps_tmux(tmp_path: Path) -> None:
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("implement me\n", encoding="utf-8")
    result = launch(
        role="implementer",
        vendor="grok",
        spec_file=str(spec),
        cwd=str(tmp_path),
        dry_run=True,
        config_home=tmp_path,
        session_id=DEFAULT_SESSION,
    )
    assert result.argv[:3] == ["tmux", "new-session", "-d"]
    assert "-s" in result.argv
    assert result.tmux_session is not None
    assert result.tmux_session.startswith("agent-lane-grok-implementer")
    assert "--" in result.argv
    assert OPERATOR_GROK_MODEL in result.argv
    inner = result.argv[result.argv.index("--") + 1 :]
    assert inner[0] == "env"
    _assert_nested_role_env_prefix(
        result.argv, config_dir=OPERATOR_GROK_HOME, provider="grok"
    )


def test_launch_no_tmux_starts_with_env(tmp_path: Path) -> None:
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("implement me\n", encoding="utf-8")
    result = launch(
        role="implementer",
        vendor="grok",
        spec_file=str(spec),
        cwd=str(tmp_path),
        dry_run=True,
        tmux=False,
        config_home=tmp_path,
        session_id=DEFAULT_SESSION,
    )
    assert result.argv[0] == "env"
    assert "tmux" not in result.argv
    assert result.tmux_session is None
    assert OPERATOR_GROK_MODEL in result.argv


def test_launch_tmux_fake_runner_gets_wrapped_argv(tmp_path: Path) -> None:
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("implement me\n", encoding="utf-8")
    seen: list[list[str]] = []

    def fake(argv: list[str], stdin_text: str | None) -> object:
        seen.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="STATUS: complete\n", stderr="")

    result = launch(
        role="implementer",
        vendor="grok",
        spec_file=str(spec),
        cwd=str(tmp_path),
        runner=fake,
        config_home=tmp_path,
        session_id=DEFAULT_SESSION,
    )
    assert len(seen) == 1
    assert seen[0][:3] == ["tmux", "new-session", "-d"]
    assert result.status == "complete"


def _tmux_script(handler):
    calls: list[list[str]] = []

    def fake(argv: list[str]) -> CompletedProcess[str]:
        calls.append(list(argv))
        return handler(argv, calls)

    return fake, calls


def test_run_in_tmux_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(argv: list[str], _calls: list[list[str]]) -> CompletedProcess[str]:
        if argv[:2] == ["tmux", "new-session"]:
            return CompletedProcess(argv, 0, "", "")
        if "remain-on-exit" in argv:
            return CompletedProcess(argv, 0, "", "")
        if argv[-1] == "#{pane_dead}":
            return CompletedProcess(argv, 0, "1\n", "")
        if argv[-1] == "#{pane_dead_status}":
            return CompletedProcess(argv, 0, "0\n", "")
        if "capture-pane" in argv:
            return CompletedProcess(argv, 0, "STATUS: complete\n", "")
        if "kill-session" in argv:
            return CompletedProcess(argv, 0, "", "")
        return CompletedProcess(argv, 0, "", "")

    fake, calls = _tmux_script(handler)
    monkeypatch.setattr("agent_cli.lane._tmux_call", fake)
    result = _run_in_tmux(["grok", "--prompt-file", "s"], name="agent-lane-t", cwd="/w", stdin_text=None)
    assert result.returncode == 0
    assert "STATUS: complete" in result.stdout
    assert calls[0][:3] == ["tmux", "new-session", "-d"]
    assert any("kill-session" in c for c in calls)


@pytest.mark.parametrize("vendor", ["claude", "openrouter"])
def test_launch_tmux_stream_json_uses_output_file_not_pane_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, vendor: str
) -> None:
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("review this\n", encoding="utf-8")
    long_result = "x" * 2500 + "\nSTATUS: complete"
    stream = json.dumps({"type": "result", "result": long_result, "usage": {}})
    pane_result = json.dumps(
        {"type": "result", "result": "pane text\nSTATUS: partial", "usage": {}}
    )

    def handler(argv: list[str], _calls: list[list[str]]) -> CompletedProcess[str]:
        if argv[:2] == ["tmux", "new-session"]:
            output_path = next(
                Path(value)
                for value in argv
                if value.endswith(".txt")
                and Path(value).name.startswith(f"agent-lane-{vendor}-")
            )
            output_path.write_text(stream, encoding="utf-8")
            return CompletedProcess(argv, 0, "", "")
        if "remain-on-exit" in argv:
            return CompletedProcess(argv, 0, "", "")
        if argv[-1] == "#{pane_dead}":
            return CompletedProcess(argv, 0, "1\n", "")
        if argv[-1] == "#{pane_dead_status}":
            return CompletedProcess(argv, 0, "0\n", "")
        if "capture-pane" in argv:
            return CompletedProcess(argv, 0, pane_result, "")
        return CompletedProcess(argv, 0, "", "")

    fake, calls = _tmux_script(handler)
    monkeypatch.setattr("agent_cli.lane._tmux_call", fake)
    result = launch(
        role="reviewer",
        vendor=vendor,
        spec_file=str(spec),
        cwd=str(tmp_path),
        config_home=tmp_path,
        session_id=DEFAULT_SESSION,
    )
    assert result.status == "complete"
    assert result.stdout == long_result
    assert "pane text" not in result.stdout
    assert any("capture-pane" in call for call in calls)


def test_run_in_tmux_pane_dead_status_is_returncode(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(argv: list[str], _calls: list[list[str]]) -> CompletedProcess[str]:
        if argv[-1] == "#{pane_dead}":
            return CompletedProcess(argv, 0, "1\n", "")
        if argv[-1] == "#{pane_dead_status}":
            return CompletedProcess(argv, 0, "42\n", "")
        if "capture-pane" in argv:
            return CompletedProcess(argv, 0, "STATUS: partial\n", "")
        return CompletedProcess(argv, 0, "", "")

    fake, calls = _tmux_script(handler)
    monkeypatch.setattr("agent_cli.lane._tmux_call", fake)
    result = _run_in_tmux(["grok"], name="agent-lane-t", cwd="/w", stdin_text=None)
    assert result.returncode == 42
    assert any("kill-session" in c for c in calls)


def test_run_in_tmux_empty_pane_dead_status_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(argv: list[str], _calls: list[list[str]]) -> CompletedProcess[str]:
        if argv[-1] == "#{pane_dead}":
            return CompletedProcess(argv, 0, "1\n", "")
        if argv[-1] == "#{pane_dead_status}":
            return CompletedProcess(argv, 0, "\n", "")
        if "capture-pane" in argv:
            return CompletedProcess(argv, 0, "", "")
        return CompletedProcess(argv, 0, "", "")

    fake, _calls = _tmux_script(handler)
    monkeypatch.setattr("agent_cli.lane._tmux_call", fake)
    result = _run_in_tmux(["grok"], name="agent-lane-t", cwd="/w", stdin_text=None)
    assert result.returncode == 1


def test_run_in_tmux_kills_on_remain_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(argv: list[str], _calls: list[list[str]]) -> CompletedProcess[str]:
        if "remain-on-exit" in argv:
            return CompletedProcess(argv, 1, "", "no tmux")
        return CompletedProcess(argv, 0, "", "")

    fake, calls = _tmux_script(handler)
    monkeypatch.setattr("agent_cli.lane._tmux_call", fake)
    result = _run_in_tmux(["grok"], name="agent-lane-t", cwd="/w", stdin_text=None)
    assert result.returncode == 1
    assert any("kill-session" in c for c in calls)


def test_run_in_tmux_kills_on_pane_dead_query_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(argv: list[str], _calls: list[list[str]]) -> CompletedProcess[str]:
        if argv[-1] == "#{pane_dead}":
            return CompletedProcess(argv, 2, "", "gone")
        return CompletedProcess(argv, 0, "", "")

    fake, calls = _tmux_script(handler)
    monkeypatch.setattr("agent_cli.lane._tmux_call", fake)
    result = _run_in_tmux(["grok"], name="agent-lane-t", cwd="/w", stdin_text=None)
    assert result.returncode == 2
    assert any("kill-session" in c for c in calls)


def test_run_in_tmux_send_keys_adds_trailing_newline(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(argv: list[str], _calls: list[list[str]]) -> CompletedProcess[str]:
        if argv[-1] == "#{pane_dead}":
            return CompletedProcess(argv, 0, "1\n", "")
        if argv[-1] == "#{pane_dead_status}":
            return CompletedProcess(argv, 0, "0\n", "")
        if "capture-pane" in argv:
            return CompletedProcess(argv, 0, "STATUS: complete\n", "")
        return CompletedProcess(argv, 0, "", "")

    fake, calls = _tmux_script(handler)
    monkeypatch.setattr("agent_cli.lane._tmux_call", fake)
    _run_in_tmux(["codex"], name="agent-lane-t", cwd="/w", stdin_text="no-newline")
    typed = [c for c in calls if "send-keys" in c and "-l" in c]
    assert typed
    assert typed[0][-1].endswith("\n")


def test_launch_tmux_passes_absolute_spec_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_operator_ai_accounts(tmp_path)
    monkeypatch.chdir(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("implement me\n", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    result = launch(
        role="implementer",
        vendor="grok",
        spec_file="spec.md",
        cwd=str(work),
        dry_run=True,
        config_home=tmp_path,
        session_id=DEFAULT_SESSION,
    )
    inner = result.argv[result.argv.index("--") + 1 :]
    prompt = inner[inner.index("--prompt-file") + 1]
    assert Path(prompt).is_absolute()
    assert Path(prompt) == spec.resolve()
    assert result.argv[result.argv.index("-c") + 1] == str(work.resolve())
    inner_cwd = inner[inner.index("--cwd") + 1]
    assert inner_cwd == str(work.resolve())


def test_run_in_tmux_kills_on_send_keys_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(argv: list[str], _calls: list[list[str]]) -> CompletedProcess[str]:
        if "send-keys" in argv and "-l" in argv:
            return CompletedProcess(argv, 3, "", "no pane")
        return CompletedProcess(argv, 0, "", "")

    fake, calls = _tmux_script(handler)
    monkeypatch.setattr("agent_cli.lane._tmux_call", fake)
    result = _run_in_tmux(["codex"], name="agent-lane-t", cwd="/w", stdin_text="spec")
    assert result.returncode == 3
    assert any("kill-session" in c for c in calls)


def test_cli_lane_run_prints_vendor_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_operator_ai_accounts(tmp_path)
    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    spec = tmp_path / "spec.md"
    spec.write_text("review this\n", encoding="utf-8")
    seen: dict = {}

    def fake_launch(**kwargs):  # type: ignore[no-untyped-def]
        seen.update(kwargs)
        return LaneResult(
            role="pr-reviewer-quality",
            vendor="grok",
            model=OPERATOR_GROK_MODEL,
            status="complete",
            argv=["grok"],
            returncode=0,
            stdout="no quality findings, distinctive-marker-abc123\nSTATUS: complete\n",
            stderr="",
        )

    monkeypatch.setattr("agent_cli.main.launch", fake_launch)
    run(
        [
            "lane",
            "run",
            "--role",
            "pr-reviewer-quality",
            "--vendor",
            "grok",
            "--session",
            DEFAULT_SESSION,
            "--spec-file",
            str(spec),
            "--cwd",
            str(tmp_path),
            "--no-tmux",
        ]
    )
    out = capsys.readouterr().out
    assert "distinctive-marker-abc123" in out
    assert "STATUS=complete" in out
    assert out.index("distinctive-marker-abc123") < out.index("STATUS=complete")
    assert seen.get("session_id") == DEFAULT_SESSION
    assert seen.get("config_home") == tmp_path


def test_cli_lane_run_prints_vendor_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_operator_ai_accounts(tmp_path)
    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    spec = tmp_path / "spec.md"
    spec.write_text("review this\n", encoding="utf-8")

    def fake_launch(**kwargs):  # type: ignore[no-untyped-def]
        return LaneResult(
            role="pr-reviewer-quality",
            vendor="grok",
            model=OPERATOR_GROK_MODEL,
            status="unavailable",
            argv=["grok"],
            returncode=1,
            stdout="",
            stderr="grok: rate limited, distinctive-marker-xyz789",
        )

    monkeypatch.setattr("agent_cli.main.launch", fake_launch)
    with pytest.raises(SystemExit):
        run(
            [
                "lane",
                "run",
                "--role",
                "pr-reviewer-quality",
                "--vendor",
                "grok",
                "--session",
                DEFAULT_SESSION,
                "--spec-file",
                str(spec),
                "--cwd",
                str(tmp_path),
                "--no-tmux",
            ]
        )
    err = capsys.readouterr().err
    assert "distinctive-marker-xyz789" in err


def test_sanitize_lane_output_strips_escape_sequences_keeps_text() -> None:
    raw = "before\x1b[31mred\x1b[0m after\x07\ttab\nline2"
    cleaned = _sanitize_lane_output(raw)
    assert "\x1b" not in cleaned
    assert "\x07" not in cleaned
    assert "red" in cleaned and "after" in cleaned
    assert "\ttab\nline2" in cleaned


def test_sanitize_lane_output_strips_c1_control_bytes() -> None:
    raw = "before\x9b2Jafter"
    cleaned = _sanitize_lane_output(raw)
    assert "\x9b" not in cleaned
    assert "before" in cleaned and "after" in cleaned


def test_sanitize_lane_output_strips_exact_range_boundaries() -> None:
    stripped = "\x00\x08\x0b\x0c\x0e\x1f\x7f\x80\x9f"
    cleaned = _sanitize_lane_output(stripped)
    assert cleaned == ""

    kept = "\t\n\r"
    assert _sanitize_lane_output(kept) == kept


def test_cli_dry_run_implementer_grok(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_operator_ai_accounts(tmp_path)
    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    spec = tmp_path / "spec.md"
    spec.write_text("implement me\n", encoding="utf-8")
    run(
        [
            "lane",
            "run",
            "--role",
            "implementer",
            "--vendor",
            "grok",
            "--session",
            DEFAULT_SESSION,
            "--spec-file",
            str(spec),
            "--cwd",
            str(tmp_path),
            "--dry-run",
        ]
    )
    out = capsys.readouterr().out.strip()
    assert "tmux" in out
    assert "new-session" in out
    assert OPERATOR_GROK_MODEL in out
    assert f"GROK_HOME={OPERATOR_GROK_HOME}" in out
    assert "STATUS=" not in out


def test_cli_no_tmux_dry_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_operator_ai_accounts(tmp_path)
    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    spec = tmp_path / "spec.md"
    spec.write_text("implement me\n", encoding="utf-8")
    run(
        [
            "lane",
            "run",
            "--role",
            "implementer",
            "--vendor",
            "grok",
            "--session",
            DEFAULT_SESSION,
            "--spec-file",
            str(spec),
            "--cwd",
            str(tmp_path),
            "--dry-run",
            "--no-tmux",
        ]
    )
    out = capsys.readouterr().out.strip()
    assert out.startswith("env ")
    assert "new-session" not in out
    assert OPERATOR_GROK_MODEL in out
    assert f"GROK_HOME={OPERATOR_GROK_HOME}" in out


def test_cli_missing_session_dies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    spec = tmp_path / "spec.md"
    spec.write_text("x\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="--session is required"):
        run(
            [
                "lane",
                "run",
                "--role",
                "implementer",
                "--vendor",
                "grok",
                "--spec-file",
                str(spec),
            ]
        )


def test_cli_missing_spec_dies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_operator_ai_accounts(tmp_path)
    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    with pytest.raises(SystemExit, match="spec"):
        run(
            [
                "lane",
                "run",
                "--role",
                "implementer",
                "--vendor",
                "grok",
                "--session",
                DEFAULT_SESSION,
                "--spec-file",
                str(tmp_path / "missing.md"),
            ]
        )


def test_cli_unknown_vendor_dies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    spec = tmp_path / "spec.md"
    spec.write_text("x\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="vendor"):
        run(
            [
                "lane",
                "run",
                "--role",
                "implementer",
                "--vendor",
                "nope",
                "--session",
                DEFAULT_SESSION,
                "--spec-file",
                str(spec),
            ]
        )


def test_cli_unconfigured_lane_dies_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Configured accounts without lane execution must not start a vendor process."""
    write_operator_ai_accounts(tmp_path)
    raw = json.loads((tmp_path / "ai-accounts.json").read_text(encoding="utf-8"))
    raw.pop("lane_execution")
    (tmp_path / "ai-accounts.json").write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    spec = tmp_path / "spec.md"
    spec.write_text("implement\n", encoding="utf-8")
    called = {"n": 0}

    def boom(
        argv: list[str], stdin_text: str | None, cwd: str | None = None
    ) -> object:
        called["n"] += 1
        raise AssertionError("runner must not be called for unconfigured lane")

    monkeypatch.setattr("agent_cli.lane._default_runner", boom)
    # Do not mock launch: AccountError from real launch must surface via main.
    with pytest.raises(SystemExit, match=re.escape(LANE_EXECUTION_ERROR)):
        run(
            [
                "lane",
                "run",
                "--role",
                "implementer",
                "--vendor",
                "grok",
                "--session",
                DEFAULT_SESSION,
                "--spec-file",
                str(spec),
                "--cwd",
                str(tmp_path),
                "--no-tmux",
            ]
        )
    assert called["n"] == 0


def test_cli_unconfigured_models_die_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_operator_ai_accounts(tmp_path)
    raw = json.loads((tmp_path / "ai-accounts.json").read_text(encoding="utf-8"))
    raw.pop("models")
    (tmp_path / "ai-accounts.json").write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    spec = tmp_path / "spec.md"
    spec.write_text("implement\n", encoding="utf-8")
    called = {"n": 0}

    def boom(
        argv: list[str], stdin_text: str | None, cwd: str | None = None
    ) -> object:
        called["n"] += 1
        raise AssertionError("runner must not be called for unconfigured models")

    monkeypatch.setattr("agent_cli.lane._default_runner", boom)
    with pytest.raises(SystemExit, match=re.escape(MODELS_NOT_CONFIGURED)):
        run(
            [
                "lane",
                "run",
                "--role",
                "implementer",
                "--vendor",
                "grok",
                "--session",
                DEFAULT_SESSION,
                "--spec-file",
                str(spec),
                "--cwd",
                str(tmp_path),
                "--no-tmux",
            ]
        )
    assert called["n"] == 0
