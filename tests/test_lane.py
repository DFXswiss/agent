from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace

import pytest

from agent_cli.ai_accounts import LANE_EXECUTION_ERROR, AccountError
from agent_cli.coordinator_common import (
    REDACTION_BOUND_MARGIN,
    redact as redact_shared,
)
from agent_cli.errors import redact as redact_error
from agent_cli.lane import (
    CODEX_CHECK_LANE_REFUSED,
    LANE_GITHUB_TOKEN_ENV,
    LANE_STRIP_ENV,
    LANE_VENDORS,
    OPENROUTER_BASE_URL,
    LaneResult,
    LaneUsage,
    _OPENROUTER_WRAPPER,
    _run_in_tmux,
    claude_argv,
    codex_argv,
    finalize_lane_output,
    grok_argv,
    hide_config_dir,
    judgment_argv,
    launch,
    openrouter_argv,
    redact_diagnostic,
    redact_lane_text,
    record_lane_run,
    tmux_wrap_argv,
)
from agent_cli.main import main
from agent_cli.model_slots import MODELS_NOT_CONFIGURED
from agent_cli.text_sanitize import (
    redact_terminal_interleaved_secret,
    redact_terminal_interleaved_tokens,
    redact_terminal_interleaved_urlencoded_secret,
    sanitize_terminal_text,
)

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


def test_judgment_argv_grok_denies_every_tool() -> None:
    argv = judgment_argv(
        "grok", spec_file="/tmp/spec.md", cwd="/empty", model="m"
    )
    assert argv[argv.index("grok") :] == [
        "grok",
        "--prompt-file",
        "/tmp/spec.md",
        "-m",
        "m",
        "--deny",
        "Read",
        "--deny",
        "Grep",
        "--deny",
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
        "/empty",
    ]
    assert "--allow" not in argv
    for key in LANE_STRIP_ENV:
        assert argv[argv.index(key) - 1] == "-u"


def test_judgment_argv_claude_has_no_tools() -> None:
    argv = judgment_argv(
        "claude", spec_file="/tmp/spec.md", cwd="/empty", model="m"
    )
    assert argv[argv.index("--tools") + 1] == ""
    assert "--restricted" in argv
    assert argv[argv.index("--permission-mode") + 1] == "default"
    assert "--add-dir" not in argv


def test_judgment_argv_openrouter_has_no_tools() -> None:
    argv = judgment_argv(
        "openrouter",
        spec_file="/tmp/spec.md",
        cwd="/empty",
        model="m",
        output_file="/tmp/out",
    )
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index(_OPENROUTER_WRAPPER) + 2] == "/tmp/out"
    with pytest.raises(
        SystemExit, match="openrouter lane requires an output file"
    ):
        judgment_argv(
            "openrouter", spec_file="/tmp/spec.md", cwd="/empty", model="m"
        )


def test_judgment_argv_refuses_codex_and_unknown_vendors() -> None:
    with pytest.raises(SystemExit, match="codex cannot run a check lane") as exc:
        judgment_argv(
            "codex", spec_file="/tmp/spec.md", cwd="/empty", model="m"
        )
    assert str(exc.value) == CODEX_CHECK_LANE_REFUSED
    with pytest.raises(SystemExit, match="vendor must be"):
        judgment_argv(
            "gemini", spec_file="/tmp/spec.md", cwd="/empty", model="m"
        )


def test_tools_argument_replaces_the_tool_list() -> None:
    claude = claude_argv(
        cwd="/w", write=False, model="m", tools=("Read",)
    )
    assert claude[claude.index("--tools") + 1] == "Read"
    openrouter = openrouter_argv(
        cwd="/w",
        write=False,
        model="m",
        output_file="/o",
        tools=(),
    )
    assert openrouter[openrouter.index("--tools") + 1] == ""


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
    home_assignment = f"{home_key}={config_dir}"
    assert home_assignment in inner
    home_index = inner.index(home_assignment)
    role_env = max(
        index for index, value in enumerate(inner[:home_index]) if value == "env"
    )
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
        key_index = inner.index(key, role_env)
        assert inner[key_index - 1] == "-u"
    # Role env_prefix precedes the existing provider argv (which itself starts with env).
    provider_env = inner.index("env", home_index + 1)
    provider_bin = "grok" if provider == "grok" else "codex"
    assert provider_bin in inner[provider_env:]


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
    # The entire target starts with cwd=/work, so T4 stores it worktree-relative.
    assert out.tools == (
        {"tool": "Read", "target": "a.py", "error": False},
        {"tool": "Grep", "target": "[redacted]", "error": True},
        {"tool": "Grep", "target": "a.py", "error": False},
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
    split_link = f"{str(link)[:-4]}\x1b[31m{str(link)[-4:]}"
    split_real = f"{str(real)[:-4]}\x1b[31m{str(real)[-4:]}"
    assert hide_config_dir(
        f"error reading {split_link}/settings.json and {split_real}/key",
        str(link),
    ) == "error reading <config_dir>/settings.json and <config_dir>/key"
    assert hide_config_dir("unchanged", None) == "unchanged"


def test_config_dir_redaction_precedes_terminal_sanitization_for_lane_sinks() -> None:
    config_dir = "/opt/profiles/router"
    split_path = "/opt/profi\x1b[31mles/router/openrouter-api-key"
    raw = f"provider failed at {split_path}"

    assert hide_config_dir(raw, config_dir) == (
        "provider failed at <config_dir>/openrouter-api-key"
    )
    assert redact_diagnostic(raw, config_dir=config_dir) == (
        "provider failed at <config_dir>/openrouter-api-key"
    )

    event = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": raw,
    }
    finalized = finalize_lane_output(
        "openrouter",
        "",
        json.dumps(event),
        cwd="/work",
        config_dir=config_dir,
    )
    assert finalized.text == "provider failed at <config_dir>/openrouter-api-key"

    class CaptureStore:
        row: dict | None = None

        def write(self, _table: str, _operation: str, _row_id: str, row: dict) -> None:
            self.row = row

    store = CaptureStore()
    record_lane_run(
        store,
        session_id=DEFAULT_SESSION,
        result=LaneResult(
            role="implementer",
            vendor="openrouter",
            model="model",
            status="unavailable",
            argv=["claude"],
            returncode=2,
            stdout="",
            stderr=raw,
            config_dir=config_dir,
        ),
    )
    assert store.row is not None
    assert store.row["payload"]["final_text"] == (
        "provider failed at <config_dir>/openrouter-api-key"
    )


