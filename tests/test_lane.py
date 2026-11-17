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

from agent_cli.ai_accounts import (
    LANE_EXECUTION_ERROR,
    AccountError,
    LaneExecution,
)
from agent_cli.coordinator_common import (
    REDACTION_BOUND_MARGIN,
    redact as redact_shared,
)
from agent_cli.drivers import (
    JUDGMENT_UNSUPPORTED,
    OUTPUT_FILE_REQUIRED,
    WRITE_READ_DIRS_REFUSED,
    credential_wrapper,
    lane_driver_ids,
    lane_io,
    lane_scrub_env,
)
from agent_cli.errors import redact as redact_error
from agent_cli.lane import (
    LANE_GITHUB_TOKEN_ENV,
    LaneResult,
    LaneUsage,
    _run_in_tmux,
    delegated_lane_argv,
    finalize_lane_output,
    hide_config_dir,
    launch,
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
    redact_terminal_string_controls,
    sanitize_terminal_text,
)
from driver_fixtures import (
    accounts_document,
    driver_specs,
    provider_argv,
    provider_judgment_argv as judgment_argv,
    write_accounts as write_placeholder_accounts,
    write_drivers,
)
from test_coordinator_support import event_stream_output

pytestmark = pytest.mark.no_pg

DRIVERS = driver_specs()
# Every variable a lane clears under the placeholder drivers, in launch order.
LANE_STRIP_ENV = lane_scrub_env(DRIVERS)
LANE_VENDORS = lane_driver_ids(DRIVERS)
DRIVER_D_BASE_URL = dict(DRIVERS["driver-d"].lane.env)["TOOL_C_BASE_URL"]
_DRIVER_D_WRAPPER = credential_wrapper(DRIVERS["driver-d"])

OPERATOR_MODEL_A = "operator-lane-driver-a-model"
OPERATOR_MODEL_B = "operator-lane-driver-b-model"
OPERATOR_HOME_A = "/operator/path/driver-a-lane-a"
OPERATOR_HOME_B = "/operator/path/driver-b-lane-a"
OPERATOR_HOME_C = "/operator/path/driver-c-lane-a"
OPERATOR_MODEL_C = "operator-lane-driver-c-model"
OPERATOR_HOME_D = "/operator/path/driver-d-lane-a"
OPERATOR_MODEL_D = "vendor/example"
OPERATOR_HOME_A2 = "/operator/path/driver-a-lane-b"
OPERATOR_MODEL_A2 = "operator-lane-driver-a-model-b"
DEFAULT_SESSION = "sess-1"
ALT_SESSION = "sess-2"


def test_judgment_argv_uses_the_judgment_template() -> None:
    argv = judgment_argv(
        "driver-a", spec_file="/tmp/spec.md", cwd="/empty", model="m"
    )
    assert argv[argv.index("tool-a") :] == [
        "tool-a",
        "--prompt-file",
        "/tmp/spec.md",
        "-m",
        "m",
        "--no-tools",
        "--cwd",
        "/empty",
    ]
    assert "--read-only" not in argv
    assert "--edit" not in argv
    for key in LANE_STRIP_ENV:
        assert argv[argv.index(key) - 1] == "-u"


def test_judgment_argv_driver_c_has_no_tools() -> None:
    argv = judgment_argv(
        "driver-c", spec_file="/tmp/spec.md", cwd="/empty", model="m"
    )
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--mode") + 1] == "default"
    assert "--add-dir" not in argv


def test_judgment_argv_driver_d_has_no_tools() -> None:
    argv = judgment_argv(
        "driver-d",
        spec_file="/tmp/spec.md",
        cwd="/empty",
        model="m",
        output_file="/tmp/out",
    )
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index(_DRIVER_D_WRAPPER) + 2] == "/tmp/out"
    with pytest.raises(SystemExit) as exc:
        judgment_argv(
            "driver-d", spec_file="/tmp/spec.md", cwd="/empty", model="m"
        )
    assert str(exc.value) == OUTPUT_FILE_REQUIRED.format(driver="driver-d")


def test_judgment_argv_refuses_a_driver_without_a_judgment_template() -> None:
    with pytest.raises(SystemExit, match="cannot run a check lane") as exc:
        judgment_argv(
            "driver-b", spec_file="/tmp/spec.md", cwd="/empty", model="m"
        )
    assert str(exc.value) == JUDGMENT_UNSUPPORTED.format(driver="driver-b")


def write_operator_ai_accounts(
    home: Path,
    *,
    sessions: dict | None = None,
    lane_execution: dict | None = None,
) -> None:
    """Write an operator-supplied ai-accounts.json for positive lane scenarios.

    Installation defaults stay empty. Callers must invoke this explicitly for
    positive cases; negative tests leave the home unconfigured.
    """
    data = {
        "lane_execution": (
            lane_execution or {"mode": "this-device-is-lane-host"}
        ),
        "models": {"first": "driver-a", "second": "driver-b"},
        "accounts": {
            "account-a": {"provider": "driver-a", "config_dir": OPERATOR_HOME_A},
            "account-b": {"provider": "driver-b", "config_dir": OPERATOR_HOME_B},
            "account-c": {"provider": "driver-c", "config_dir": OPERATOR_HOME_C},
            "account-a2": {"provider": "driver-a", "config_dir": OPERATOR_HOME_A2},
            "account-d": {
                "provider": "driver-d", "config_dir": OPERATOR_HOME_D
            },
        },
        "roles": {
            "lane-builder": {
                "account": "account-a",
                "model": OPERATOR_MODEL_A,
                "access": "workspace-write",
            },
            "lane-reader": {
                "account": "account-a",
                "model": OPERATOR_MODEL_A,
                "access": "read-only",
            },
            "driver-b-builder": {
                "account": "account-b",
                "model": OPERATOR_MODEL_B,
                "access": "workspace-write",
            },
            "driver-b-reader": {
                "account": "account-b",
                "model": OPERATOR_MODEL_B,
                "access": "read-only",
            },
            "driver-c-builder": {
                "account": "account-c",
                "model": OPERATOR_MODEL_C,
                "access": "workspace-write",
            },
            "driver-c-reader": {
                "account": "account-c",
                "model": OPERATOR_MODEL_C,
                "access": "read-only",
            },
            "lane-builder-b": {
                "account": "account-a2",
                "model": OPERATOR_MODEL_A2,
                "access": "workspace-write",
            },
            "driver-d-builder": {
                "account": "account-d",
                "model": OPERATOR_MODEL_D,
                "access": "workspace-write",
            },
            "driver-d-reader": {
                "account": "account-d",
                "model": OPERATOR_MODEL_D,
                "access": "read-only",
            },
        },
        "sessions": sessions
        or {
            DEFAULT_SESSION: {
                "interactive": "lane-builder",
                "lanes": {
                    "driver-a:implementer": "lane-builder",
                    "driver-a:reviewer": "lane-reader",
                    "driver-a:pr-reviewer-quality": "lane-reader",
                    "driver-a:pr-reviewer-logic": "lane-reader",
                    "driver-b:implementer": "driver-b-builder",
                    "driver-b:reviewer": "driver-b-reader",
                    "driver-b:pr-reviewer-quality": "driver-b-reader",
                    "driver-b:pr-reviewer-logic": "driver-b-reader",
                    "driver-c:implementer": "driver-c-builder",
                    "driver-c:reviewer": "driver-c-reader",
                    "driver-c:pr-reviewer-quality": "driver-c-reader",
                    "driver-c:pr-reviewer-logic": "driver-c-reader",
                    "driver-d:implementer": "driver-d-builder",
                    "driver-d:reviewer": "driver-d-reader",
                    "driver-d:pr-reviewer-quality": "driver-d-reader",
                    "driver-d:pr-reviewer-logic": "driver-d-reader",
                },
            },
            ALT_SESSION: {
                "interactive": "lane-builder-b",
                "lanes": {
                    "driver-a:implementer": "lane-builder-b",
                },
            },
        },
    }
    write_drivers(home)
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
    # A tmux stream redirect wrapper may precede the provider command.
    inner = inner[inner.index("env") :]
    assert inner[0] == "env"
    home_key = DRIVERS[provider].home_env
    home_assignment = f"{home_key}={config_dir}"
    assert home_assignment in inner
    home_index = inner.index(home_assignment)
    role_env = max(
        index for index, value in enumerate(inner[:home_index]) if value == "env"
    )
    for key in (
        "TOOL_A_API_KEY",
        "TOOL_B_API_KEY",
        "TOOL_C_API_KEY",
        "TOOL_C_TOKEN",
        "TOOL_D_API_KEY",
        "TOOL_A_HOME",
        "TOOL_B_HOME",
        "TOOL_C_CONFIG",
    ):
        assert key in inner
        key_index = inner.index(key, role_env)
        assert inner[key_index - 1] == "-u"
    # Role env_prefix precedes the existing provider argv (which itself starts with env).
    provider_env = inner.index("env", home_index + 1)
    provider_bin = DRIVERS[provider].lane.argv_read[0]
    assert provider_bin in inner[provider_env:]


def test_driver_a_implementer_argv_requires_explicit_model() -> None:
    argv = provider_argv(
        "driver-a",
        spec_file="/tmp/spec.md",
        cwd="/work",
        write=True,
        model="explicit-model-a",
    )
    assert argv[0] == "env"
    for key in LANE_STRIP_ENV:
        assert argv[argv.index(key) - 1] == "-u"
    assert argv[argv.index("tool-a") :] == [
        "tool-a",
        "--prompt-file",
        "/tmp/spec.md",
        "-m",
        "explicit-model-a",
        "--edit",
        "--cwd",
        "/work",
    ]
    strip_idx = [argv.index(k) for k in LANE_STRIP_ENV]
    assert strip_idx == sorted(strip_idx)


def test_driver_a_reviewer_argv_requires_explicit_model() -> None:
    argv = provider_argv(
        "driver-a",
        spec_file="/tmp/spec.md",
        cwd="/work",
        write=False,
        model="explicit-readonly-model",
    )
    assert "--edit" not in argv
    assert argv[argv.index("tool-a") :] == [
        "tool-a",
        "--prompt-file",
        "/tmp/spec.md",
        "-m",
        "explicit-readonly-model",
        "--read-only",
        "--cwd",
        "/work",
    ]


def test_pr_reviewer_quality_uses_the_read_argv(tmp_path: Path) -> None:
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("review this\n", encoding="utf-8")
    result = launch(
        role="pr-reviewer-quality",
        vendor="driver-a",
        spec_file=str(spec),
        cwd=str(tmp_path),
        dry_run=True,
        tmux=False,
        config_home=tmp_path,
        session_id=DEFAULT_SESSION,
    )
    assert "--read-only" in result.argv
    assert "--edit" not in result.argv
    assert OPERATOR_MODEL_A in result.argv
    _assert_nested_role_env_prefix(
        result.argv, config_dir=OPERATOR_HOME_A, provider="driver-a"
    )


def test_driver_b_implementer_argv_requires_explicit_model() -> None:
    argv = provider_argv(
        "driver-b",
        cwd="/work",
        write=True,
        output_file="/tmp/out.txt",
        model="explicit-model-b",
    )
    assert "workspace-write" in argv
    assert "read-only" not in argv
    assert argv[-1] == "-"
    assert argv[0] == "env"
    for key in LANE_STRIP_ENV:
        assert key in argv
    assert argv[argv.index("--model") + 1] == "explicit-model-b"


def test_driver_b_argv_names_the_output_file() -> None:
    for write in (False, True):
        argv = provider_argv(
            "driver-b",
            cwd="/work",
            write=write,
            output_file="/tmp/out.txt",
            model="explicit-model-b",
        )
        assert argv[argv.index("--last-message") + 1] == "/tmp/out.txt"
        assert argv[argv.index("--cd") + 1] == "/work"


def test_driver_b_reviewer_argv_requires_explicit_model() -> None:
    argv = provider_argv(
        "driver-b",
        cwd="/work",
        write=False,
        output_file="/tmp/out.txt",
        model="explicit-model-b-ro",
    )
    assert "read-only" in argv
    assert "workspace-write" not in argv
    assert argv[argv.index("--model") + 1] == "explicit-model-b-ro"
    assert argv[-1] == "-"


def _strip_prefix() -> list[str]:
    prefix = ["env"]
    for key in LANE_STRIP_ENV:
        prefix.extend(["-u", key])
    return prefix


