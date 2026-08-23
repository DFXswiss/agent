from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_cli.lane import (
    GROK_STRIP_ENV,
    codex_argv,
    grok_argv,
    launch,
    parse_status,
)
from agent_cli.main import main

pytestmark = pytest.mark.no_pg


def run(argv: list[str]) -> None:
    main(argv)


def test_grok_implementer_argv() -> None:
    argv = grok_argv(spec_file="/tmp/spec.md", cwd="/work", write=True)
    assert "--session-id" not in argv
    assert "--always-approve" not in argv
    assert argv[0] == "env"
    for key in GROK_STRIP_ENV:
        assert "-u" in argv
        assert key in argv
    assert argv[argv.index("grok") :] == [
        "grok",
        "--prompt-file",
        "/tmp/spec.md",
        "-m",
        "grok-4.5",
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
    # env strip order preserved
    strip_idx = [argv.index(k) for k in GROK_STRIP_ENV]
    assert strip_idx == sorted(strip_idx)


def test_grok_reviewer_argv() -> None:
    argv = grok_argv(spec_file="/tmp/spec.md", cwd="/work", write=False)
    assert "--permission-mode" not in argv
    assert "acceptEdits" not in argv
    assert "--always-approve" not in argv
    assert "--session-id" not in argv
    assert argv[argv.index("grok") :] == [
        "grok",
        "--prompt-file",
        "/tmp/spec.md",
        "-m",
        "grok-4.5",
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
    spec = tmp_path / "spec.md"
    spec.write_text("review this\n", encoding="utf-8")
    result = launch(
        role="pr-reviewer-quality",
        vendor="grok",
        spec_file=str(spec),
        cwd=str(tmp_path),
        dry_run=True,
    )
    assert "--deny" in result.argv
    assert "Write" in result.argv
    assert "acceptEdits" not in result.argv
    assert "--permission-mode" not in result.argv


def test_codex_implementer_argv() -> None:
    argv = codex_argv(cwd="/work", write=True, output_file="/tmp/out.txt")
    assert "workspace-write" in argv
    assert "gpt-5.6-sol" in argv
    assert argv[-1] == "-"
    assert argv[0] == "env"
    for key in GROK_STRIP_ENV:
        assert key in argv


def test_codex_reviewer_argv() -> None:
    argv = codex_argv(cwd="/work", write=False, output_file="/tmp/out.txt")
    assert "read-only" in argv
    assert "workspace-write" not in argv
    assert argv[-1] == "-"


def test_parse_status_complete() -> None:
    assert parse_status("hello\nSTATUS: complete\n", 0) == "complete"


def test_parse_status_last_line_wins() -> None:
    assert parse_status("STATUS: complete\nSTATUS: partial\n", 0) == "partial"


def test_parse_status_rc_124_timeout() -> None:
    assert parse_status("no status here", 124) == "timeout"


def test_parse_status_rc_nonzero_unavailable() -> None:
    assert parse_status("no status here", 1) == "unavailable"


def test_parse_status_rc_zero_partial() -> None:
    assert parse_status("no status here", 0) == "partial"


def test_launch_dry_run_does_not_call_runner(tmp_path: Path) -> None:
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
    )
    assert result.status == ""
    assert result.returncode == 0
    assert "grok-4.5" in result.argv


def test_launch_fake_runner_codex_stdin(tmp_path: Path) -> None:
    spec = tmp_path / "spec.md"
    contents = "codex please implement\n"
    spec.write_text(contents, encoding="utf-8")
    seen: list[tuple[list[str], str | None]] = []

    def fake(argv: list[str], stdin_text: str | None) -> object:
        seen.append((argv, stdin_text))
        return SimpleNamespace(returncode=0, stdout="STATUS: complete\n", stderr="")

    result = launch(
        role="implementer",
        vendor="codex",
        spec_file=str(spec),
        cwd=str(tmp_path),
        runner=fake,
    )
    assert len(seen) == 1
    assert seen[0][1] == contents
    assert "codex" in seen[0][0]
    assert result.status == "complete"
    assert result.returncode == 0


def test_launch_fake_runner_grok_stdin_none_or_empty(tmp_path: Path) -> None:
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
    )
    assert seen == [None] or seen == [""]
    assert result.status == "complete"


def test_cli_dry_run_implementer_grok(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
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
            "--spec-file",
            str(spec),
            "--cwd",
            str(tmp_path),
            "--dry-run",
        ]
    )
    out = capsys.readouterr().out.strip()
    assert "grok-4.5" in out
    assert "STATUS=" not in out


def test_cli_missing_spec_dies(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="spec"):
        run(
            [
                "lane",
                "run",
                "--role",
                "implementer",
                "--vendor",
                "grok",
                "--spec-file",
                str(tmp_path / "missing.md"),
            ]
        )


def test_cli_unknown_vendor_dies(tmp_path: Path) -> None:
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
                "--spec-file",
                str(spec),
            ]
        )