def test_controlled_line_config_dir_placeholder_exemption_is_exact() -> None:
    placeholder = "<config_dir>/openrouter-api-key"
    wrapped = "\x1b[31mordinary\x1b[0m"

    assert redact_lane_text(
        f"prefix {wrapped} {placeholder}", limit=None
    ) == f"prefix ordinary {placeholder}"
    assert redact_lane_text(
        f"prefix {wrapped} {placeholder}=hunter2", limit=None
    ) == "prefix ordinary [redacted]"
    assert redact_lane_text(
        f"prefix {wrapped} {placeholder} token=hunter2", limit=None
    ) == f"prefix ordinary {placeholder} [redacted]"


@pytest.mark.parametrize(
    "key",
    (
        "A" * 65 + "token",
        "token" + "A" * 65,
        "A" * 10_000 + "token",
        "token" + "A" * 10_000,
    ),
    ids=("65-prefix", "65-suffix", "10000-prefix", "10000-suffix"),
)
def test_assignment_redaction_has_no_secret_key_affix_limit(key: str) -> None:
    raw = f"{key}=credential-value visible"

    for cleaned in (
        redact_shared(raw, limit=None),
        redact_lane_text(raw, limit=None),
    ):
        assert "credential-value" not in cleaned
        assert "visible" in cleaned
        assert "[redacted]" in cleaned


def test_assignment_redaction_handles_json_keys_and_bounded_quoted_values() -> None:
    json_value = '{"prefix-token-suffix":"two word credential","visible":"ok"}'
    bounded_value = 'password="first ' + "secret-part " * 500 + '" visible'

    for redact_value in (
        lambda value, limit: redact_shared(value, limit=limit),
        lambda value, limit: redact_lane_text(value, limit=limit),
    ):
        cleaned_json = redact_value(json_value, None)
        assert "two word credential" not in cleaned_json
        assert '"visible":"ok"' in cleaned_json
        cleaned_bounded = redact_value(bounded_value, 2000)
        assert "first" not in cleaned_bounded
        assert "secret-part" not in cleaned_bounded
        assert cleaned_bounded.endswith("\n…[truncated]")


def test_shared_redaction_covers_quoted_json_keys_with_spaces_and_dots() -> None:
    raw = '{"access token":"s3cr3t","auth.token":"t0k3n","visible":"keep-me"}'
    for cleaned in (
        redact_shared(raw, limit=None),
        redact_lane_text(raw, limit=None),
    ):
        assert "s3cr3t" not in cleaned
        assert "t0k3n" not in cleaned
        assert '"visible":"keep-me"' in cleaned


def test_shared_redaction_covers_userinfo_after_a_long_scheme() -> None:
    raw = "s" * 65 + "://user:hunter2@localhost"
    for cleaned in (
        redact_shared(raw, limit=None),
        redact_lane_text(raw, limit=None),
    ):
        assert "hunter2" not in cleaned
        assert "://[redacted]@localhost" in cleaned


def _shared_lane_and_error_redactions(raw: str) -> tuple[str, str, str]:
    return (
        redact_shared(raw, limit=None),
        redact_lane_text(raw, limit=None),
        redact_error(raw),
    )


def test_shared_redaction_covers_a_secret_key_with_a_unicode_suffix() -> None:
    for cleaned in _shared_lane_and_error_redactions("token\u00e9=hunter2 visible"):
        assert cleaned == "[redacted] visible"


def test_shared_redaction_covers_a_single_quoted_key_with_other_characters() -> None:
    raw = "{'access token': 's3cr3t', 'visible': 'keep-me'}"
    for cleaned in _shared_lane_and_error_redactions(raw):
        assert cleaned == "{'access token': '[redacted]', 'visible': 'keep-me'}"


def test_shared_redaction_covers_a_quoted_key_followed_by_an_equals_sign() -> None:
    raw = '"access token"="s3cr3t" visible'
    for cleaned in _shared_lane_and_error_redactions(raw):
        assert cleaned == '"access token"="[redacted]" visible'


def test_shared_redaction_covers_an_unquoted_value_after_a_quoted_key() -> None:
    raw = '{"access token": 12345, "visible": "keep-me"}'
    for cleaned in _shared_lane_and_error_redactions(raw):
        assert cleaned == '{"access token": [redacted], "visible": "keep-me"}'


def test_shared_redaction_is_linear_on_a_large_unicode_key() -> None:
    raw = "token" + "\u00e9" * 1_000_000 + "=hunter2 visible"
    started = time.monotonic()
    results = _shared_lane_and_error_redactions(raw)
    elapsed = time.monotonic() - started
    assert results == ("[redacted] visible",) * 3
    assert elapsed < 2, f"redaction took {elapsed:.3f}s"


def test_assignment_redaction_handles_terminal_interleaved_quoted_keys() -> None:
    raw = '"pass\x1b[31mword"="first secret words" visible'

    for cleaned in (
        redact_shared(raw, limit=None),
        redact_lane_text(raw, limit=None),
    ):
        assert cleaned == "[redacted]"


@pytest.mark.parametrize(
    "control",
    (
        "\x1b]8;;https://example hidden\x07",
        "\x1bPopaque payload\x1b\\",
        "\x1b^opaque payload\x1b\\",
        "\x1b_opaque payload\x1b\\",
        "\x1bXopaque payload\x1b\\",
    ),
    ids=("osc", "dcs", "pm", "apc", "sos"),
)
@pytest.mark.parametrize("quoted", (False, True), ids=("unquoted", "quoted"))
@pytest.mark.parametrize(
    "placement",
    ("inside-key", "before-operator", "before-value", "inside-value"),
)
def test_assignment_redaction_classifies_around_raw_string_controls(
    control: str, quoted: bool, placement: str
) -> None:
    if placement == "inside-key":
        raw = (
            f'"pass{control}word"="hunter two" visible'
            if quoted
            else f"pass{control}word=hunter2 visible"
        )
    elif placement == "before-operator":
        raw = (
            f'"password"{control}:"hunter two" visible'
            if quoted
            else f"password{control}=hunter2 visible"
        )
    elif placement == "before-value":
        raw = (
            f'"password":{control} "hunter two" visible'
            if quoted
            else f"password={control} hunter2 visible"
        )
    else:
        raw = (
            f'"password":"hun{control}ter two" visible'
            if quoted
            else f"password=hun{control}ter2 visible"
        )

    for cleaned in (
        redact_shared(raw, limit=None),
        redact_lane_text(raw, limit=None),
    ):
        assert cleaned == "[redacted]"