def test_driver_c_argv_is_exactly_the_configured_template() -> None:
    assert provider_argv("driver-c", cwd="/work", write=True, model="model-a") == [
        *_strip_prefix(),
        "tool-c", "--print", "--model", "model-a", "--tools", "Read,Edit",
        "--mode", "edit",
    ]
    assert provider_argv(
        "driver-c",
        cwd="/work",
        write=False,
        model="model-b",
        read_dirs=("/control", "/evidence"),
    ) == [
        *_strip_prefix(),
        "tool-c", "--print", "--model", "model-b", "--tools", "Read",
        "--mode", "default", "--add-dir", "/control", "--add-dir", "/evidence",
    ]
    with pytest.raises(SystemExit, match="write lane takes no extra directories"):
        provider_argv(
            "driver-c", cwd="/work", write=True, model="m", read_dirs=("/control",)
        )


def test_driver_d_argv_has_wrapper_and_never_has_key() -> None:
    argv = provider_argv(
        "driver-d",
        cwd="/work",
        write=False,
        model="vendor/example",
        output_file="/tmp/output.jsonl",
        read_dirs=("/control",),
    )
    assert argv == [
        *_strip_prefix(),
        f"TOOL_C_BASE_URL={DRIVER_D_BASE_URL}",
        "TOOL_C_API_KEY=",
        "sh", "-c", _DRIVER_D_WRAPPER, "sh", "/tmp/output.jsonl",
        "tool-c", "--print", "--model", "vendor/example", "--tools", "Read",
        "--mode", "default", "--add-dir", "/control",
    ]
    assert "secret-test-key" not in " ".join(argv)


def test_driver_d_wrapper_missing_and_present_key(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "record.txt"
    fake = bin_dir / "tool-c"
    fake.write_text(
        "#!/bin/sh\n"
        "printf '%s|%s|%s\\n' \"${TOOL_C_TOKEN:+set}\" \"$TOOL_C_BASE_URL\" \"$*\" > \"$FAKE_RECORD\"\n"
        "printf '%s\\n' '{\"type\":\"result\",\"result\":\"STATUS: complete\",\"usage\":{}}'\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    output = tmp_path / "stream.jsonl"
    argv = provider_argv(
        "driver-d",
        cwd=str(tmp_path),
        write=False,
        model="vendor/model",
        output_file=str(output),
    )
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["FAKE_RECORD"] = str(record)
    env["TOOL_C_CONFIG"] = str(config)
    missing = subprocess.run(argv, capture_output=True, text=True, check=False, env=env)
    assert missing.returncode == 2
    assert missing.stdout == ""
    assert "STATUS: unavailable" in missing.stderr
    assert "driver-d key missing" in missing.stderr
    assert not output.exists()

    key = "secret-test-key"
    (config / "tool-d-key").write_text(key, encoding="utf-8")
    present = subprocess.run(argv, capture_output=True, text=True, check=False, env=env)
    assert present.returncode == 0
    assert present.stdout == ""
    assert '"type":"result"' in output.read_text(encoding="utf-8")
    recorded = record.read_text(encoding="utf-8")
    assert recorded.startswith(f"set|{DRIVER_D_BASE_URL}|")
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
    out = finalize_lane_output(lane_io(DRIVERS["driver-c"], tmux=False), stream, "", cwd="/work")
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
    out = finalize_lane_output(lane_io(DRIVERS["driver-c"], tmux=False), json.dumps(event), "", cwd="/work")
    assert out.text == ""
    assert out.usage is not None and out.usage.input_tokens == 5


@pytest.mark.parametrize("vendor", ["driver-d", "driver-b", "driver-a"])
def test_finalize_lane_output_hides_the_account_directory_in_the_final_text(vendor: str) -> None:
    text = "Unable to use /opt/profiles/router/settings.json\nSTATUS: complete"
    if vendor == "driver-d":
        event = {"type": "result", "subtype": "success", "is_error": False, "result": text}
        out = finalize_lane_output(
            lane_io(DRIVERS[vendor], tmux=False), "", json.dumps(event), cwd="/work", config_dir="/opt/profiles/router"
        )
    elif vendor == "driver-a":
        out = finalize_lane_output(
            lane_io(DRIVERS[vendor], tmux=False),
            event_stream_output(text),
            "",
            cwd="/work",
            config_dir="/opt/profiles/router",
        )
    else:
        out = finalize_lane_output(
            lane_io(DRIVERS[vendor], tmux=False), text, "", cwd="/work", config_dir="/opt/profiles/router"
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


def test_config_dir_redaction_precedes_terminal_sanitization_for_lane_sinks(
    nested_terminal_string_control: str,
) -> None:
    config_dir = "/opt/profiles/router"
    split_path = (
        f"/opt/profi{nested_terminal_string_control}"
        "les/router/tool-d-key"
    )
    raw = f"provider failed at {split_path}"

    assert hide_config_dir(raw, config_dir) == (
        "provider failed at <config_dir>/tool-d-key"
    )
    assert redact_diagnostic(raw, config_dir=config_dir) == (
        "provider failed at <config_dir>/tool-d-key"
    )

    event = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": raw,
    }
    finalized = finalize_lane_output(
        lane_io(DRIVERS["driver-d"], tmux=False),
        "",
        json.dumps(event),
        cwd="/work",
        config_dir=config_dir,
    )
    assert finalized.text == "provider failed at <config_dir>/tool-d-key"

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
            vendor="driver-d",
            model="model",
            status="unavailable",
            argv=["tool-c"],
            returncode=2,
            stdout="",
            stderr=raw,
            config_dir=config_dir,
            audit_stderr_when_empty=True,
        ),
    )
    assert store.row is not None
    assert store.row["payload"]["final_text"] == (
        "provider failed at <config_dir>/tool-d-key"
    )


def test_controlled_line_config_dir_placeholder_exemption_is_exact() -> None:
    placeholder = "<config_dir>/tool-d-key"
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


@pytest.mark.timing
def test_shared_redaction_is_linear_on_a_large_unicode_key() -> None:
    raw = "token" + "\u00e9" * 1_000_000 + "=hunter2 visible"
    started = time.monotonic()
    results = _shared_lane_and_error_redactions(raw)
    elapsed = time.monotonic() - started
    assert results == ("[redacted] visible",) * 3
    assert elapsed < 2, f"redaction took {elapsed:.3f}s"


@pytest.mark.parametrize("separator", (",", ";"), ids=("comma", "semicolon"))
def test_shared_redaction_keeps_an_unquoted_secret_value_whole(
    separator: str,
) -> None:
    for raw, expected in (
        (f"ERROR password=correct{separator}horse", "ERROR [redacted]"),
        (
            f"ERROR password=correct{separator}horse visible",
            "ERROR [redacted] visible",
        ),
        (
            f"ERROR password: correct{separator}horse visible",
            "ERROR password: [redacted] visible",
        ),
        (
            f"ERROR api_key = a{separator}b{separator}c visible",
            "ERROR api_key = [redacted] visible",
        ),
        (
            f'ERROR password="correct"{separator}horse visible',
            f'ERROR password="[redacted]"{separator}horse visible',
        ),
    ):
        for cleaned in _shared_lane_and_error_redactions(raw):
            assert cleaned == expected


def test_shared_redaction_scans_a_second_assignment_inside_an_unquoted_secret_value() -> None:
    cases = (
        ("token=abc;password: hunter2", ("hunter2",)),
        ('token=a,password="qx zq"', ("qx", "zq")),
        ('token=abc,"password": "s3cr3t"', ("s3cr3t",)),
        ('token=a;password="kq,vw zz"', ("kq", "vw", "zz")),
    )

    for raw, secrets in cases:
        for cleaned in _shared_lane_and_error_redactions(raw):
            for secret in secrets:
                assert secret not in cleaned


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


@pytest.mark.parametrize(
    "control",
    (
        "\x1bPprefix \x1b]api_key\x07\x1b\\",
        "\x90prefix \x9dapi_key\x07\x9c",
    ),
    ids=("7-bit-nested-string", "8-bit-nested-string"),
)
@pytest.mark.parametrize(
    "suffix",
    ("=topsecret-value", " topsecret-value", ": topsecret-value"),
    ids=("equals", "space", "colon-space"),
)
def test_assignment_redaction_keeps_nested_string_payload_for_classification(
    control: str, suffix: str
) -> None:
    raw = f"{control}{suffix}"

    for cleaned in (
        redact_shared(raw, limit=None),
        redact_lane_text(raw, limit=None),
    ):
        assert cleaned == "[redacted]"


@pytest.mark.parametrize(
    "control",
    (
        "\x1bPprefix \x1b]api_key",
        "\x90prefix \x9dapi_key",
    ),
    ids=("7-bit", "8-bit"),
)
def test_assignment_redaction_fails_closed_on_unterminated_nested_string_control(
    control: str,
) -> None:
    raw = f"visible {control}=topsecret-value"

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
    finalized = finalize_lane_output(
        lane_io(DRIVERS["driver-a"], tmux=False), event_stream_output(raw), "", cwd="/work"
    )
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
            vendor="driver-a",
            model="model",
            status="partial",
            argv=["tool-a"],
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
@pytest.mark.timing
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
@pytest.mark.timing
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


@pytest.mark.timing
def test_terminal_string_control_scanner_is_linear_at_100000_nested_introducers(
) -> None:
    depth = 100_000
    raw = ("\x1bP" * depth) + "Zx7Qp9Lm4V2" + ("\x1b\\" * depth)
    calls = (
        (
            "shared redaction",
            lambda: redact_shared(raw, limit=None),
            "[redacted]",
        ),
        (
            "lane redaction",
            lambda: redact_lane_text(raw, limit=None),
            "[redacted]",
        ),
        (
            "string-control redaction",
            lambda: redact_terminal_string_controls(raw),
            "[redacted]",
        ),
        ("terminal sanitization", lambda: sanitize_terminal_text(raw), ""),
    )

    for label, call, expected in calls:
        started = time.monotonic()
        cleaned = call()
        elapsed = time.monotonic() - started
        assert elapsed < 2, f"{label} took {elapsed:.3f}s"
        assert cleaned == expected


@pytest.mark.timing
def test_known_secret_scanners_are_linear_across_one_large_nested_string_control(
) -> None:
    depth = 100_000
    control = ("\x1bP" * depth) + "opaque payload" + ("\x1b\\" * depth)
    raw = f"api_key=model-{control}secret"
    calls = (
        (
            "exact",
            lambda: redact_terminal_interleaved_secret(
                raw, "api_key=model-secret"
            ),
        ),
        (
            "urlencoded",
            lambda: redact_terminal_interleaved_urlencoded_secret(
                raw, "api_key=model-secret"
            ),
        ),
    )

    for label, call in calls:
        started = time.monotonic()
        cleaned = call()
        elapsed = time.monotonic() - started
        assert elapsed < 2, f"{label} took {elapsed:.3f}s"
        assert cleaned == "[redacted]"


@pytest.mark.timing
def test_known_secret_scanners_are_linear_across_repeated_candidates_and_nested_unterminated_controls(
) -> None:
    config_dir = "/opt/provider-profile"
    raw = "/\x1b]" * 100_000
    calls = (
        (
            "exact",
            lambda: redact_terminal_interleaved_secret(raw, config_dir),
            raw,
        ),
        (
            "lane config path",
            lambda: redact_lane_text(raw, config_dir=config_dir, limit=None),
            "/[redacted]",
        ),
    )

    for label, call, expected in calls:
        started = time.monotonic()
        cleaned = call()
        elapsed = time.monotonic() - started
        assert elapsed < 2, f"{label} redaction took {elapsed:.3f}s"
        assert cleaned == expected


@pytest.mark.parametrize(
    "control_run",
    (
        "\x1b]\x07" * 65,
        "\x1b]\x07" * 1_000,
        "\x07" * 65,
        "\x1b]\x07\x07\x1b[31m\x1b7\x1bP\x1b\\" * 65,
    ),
    ids=("65-osc", "1000-osc", "65-c0", "mixed"),
)
@pytest.mark.parametrize("encoded", (False, True), ids=("exact", "percent-encoded"))
@pytest.mark.timing
def test_known_secret_scanners_cross_unbounded_terminal_control_runs(
    control_run: str, encoded: bool
) -> None:
    secret = "/opt/key"
    representation = (
        "".join(f"%{byte:02X}" for byte in secret.encode("utf-8"))
        if encoded
        else secret
    )
    split_at = 1
    raw = (
        f"before {representation[:split_at]}{control_run}"
        f"{representation[split_at:]} after"
    )
    redact_secret = (
        redact_terminal_interleaved_urlencoded_secret
        if encoded
        else redact_terminal_interleaved_secret
    )

    started = time.monotonic()
    cleaned = redact_secret(raw, secret)
    elapsed = time.monotonic() - started

    assert elapsed < 2, f"known-secret redaction took {elapsed:.3f}s"
    assert cleaned == "before [redacted] after"


