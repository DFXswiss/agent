from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace

import pytest

from agent_cli.lane import (
    GROK_STRIP_ENV,
    LaneResult,
    _run_in_tmux,
    codex_argv,
    grok_argv,
    has_single_terminal_report,
    launch,
    parse_status,
    tmux_wrap_argv,
)
from agent_cli.main import _sanitize_lane_output, main

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
        tmux=False,
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


def test_has_single_terminal_report_accepts_one_block() -> None:
    text = "STATUS: complete\nFINDINGS: none\n"
    assert has_single_terminal_report(text) is True


def test_has_single_terminal_report_rejects_example_plus_real() -> None:
    """Early example STATUS/FINDINGS plus a real report → unparseable."""
    text = (
        "Example format:\n"
        "STATUS: complete\n"
        "FINDINGS: none\n"
        "\n"
        "FINDINGS:\n"
        "- real bug in foo.py:1\n"
        "STATUS: complete\n"
    )
    assert has_single_terminal_report(text) is False


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
        tmux=False,
    )
    assert result.status == ""
    assert result.returncode == 0
    assert "grok-4.5" in result.argv


def test_launch_codex_dry_run_skips_mkstemp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    )
    assert "--output-last-message" in result.argv


def test_launch_fake_runner_codex_stdin(tmp_path: Path) -> None:
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
    )
    assert len(seen) == 1
    assert seen[0][1] == contents
    assert "codex" in seen[0][0]
    assert result.status == "complete"
    assert result.returncode == 0
    assert output_paths
    assert not Path(output_paths[0]).exists()


def test_launch_codex_unlinks_output_file_on_runner_exception(tmp_path: Path) -> None:
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
        )
    assert out_path is not None
    assert not Path(out_path).exists()


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
        tmux=False,
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
    spec = tmp_path / "spec.md"
    spec.write_text("implement me\n", encoding="utf-8")
    result = launch(
        role="implementer",
        vendor="grok",
        spec_file=str(spec),
        cwd=str(tmp_path),
        dry_run=True,
    )
    assert result.argv[:3] == ["tmux", "new-session", "-d"]
    assert "-s" in result.argv
    assert result.tmux_session is not None
    assert result.tmux_session.startswith("agent-lane-grok-implementer")
    assert "--" in result.argv
    assert "grok-4.5" in result.argv
    inner = result.argv[result.argv.index("--") + 1 :]
    assert inner[0] == "env"


def test_launch_no_tmux_starts_with_env(tmp_path: Path) -> None:
    spec = tmp_path / "spec.md"
    spec.write_text("implement me\n", encoding="utf-8")
    result = launch(
        role="implementer",
        vendor="grok",
        spec_file=str(spec),
        cwd=str(tmp_path),
        dry_run=True,
        tmux=False,
    )
    assert result.argv[0] == "env"
    assert "tmux" not in result.argv
    assert result.tmux_session is None


def test_launch_tmux_fake_runner_gets_wrapped_argv(tmp_path: Path) -> None:
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
    spec = tmp_path / "spec.md"
    spec.write_text("review this\n", encoding="utf-8")

    def fake_launch(**kwargs):  # type: ignore[no-untyped-def]
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


def test_cli_lane_run_prints_vendor_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
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
    assert "tmux" in out
    assert "new-session" in out
    assert "grok-4.5" in out
    assert "STATUS=" not in out


def test_cli_no_tmux_dry_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
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
            "--no-tmux",
        ]
    )
    out = capsys.readouterr().out.strip()
    assert out.startswith("env ")
    assert "new-session" not in out
    assert "grok-4.5" in out


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