def test_assignment_redaction_preserves_legacy_compact_token_shape() -> None:
    for redact_value in (
        lambda value: redact_shared(value, limit=None),
        lambda value: redact_lane_text(value, limit=None),
    ):
        assert redact_value("Mask password=value and token=other") == (
            "Mask [redacted] and [redacted]"
        )
        assert redact_value('password="two word value" visible') == (
            'password="[redacted]" visible'
        )
        assert redact_value('{"password":"two word value","visible":"ok"}') == (
            '{"password":"[redacted]","visible":"ok"}'
        )


@pytest.mark.parametrize(
    "raw",
    [
        "\x1bopaque-credential",
        "opaque\x1b-credential",
        "opaque-credential\x1b",
    ],
    ids=("start", "middle", "end"),
)
def test_lone_escape_redacts_tokens_across_lane_sinks(raw: str) -> None:
    assert redact_diagnostic(raw) == "[redacted]"
    finalized = finalize_lane_output("grok", raw, "", cwd="/work")
    assert finalized.text == "[redacted]"

    class CaptureStore:
        row: dict | None = None

        def write(self, _table: str, _operation: str, _row_id: str, row: dict) -> None:
            self.row = row

    store = CaptureStore()
    record_lane_run(
        store,
        session_id=DEFAULT_SESSION,
        result=LaneResult(
            role="implementer",
            vendor="grok",
            model="model",
            status="partial",
            argv=["grok"],
            returncode=0,
            stdout=raw,
            stderr="",
        ),
    )
    assert store.row is not None
    assert store.row["payload"]["final_text"] == "[redacted]"


def test_redact_diagnostic_hides_config_path_before_length_bound() -> None:
    config_dir = "/operator/very-secret-profile"
    diagnostic = (
        ("x" * 3920)
        + "<config_dir>/cache "
        + config_dir
        + "/settings.json"
    )

    redacted = redact_diagnostic(diagnostic, config_dir=config_dir)

    assert "/operator" not in redacted
    assert "very-secret-profile" not in redacted
    assert "<config-dir>" not in redacted
    assert "<config_dir>/cache" in redacted
    assert "<config_dir>/settings.json" in redacted


@pytest.mark.parametrize(
    ("raw", "contains_secret"),
    [
        ("m" * 1_000_000, False),
        ("api_key=model-secret-" + "m" * 1_000_000, True),
        ("\x1b[31m" + "m" * 1_000_000 + "\x1b[0m", False),
    ],
    ids=("separatorless-word", "keyword-prefix", "raw-control-wrapper"),
)
def test_public_shared_and_lane_redaction_are_linear_on_large_words(
    raw: str, contains_secret: bool
) -> None:
    calls = (
        ("shared", lambda: redact_shared(raw, limit=None)),
        ("lane", lambda: redact_lane_text(raw, limit=None)),
        ("diagnostic", lambda: redact_diagnostic(raw)),
    )

    for label, call in calls:
        started = time.monotonic()
        cleaned = call()
        elapsed = time.monotonic() - started
        assert elapsed < 2, f"{label} redaction took {elapsed:.3f}s"
        if contains_secret:
            assert "model-secret" not in cleaned
            assert "[redacted]" in cleaned


@pytest.mark.parametrize(
    "raw",
    [
        "m" * 1_000_000,
        "api_key=model-secret-" + "m" * 1_000_000,
    ],
    ids=("separatorless-word", "keyword-prefix"),
)
def test_public_terminal_redaction_helpers_are_linear_on_large_words(
    raw: str,
) -> None:
    calls = (
        ("tokens", lambda: redact_terminal_interleaved_tokens(raw)),
        (
            "exact",
            lambda: redact_terminal_interleaved_secret(
                raw, "api_key=model-secret-"
            ),
        ),
        (
            "urlencoded",
            lambda: redact_terminal_interleaved_urlencoded_secret(
                raw, "api_key=model-secret-"
            ),
        ),
        ("sanitize", lambda: sanitize_terminal_text(raw)),
    )

    for label, call in calls:
        started = time.monotonic()
        call()
        elapsed = time.monotonic() - started
        assert elapsed < 2, f"{label} redaction took {elapsed:.3f}s"


def test_bounded_lane_redaction_fails_closed_on_the_cut_token() -> None:
    cleaned = redact_lane_text(
        "visible " + "m" * 1_000_000,
        limit=100,
    )

    assert cleaned.startswith("visible [redacted]")
    assert cleaned.endswith("\n…[truncated]")
    assert len(cleaned) <= 100


def test_bounded_lane_redaction_classifies_complete_controlled_line_before_cut(
) -> None:
    limit = 100
    raw = "password hunter2 " + ("word " * 100) + "\x1b[31m"
    cutoff = limit + REDACTION_BOUND_MARGIN
    assert raw[cutoff].isspace()
    assert raw.index("\x1b") > cutoff

    cleaned = redact_lane_text(raw, limit=limit)

    assert "hunter2" not in cleaned
    assert cleaned.startswith("[redacted]")
    assert cleaned.endswith("\n…[truncated]")
    assert len(cleaned) <= limit


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


@pytest.mark.parametrize("target_kind", ("other", "inside", "relative"))
def test_tool_target_redacts_token_crossing_output_cut(
    tmp_path: Path, target_kind: str
) -> None:
    from agent_cli.lane import _tool_target

    worktree = tmp_path / "worktree"
    token = "sk-" + "A" * 32
    prefix = "v/" * 145
    if target_kind == "other":
        raw = prefix + token
        kind = "command"
    elif target_kind == "inside":
        raw = str(worktree / prefix / token)
        kind = "path"
    else:
        raw = prefix + token
        kind = "path"

    cleaned = _tool_target(raw, str(worktree), kind)

    assert "sk-" not in cleaned
    assert len(cleaned) <= 300


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