@pytest.mark.timing
def test_known_secret_scanner_is_linear_across_ambiguous_csi_final_bytes() -> None:
    secret = "/opt/key"
    control_run = "\x1b[o" * 1_000
    raw = f"before /{control_run}opt/key after"

    started = time.monotonic()
    cleaned = redact_terminal_interleaved_secret(raw, secret)
    elapsed = time.monotonic() - started

    assert elapsed < 2, f"known-secret redaction took {elapsed:.3f}s"
    assert cleaned == "before [redacted] after"


@pytest.mark.parametrize(
    "prefix",
    ("\x1b", "\x1b["),
    ids=("lone-esc", "lone-csi"),
)
def test_lane_config_dir_redaction_crosses_a_truncated_escape_prefix(
    prefix: str,
) -> None:
    config_dir = "/opt/my profile"
    raw = f"failed {prefix}{config_dir}/key"

    cleaned = redact_lane_text(raw, config_dir=config_dir, limit=None)

    assert config_dir not in cleaned
    assert "profile/key" not in cleaned
    assert "my profile" not in cleaned


@pytest.mark.timing
def test_known_secret_scanner_is_linear_across_many_candidate_starts() -> None:
    n = 20_000
    raw = "\x1b[a" * n
    secret = "a" * (n // 2) + "b"

    started = time.monotonic()
    cleaned = redact_terminal_interleaved_secret(raw, secret)
    elapsed = time.monotonic() - started

    assert elapsed < 2, f"known-secret redaction took {elapsed:.3f}s"
    assert cleaned == raw


@pytest.mark.timing
def test_known_secret_scanner_is_linear_across_many_percent_encoded_candidate_starts(
) -> None:
    n = 20_000
    raw = "\x1b[a" * n
    secret = "a" * (n // 2) + "b"

    started = time.monotonic()
    cleaned = redact_terminal_interleaved_urlencoded_secret(raw, secret)
    elapsed = time.monotonic() - started

    assert elapsed < 2, f"known-secret redaction took {elapsed:.3f}s"
    assert cleaned == raw


@pytest.mark.timing
def test_urlencoded_matcher_is_linear_on_a_literal_percent_run() -> None:
    n = 20_000
    raw = "%" * n
    secret = "%" * (n // 2) + "b"

    started = time.monotonic()
    cleaned = redact_terminal_interleaved_urlencoded_secret(raw, secret)
    elapsed = time.monotonic() - started

    assert elapsed < 2, f"known-secret redaction took {elapsed:.3f}s"
    assert cleaned == raw


@pytest.mark.timing
def test_urlencoded_matcher_is_linear_on_an_encoded_percent_run() -> None:
    n = 20_000
    raw = "%25" * n
    secret = "%" * (n // 2) + "b"

    started = time.monotonic()
    cleaned = redact_terminal_interleaved_urlencoded_secret(raw, secret)
    elapsed = time.monotonic() - started

    assert elapsed < 2, f"known-secret redaction took {elapsed:.3f}s"
    assert cleaned == raw


def test_urlencoded_secret_containing_percent_is_redacted_raw_encoded_and_mixed(
) -> None:
    secret = "j%41"
    fully_encoded = "".join(f"%{byte:02X}" for byte in secret.encode("utf-8"))
    representations = (
        secret,
        fully_encoded,
        fully_encoded.lower(),
        "j%2541",
        "%6A%2541",
        "%6a%25%341",
        "%6a%25%34%31",
    )

    for representation in representations:
        cleaned = redact_terminal_interleaved_urlencoded_secret(
            f"xx{representation}yy", secret
        )
        assert cleaned == "xx[redacted]yy", representation

    assert (
        redact_terminal_interleaved_urlencoded_secret("xxj%2542yy", secret)
        == "xxj%2542yy"
    )
    assert (
        redact_terminal_interleaved_urlencoded_secret(
            "xxj\x1b[31m%2541yy", secret
        )
        == "xx[redacted]yy"
    )


@pytest.mark.timing
def test_known_secret_scanner_is_linear_across_a_repeated_visible_prefix() -> None:
    raw = "/opt/" * 20_000
    secret = "/opt/" * 5_000 + "X"

    started = time.monotonic()
    cleaned = redact_terminal_interleaved_secret(raw, secret)
    elapsed = time.monotonic() - started

    assert elapsed < 2, f"known-secret redaction took {elapsed:.3f}s"
    assert cleaned == raw


@pytest.mark.timing
def test_known_secret_scanner_is_linear_across_repeated_prefixes_split_by_control_runs(
) -> None:
    raw = ("/opt/" + "\x1b[31m") * 20_000
    secret = "/opt/" * 5_000 + "X"

    started = time.monotonic()
    cleaned = redact_terminal_interleaved_secret(raw, secret)
    elapsed = time.monotonic() - started

    assert elapsed < 2, f"known-secret redaction took {elapsed:.3f}s"
    assert cleaned == raw


def test_known_secret_scanner_redacts_the_match_after_a_long_false_prefix() -> None:
    secret = "/opt/" * 1_000 + "X"
    raw = "/opt/" * 3_000 + "X"

    cleaned = redact_terminal_interleaved_secret(raw, secret)

    assert cleaned == ("/opt/" * 2_000) + "[redacted]"


def test_known_secret_redacts_a_path_that_starts_inside_an_osc_payload() -> None:
    cleaned = redact_lane_text(
        "failed \x1b]/opt/my\x07 profile/key",
        config_dir="/opt/my profile",
        limit=None,
    )

    assert "/opt/my profile" not in cleaned
    assert "profile/key" not in cleaned


def test_known_secret_redacts_a_secret_that_starts_on_a_dcs_introducer() -> None:
    cleaned = redact_terminal_interleaved_secret(
        "\x1bP\x1b\\rivate-key",
        "Private-key",
    )

    assert "Private-key" not in cleaned
    assert "rivate-key" not in cleaned


def test_known_secret_redacts_8bit_osc_and_dcs_payloads() -> None:
    osc = redact_lane_text(
        "failed \x9d/opt/my\x07 profile/key",
        config_dir="/opt/my profile",
        limit=None,
    )
    assert "/opt/my profile" not in osc
    assert "profile/key" not in osc

    dcs = redact_terminal_interleaved_secret(
        "\x90Private\x9c-key",
        "Private-key",
    )
    assert "Private-key" not in dcs
    assert "Private" not in dcs
    assert "-key" not in dcs


def test_known_secret_redacts_a_secret_split_across_two_string_payloads() -> None:
    cleaned = redact_terminal_interleaved_secret(
        "\x1b]/opt/my\x07\x1b] profile\x07",
        "/opt/my profile",
    )

    assert "/opt/my profile" not in cleaned
    assert "/opt/my" not in cleaned
    assert " profile" not in cleaned


@pytest.mark.timing
def test_known_secret_scanner_is_linear_across_repeated_csi_landings() -> None:
    n = 20_000
    raw = "\x1b[aa" * n
    secret = "a" * (n // 2)

    started = time.monotonic()
    cleaned = redact_terminal_interleaved_secret(raw, secret)
    elapsed = time.monotonic() - started

    assert elapsed < 2, f"known-secret redaction took {elapsed:.3f}s"
    assert "[redacted]" in cleaned
    assert secret not in cleaned


def test_known_secret_redacts_a_csi_final_that_continues_the_secret() -> None:
    assert (
        redact_terminal_interleaved_secret("se\x1b[31cret", "secret")
        == "[redacted]"
    )


def test_known_secret_redacts_an_st_backslash_that_continues_the_secret() -> None:
    assert redact_terminal_interleaved_secret("a\x1b\\b", "a\\b") == "[redacted]"


def test_lane_config_dir_redacts_a_path_continued_by_a_csi_final() -> None:
    cleaned = redact_lane_text(
        "failed /opt/m\x1b[31y profile/key",
        config_dir="/opt/my profile",
        limit=None,
    )

    assert cleaned == "failed <config_dir>/key"
    assert "profile/key" not in cleaned
    assert "/opt/m" not in cleaned


def _known_secret_control_placements() -> list[tuple[str, str, str, str]]:
    """Control kind × position. ``before`` / ``between`` / ``c-*`` are (a)/(b)/(c).

    Position (c) makes the control's final or introducer byte the next secret
    character, with that control's parameters, intermediates, or payload still
    present. At least one fixed view has to yield the secret.
    """
    rows: list[tuple[str, str, str, str]] = []
    secret = "Qw"
    for kind, control in (
        ("lone-esc", "\x1b"),
        ("esc-intermediate-final", "\x1b(/Z"),
        ("csi-7", "\x1b[32!Z"),
        ("csi-8", "\x9b32!Z"),
        ("osc-7-bel", "\x1b]xx\x07"),
        ("osc-8-bel", "\x9dxx\x07"),
        ("osc-7-st", "\x1b]xx\x1b\\"),
        ("osc-8-st", "\x9dxx\x9c"),
        ("dcs-7", "\x1bPxx\x1b\\"),
        ("dcs-8", "\x90xx\x9c"),
        ("pm-7", "\x1b^xx\x1b\\"),
        ("pm-8", "\x9exx\x9c"),
        ("apc-7", "\x1b_xx\x1b\\"),
        ("apc-8", "\x9fxx\x9c"),
        ("sos-7", "\x1bXxx\x1b\\"),
        ("sos-8", "\x98xx\x9c"),
        ("bel", "\x07"),
        ("st-7", "\x1b\\"),
        ("st-8", "\x9c"),
    ):
        rows.append((kind, "before", control + secret, secret))
        rows.append((kind, "between", "Q" + control + "w", secret))
    rows.extend(
        (
            ("lone-esc", "c-introducer", "Q\x1bw", "Q\x1bw"),
            ("esc-intermediate-final", "c-final", "Q\x1b(/Bw", "QBw"),
            ("csi-7", "c-final", "Q\x1b[32!mw", "Qmw"),
            ("csi-7", "c-introducer", "Q\x1b[32!Zw", "Q[w"),
            ("csi-8", "c-final", "Q\x9b32!mw", "Qmw"),
            ("csi-8", "c-introducer", "Q\x9b32!Zw", "Q\x9bw"),
            ("osc-7-bel", "c-introducer", "Q\x1b]xx\x07w", "Q]w"),
            ("osc-7-bel", "c-terminator", "Q\x1b]xx\x07w", "Q\x07w"),
            ("osc-8-bel", "c-introducer", "Q\x9dxx\x07w", "Q\x9dw"),
            ("osc-8-bel", "c-terminator", "Q\x9dxx\x07w", "Q\x07w"),
            ("osc-7-st", "c-introducer", "Q\x1b]xx\x1b\\w", "Q]w"),
            ("osc-7-st", "c-final", "Q\x1b]xx\x1b\\w", "Q\\w"),
            ("osc-8-st", "c-introducer", "Q\x9dxx\x1b\\w", "Q\x9dw"),
            ("osc-8-st", "c-final", "Q\x9dxx\x1b\\w", "Q\\w"),
            ("osc-8-st-byte", "c-final", "Q\x9dxx\x9cw", "Q\x9cw"),
            ("dcs-7", "c-introducer", "Q\x1bPxx\x1b\\w", "QPw"),
            ("dcs-7", "c-final", "Q\x1bPxx\x1b\\w", "Q\\w"),
            ("dcs-8", "c-introducer", "Q\x90xx\x9cw", "Q\x90w"),
            ("dcs-8", "c-final", "Q\x90xx\x1b\\w", "Q\\w"),
            ("dcs-8-st-byte", "c-final", "Q\x90xx\x9cw", "Q\x9cw"),
            ("pm-7", "c-introducer", "Q\x1b^xx\x1b\\w", "Q^w"),
            ("pm-7", "c-final", "Q\x1b^xx\x1b\\w", "Q\\w"),
            ("pm-8", "c-introducer", "Q\x9exx\x9cw", "Q\x9ew"),
            ("pm-8", "c-final", "Q\x9exx\x1b\\w", "Q\\w"),
            ("pm-8-st-byte", "c-final", "Q\x9exx\x9cw", "Q\x9cw"),
            ("apc-7", "c-introducer", "Q\x1b_xx\x1b\\w", "Q_w"),
            ("apc-7", "c-final", "Q\x1b_xx\x1b\\w", "Q\\w"),
            ("apc-8", "c-introducer", "Q\x9fxx\x9cw", "Q\x9fw"),
            ("apc-8", "c-final", "Q\x9fxx\x1b\\w", "Q\\w"),
            ("apc-8-st-byte", "c-final", "Q\x9fxx\x9cw", "Q\x9cw"),
            ("sos-7", "c-introducer", "Q\x1bXxx\x1b\\w", "QXw"),
            ("sos-7", "c-final", "Q\x1bXxx\x1b\\w", "Q\\w"),
            ("sos-8", "c-introducer", "Q\x98xx\x9cw", "Q\x98w"),
            ("sos-8", "c-final", "Q\x98xx\x1b\\w", "Q\\w"),
            ("sos-8-st-byte", "c-final", "Q\x98xx\x9cw", "Q\x9cw"),
            ("bel", "c-final", "Q\x07w", "Q\x07w"),
            ("st-7", "c-final", "Q\x1b\\w", "Q\\w"),
            ("st-8", "c-final", "Q\x9cw", "Q\x9cw"),
        )
    )
    return rows


_CONTROL_PLACEMENTS = _known_secret_control_placements()


@pytest.mark.parametrize(
    ("kind", "position", "text", "secret"),
    _CONTROL_PLACEMENTS,
    ids=[
        f"{kind}-{position}"
        for kind, position, _text, _secret in _CONTROL_PLACEMENTS
    ],
)
def test_known_secret_views_yield_the_secret_for_every_control_placement(
    kind: str, position: str, text: str, secret: str
) -> None:
    cleaned = redact_terminal_interleaved_secret(text, secret)

    assert secret not in cleaned, f"{kind} {position}"
    assert "[redacted]" in cleaned


@pytest.mark.timing
def test_known_secret_scanner_is_linear_across_kept_csi_finals() -> None:
    n = 20_000
    raw = "\x1b[31a" * n
    secret = "a" * (n - 1) + "b"

    started = time.monotonic()
    cleaned = redact_terminal_interleaved_secret(raw, secret)
    elapsed = time.monotonic() - started

    assert elapsed < 2, f"known-secret redaction took {elapsed:.3f}s"
    assert cleaned == raw


@pytest.mark.parametrize(
    "representation",
    (
        "%74%C3%B6%6B",
        "%74%c3%b6%6b",
        "%74%C3%b6%6B",
        "t%C3%B6k",
        "t%C3%B6%6B",
        "%74%C3%B6k",
        "tö%6B",
    ),
)
def test_urlencoded_utf8_secret_is_redacted_in_any_hex_case(
    representation: str,
) -> None:
    cleaned = redact_terminal_interleaved_urlencoded_secret(
        f"xx{representation}yy", "tök"
    )

    assert cleaned == "xx[redacted]yy", representation


def test_urlencoded_valid_utf8_is_not_also_matched_as_latin1_bytes() -> None:
    assert (
        redact_terminal_interleaved_urlencoded_secret("%C3%B6", "\u00c3\u00b6")
        == "%C3%B6"
    )


@pytest.mark.parametrize(
    "raw",
    (
        "xx%FF%FEyy",
        "xx%C3yy",
        "xx%C3%28yy",
        "xx%E2%82yy",
        "xx%C0%80yy",
        "xx%ED%A0%80yy",
    ),
)
@pytest.mark.parametrize("secret", ("tök", "€", "\x00"))
def test_urlencoded_invalid_utf8_percent_run_is_not_a_false_match(
    raw: str, secret: str
) -> None:
    assert redact_terminal_interleaved_urlencoded_secret(raw, secret) == raw


@pytest.mark.parametrize(
    ("raw", "secret"),
    (
        ("se%1B[31mkret", "sekret"),
        ("se%1B%5B31mkret", "sekret"),
        ("s%1B[0me%1B[0mkret", "sekret"),
        ("se%1B%5D0;x%07kret", "sekret"),
        ("Bearer%20se%1B[31mkret", "Bearer sekret"),
        ("a%1B[31m%2541", "a%41"),
    ),
    ids=(
        "encoded-csi",
        "fully-encoded-csi",
        "repeated-sgr",
        "encoded-osc",
        "encoded-bearer-space",
        "encoded-csi-keeps-literal-percent",
    ),
)
def test_urlencoded_control_views_redact_a_secret_split_by_a_decoded_control(
    raw: str, secret: str
) -> None:
    assert (
        redact_terminal_interleaved_urlencoded_secret(raw, secret)
        == "[redacted]"
    )


def test_urlencoded_control_views_redact_only_the_mapped_raw_span() -> None:
    assert (
        redact_terminal_interleaved_urlencoded_secret(
            "xxse%1B[31mkretyy", "sekret"
        )
        == "xx[redacted]yy"
    )


def test_urlencoded_control_views_do_not_decode_a_percent_escape_twice() -> None:
    assert (
        redact_terminal_interleaved_urlencoded_secret("%61%41", "a%41")
        == "%61%41"
    )
    assert redact_terminal_interleaved_urlencoded_secret("%2541", "A") == "%2541"


def test_urlencoded_double_encoded_esc_is_not_a_control() -> None:
    raw = "se%251B[31mkret"
    assert redact_terminal_interleaved_urlencoded_secret(raw, "sekret") == raw


@pytest.mark.timing
def test_urlencoded_control_views_are_linear_on_percent_decoded_csi() -> None:
    n = 20_000
    raw = "%1B[31ma" * n
    secret = "a" * n + "b"

    started = time.monotonic()
    cleaned = redact_terminal_interleaved_urlencoded_secret(raw, secret)
    elapsed = time.monotonic() - started

    assert elapsed < 2, f"known-secret redaction took {elapsed:.3f}s"
    assert cleaned == raw


def test_lane_config_dir_redaction_crosses_long_and_mixed_control_runs() -> None:
    config_dir = "/opt/provider-profile"
    runs = (
        "\x1b]\x07" * 65,
        "\x1b]\x07" * 1_000,
        "\x07" * 65,
        "\x1b]\x07\x07\x1b[31m\x1b7\x1bP\x1b\\" * 65,
    )

    for control_run in runs:
        raw = f"failed at /opt/{control_run}provider-profile/key"
        assert redact_diagnostic(raw, config_dir=config_dir) == (
            "failed at <config_dir>/key"
        )


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
    out = finalize_lane_output(lane_io(DRIVERS["driver-d"], tmux=False), "", json.dumps(event), cwd="/work")
    assert out.text == "STATUS: complete"


def test_finalize_lane_output_fail_closed_and_bounds_tools() -> None:
    no_result = json.dumps({"type": "assistant", "message": {"content": []}})
    assert finalize_lane_output(lane_io(DRIVERS["driver-c"], tmux=False), no_result, "", cwd="/work").text == ""
    assert finalize_lane_output(lane_io(DRIVERS["driver-c"], tmux=False), "not json", "", cwd="/work").usage is None
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
        lane_io(DRIVERS["driver-d"], tmux=False), "pane noise", stream, cwd="/work"
    )
    assert len(out.tools) == 200
    assert out.tools_truncated is True
    assert len(out.tools[0]["target"]) <= 300
    assert out.usage is not None and out.usage.model == "fallback"
    assert finalize_lane_output(lane_io(DRIVERS["driver-b"], tmux=False), "stdout", "file", cwd="/work").text == "file"
    # driver-a writes an event stream: plain stdout has no terminal result event.
    assert finalize_lane_output(lane_io(DRIVERS["driver-a"], tmux=False), "stdout", "file", cwd="/work").text == ""
    assert (
        finalize_lane_output(
            lane_io(DRIVERS["driver-a"], tmux=False),
            event_stream_output("STATUS: complete"),
            "file",
            cwd="/work",
        ).text
        == "STATUS: complete"
    )


def test_finalize_lane_output_driver_a_reads_only_the_final_assistant_message() -> None:
    from agent_cli.coordinator_common import parse_model_result

    progress = "Working on the diff artifact."
    final = "STATUS: complete\nRESULT: approved"
    events = [
        {"type": "system", "subtype": "init", "model": "driver-a-model"},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": progress},
                    {"type": "tool_use", "id": "driver-a-tool-1", "name": "Read",
                     "input": {"file_path": "/work/a.py"}},
                ]
            },
        },
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": final}]},
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": final,
            "usage": {"input_tokens": 7, "output_tokens": 3},
        },
    ]
    stream = "\n".join(json.dumps(event) for event in events)

    out = finalize_lane_output(lane_io(DRIVERS["driver-a"], tmux=False), stream, "", cwd="/work")

    assert out.text == final
    assert out.usage is not None and out.usage.input_tokens == 7
    assert out.tools == ({"tool": "Read", "target": "a.py", "error": False},)
    assert parse_model_result(out.text, 0) == ("complete", "approved")
    # The plain concatenation glues the progress sentence to the answer.
    assert parse_model_result(progress + final, 0) == ("partial", "")


