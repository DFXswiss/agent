from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace

import pytest

from agent_cli.ai_accounts import AccountError
from agent_cli.lane import (
    LaneResult,
    launch,
    parse_status,
)
from agent_cli.main import _sanitize_lane_output, main
from agent_cli.lane_protocol import ProtocolError

pytestmark = pytest.mark.no_pg


@pytest.mark.parametrize("final,expected", [
    ("STATUS: complete\nRESULT: done", "complete"),
    ("STATUS: complete\nRESULT: blocked", "partial"),
    ("STATUS: complete\nRESULT: ask", "partial"),
    ("STATUS: complete", "partial"),
])
def test_bounded_launch_requires_actual_done_and_needs_no_github(tmp_path, monkeypatch, final, expected):
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "task.md"
    spec.write_text("bounded task")
    monkeypatch.setattr("agent_cli.lane_workspace.local_manifest", lambda cwd: ["a.py"])
    seen = []
    def executor(selected, **kwargs):
        seen.append((selected, kwargs))
        return CompletedProcess([], 0, final, "")
    monkeypatch.setattr("agent_cli.lane_executor.execute", executor)
    result = launch(role="implementer", vendor="grok", cwd=str(tmp_path), spec_file=str(spec),
                    config_home=tmp_path, session_id=DEFAULT_SESSION)
    assert result.status == expected and result.stdout == final
    assert result.argv == [] and result.tmux_session is None
    assert seen[0][1]["manifest"] == ["a.py"]
    assert seen[0][0].account.name == "grok-a"


def test_legacy_runner_cannot_receive_unrestricted_native_command(tmp_path):
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "task.md"
    spec.write_text("task")
    calls = []
    with pytest.raises(ProtocolError, match="legacy"):
        launch(role="implementer", vendor="grok", cwd=str(tmp_path), spec_file=str(spec),
               config_home=tmp_path, session_id=DEFAULT_SESSION, runner=lambda *a: calls.append(a))
    assert calls == []


def test_dry_run_selects_explicit_session_without_starting_transport(tmp_path, monkeypatch):
    write_operator_ai_accounts(tmp_path)
    spec = tmp_path / "task.md"
    spec.write_text("task")
    def fail(*a, **kw):
        raise AssertionError("dry run started work")
    monkeypatch.setattr("agent_cli.lane_executor.execute", fail)
    monkeypatch.setattr("agent_cli.lane_workspace.local_manifest", fail)
    for session, account, model in [(DEFAULT_SESSION, "grok-a", OPERATOR_GROK_MODEL),
                                    (ALT_SESSION, "grok-b", OPERATOR_GROK_MODEL_B)]:
        result = launch(role="implementer", vendor="grok", cwd=str(tmp_path), spec_file=str(spec),
                        config_home=tmp_path, session_id=session, dry_run=True)
        plan = json.loads(result.stdout)
        assert plan["account"] == account and plan["model"] == model
        assert result.argv == [] and result.tmux_session is None

OPERATOR_GROK_MODEL = "operator-lane-grok-model"
OPERATOR_CODEX_MODEL = "operator-lane-codex-model"
OPERATOR_GROK_HOME = "/operator/path/grok-lane-a"
OPERATOR_CODEX_HOME = "/operator/path/codex-lane-a"
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
        "accounts": {
            "grok-a": {"provider": "grok", "config_dir": OPERATOR_GROK_HOME},
            "codex-a": {"provider": "codex", "config_dir": OPERATOR_CODEX_HOME},
            "grok-b": {"provider": "grok", "config_dir": OPERATOR_GROK_HOME_B},
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
            "lane-builder-b": {
                "account": "grok-b",
                "model": OPERATOR_GROK_MODEL_B,
                "access": "workspace-write",
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
    for account in data["accounts"].values():
        account["lane_runtime"] = {"binary": "/explicit/native", "sha256": "0" * 64}
    (home / "ai-accounts.json").write_text(json.dumps(data), encoding="utf-8")


def run(argv: list[str]) -> None:
    main(argv)


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
    """Genuinely empty defaults: no ai-accounts.json, runner must not start."""
    spec = tmp_path / "spec.md"
    spec.write_text("do the thing\n", encoding="utf-8")
    called = {"n": 0}

    def boom(argv: list[str], stdin_text: str | None) -> object:
        called["n"] += 1
        raise AssertionError("runner must not be called for unconfigured lane")

    with pytest.raises(AccountError, match="No AI session configured"):
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
    assert "bounded-source" in out
    assert "new-session" not in out
    assert OPERATOR_GROK_MODEL in out
    assert "grok-a" in out
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
    assert "bounded-source" in out
    assert "new-session" not in out
    assert OPERATOR_GROK_MODEL in out
    assert "grok-a" in out


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
    """Empty defaults: CLI lane run must fail without starting a vendor process."""
    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    spec = tmp_path / "spec.md"
    spec.write_text("implement\n", encoding="utf-8")
    # Do not mock launch: AccountError from real launch must surface via main.
    # dry-run still resolves accounts before building argv, so no process starts.
    with pytest.raises(SystemExit, match="No AI session configured"):
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