def test_finalize_lane_output_redacts_paths_in_glob_and_grep_patterns(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    (worktree / "src").mkdir(parents=True)
    events = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "glob-outside",
                        "name": "Glob",
                        "input": {"pattern": "/opt/openrouter-profile/openrouter-api-key"},
                    },
                    {
                        "type": "tool_use",
                        "id": "glob-escape",
                        "name": "Glob",
                        "input": {"pattern": "../outside/**/*.py"},
                    },
                    {
                        "type": "tool_use",
                        "id": "glob-wildcard-outside",
                        "name": "Glob",
                        "input": {"pattern": "/opt/**/*.json"},
                    },
                    {
                        "type": "tool_use",
                        "id": "glob-inside",
                        "name": "Glob",
                        "input": {"pattern": "src/**/*.py"},
                    },
                    {
                        "type": "tool_use",
                        "id": "glob-inside-absolute",
                        "name": "Glob",
                        "input": {"pattern": str(worktree / "src" / "*.py")},
                    },
                    {
                        "type": "tool_use",
                        "id": "grep-plain",
                        "name": "Grep",
                        "input": {"pattern": "secret=abc"},
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
        {"tool": "Glob", "target": "[redacted]", "error": False},
        {"tool": "Glob", "target": "[redacted]", "error": False},
        {"tool": "Glob", "target": "[redacted]", "error": False},
        {"tool": "Glob", "target": "[redacted]", "error": False},
        {"tool": "Glob", "target": "src/*.py", "error": False},
        {"tool": "Grep", "target": "[redacted]", "error": False},
    )


def test_tool_target_redacts_escaping_path_fragments_in_grep_pattern(
    tmp_path: Path,
) -> None:
    from agent_cli.lane import _tool_target

    worktree = tmp_path / "worktree"
    (worktree / "src").mkdir(parents=True)
    # Fail closed: one escaping path fragment redacts the complete Grep target
    # so surrounding regex or prose cannot leak host details.
    assert (
        _tool_target(
            "needle in /opt/openrouter-profile/key", str(worktree), "pattern"
        )
        == "[redacted]"
    )
    assert (
        _tool_target(
            "needle in C:\\Users\\someone\\key", str(worktree), "pattern"
        )
        == "[redacted]"
    )
    assert (
        _tool_target(
            "needle in ../../outside.txt", str(worktree), "pattern"
        )
        == "[redacted]"
    )
    assert (
        _tool_target(
            "needle in src/module.py", str(worktree), "pattern"
        )
        == "needle in src/module.py"
    )
    assert (
        _tool_target(
            "secret=abc", str(worktree), "pattern"
        )
        == "[redacted]"
    )


def test_tool_target_api_is_tool_independent() -> None:
    from inspect import signature

    from agent_cli.lane import _tool_target

    assert tuple(signature(_tool_target).parameters) == ("target", "cwd", "kind")


def test_tool_target_redacts_regex_paths_and_post_wildcard_glob_traversal(
    tmp_path: Path,
) -> None:
    from agent_cli.lane import _tool_target

    worktree = tmp_path / "worktree"
    (worktree / "src").mkdir(parents=True)

    assert (
        _tool_target(
            "^/opt/profiles/router/openrouter-api-key$",
            str(worktree),
            "pattern",
        )
        == "[redacted]"
    )
    assert (
        _tool_target(
            "^src/module.py$", str(worktree), "pattern"
        )
        == "^src/module.py$"
    )
    assert (
        _tool_target(
            "**/../../outside.txt", str(worktree), "pattern"
        )
        == "[redacted]"
    )


@pytest.mark.parametrize(
    "pattern",
    [
        "path=/opt/profiles/router/openrouter-api-key",
        "source=/opt/company/private-repo/config.json",
        "source='/opt/company/private-repo/config.json'",
        'source="/opt/company/private-repo/config.json"',
        "source:/opt/company/private-repo/config.json",
        "^path=/opt/profiles/router/openrouter-api-key$",
        "(?:/opt/profiles/router/openrouter-api-key)",
        r"path=\/opt/profiles/router/openrouter-api-key",
        "path=~/private/config.json",
        r"path=C:Users\someone\config.json",
        r"C:Users\someone\config.json",
        r"path=C:\Users\someone\config.json",
        "path=C:/Users/someone/config.json",
        r"path=C:\\Users\\someone\\config.json",
        r"path=C\:Users\someone\config\.json",
        r"path=C\:\\Users\\someone\\config\.json",
        r"path=\\\\server\\share\\config.json",
        "path=../../private.txt",
        "source=../outside/*.json",
        "source:(../../outside.json)",
        r"..\..\x",
        r"\.\.\/outside.json",
        r"path=\.\.\/outside.json",
        r"source:..\..\outside.json",
        "source=/opt/company/private-repo/*.json",
    ],
)
def test_tool_target_redacts_embedded_outside_path_fragments(
    tmp_path: Path, pattern: str
) -> None:
    from agent_cli.lane import _tool_target

    worktree = tmp_path / "worktree"
    worktree.mkdir()

    assert _tool_target(pattern, str(worktree), "pattern") == "[redacted]"


@pytest.mark.parametrize("target", ["key:value", "http:", "scheme:http:value"])
def test_tool_target_does_not_treat_ordinary_colon_text_as_drive_fragment(
    tmp_path: Path, target: str
) -> None:
    from agent_cli.lane import _tool_target

    worktree = tmp_path / "worktree"
    worktree.mkdir()

    assert _tool_target(target, str(worktree), "pattern") == target


@pytest.mark.parametrize(
    "target",
    [
        "path=../../private.txt",
        "source=../outside/*.json",
        "source:(../../outside.json)",
    ],
)
def test_tool_target_redacts_assigned_relative_traversal_paths(
    tmp_path: Path, target: str
) -> None:
    from agent_cli.lane import _tool_target

    worktree = tmp_path / "worktree"
    worktree.mkdir()

    assert _tool_target(target, str(worktree), "path") == "[redacted]"


def test_tool_target_redacts_embedded_absolute_path_even_inside_worktree(
    tmp_path: Path,
) -> None:
    from agent_cli.lane import _tool_target

    worktree = tmp_path / "worktree"
    (worktree / "src").mkdir(parents=True)
    pattern = f'source="{worktree / "src" / "module.py"}"'

    # The former partial behavior retained embedded worktree paths. The
    # fail-closed exception now applies only when the whole target is that path.
    assert _tool_target(pattern, str(worktree), "pattern") == "[redacted]"


@pytest.mark.parametrize("prefix", [";", "|", ")", ",", "'", '"', "=", ":"])
@pytest.mark.parametrize(
    "fragment", ["../private-repo/config.json", "/opt/private/config.json"]
)
def test_tool_target_redacts_traversal_and_absolute_fragments_after_any_prefix(
    tmp_path: Path, prefix: str, fragment: str
) -> None:
    from agent_cli.lane import _tool_target

    worktree = tmp_path / "worktree"
    worktree.mkdir()

    assert (
        _tool_target(f"needle{prefix}{fragment}", str(worktree), "pattern")
        == "[redacted]"
    )