@pytest.mark.parametrize(
    "result_fields",
    [
        {"is_error": True, "subtype": "success"},
        {"is_error": False, "subtype": "error_max_turns"},
    ],
)
def test_finalize_lane_output_driver_a_drops_text_of_a_failed_result(
    result_fields,
) -> None:
    event = {
        "type": "result",
        "result": "STATUS: complete\nRESULT: approved",
        "usage": {},
        **result_fields,
    }
    out = finalize_lane_output(lane_io(DRIVERS["driver-a"], tmux=False), json.dumps(event), "", cwd="/work")
    assert out.text == ""


def test_finalize_lane_output_driver_a_requires_a_terminal_result_event() -> None:
    stream = "\n".join(
        [
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": "STATUS: complete\nRESULT: approved",
                            }
                        ]
                    },
                }
            ),
        ]
    )
    assert finalize_lane_output(lane_io(DRIVERS["driver-a"], tmux=False), stream, "", cwd="/work").text == ""
    assert (
        finalize_lane_output(
            lane_io(DRIVERS["driver-a"], tmux=False), "STATUS: complete\nRESULT: approved", "", cwd="/work"
        ).text
        == ""
    )


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
                        "input": {"file_path": "/opt/driver-d-profile/tool-d-key"},
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
        lane_io(DRIVERS["driver-c"], tmux=False),
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
                        "input": {"pattern": "/opt/driver-d-profile/tool-d-key"},
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
        lane_io(DRIVERS["driver-c"], tmux=False),
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
            "needle in /opt/driver-d-profile/key", str(worktree), "pattern"
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
            "^/opt/profiles/router/tool-d-key$",
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
        "path=/opt/profiles/router/tool-d-key",
        "source=/opt/company/private-repo/config.json",
        "source='/opt/company/private-repo/config.json'",
        'source="/opt/company/private-repo/config.json"',
        "source:/opt/company/private-repo/config.json",
        "^path=/opt/profiles/router/tool-d-key$",
        "(?:/opt/profiles/router/tool-d-key)",
        r"path=\/opt/profiles/router/tool-d-key",
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
        lane_io(DRIVERS["driver-c"], tmux=False),
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
            vendor="driver-c",
            model="model",
            status="complete",
            argv=["tool-c"],
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
        vendor="driver-d",
        model=long_model,
        status="complete",
        argv=["tool-c"],
        returncode=0,
        stdout="STATUS: complete\n",
        stderr="",
        usage=LaneUsage(
            model="vendor/sk-test-v1-abcdefghijklmnopqrstuvwxyz012345",
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
    assert "sk-test-v1-abcdefghijklmnopqrstuvwxyz012345" not in payload["reported_model"]
    assert len(payload["reported_model"]) <= 200


def test_record_lane_run_removes_escape_sequence_before_secret_redaction() -> None:
    class CaptureStore:
        row: dict | None = None

        def write(self, _table: str, _operation: str, _row_id: str, row: dict) -> None:
            self.row = row

    split_secret = "sk-test-\x1b[31mv1-abcdefghijklmnopqrstuvwxyz012345"
    store = CaptureStore()
    record_lane_run(
        store,
        session_id=DEFAULT_SESSION,
        result=LaneResult(
            role="implementer",
            vendor="driver-d",
            model="model",
            status="complete",
            argv=["tool-c"],
            returncode=0,
            stdout=f"provider output {split_secret}\nSTATUS: complete",
            stderr="",
        ),
    )

    assert store.row is not None
    final_text = store.row["payload"]["final_text"]
    assert "sk-test-" not in final_text
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
            vendor="driver-d",
            model=split_token,
            status="complete",
            argv=["tool-c"],
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
def test_launch_refuses_driver_d_config_and_worktree_overlap(
    tmp_path: Path, config_inside_worktree: bool
) -> None:
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("implement\n", encoding="utf-8")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    config_dir = worktree / "config" if config_inside_worktree else tmp_path
    raw = json.loads((tmp_path / "ai-accounts.json").read_text(encoding="utf-8"))
    raw["accounts"]["account-d"]["config_dir"] = str(config_dir)
    (tmp_path / "ai-accounts.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(SystemExit, match="overlaps"):
        launch(
            role="implementer",
            vendor="driver-d",
            spec_file=str(spec),
            cwd=str(worktree),
            dry_run=True,
            tmux=False,
            config_home=tmp_path,
            session_id=DEFAULT_SESSION,
        )


def test_launch_refuses_driver_d_read_directory_inside_config(tmp_path: Path) -> None:
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
    raw["accounts"]["account-d"]["config_dir"] = str(config_dir)
    (tmp_path / "ai-accounts.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(SystemExit, match="read directory overlaps"):
        launch(
            role="reviewer",
            vendor="driver-d",
            spec_file=str(spec),
            cwd=str(worktree),
            read_dirs=(str(exposed),),
            dry_run=True,
            tmux=False,
            config_home=tmp_path,
            session_id=DEFAULT_SESSION,
        )


def test_launch_driver_d_reads_stream_from_output_file(tmp_path: Path) -> None:
    write_operator_ai_accounts(tmp_path)
    config_dir = tmp_path / "router-config"
    config_dir.mkdir()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    raw = json.loads((tmp_path / "ai-accounts.json").read_text(encoding="utf-8"))
    raw["accounts"]["account-d"]["config_dir"] = str(config_dir)
    (tmp_path / "ai-accounts.json").write_text(json.dumps(raw), encoding="utf-8")
    spec = tmp_path / "spec.md"
    spec.write_text("implement\n", encoding="utf-8")

    def fake(argv: list[str], stdin_text: str | None) -> object:
        wrapper_index = next(i for i, value in enumerate(argv) if "tool-d-key" in value)
        output_file = Path(argv[wrapper_index + 2])
        output_file.write_text(
            json.dumps(
                {
                    "type": "result",
                    "result": "STATUS: complete\nRESULT: done",
                    "usage": {"input_tokens": 7, "output_tokens": 2},
                    "modelUsage": {OPERATOR_MODEL_D: {}},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        assert stdin_text == "implement\n"
        return SimpleNamespace(returncode=0, stdout="pane output is not the result", stderr="")

    result = launch(
        role="implementer",
        vendor="driver-d",
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
    assert result.usage.model == OPERATOR_MODEL_D
    assert result.usage.input_tokens == 7


def test_launch_driver_d_invalid_utf8_returns_unavailable_and_removes_output(
    tmp_path: Path,
) -> None:
    write_operator_ai_accounts(tmp_path)
    config_dir = tmp_path / "router-config"
    config_dir.mkdir()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    raw = json.loads((tmp_path / "ai-accounts.json").read_text(encoding="utf-8"))
    raw["accounts"]["account-d"]["config_dir"] = str(config_dir)
    (tmp_path / "ai-accounts.json").write_text(json.dumps(raw), encoding="utf-8")
    spec = tmp_path / "spec.md"
    spec.write_text("implement\n", encoding="utf-8")
    output_paths: list[Path] = []

    def fake(argv: list[str], stdin_text: str | None) -> object:
        wrapper_index = next(
            i for i, value in enumerate(argv) if "tool-d-key" in value
        )
        output_file = Path(argv[wrapper_index + 2])
        output_paths.append(output_file)
        output_file.write_bytes(b"\xff")
        assert stdin_text == "implement\n"
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    result = launch(
        role="implementer",
        vendor="driver-d",
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


def test_launch_driver_d_unreadable_output_returns_unavailable(
    tmp_path: Path,
) -> None:
    write_operator_ai_accounts(tmp_path)
    config_dir = tmp_path / "router-config"
    config_dir.mkdir()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    raw = json.loads((tmp_path / "ai-accounts.json").read_text(encoding="utf-8"))
    raw["accounts"]["account-d"]["config_dir"] = str(config_dir)
    (tmp_path / "ai-accounts.json").write_text(json.dumps(raw), encoding="utf-8")
    spec = tmp_path / "spec.md"
    spec.write_text("implement\n", encoding="utf-8")

    def deleted_output(argv: list[str], stdin_text: str | None) -> object:
        wrapper_index = next(
            i for i, value in enumerate(argv) if "tool-d-key" in value
        )
        Path(argv[wrapper_index + 2]).unlink()
        return SimpleNamespace(
            returncode=0,
            stdout="",
            stderr="Authorization: Bearer sk-test-v1-abcdefghijklmnopqrstuvwxyz012345\n",
        )

    result = launch(
        role="implementer",
        vendor="driver-d",
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
    assert "sk-test-v1-abcdefghijklmnopqrstuvwxyz012345" not in result.stderr
    assert "Bearer sk-test" not in result.stderr
    assert len(result.stderr) <= 4000


def test_launch_driver_d_in_tmux_keeps_pane_diagnostic_as_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_operator_ai_accounts(tmp_path)
    config_dir = tmp_path / "router-config"
    config_dir.mkdir()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    raw = json.loads((tmp_path / "ai-accounts.json").read_text(encoding="utf-8"))
    raw["accounts"]["account-d"]["config_dir"] = str(config_dir)
    (tmp_path / "ai-accounts.json").write_text(json.dumps(raw), encoding="utf-8")
    spec = tmp_path / "spec.md"
    spec.write_text("implement\n", encoding="utf-8")
    reason = "STATUS: unavailable\ndriver-d key missing\n"
    monkeypatch.setattr(
        "agent_cli.lane._run_in_tmux",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=2, stdout=reason, stderr=""),
    )

    result = launch(
        role="implementer",
        vendor="driver-d",
        spec_file=str(spec),
        cwd=str(worktree),
        tmux=True,
        config_home=tmp_path,
        session_id=DEFAULT_SESSION,
    )

    assert result.status == "unavailable"
    assert result.stdout == ""
    assert "driver-d key missing" in result.stderr


def test_launch_driver_d_missing_key_records_stderr_reason(tmp_path: Path) -> None:
    write_operator_ai_accounts(tmp_path)
    config_dir = tmp_path / "router-config"
    config_dir.mkdir()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    raw = json.loads((tmp_path / "ai-accounts.json").read_text(encoding="utf-8"))
    raw["accounts"]["account-d"]["config_dir"] = str(config_dir)
    (tmp_path / "ai-accounts.json").write_text(json.dumps(raw), encoding="utf-8")
    spec = tmp_path / "spec.md"
    spec.write_text("implement\n", encoding="utf-8")
    reason = "STATUS: unavailable\ndriver-d key missing\n"

    def missing_key(argv: list[str], stdin_text: str | None) -> object:
        assert any("tool-d-key" in value for value in argv)
        assert stdin_text == "implement\n"
        return SimpleNamespace(returncode=2, stdout="", stderr=reason)

    result = launch(
        role="implementer",
        vendor="driver-d",
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
            vendor="driver-a",
            spec_file=str(spec),
            cwd=str(tmp_path),
            runner=boom,
            dry_run=True,
            tmux=False,
        )
    with pytest.raises(SystemExit, match="explicit session and AI configuration home"):
        launch(
            role="implementer",
            vendor="driver-a",
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
            vendor="driver-a",
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
            vendor="driver-a",
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
        vendor="driver-a",
        spec_file=str(spec),
        cwd=str(tmp_path),
        runner=boom,
        dry_run=True,
        tmux=False,
        config_home=tmp_path,
        session_id=DEFAULT_SESSION,
    )
    assert result.returncode == 0
    assert OPERATOR_MODEL_A in result.argv


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
            vendor="driver-a",
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
    raw["sessions"][DEFAULT_SESSION]["lanes"].pop("driver-a:implementer")
    (tmp_path / "ai-accounts.json").write_text(json.dumps(raw), encoding="utf-8")
    spec = tmp_path / "spec.md"
    spec.write_text("do the thing\n", encoding="utf-8")

    with pytest.raises(
        AccountError,
        match=f"No AI lane configured for session {DEFAULT_SESSION} slot driver-a:implementer",
    ):
        launch(
            role="implementer",
            vendor="driver-a",
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
        DEFAULT_SESSION: {"lanes": {"driver-a:implementer": "driver-b-builder"}},
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
            vendor="driver-a",
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
            vendor="driver-a",
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
            vendor="driver-a",
            spec_file=str(spec),
            cwd=str(tmp_path),
            runner=runner,
            tmux=False,
            config_home=tmp_path,
            session_id=DEFAULT_SESSION,
        )
    assert called["n"] == 0


def test_delegated_lane_argv_local_mode_is_unchanged() -> None:
    argv = ["env", "TOOL_A_HOME=/profile", "tool-a", "--prompt-file", "/spec.md"]
    execution = LaneExecution("this-device-is-lane-host")
    assert (
        delegated_lane_argv(
            execution, argv, workdir="/work", reads=("/profile",)
        )
        == argv
    )


def test_delegated_lane_argv_prefixes_order_and_dedupes(tmp_path: Path) -> None:
    execution = LaneExecution(
        "delegate", ("/opt/operator/run-lane", "--profile", "work")
    )
    workdir = tmp_path / "work"
    read_one = tmp_path / "one"
    read_two = tmp_path / "two"
    write_back = tmp_path / "out.txt"

    argv = delegated_lane_argv(
        execution,
        ["env", "tool-a"],
        workdir=str(workdir),
        reads=(str(read_two), str(read_one), str(read_two), ""),
        write_backs=(str(write_back), str(write_back)),
    )

    assert argv == [
        "/opt/operator/run-lane",
        "--profile",
        "work",
        "--workdir",
        str(workdir),
        "--read",
        str(read_two),
        "--read",
        str(read_one),
        "--write-back",
        str(write_back),
        "--",
        "env",
        "tool-a",
    ]


def test_launch_delegate_prefixes_argv_and_skips_tmux(tmp_path: Path) -> None:
    write_operator_ai_accounts(
        tmp_path,
        lane_execution={
            "mode": "delegate",
            "argv": ["/opt/operator/run-lane", "--profile", "work"],
        },
    )
    spec = tmp_path / "spec.md"
    spec.write_text("implement\n", encoding="utf-8")
    seen: list[list[str]] = []

    def fake(argv: list[str], stdin_text: str | None) -> object:
        seen.append(list(argv))
        reads = [
            argv[index + 1]
            for index, value in enumerate(argv[:-1])
            if value == "--read"
        ]
        assert str(spec.resolve()) in reads
        assert OPERATOR_HOME_A in reads
        assert any("agent-lane-empty-gh-" in Path(value).name for value in reads)
        assert all(Path(value).is_absolute() for value in reads)
        return SimpleNamespace(
            returncode=0,
            stdout=event_stream_output("STATUS: complete\nRESULT: done"),
            stderr="",
        )

    result = launch(
        role="implementer",
        vendor="driver-a",
        spec_file=str(spec),
        cwd=str(tmp_path),
        runner=fake,
        tmux=True,
        config_home=tmp_path,
        session_id=DEFAULT_SESSION,
    )

    assert len(seen) == 1
    argv = seen[0]
    assert argv[:3] == ["/opt/operator/run-lane", "--profile", "work"]
    assert argv[argv.index("--workdir") + 1] == str(tmp_path.resolve())
    assert argv[argv.index("--read") + 1] == str(spec.resolve())
    assert argv.count("--workdir") == 1
    assert argv.index("--") < argv.index("env")
    assert "tmux" not in argv
    assert result.tmux_session is None
    assert result.status == "complete"
    assert OPERATOR_MODEL_A in result.argv[result.argv.index("--") :]


def test_launch_delegate_propagates_runner_exception_without_git_change(
    tmp_path: Path,
) -> None:
    write_operator_ai_accounts(
        tmp_path,
        lane_execution={"mode": "delegate", "argv": ["/opt/operator/run-lane"]},
    )
    worktree = tmp_path / "work"
    worktree.mkdir()
    gitdir = worktree / ".git"
    gitdir.mkdir()
    head = gitdir / "HEAD"
    head.write_text("ref: refs/heads/main\n", encoding="utf-8")
    spec = tmp_path / "spec.md"
    spec.write_text("implement\n", encoding="utf-8")

    def fake(argv: list[str], stdin_text: str | None) -> object:
        raise RuntimeError("runner failed")

    with pytest.raises(RuntimeError, match="runner failed"):
        launch(
            role="implementer",
            vendor="driver-a",
            spec_file=str(spec),
            cwd=str(worktree),
            runner=fake,
            tmux=False,
            config_home=tmp_path,
            session_id=DEFAULT_SESSION,
        )

    assert head.read_text(encoding="utf-8") == "ref: refs/heads/main\n"


def test_launch_delegate_allows_tracked_file_write_back(tmp_path: Path) -> None:
    write_operator_ai_accounts(
        tmp_path,
        lane_execution={"mode": "delegate", "argv": ["/opt/operator/run-lane"]},
    )
    worktree = tmp_path / "work"
    worktree.mkdir()
    gitdir = worktree / ".git"
    gitdir.mkdir()
    (gitdir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    spec = tmp_path / "spec.md"
    spec.write_text("implement\n", encoding="utf-8")

    def fake(argv: list[str], stdin_text: str | None) -> object:
        (worktree / "tracked.txt").write_text("changed\n", encoding="utf-8")
        return SimpleNamespace(
            returncode=0,
            stdout=event_stream_output("STATUS: complete\nRESULT: done"),
            stderr="",
        )

    result = launch(
        role="implementer",
        vendor="driver-a",
        spec_file=str(spec),
        cwd=str(worktree),
        runner=fake,
        tmux=False,
        config_home=tmp_path,
        session_id=DEFAULT_SESSION,
    )

    assert result.returncode == 0
    assert result.status == "complete"
    assert (worktree / "tracked.txt").read_text(encoding="utf-8") == "changed\n"


def test_launch_delegate_hardens_the_provider_argv(tmp_path: Path) -> None:
    write_operator_ai_accounts(
        tmp_path,
        lane_execution={
            "mode": "delegate",
            "argv": ["/opt/operator/run-lane", "--no-shell"],
        },
    )
    spec = tmp_path / "spec.md"
    spec.write_text("implement\n", encoding="utf-8")
    seen: list[list[str]] = []

    def fake(argv: list[str], stdin_text: str | None) -> object:
        seen.append(list(argv))
        return SimpleNamespace(
            returncode=0,
            stdout=event_stream_output("STATUS: complete\nRESULT: done"),
            stderr="",
        )

    result = launch(
        role="implementer",
        vendor="driver-a",
        spec_file=str(spec),
        cwd=str(tmp_path),
        runner=fake,
        tmux=False,
        config_home=tmp_path,
        session_id=DEFAULT_SESSION,
    )

    assert result.returncode == 0
    argv = seen[0]
    assert argv[:2] == ["/opt/operator/run-lane", "--no-shell"]
    provider = argv[argv.index("--") + 1 :]
    assert "--no-shell" in provider
    assert provider[-2:] == ["--deny", "Shell"]


def test_launch_local_mode_keeps_the_provider_argv_unhardened(
    tmp_path: Path,
) -> None:
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("implement\n", encoding="utf-8")
    seen: list[list[str]] = []

    def fake(argv: list[str], stdin_text: str | None) -> object:
        seen.append(list(argv))
        return SimpleNamespace(
            returncode=0,
            stdout=event_stream_output("STATUS: complete\nRESULT: done"),
            stderr="",
        )

    result = launch(
        role="implementer",
        vendor="driver-a",
        spec_file=str(spec),
        cwd=str(tmp_path),
        runner=fake,
        tmux=False,
        config_home=tmp_path,
        session_id=DEFAULT_SESSION,
    )

    assert result.returncode == 0
    assert "--no-shell" not in seen[0]
    assert "--deny" not in seen[0]


def test_launch_writable_reviewer_binding_refused_before_runner(tmp_path: Path) -> None:
    write_operator_ai_accounts(tmp_path)
    raw = json.loads((tmp_path / "ai-accounts.json").read_text(encoding="utf-8"))
    raw["sessions"][DEFAULT_SESSION]["lanes"]["driver-a:reviewer"] = "lane-builder"
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
            vendor="driver-a",
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
    monkeypatch.setenv("TOOL_A_API_KEY", "ambient-tool-a-token")
    monkeypatch.setenv("TOOL_C_TOKEN", "ambient-tool-c-token")
    monkeypatch.setenv("TOOL_A_HOME", "/ambient/should-not-leak")
    monkeypatch.setenv("TOOL_B_API_KEY", "ambient-tool-b-token")

    result_a = launch(
        role="implementer",
        vendor="driver-a",
        spec_file=str(spec),
        cwd=str(tmp_path),
        dry_run=True,
        tmux=False,
        config_home=tmp_path,
        session_id=DEFAULT_SESSION,
    )
    result_b = launch(
        role="implementer",
        vendor="driver-a",
        spec_file=str(spec),
        cwd=str(tmp_path),
        dry_run=True,
        tmux=False,
        config_home=tmp_path,
        session_id=ALT_SESSION,
    )

    assert OPERATOR_MODEL_A in result_a.argv
    assert OPERATOR_MODEL_A2 in result_b.argv
    assert OPERATOR_MODEL_A2 not in result_a.argv
    assert OPERATOR_MODEL_A not in result_b.argv
    assert f"TOOL_A_HOME={OPERATOR_HOME_A}" in result_a.argv
    assert f"TOOL_A_HOME={OPERATOR_HOME_A2}" in result_b.argv
    assert "/ambient/should-not-leak" not in result_a.argv
    assert "/ambient/should-not-leak" not in result_b.argv
    assert "ambient-tool-a-token" not in " ".join(result_a.argv)
    assert "ambient-tool-c-token" not in " ".join(result_b.argv)
    assert os.environ["TOOL_A_API_KEY"] == "ambient-tool-a-token"
    assert os.environ["TOOL_A_HOME"] == "/ambient/should-not-leak"
    _assert_nested_role_env_prefix(
        result_a.argv, config_dir=OPERATOR_HOME_A, provider="driver-a"
    )
    _assert_nested_role_env_prefix(
        result_b.argv, config_dir=OPERATOR_HOME_A2, provider="driver-a"
    )


def test_launch_dry_run_does_not_call_runner(tmp_path: Path) -> None:
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("do the thing\n", encoding="utf-8")

    def boom(argv: list[str], stdin_text: str | None) -> object:
        raise AssertionError("runner must not be called on dry_run")

    result = launch(
        role="implementer",
        vendor="driver-a",
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
    assert result.model == OPERATOR_MODEL_A
    assert OPERATOR_MODEL_A in result.argv
    _assert_nested_role_env_prefix(
        result.argv, config_dir=OPERATOR_HOME_A, provider="driver-a"
    )


@pytest.mark.parametrize("vendor", LANE_VENDORS)
def test_every_provider_unsets_ambient_driver_d_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, vendor: str
) -> None:
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("implement\n", encoding="utf-8")
    monkeypatch.setenv("TOOL_D_API_KEY", "ambient-driver-d-token")

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

    key_index = result.argv.index("TOOL_D_API_KEY")
    assert result.argv[key_index - 1] == "-u"
    assert "ambient-driver-d-token" not in result.argv
    assert os.environ["TOOL_D_API_KEY"] == "ambient-driver-d-token"


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


def test_launch_driver_b_dry_run_skips_mkstemp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("driver-b dry run\n", encoding="utf-8")

    def boom_mkstemp(*_args: object, **_kwargs: object) -> tuple[int, str]:
        raise AssertionError("tempfile.mkstemp must not be called on dry_run")

    monkeypatch.setattr("agent_cli.lane.tempfile.mkstemp", boom_mkstemp)
    result = launch(
        role="implementer",
        vendor="driver-b",
        spec_file=str(spec),
        cwd=str(tmp_path),
        dry_run=True,
        tmux=False,
        config_home=tmp_path,
        session_id=DEFAULT_SESSION,
    )
    assert "--last-message" in result.argv
    assert OPERATOR_MODEL_B in result.argv
    _assert_nested_role_env_prefix(
        result.argv, config_dir=OPERATOR_HOME_B, provider="driver-b"
    )


def test_launch_fake_runner_driver_b_stdin(tmp_path: Path) -> None:
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    contents = "driver-b please implement\n"
    spec.write_text(contents, encoding="utf-8")
    seen: list[tuple[list[str], str | None]] = []
    output_paths: list[str] = []

    def fake(argv: list[str], stdin_text: str | None) -> object:
        seen.append((argv, stdin_text))
        out_path = argv[argv.index("--last-message") + 1]
        output_paths.append(out_path)
        Path(out_path).write_text(
            "STATUS: complete\nRESULT: done\n", encoding="utf-8"
        )
        return SimpleNamespace(returncode=0, stdout="STATUS: partial\n", stderr="")

    result = launch(
        role="implementer",
        vendor="driver-b",
        spec_file=str(spec),
        cwd=str(tmp_path),
        runner=fake,
        tmux=False,
        config_home=tmp_path,
        session_id=DEFAULT_SESSION,
    )
    assert len(seen) == 1
    assert seen[0][1] == contents
    assert "tool-b" in seen[0][0]
    assert OPERATOR_MODEL_B in seen[0][0]
    assert result.status == "complete"
    assert result.returncode == 0
    assert output_paths
    assert not Path(output_paths[0]).exists()


@pytest.mark.parametrize("vendor", ["driver-b", "driver-c"])
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
        if vendor == "driver-b":
            output = Path(argv[argv.index("--last-message") + 1])
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
        output = Path(argv[argv.index("--last-message") + 1])
        output.write_text("STATUS: complete\nRESULT: done\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("agent_cli.lane._default_runner", fake_default)
    launch(
        role="implementer",
        vendor="driver-b",
        spec_file=str(spec),
        cwd=str(tmp_path),
        tmux=False,
        config_home=tmp_path,
        session_id=DEFAULT_SESSION,
    )

    assert len(profiles) == 1
    assert not profiles[0].exists()


def test_launch_no_tmux_driver_c_keeps_stream_json_on_stdout(tmp_path: Path) -> None:
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    contents = "driver-c please review\n"
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
        vendor="driver-c",
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


def test_launch_driver_b_unreadable_output_returns_unavailable_not_empty(
    tmp_path: Path,
) -> None:
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("driver-b please implement\n", encoding="utf-8")

    deleted_paths: list[Path] = []

    def deletes_output(argv: list[str], stdin_text: str | None) -> object:
        output_path = Path(argv[argv.index("--last-message") + 1])
        deleted_paths.append(output_path)
        output_path.unlink()
        return SimpleNamespace(returncode=0, stdout="", stderr="driver-b wrote nothing\n")

    result = launch(
        role="implementer",
        vendor="driver-b",
        spec_file=str(spec),
        cwd=str(tmp_path),
        runner=deletes_output,
        tmux=False,
        config_home=tmp_path,
        session_id=DEFAULT_SESSION,
    )

    assert result.status == "unavailable"
    assert result.stdout == ""
    assert "driver-b wrote nothing" in result.stderr
    assert "lane output file is unavailable" in result.stderr
    assert deleted_paths
    assert str(deleted_paths[0]) not in result.stderr
    assert "FileNotFoundError" not in result.stderr


def test_launch_driver_b_unlinks_output_file_on_runner_exception(tmp_path: Path) -> None:
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("driver-b please fail\n", encoding="utf-8")
    out_path: str | None = None

    def fake(argv: list[str], stdin_text: str | None) -> object:
        nonlocal out_path
        out_path = argv[argv.index("--last-message") + 1]
        Path(out_path).write_text("STATUS: complete\n", encoding="utf-8")
        raise RuntimeError("driver-b runner failed")

    with pytest.raises(RuntimeError, match="driver-b runner failed"):
        launch(
            role="implementer",
            vendor="driver-b",
            spec_file=str(spec),
            cwd=str(tmp_path),
            runner=fake,
            tmux=False,
            config_home=tmp_path,
            session_id=DEFAULT_SESSION,
        )
    assert out_path is not None
    assert not Path(out_path).exists()


def test_launch_fake_runner_driver_a_stdin_none_or_empty(tmp_path: Path) -> None:
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("driver-a please implement\n", encoding="utf-8")
    seen: list[str | None] = []

    @dataclass
    class FakeDone:
        returncode: int
        stdout: str
        stderr: str

    def fake(argv: list[str], stdin_text: str | None) -> object:
        seen.append(stdin_text)
        return FakeDone(
            0, event_stream_output("STATUS: complete\nRESULT: done\n"), ""
        )

    result = launch(
        role="implementer",
        vendor="driver-a",
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
    inner = ["env", "-u", "TOOL_C_API_KEY", "tool-a", "--prompt-file", "s"]
    argv = tmux_wrap_argv(inner, name="agent-lane-driver-a-implementer", cwd="/work")
    assert argv[:6] == [
        "tmux",
        "new-session",
        "-d",
        "-s",
        "agent-lane-driver-a-implementer",
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
        vendor="driver-a",
        spec_file=str(spec),
        cwd=str(tmp_path),
        dry_run=True,
        config_home=tmp_path,
        session_id=DEFAULT_SESSION,
    )
    assert result.argv[:3] == ["tmux", "new-session", "-d"]
    assert "-s" in result.argv
    assert result.tmux_session is not None
    assert result.tmux_session.startswith("agent-lane-driver-a-implementer")
    assert "--" in result.argv
    assert OPERATOR_MODEL_A in result.argv
    inner = result.argv[result.argv.index("--") + 1 :]
    # The tmux pane redirects the message stream to a temp file, then runs env.
    assert inner[0] == "sh"
    assert inner[1] == "-c"
    assert inner[3] == "sh"
    assert Path(inner[4]).name.startswith("agent-lane-driver-a-")
    assert inner[4].endswith(".txt")
    assert inner[5] == "env"
    _assert_nested_role_env_prefix(
        result.argv, config_dir=OPERATOR_HOME_A, provider="driver-a"
    )


def test_launch_no_tmux_starts_with_env(tmp_path: Path) -> None:
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("implement me\n", encoding="utf-8")
    result = launch(
        role="implementer",
        vendor="driver-a",
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
    assert OPERATOR_MODEL_A in result.argv


def test_launch_tmux_fake_runner_gets_wrapped_argv(tmp_path: Path) -> None:
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("implement me\n", encoding="utf-8")
    seen: list[list[str]] = []

    def fake(argv: list[str], stdin_text: str | None) -> object:
        seen.append(list(argv))
        output = next(
            Path(value)
            for value in argv
            if value.endswith(".txt")
            and Path(value).name.startswith("agent-lane-driver-a-")
        )
        output.write_text(
            event_stream_output("STATUS: complete\nRESULT: done\n"),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    result = launch(
        role="implementer",
        vendor="driver-a",
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
    result = _run_in_tmux(["tool-a", "--prompt-file", "s"], name="agent-lane-t", cwd="/w", stdin_text=None)
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

    token = "sk-test-v1-abcdefghijklmnopqrstuvwxyz012345"

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
        ["tool-a"], name="agent-lane-t", cwd="/w", stdin_text=None
    )
    diagnostic = redact_diagnostic(captured.stdout)
    store = CaptureStore()
    record_lane_run(
        store,
        session_id=DEFAULT_SESSION,
        result=LaneResult(
            role="implementer",
            vendor="driver-a",
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
    assert "sk-test-v1-" not in diagnostic
    assert "qrstuvwxyz012345" not in diagnostic
    assert "[redacted]" in diagnostic
    assert store.row is not None
    evidence = store.row["payload"]["final_text"]
    assert token not in evidence
    assert "sk-test-v1-" not in evidence
    assert "qrstuvwxyz012345" not in evidence
    assert "[redacted]" in evidence


@pytest.mark.parametrize("vendor", ["driver-c", "driver-d", "driver-a"])
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
    result = _run_in_tmux(["tool-a"], name="agent-lane-t", cwd="/w", stdin_text=None)
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
    result = _run_in_tmux(["tool-a"], name="agent-lane-t", cwd="/w", stdin_text=None)
    assert result.returncode == 1


def test_run_in_tmux_kills_on_remain_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(argv: list[str], _calls: list[list[str]]) -> CompletedProcess[str]:
        if "remain-on-exit" in argv:
            return CompletedProcess(argv, 1, "", "no tmux")
        return CompletedProcess(argv, 0, "", "")

    fake, calls = _tmux_script(handler)
    monkeypatch.setattr("agent_cli.lane._tmux_call", fake)
    result = _run_in_tmux(["tool-a"], name="agent-lane-t", cwd="/w", stdin_text=None)
    assert result.returncode == 1
    assert any("kill-session" in c for c in calls)


def test_run_in_tmux_kills_on_pane_dead_query_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(argv: list[str], _calls: list[list[str]]) -> CompletedProcess[str]:
        if argv[-1] == "#{pane_dead}":
            return CompletedProcess(argv, 2, "", "gone")
        return CompletedProcess(argv, 0, "", "")

    fake, calls = _tmux_script(handler)
    monkeypatch.setattr("agent_cli.lane._tmux_call", fake)
    result = _run_in_tmux(["tool-a"], name="agent-lane-t", cwd="/w", stdin_text=None)
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
    _run_in_tmux(["tool-b"], name="agent-lane-t", cwd="/w", stdin_text="no-newline")
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
        vendor="driver-a",
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
    result = _run_in_tmux(["tool-b"], name="agent-lane-t", cwd="/w", stdin_text="spec")
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
            vendor="driver-a",
            model=OPERATOR_MODEL_A,
            status="complete",
            argv=["tool-a"],
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
            "driver-a",
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
            vendor="driver-a",
            model=OPERATOR_MODEL_A,
            status="unavailable",
            argv=["tool-a"],
            returncode=1,
            stdout="",
            stderr="driver-a: rate limited, distinctive-marker-xyz789",
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
                "driver-a",
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


def test_cli_lane_run_invalid_driver_d_output_is_a_controlled_failure(
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
            i for i, value in enumerate(argv) if "tool-d-key" in value
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
                "driver-d",
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


def test_cli_dry_run_implementer_driver_a(
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
            "driver-a",
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
    assert OPERATOR_MODEL_A in out
    assert f"TOOL_A_HOME={OPERATOR_HOME_A}" in out
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
            "driver-a",
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
    assert OPERATOR_MODEL_A in out
    assert f"TOOL_A_HOME={OPERATOR_HOME_A}" in out


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
                "driver-a",
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
                "driver-a",
                "--session",
                DEFAULT_SESSION,
                "--spec-file",
                str(tmp_path / "missing.md"),
            ]
        )


def test_cli_unknown_vendor_dies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    write_drivers(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("x\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="vendor must be driver-a"):
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
                "driver-a",
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
                "driver-a",
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


# --- placeholder drivers ---------------------------------------------------------

PLACEHOLDER_SESSION = "session-a"


def _placeholder_home(tmp_path: Path, *, accounts: dict | None = None) -> Path:
    write_drivers(tmp_path)
    write_placeholder_accounts(tmp_path, accounts)
    spec = tmp_path / "spec.md"
    spec.write_text("Implement the change.\n", encoding="utf-8")
    return spec


def _placeholder_launch(tmp_path: Path, spec: Path, driver: str, **kwargs) -> LaneResult:
    options = {
        "role": "implementer",
        "cwd": str(tmp_path),
        "config_home": tmp_path,
        "session_id": PLACEHOLDER_SESSION,
        **kwargs,
    }
    return launch(vendor=driver, spec_file=str(spec), **options)


def test_launch_builds_the_argv_from_the_driver_template(tmp_path: Path) -> None:
    spec = _placeholder_home(tmp_path)
    result = _placeholder_launch(tmp_path, spec, "driver-a", dry_run=True, tmux=False)
    inner = result.argv[result.argv.index("tool-a") :]
    assert inner == [
        "tool-a", "--prompt-file", str(spec.resolve()), "-m", "driver-a-model",
        "--edit", "--cwd", str(tmp_path.resolve()),
    ]
    lane_env = result.argv.index("env", result.argv.index("TOOL_A_HOME=/operator/path/tool-a"))
    for key in ("TOOL_A_API_KEY", "TOOL_B_API_KEY", "TOOL_D_API_KEY", "HTTP_PROXY"):
        assert result.argv[result.argv.index(key, lane_env) - 1] == "-u"
    assert result.audit_stderr_when_empty is False


@pytest.mark.parametrize(
    ("driver", "redirect"),
    [
        ("driver-a", ["sh", "-c", 'out=$1; shift; exec "$@" > "$out"', "sh", "OUT"]),
        ("driver-b", []),
        (
            "driver-c",
            ["sh", "-c", 'spec=$1; out=$2; shift 2; exec "$@" < "$spec" > "$out"', "sh", "SPEC", "OUT"],
        ),
        ("driver-d", ["sh", "-c", 'f=$1; shift; exec "$@" < "$f"', "sh", "SPEC"]),
    ],
)
def test_tmux_lane_redirects_follow_the_driver_io(
    tmp_path: Path, driver: str, redirect: list[str]
) -> None:
    spec = _placeholder_home(tmp_path)
    result = _placeholder_launch(tmp_path, spec, driver, dry_run=True, tmux=True)
    inner = result.argv[result.argv.index("--") + 1 :]
    names = {"SPEC": str(spec.resolve()), "OUT": f"/tmp/agent-lane-{driver}-dry-run.txt"}
    assert inner[: len(redirect)] == [names.get(item, item) for item in redirect]
    assert inner[len(redirect)] == "env"


def test_launch_refuses_unconfigured_and_record_only_drivers(tmp_path: Path) -> None:
    spec = _placeholder_home(tmp_path)
    refusal = (
        "vendor must be driver-a|driver-b|driver-c|driver-d "
        "(see drivers.json, docs/ai-accounts.md#drivers)"
    )
    for driver in ("driver-x", "driver-e"):
        with pytest.raises(SystemExit) as excinfo:
            _placeholder_launch(tmp_path, spec, driver, dry_run=True)
        assert str(excinfo.value) == refusal


def test_launch_without_drivers_json_fails_loudly(tmp_path: Path) -> None:
    write_placeholder_accounts(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("Implement.\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="drivers.json"):
        _placeholder_launch(tmp_path, spec, "driver-a", dry_run=True)


def test_write_lane_with_read_directories_is_refused_for_a_read_dir_driver(
    tmp_path: Path,
) -> None:
    spec = _placeholder_home(tmp_path)
    with pytest.raises(SystemExit, match=WRITE_READ_DIRS_REFUSED):
        _placeholder_launch(
            tmp_path, spec, "driver-c", dry_run=True, read_dirs=(str(tmp_path),)
        )


def test_launch_refuses_a_worktree_inside_the_key_directory(tmp_path: Path) -> None:
    accounts = accounts_document()
    accounts["accounts"]["driver-d-account"]["config_dir"] = str(tmp_path)
    spec = _placeholder_home(tmp_path, accounts=accounts)
    worktree = tmp_path / "work"
    worktree.mkdir()
    with pytest.raises(SystemExit, match="driver-d worktree overlaps account config_dir"):
        _placeholder_launch(tmp_path, spec, "driver-d", cwd=str(worktree), dry_run=True)


@pytest.mark.parametrize(("driver", "fallback"), [("driver-a", False), ("driver-c", True)])
def test_audit_text_falls_back_to_stderr_only_when_configured(
    tmp_path: Path, driver: str, fallback: bool
) -> None:
    spec = _placeholder_home(tmp_path)
    result = _placeholder_launch(
        tmp_path,
        spec,
        driver,
        tmux=False,
        runner=lambda _argv, _stdin: SimpleNamespace(
            returncode=2, stdout="", stderr="driver failed before output"
        ),
    )

    class CaptureStore:
        row: dict | None = None

        def write(self, _table: str, _operation: str, _row_id: str, row: dict) -> None:
            self.row = row

    store = CaptureStore()
    record_lane_run(store, session_id=PLACEHOLDER_SESSION, result=result)
    assert store.row is not None
    assert store.row["payload"]["final_text"] == (
        "driver failed before output" if fallback else ""
    )


# Final text of an external reviewer lane, shape kept from a reproduced run and
# content replaced: the caller's own result line ("RESULT: <N> defects") ahead
# of sections and the single STATUS line the lane starter asks for. The raw text
# carried no escape or control characters.
EXTERNAL_REVIEWER_FINAL_TEXT = (
    "The reviewed function returns the expected value for every input.\n"
    "\n"
    "RESULT: 0 defects\n"
    "\n"
    "HINTS\n"
    "none\n"
    "\n"
    "NOT-VERIFIABLE\n"
    "none\n"
    "\n"
    "STATUS: complete"
)


def test_lane_status_reads_the_status_line_alone() -> None:
    from agent_cli.coordinator_common import parse_lane_status, parse_model_result

    sample = EXTERNAL_REVIEWER_FINAL_TEXT
    assert sanitize_terminal_text(sample) == sample
    assert parse_lane_status(sample, 0) == "complete"
    # The joint verdict still requires one role-valid RESULT record.
    assert parse_model_result(sample, 0) == ("partial", "")
    clean = "Review done.\n\nSTATUS: complete\nRESULT: approved\n"
    assert parse_lane_status(clean, 0) == "complete"
    assert parse_model_result(clean, 0) == ("complete", "approved")
    assert parse_lane_status("Review done.\n\nSTATUS: partial\n", 0) == "partial"
    assert parse_lane_status("Review done.\r\nSTATUS: complete\r\n", 0) == "complete"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Review done.\n",
        "Review done.\nSTATUS: complete\nSTATUS: complete\n",
        "Review done.\nSTATUS: complete\nSTATUS: partial\n",
        "Review done.\nSTATUS: finished\n",
        "Review done.STATUS: complete\n",
        "Review done.\n  STATUS: complete\n",
        "Review done.\nSTATUS: complete, all checked\n",
    ],
)
def test_lane_status_fails_closed(text: str) -> None:
    from agent_cli.coordinator_common import parse_lane_status

    assert parse_lane_status(text, 0) == "partial"


@pytest.mark.parametrize(
    ("returncode", "expected"), [(124, "timeout"), (1, "unavailable"), (-9, "unavailable")]
)
def test_lane_status_nonzero_returncode_is_unchanged(
    returncode: int, expected: str
) -> None:
    from agent_cli.coordinator_common import parse_lane_status, parse_model_result

    assert parse_lane_status(EXTERNAL_REVIEWER_FINAL_TEXT, returncode) == expected
    assert parse_model_result(EXTERNAL_REVIEWER_FINAL_TEXT, returncode) == (
        expected,
        "",
    )


def _launch_driver_d_with_final_text(tmp_path: Path, final_text: str) -> LaneResult:
    write_operator_ai_accounts(tmp_path)
    config_dir = tmp_path / "router-config"
    config_dir.mkdir()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    raw = json.loads((tmp_path / "ai-accounts.json").read_text(encoding="utf-8"))
    raw["accounts"]["account-d"]["config_dir"] = str(config_dir)
    (tmp_path / "ai-accounts.json").write_text(json.dumps(raw), encoding="utf-8")
    spec = tmp_path / "spec.md"
    spec.write_text("review\n", encoding="utf-8")

    def fake(argv: list[str], stdin_text: str | None) -> object:
        wrapper_index = next(i for i, value in enumerate(argv) if "tool-d-key" in value)
        Path(argv[wrapper_index + 2]).write_text(
            json.dumps({"type": "result", "result": final_text}) + "\n",
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return launch(
        role="reviewer",
        vendor="driver-d",
        spec_file=str(spec),
        cwd=str(worktree),
        runner=fake,
        tmux=False,
        config_home=tmp_path,
        session_id=DEFAULT_SESSION,
    )


def test_launch_keeps_joint_verdict_and_reports_lane_status(tmp_path: Path) -> None:
    result = _launch_driver_d_with_final_text(tmp_path, EXTERNAL_REVIEWER_FINAL_TEXT)
    assert result.lane_status == "complete"
    assert result.status == "partial"
    assert result.protocol_result == ""


def test_launch_duplicate_status_fails_closed_for_both_verdicts(
    tmp_path: Path,
) -> None:
    result = _launch_driver_d_with_final_text(
        tmp_path, "STATUS: complete\n" + EXTERNAL_REVIEWER_FINAL_TEXT
    )
    assert result.lane_status == "partial"
    assert result.status == "partial"


def _run_cli_lane_with(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result: LaneResult
) -> None:
    write_operator_ai_accounts(tmp_path)
    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    spec = tmp_path / "spec.md"
    spec.write_text("review this\n", encoding="utf-8")
    monkeypatch.setattr("agent_cli.main.launch", lambda **_kwargs: result)
    run(
        [
            "lane",
            "run",
            "--role",
            "reviewer",
            "--vendor",
            "driver-a",
            "--session",
            DEFAULT_SESSION,
            "--spec-file",
            str(spec),
            "--cwd",
            str(tmp_path),
            "--no-tmux",
        ]
    )


def _cli_lane_result(*, status: str, lane_status: str, returncode: int = 0) -> LaneResult:
    return LaneResult(
        role="reviewer",
        vendor="driver-a",
        model=OPERATOR_MODEL_A,
        status=status,
        argv=["tool-a"],
        returncode=returncode,
        stdout=EXTERNAL_REVIEWER_FINAL_TEXT,
        stderr="",
        lane_status=lane_status,
    )


def test_cli_lane_run_reports_the_lane_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_cli_lane_with(
        tmp_path,
        monkeypatch,
        _cli_lane_result(status="partial", lane_status="complete"),
    )
    out = capsys.readouterr().out
    assert out.endswith("lane role=reviewer vendor=driver-a STATUS=complete rc=0\n")


@pytest.mark.parametrize(
    ("status", "lane_status", "returncode", "printed"),
    [
        ("partial", "partial", 0, "STATUS=partial rc=0"),
        ("unavailable", "unavailable", 1, "STATUS=unavailable rc=1"),
        ("timeout", "timeout", 124, "STATUS=timeout rc=124"),
        # A result that was never parsed falls back to the joint verdict.
        ("unavailable", "", 0, "STATUS=unavailable rc=0"),
    ],
)
def test_cli_lane_run_exits_2_unless_the_lane_status_is_complete(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    lane_status: str,
    returncode: int,
    printed: str,
) -> None:
    with pytest.raises(SystemExit) as exc:
        _run_cli_lane_with(
            tmp_path,
            monkeypatch,
            _cli_lane_result(
                status=status, lane_status=lane_status, returncode=returncode
            ),
        )
    assert exc.value.code == 2
    assert capsys.readouterr().out.endswith(
        f"lane role=reviewer vendor=driver-a {printed}\n"
    )


def _write_spec(tmp_path: Path, text: str = "review\n") -> Path:
    spec = tmp_path / "spec.md"
    spec.write_text(text, encoding="utf-8")
    return spec


def test_launch_refuses_an_unknown_role(tmp_path: Path) -> None:
    write_operator_ai_accounts(tmp_path)
    with pytest.raises(SystemExit, match="role must be"):
        launch(
            role="not-a-role",
            vendor="driver-a",
            spec_file=str(_write_spec(tmp_path)),
            cwd=str(tmp_path),
            dry_run=True,
            config_home=tmp_path,
            session_id=DEFAULT_SESSION,
        )


def test_launch_refuses_a_binding_for_another_driver(tmp_path: Path) -> None:
    from agent_cli.ai_accounts import load_ai_accounts

    write_operator_ai_accounts(tmp_path)
    other = load_ai_accounts(tmp_path, defer_model_configuration=True).for_lane(
        DEFAULT_SESSION, "reviewer", "driver-b", require_execution=False
    )
    with pytest.raises(SystemExit, match="does not match requested vendor"):
        launch(
            role="reviewer",
            vendor="driver-a",
            spec_file=str(_write_spec(tmp_path)),
            cwd=str(tmp_path),
            dry_run=True,
            config_home=tmp_path,
            session_id=DEFAULT_SESSION,
            binding=other,
        )


def test_launch_accepts_a_binding_for_the_requested_driver(tmp_path: Path) -> None:
    from agent_cli.ai_accounts import load_ai_accounts

    write_operator_ai_accounts(tmp_path)
    binding = load_ai_accounts(tmp_path, defer_model_configuration=True).for_lane(
        DEFAULT_SESSION, "reviewer", "driver-a", require_execution=False
    )
    result = launch(
        role="reviewer",
        vendor="driver-a",
        spec_file=str(_write_spec(tmp_path)),
        cwd=str(tmp_path),
        dry_run=True,
        config_home=tmp_path,
        session_id=DEFAULT_SESSION,
        binding=binding,
    )
    assert result.vendor == "driver-a"
    assert result.model == binding.model


def test_launch_write_lane_ignores_read_directories_for_a_driver_without_them(
    tmp_path: Path,
) -> None:
    write_operator_ai_accounts(tmp_path)
    extra = tmp_path / "extra-read"
    extra.mkdir()
    result = launch(
        role="implementer",
        vendor="driver-a",
        spec_file=str(_write_spec(tmp_path)),
        cwd=str(tmp_path),
        dry_run=True,
        config_home=tmp_path,
        session_id=DEFAULT_SESSION,
        read_dirs=(str(extra),),
    )
    assert result.vendor == "driver-a"
    assert not any(str(extra.resolve()) in arg for arg in result.argv)


def test_launch_keeps_child_stderr_when_the_output_file_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_operator_ai_accounts(tmp_path)
    config_dir = tmp_path / "router-config"
    config_dir.mkdir()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    raw = json.loads((tmp_path / "ai-accounts.json").read_text(encoding="utf-8"))
    raw["accounts"]["account-d"]["config_dir"] = str(config_dir)
    (tmp_path / "ai-accounts.json").write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(
        "agent_cli.lane._run_in_tmux",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=2, stdout="pane text\n", stderr="child stderr\n"
        ),
    )
    result = launch(
        role="implementer",
        vendor="driver-d",
        spec_file=str(_write_spec(tmp_path)),
        cwd=str(worktree),
        tmux=True,
        config_home=tmp_path,
        session_id=DEFAULT_SESSION,
    )
    assert result.status == "unavailable"
    assert result.stdout == ""
    assert "child stderr" in result.stderr
    assert "pane text" not in result.stderr


def test_launch_refuses_an_empty_spec(tmp_path: Path) -> None:
    write_operator_ai_accounts(tmp_path)
    with pytest.raises(SystemExit, match="spec-file is empty"):
        launch(
            role="reviewer",
            vendor="driver-a",
            spec_file=str(_write_spec(tmp_path, " \n")),
            cwd=str(tmp_path),
            dry_run=True,
            config_home=tmp_path,
            session_id=DEFAULT_SESSION,
        )


def test_launch_removes_the_empty_github_profile_when_the_command_cannot_be_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_operator_ai_accounts(tmp_path)
    spec = _write_spec(tmp_path)
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(temp_root))

    def broken_wrap(inner: list[str], *, name: str, cwd: str) -> list[str]:
        assert any(temp_root.glob("agent-lane-empty-gh-*"))
        raise RuntimeError("wrap failed")

    monkeypatch.setattr("agent_cli.lane.tmux_wrap_argv", broken_wrap)
    with pytest.raises(RuntimeError, match="wrap failed"):
        launch(
            role="reviewer",
            vendor="driver-a",
            spec_file=str(spec),
            cwd=str(tmp_path),
            dry_run=True,
            config_home=tmp_path,
            session_id=DEFAULT_SESSION,
        )
    assert not any(temp_root.glob("agent-lane-empty-gh-*"))


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ("separator", "tmux argv missing command separator"),
        ("session", "tmux session name missing"),
    ],
)
def test_launch_in_tmux_refuses_an_incomplete_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patch: str, message: str
) -> None:
    write_operator_ai_accounts(tmp_path)
    started: list[list[str]] = []
    monkeypatch.setattr(
        "agent_cli.lane._run_in_tmux",
        lambda inner, **_kwargs: started.append(inner),
    )
    if patch == "separator":
        monkeypatch.setattr(
            "agent_cli.lane.tmux_wrap_argv",
            lambda inner, *, name, cwd: ["tmux", "new-session", "-d", *inner],
        )
    else:
        monkeypatch.setattr("agent_cli.lane.lane_tmux_name", lambda **_kwargs: None)
    with pytest.raises(SystemExit, match=message):
        launch(
            role="reviewer",
            vendor="driver-a",
            spec_file=str(_write_spec(tmp_path)),
            cwd=str(tmp_path),
            config_home=tmp_path,
            session_id=DEFAULT_SESSION,
        )
    assert started == []


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["lane"], "Usage: agent lane"),
        (["lane", "start"], "Usage: agent lane"),
        (
            [
                "lane",
                "run",
                "--role",
                "not-a-role",
                "--vendor",
                "driver-a",
                "--session",
                DEFAULT_SESSION,
                "--spec-file",
                "spec.md",
            ],
            "role must be",
        ),
    ],
)
def test_cli_lane_refuses_bad_arguments_before_launch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    message: str,
) -> None:
    write_operator_ai_accounts(tmp_path)
    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    _write_spec(tmp_path)

    def no_launch(**_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("launch must not start")

    monkeypatch.setattr("agent_cli.main.launch", no_launch)
    with pytest.raises(SystemExit) as exc:
        run(argv)
    captured = capsys.readouterr()
    assert message in f"{exc.value.code}{captured.err}{captured.out}"