@pytest.mark.parametrize(
    "target",
    [
        r"needle;..\private-repo\config.json",
        r"needle;\.\.\/private-repo/config.json",
        r"needle;\opt\private\config.json",
        r"needle;\/opt/private/config.json",
        "needle;\x1b[31m/opt/private/config.json",
    ],
)
def test_tool_target_redacts_backslash_and_regex_escaped_fragments(
    tmp_path: Path, target: str
) -> None:
    from agent_cli.lane import _tool_target

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    assert _tool_target(target, str(worktree), "pattern") == "[redacted]"


@pytest.mark.parametrize(
    ("kind", "target"),
    [
        ("pattern", r"path=C\x3aUsers/someone/config.json"),
        ("pattern", r"src\x2f\x2e\x2e\x2fprivate"),
        ("path", r"src\x2fprivate"),
        ("pattern", r"path\u002fprivate"),
        ("pattern", r"path\U0000002fprivate"),
        ("pattern", "path\\N{SOLIDUS}private"),
        ("pattern", r"path\0private"),
        ("pattern", r"path\7private"),
        ("pattern", r"path\cprivate"),
    ],
)
def test_tool_target_redacts_numeric_and_named_escapes(
    tmp_path: Path, kind: str, target: str
) -> None:
    from agent_cli.lane import _tool_target

    worktree = tmp_path / "worktree"
    worktree.mkdir()

    assert _tool_target(target, str(worktree), kind) == "[redacted]"


@pytest.mark.parametrize(
    ("kind", "target"),
    (
        ("path", "%2fhome%2falice%2f.config%2fsecret"),
        ("file_path", "%2fhome%2falice%2f.config%2fsecret"),
        ("pattern", "%2Fhome%2Falice%2F.config%2Fsecret"),
        ("path", "%2e%2e%2fprivate/config.json"),
        ("pattern", "src%2f%2e%2e%2fprivate/*.json"),
        ("path", "api%5fkey=hunter2"),
        ("pattern", "secr%65t=hunter2"),
    ),
    ids=(
        "encoded-path-lowercase",
        "encoded-file-path",
        "encoded-path-uppercase",
        "encoded-traversal",
        "encoded-pattern-traversal",
        "encoded-api-key",
        "encoded-secret",
    ),
)
def test_tool_target_redacts_percent_escaped_paths_traversal_and_secrets(
    tmp_path: Path, kind: str, target: str
) -> None:
    from agent_cli.lane import _tool_target

    worktree = tmp_path / "worktree"
    worktree.mkdir()

    assert _tool_target(target, str(worktree), kind) == "[redacted]"


def test_tool_target_docs_fail_closed_on_percent_escapes() -> None:
    root = Path(__file__).parents[1]
    ai_accounts = (root / "docs" / "ai-accounts.md").read_text(encoding="utf-8")
    coordinator = (root / "docs" / "issue-coordinator.md").read_text(
        encoding="utf-8"
    )
    readme = (root / "README.md").read_text(encoding="utf-8")

    assert "percent escape of the form `%HH`" in ai_accounts
    assert (
        "before\nnormalization, for both path and pattern targets"
        in ai_accounts
    )
    assert "percent escape of the form `%HH`" in coordinator
    assert "A `%HH` percent escape in a\npath or pattern target" in readme


def test_percent_escaped_tool_targets_are_redacted_in_lane_run() -> None:
    events = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "path",
                        "name": "Read",
                        "input": {
                            "file_path": "%2fhome%2falice%2f.config%2fsecret"
                        },
                    },
                    {
                        "type": "tool_use",
                        "id": "traversal",
                        "name": "Glob",
                        "input": {"pattern": "%2e%2e%2fprivate/*.json"},
                    },
                    {
                        "type": "tool_use",
                        "id": "secret",
                        "name": "Grep",
                        "input": {"pattern": "api%5fkey=hunter2"},
                    },
                ]
            },
        },
        {"type": "result", "result": "STATUS: complete", "usage": {}},
    ]
    finalized = finalize_lane_output(
        "claude",
        "\n".join(json.dumps(event) for event in events),
        "",
        cwd="/work",
    )

    class CaptureStore:
        row: dict | None = None

        def write(
            self, _table: str, _operation: str, _row_id: str, row: dict
        ) -> None:
            self.row = row

    store = CaptureStore()
    record_lane_run(
        store,
        session_id=DEFAULT_SESSION,
        result=LaneResult(
            role="implementer",
            vendor="claude",
            model="model",
            status="complete",
            argv=["claude"],
            returncode=0,
            stdout=finalized.text,
            stderr="",
            tools=finalized.tools,
        ),
    )

    assert store.row is not None
    assert [item["target"] for item in store.row["payload"]["tools"]] == [
        "[redacted]",
        "[redacted]",
        "[redacted]",
    ]


@pytest.mark.parametrize(
    "target",
    [
        "path=\x1b/opt/private/config.json",
        "path=\x1b[31C:/private/config.json",
        "path=\x1b[31~/private/config.json",
        "path=\x1b../private/config.json",
        "src/allowed\x08/hidden",
    ],
)
def test_tool_target_redacts_raw_control_bytes_before_path_classification(
    tmp_path: Path, target: str
) -> None:
    from agent_cli.lane import _tool_target

    worktree = tmp_path / "worktree"
    worktree.mkdir()

    assert _tool_target(target, str(worktree), "pattern") == "[redacted]"


def test_tool_target_only_relativizes_a_plain_whole_worktree_glob(tmp_path: Path) -> None:
    from agent_cli.lane import _tool_target

    worktree = tmp_path / "worktree"
    (worktree / "src").mkdir(parents=True)

    assert (
        _tool_target(
            str(worktree / "src" / "*.py"), str(worktree), "pattern"
        )
        == "src/*.py"
    )
    assert (
        _tool_target(
            f"{worktree}/src path=/opt/company/private-repo/config.json",
            str(worktree),
            "pattern",
        )
        == "[redacted]"
    )


def test_record_lane_run_bounds_and_redacts_both_models() -> None:
    class CaptureStore:
        row: dict | None = None

        def write(self, table: str, operation: str, row_id: str, row: dict) -> None:
            assert (table, operation, row_id) == ("activity", "insert", row["id"])
            self.row = row

    long_model = "vendor/" + "m" * 500
    result = LaneResult(
        role="implementer",
        vendor="openrouter",
        model=long_model,
        status="complete",
        argv=["claude"],
        returncode=0,
        stdout="STATUS: complete\n",
        stderr="",
        usage=LaneUsage(
            model="vendor/sk-or-v1-abcdefghijklmnopqrstuvwxyz012345",
            input_tokens=1,
            output_tokens=2,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            num_turns=1,
            duration_ms=1,
        ),
    )
    store = CaptureStore()
    record_lane_run(store, session_id=DEFAULT_SESSION, result=result)
    assert store.row is not None
    payload = store.row["payload"]
    assert len(payload["model"]) <= 200
    assert "sk-or-v1-abcdefghijklmnopqrstuvwxyz012345" not in payload["reported_model"]
    assert len(payload["reported_model"]) <= 200


def test_record_lane_run_removes_escape_sequence_before_secret_redaction() -> None:
    class CaptureStore:
        row: dict | None = None

        def write(self, _table: str, _operation: str, _row_id: str, row: dict) -> None:
            self.row = row

    split_secret = "sk-or-\x1b[31mv1-abcdefghijklmnopqrstuvwxyz012345"
    store = CaptureStore()
    record_lane_run(
        store,
        session_id=DEFAULT_SESSION,
        result=LaneResult(
            role="implementer",
            vendor="openrouter",
            model="model",
            status="complete",
            argv=["claude"],
            returncode=0,
            stdout=f"provider output {split_secret}\nSTATUS: complete",
            stderr="",
        ),
    )

    assert store.row is not None
    final_text = store.row["payload"]["final_text"]
    assert "sk-or-" not in final_text
    assert "[31m" not in final_text
    assert "[redacted]" in final_text


def test_raw_redaction_precedes_terminal_sanitization_for_diagnostics_and_lane_evidence() -> None:
    class CaptureStore:
        row: dict | None = None

        def write(self, _table: str, _operation: str, _row_id: str, row: dict) -> None:
            self.row = row

    split_token = "github_\x1b[31pat_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
    diagnostic = redact_diagnostic(f"provider rejected {split_token}")
    assert "github_" not in diagnostic
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345" not in diagnostic
    assert "[redacted]" in diagnostic

    store = CaptureStore()
    record_lane_run(
        store,
        session_id=DEFAULT_SESSION,
        result=LaneResult(
            role="implementer",
            vendor="openrouter",
            model=split_token,
            status="complete",
            argv=["claude"],
            returncode=0,
            stdout=f"provider output {split_token}\nSTATUS: complete",
            stderr="",
            usage=LaneUsage(
                model=split_token,
                input_tokens=1,
                output_tokens=1,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
                num_turns=1,
                duration_ms=1,
            ),
            tools=({"tool": split_token, "target": "src/module.py", "error": False},),
        ),
    )

    assert store.row is not None
    payload = store.row["payload"]
    for value in (
        payload["model"],
        payload["reported_model"],
        payload["final_text"],
        payload["tools"][0]["tool"],
    ):
        assert "github_" not in value
        assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345" not in value
        assert "[redacted]" in value


def test_lane_diagnostic_redacts_any_token_split_by_an_interior_csi() -> None:
    split_tokens = (
        "ey\x1b[31JhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.abc_def-ghi",
        "use\x1b[31Jr@example.com",
        "0123456789\x1b[31abcdefABCD",
        "AKI\x9b31AIOSFODNN7EXAMPLE",
    )

    assert redact_diagnostic("\t".join(split_tokens)) == "\t".join(
        ["[redacted]"] * 4
    )


@pytest.mark.parametrize(
    "raw",
    (
        "\x1b[31A123456789012345678F",
        "A123456789012345678\x1b[31F",
    ),
    ids=("start-boundary", "end-boundary"),
)
def test_lane_diagnostic_redacts_20_hex_token_with_boundary_csi(raw: str) -> None:
    assert redact_diagnostic(raw) == "[redacted]"


def test_lane_diagnostic_keeps_strict_sgr_wrapper_around_whole_word() -> None:
    raw = "\x1b[31mERROR\x1b[0m visible"

    assert redact_diagnostic(raw) == "ERROR visible"


def test_lane_diagnostic_redacts_sgr_wrapper_with_glued_suffix() -> None:
    assert redact_diagnostic("\x1b[31mERROR\x1b[0m:") == "[redacted]"


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
                    "result": "STATUS: complete\nRESULT: done",
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
    assert result.stdout == "STATUS: complete\nRESULT: done"
    assert result.status == "complete"
    assert result.usage is not None
    assert result.usage.model == OPERATOR_OPENROUTER_MODEL
    assert result.usage.input_tokens == 7


def test_launch_openrouter_invalid_utf8_returns_unavailable_and_removes_output(
    tmp_path: Path,
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
    output_paths: list[Path] = []

    def fake(argv: list[str], stdin_text: str | None) -> object:
        wrapper_index = next(
            i for i, value in enumerate(argv) if "openrouter-api-key" in value
        )
        output_file = Path(argv[wrapper_index + 2])
        output_paths.append(output_file)
        output_file.write_bytes(b"\xff")
        assert stdin_text == "implement\n"
        return SimpleNamespace(returncode=0, stdout="", stderr="")

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

    assert result.status == "unavailable"
    assert result.stdout == ""
    assert "lane output file is not valid UTF-8" in result.stderr
    assert output_paths and not output_paths[0].exists()


def test_launch_openrouter_unreadable_output_returns_unavailable(
    tmp_path: Path,
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

    def deleted_output(argv: list[str], stdin_text: str | None) -> object:
        wrapper_index = next(
            i for i, value in enumerate(argv) if "openrouter-api-key" in value
        )
        Path(argv[wrapper_index + 2]).unlink()
        return SimpleNamespace(
            returncode=0,
            stdout="",
            stderr="Authorization: Bearer sk-or-v1-abcdefghijklmnopqrstuvwxyz012345\n",
        )

    result = launch(
        role="implementer",
        vendor="openrouter",
        spec_file=str(spec),
        cwd=str(worktree),
        runner=deleted_output,
        tmux=False,
        config_home=tmp_path,
        session_id=DEFAULT_SESSION,
    )

    assert result.status == "unavailable"
    assert result.stdout == ""
    assert "lane output file is unavailable" in result.stderr
    assert "sk-or-v1-abcdefghijklmnopqrstuvwxyz012345" not in result.stderr
    assert "Bearer sk-or" not in result.stderr
    assert len(result.stderr) <= 4000


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


_PROXY_AND_CA_VARS = (
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
    "NODE_EXTRA_CA_CERTS",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
)


def test_lane_strip_env_contains_proxy_and_ca_overrides() -> None:
    for key in _PROXY_AND_CA_VARS:
        assert key in LANE_STRIP_ENV


@pytest.mark.parametrize("vendor", LANE_VENDORS)
def test_every_provider_unsets_proxy_and_ca_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, vendor: str
) -> None:
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("implement\n", encoding="utf-8")
    proxy = "http://127.0.0.1:8123"
    ca = "/ambient/intercept-ca.pem"
    for key in _PROXY_AND_CA_VARS:
        monkeypatch.setenv(key, ca if "CERT" in key or "BUNDLE" in key else proxy)

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

    inner = result.argv[result.argv.index("--") + 1 :] if "--" in result.argv else result.argv
    # Find the first "env -u ..." run in the argv; the role prefix comes first.
    first_env = inner.index("env")
    unset = {
        inner[i + 1]
        for i in range(first_env, len(inner))
        if inner[i] == "-u"
    }
    for key in _PROXY_AND_CA_VARS:
        assert key in unset, key
    assert proxy not in result.argv
    assert ca not in result.argv


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
        Path(out_path).write_text(
            "STATUS: complete\nRESULT: done\n", encoding="utf-8"
        )
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


@pytest.mark.parametrize("vendor", ["codex", "claude"])
@pytest.mark.parametrize("tmux", [False, True])
def test_launch_sanitizes_github_environment_for_injected_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    vendor: str,
    tmux: bool,
) -> None:
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("implement without GitHub credentials\n", encoding="utf-8")
    for key in LANE_GITHUB_TOKEN_ENV:
        monkeypatch.setenv(key, f"ambient-{key.lower()}")
    monkeypatch.setenv("GH_CONFIG_DIR", "/ambient/github-profile")
    profiles: list[Path] = []

    def fake(argv: list[str], _stdin_text: str | None) -> object:
        assignments = [
            item for item in argv if item.startswith("GH_CONFIG_DIR=")
        ]
        assert len(assignments) == 1
        profile = Path(assignments[0].split("=", 1)[1])
        profiles.append(profile)
        assert profile.is_dir()
        assert list(profile.iterdir()) == []
        for key in LANE_GITHUB_TOKEN_ENV:
            assert any(
                argv[index : index + 2] == ["-u", key]
                for index in range(len(argv) - 1)
            )
        assert "/ambient/github-profile" not in argv
        if vendor == "codex":
            output = Path(argv[argv.index("--output-last-message") + 1])
            output.write_text(
                "STATUS: complete\nRESULT: done\n", encoding="utf-8"
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    launch(
        role="implementer",
        vendor=vendor,
        spec_file=str(spec),
        cwd=str(tmp_path),
        runner=fake,
        tmux=tmux,
        config_home=tmp_path,
        session_id=DEFAULT_SESSION,
    )

    assert len(profiles) == 1
    assert not profiles[0].exists()
    for key in LANE_GITHUB_TOKEN_ENV:
        assert os.environ[key] == f"ambient-{key.lower()}"
    assert os.environ["GH_CONFIG_DIR"] == "/ambient/github-profile"


def test_launch_sanitizes_github_environment_for_default_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("implement without GitHub credentials\n", encoding="utf-8")
    for key in LANE_GITHUB_TOKEN_ENV:
        monkeypatch.setenv(key, f"ambient-{key.lower()}")
    monkeypatch.setenv("GH_CONFIG_DIR", "/ambient/github-profile")
    profiles: list[Path] = []

    def fake_default(
        argv: list[str], _stdin_text: str | None, _cwd: str | None
    ) -> object:
        assignment = next(
            item for item in argv if item.startswith("GH_CONFIG_DIR=")
        )
        profile = Path(assignment.split("=", 1)[1])
        profiles.append(profile)
        assert profile.is_dir()
        assert list(profile.iterdir()) == []
        for key in LANE_GITHUB_TOKEN_ENV:
            assert any(
                argv[index : index + 2] == ["-u", key]
                for index in range(len(argv) - 1)
            )
        output = Path(argv[argv.index("--output-last-message") + 1])
        output.write_text("STATUS: complete\nRESULT: done\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("agent_cli.lane._default_runner", fake_default)
    launch(
        role="implementer",
        vendor="codex",
        spec_file=str(spec),
        cwd=str(tmp_path),
        tmux=False,
        config_home=tmp_path,
        session_id=DEFAULT_SESSION,
    )

    assert len(profiles) == 1
    assert not profiles[0].exists()


def test_launch_no_tmux_claude_keeps_stream_json_on_stdout(tmp_path: Path) -> None:
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    contents = "claude please review\n"
    spec.write_text(contents, encoding="utf-8")
    stream = json.dumps(
        {
            "type": "result",
            "result": "reviewed\nSTATUS: complete\nRESULT: approved",
            "usage": {},
        }
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
    assert result.stdout == "reviewed\nSTATUS: complete\nRESULT: approved"


def test_launch_codex_unreadable_output_returns_unavailable_not_empty(
    tmp_path: Path,
) -> None:
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("codex please implement\n", encoding="utf-8")

    deleted_paths: list[Path] = []

    def deletes_output(argv: list[str], stdin_text: str | None) -> object:
        output_path = Path(argv[argv.index("--output-last-message") + 1])
        deleted_paths.append(output_path)
        output_path.unlink()
        return SimpleNamespace(returncode=0, stdout="", stderr="codex wrote nothing\n")

    result = launch(
        role="implementer",
        vendor="codex",
        spec_file=str(spec),
        cwd=str(tmp_path),
        runner=deletes_output,
        tmux=False,
        config_home=tmp_path,
        session_id=DEFAULT_SESSION,
    )

    assert result.status == "unavailable"
    assert result.stdout == ""
    assert "codex wrote nothing" in result.stderr
    assert "lane output file is unavailable" in result.stderr
    assert deleted_paths
    assert str(deleted_paths[0]) not in result.stderr
    assert "FileNotFoundError" not in result.stderr


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
        return FakeDone(0, "STATUS: complete\nRESULT: done\n", "")

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
        return SimpleNamespace(
            returncode=0,
            stdout="STATUS: complete\nRESULT: done\n",
            stderr="",
        )

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


def test_run_in_tmux_joins_soft_wrapped_secret_before_lane_redaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CaptureStore:
        row: dict | None = None

        def write(self, _table: str, _operation: str, _row_id: str, row: dict) -> None:
            self.row = row

    token = "sk-or-v1-abcdefghijklmnopqrstuvwxyz012345"

    def handler(argv: list[str], _calls: list[list[str]]) -> CompletedProcess[str]:
        if argv[-1] == "#{pane_dead}":
            return CompletedProcess(argv, 0, "1\n", "")
        if argv[-1] == "#{pane_dead_status}":
            return CompletedProcess(argv, 0, "0\n", "")
        if "capture-pane" in argv:
            captured = token if "-J" in argv else token[:25] + "\n" + token[25:]
            return CompletedProcess(argv, 0, f"credential={captured}\n", "")
        return CompletedProcess(argv, 0, "", "")

    fake, calls = _tmux_script(handler)
    monkeypatch.setattr("agent_cli.lane._tmux_call", fake)

    captured = _run_in_tmux(
        ["grok"], name="agent-lane-t", cwd="/w", stdin_text=None
    )
    diagnostic = redact_diagnostic(captured.stdout)
    store = CaptureStore()
    record_lane_run(
        store,
        session_id=DEFAULT_SESSION,
        result=LaneResult(
            role="implementer",
            vendor="grok",
            model="model",
            status="unavailable",
            argv=list(captured.args),
            returncode=captured.returncode,
            stdout=captured.stdout,
            stderr=captured.stderr,
        ),
    )

    capture_call = next(call for call in calls if "capture-pane" in call)
    assert "-J" in capture_call
    assert token not in diagnostic
    assert "sk-or-v1-" not in diagnostic
    assert "qrstuvwxyz012345" not in diagnostic
    assert "[redacted]" in diagnostic
    assert store.row is not None
    evidence = store.row["payload"]["final_text"]
    assert token not in evidence
    assert "sk-or-v1-" not in evidence
    assert "qrstuvwxyz012345" not in evidence
    assert "[redacted]" in evidence


@pytest.mark.parametrize("vendor", ["claude", "openrouter"])
def test_launch_tmux_stream_json_uses_output_file_not_pane_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, vendor: str
) -> None:
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("review this\n", encoding="utf-8")
    long_result = "x" * 2500 + "\nSTATUS: complete\nRESULT: approved"
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


def test_cli_lane_run_invalid_openrouter_output_is_a_controlled_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_operator_ai_accounts(tmp_path)
    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    spec = tmp_path / "spec.md"
    spec.write_text("implement\n", encoding="utf-8")
    output_paths: list[Path] = []

    def invalid_output(
        argv: list[str], stdin_text: str | None, cwd: str | None = None
    ) -> object:
        wrapper_index = next(
            i for i, value in enumerate(argv) if "openrouter-api-key" in value
        )
        output_file = Path(argv[wrapper_index + 2])
        output_paths.append(output_file)
        output_file.write_bytes(b"\xff")
        assert stdin_text == "implement\n"
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    class DummyStore:
        def write(
            self, table: str, operation: str, row_id: str, row: dict
        ) -> None:
            assert (table, operation, row_id) == ("activity", "insert", row["id"])

        def close(self) -> None:
            pass

    monkeypatch.setattr("agent_cli.lane._default_runner", invalid_output)
    monkeypatch.setattr("agent_cli.main.open_store", lambda: DummyStore())
    with pytest.raises(SystemExit) as exc:
        run(
            [
                "lane",
                "run",
                "--role",
                "implementer",
                "--vendor",
                "openrouter",
                "--session",
                DEFAULT_SESSION,
                "--spec-file",
                str(spec),
                "--cwd",
                str(tmp_path),
                "--no-tmux",
            ]
        )

    captured = capsys.readouterr()
    assert exc.value.code == 2
    assert "STATUS=unavailable" in captured.out
    assert "lane output file is not valid UTF-8" in captured.err
    assert output_paths and not output_paths[0].exists()


def test_main_has_no_dead_lane_sanitizer_wrapper() -> None:
    import agent_cli.main as main_module

    assert not hasattr(main_module, "_sanitize_lane_output")


def test_sanitize_terminal_text_strips_escape_sequences_keeps_text() -> None:
    raw = "before\x1b[31mred\x1b[0m after\x07\ttab\nline2"
    cleaned = sanitize_terminal_text(raw)
    assert "\x1b" not in cleaned
    assert "[31m" not in cleaned and "[0m" not in cleaned
    assert "\x07" not in cleaned
    assert "red" in cleaned and "after" in cleaned
    assert "\ttab\nline2" in cleaned


def test_sanitize_terminal_text_strips_c1_control_bytes() -> None:
    raw = "before\x9b2Jafter"
    cleaned = sanitize_terminal_text(raw)
    assert "\x9b" not in cleaned
    assert "2J" not in cleaned
    assert "before" in cleaned and "after" in cleaned


def test_sanitize_terminal_text_strips_exact_range_boundaries() -> None:
    stripped = "\x00\x08\x0b\x0c\x0e\x1f\x7f\x80\x9f"
    cleaned = sanitize_terminal_text(stripped)
    assert cleaned == ""

    kept = "\t\n"
    assert sanitize_terminal_text(kept) == kept
    assert sanitize_terminal_text("\r") == ""


def test_sanitize_terminal_text_removes_complete_osc_and_string_sequences() -> None:
    raw = "a\x1b]0;private title\x07b\x1bPprivate payload\x1b\\c"
    assert sanitize_terminal_text(raw) == "abc"


@pytest.mark.parametrize("introducer", ["\x1bP", "\x1b^", "\x1b_", "\x1bX"])
@pytest.mark.parametrize("terminator", ["\x9c", "\x1b\\"])
def test_sanitize_terminal_text_removes_7_bit_string_controls_with_either_st(
    introducer: str, terminator: str
) -> None:
    raw = f"before{introducer}private payload{terminator}after"
    assert sanitize_terminal_text(raw) == "beforeafter"


@pytest.mark.parametrize("introducer", ["\x1bP", "\x1b^", "\x1b_", "\x1bX"])
def test_sanitize_terminal_text_removes_unterminated_string_control_to_end(
    introducer: str,
) -> None:
    assert sanitize_terminal_text(f"before{introducer}private payload") == "before"


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
