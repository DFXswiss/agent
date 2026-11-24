"""U-RUN-identity / enroll: `agent-runner enroll --code-file PATH` (ADR-074 section 3): the code read from a file of
at most 4 KiB, or from standard input for `-` (whitespace stripped, never printed), exactly one of `--code` and
`--code-file`, and every unreadable file or input refused as `invite_code_invalid`."""

from __future__ import annotations

import importlib.machinery
import io
import json
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agent_runner.cli import run_cli
from agent_runner.config import Config
from agent_runner.enroll import cli
from agent_runner.errors import RunnerError

CODE = "ABCDE-FGHIJ-KLMNO-PQRST-UVWXY"
URL = "http://127.0.0.1:9"


def test_the_code_is_read_and_stripped(tmp_path: Path) -> None:
    path = tmp_path / "code"
    path.write_text(f"\n  {CODE}\t\n", encoding="utf-8")
    assert cli.read_code(str(path)) == CODE


def test_a_code_of_exactly_the_limit_is_read(tmp_path: Path) -> None:
    path = tmp_path / "code"
    path.write_text("A" * cli.CODE_FILE_MAX, encoding="utf-8")
    assert cli.read_code(str(path)) == "A" * 4096


@pytest.mark.parametrize(
    "content",
    [b"", b" \n\t\n", ("A" * 4097).encode(), b"\xff\xfeABC", b"A" * 4000 + b"\n" * 200],
)
def test_unusable_code_files_are_refused(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "code"
    path.write_bytes(content)
    with pytest.raises(RunnerError, match=r"^invite_code_invalid$"):
        cli.read_code(str(path))


def test_a_missing_or_unreadable_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RunnerError, match=r"^invite_code_invalid$"):
        cli.read_code(str(tmp_path / "absent"))
    with pytest.raises(RunnerError, match=r"^invite_code_invalid$"):
        cli.read_code(str(tmp_path))


def test_a_dash_reads_the_code_from_standard_input(monkeypatch: pytest.MonkeyPatch) -> None:
    assert cli.read_code("-", io.StringIO(f"  {CODE}\n")) == CODE
    monkeypatch.setattr(sys, "stdin", io.StringIO(CODE + "\n"))
    assert cli.read_code("-") == CODE


@pytest.mark.parametrize("content", [b"", b"\n", b"A" * 4097, b"\xff\xfeABC"])
def test_unusable_standard_input_is_refused(content: bytes) -> None:
    stdin = io.TextIOWrapper(io.BytesIO(content), encoding="utf-8")
    with pytest.raises(RunnerError, match=r"^invite_code_invalid$"):
        cli.read_code("-", stdin)


def test_a_dash_is_never_a_file_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "-").write_text("FROM-A-FILE", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert cli.read_code("-", io.StringIO(CODE)) == CODE


def org_module(monkeypatch: pytest.MonkeyPatch, calls: list[tuple[Config, str]]) -> None:
    def org_enroll(config: Config, code: str, out: object) -> int:
        calls.append((config, code))
        return 0

    fake = types.ModuleType(cli.ORG_MODULE)
    fake.__spec__ = importlib.machinery.ModuleSpec(cli.ORG_MODULE, None)
    fake.enroll = org_enroll  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, cli.ORG_MODULE, fake)


def test_the_enroll_command_takes_the_code_from_the_file(
    tmp_path: Path,
    runner_home: Path,
    env_for: Callable[..., dict[str, str]],
    make_host: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Config, str]] = []
    org_module(monkeypatch, calls)
    path = tmp_path / "code"
    path.write_text(CODE + "\n", encoding="utf-8")
    captured = make_host(env_for(runner_home))
    assert run_cli(["enroll", "--hub", URL, "--code-file", str(path)], captured.host) == 0
    assert [(config.hub_url, code) for config, code in calls] == [(URL, CODE)]
    assert CODE not in captured.stdout.getvalue() + captured.stderr.getvalue()


def test_the_enroll_command_takes_the_code_from_standard_input(
    runner_home: Path,
    env_for: Callable[..., dict[str, str]],
    make_host: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Config, str]] = []
    org_module(monkeypatch, calls)
    monkeypatch.setattr(sys, "stdin", io.StringIO(CODE + "\n"))
    captured = make_host(env_for(runner_home))
    assert run_cli(["enroll", "--hub", URL, "--code-file", "-"], captured.host) == 0
    assert [(config.hub_url, code) for config, code in calls] == [(URL, CODE)]
    assert CODE not in captured.stdout.getvalue() + captured.stderr.getvalue()


def test_an_unreadable_code_file_fails_the_command(
    tmp_path: Path,
    runner_home: Path,
    env_for: Callable[..., dict[str, str]],
    make_host: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Config, str]] = []
    org_module(monkeypatch, calls)
    captured = make_host(env_for(runner_home))
    argv = ["--json", "enroll", "--hub", URL, "--code-file", str(tmp_path / "absent")]
    assert run_cli(argv, captured.host) == 1
    assert json.loads(captured.stdout.getvalue())["error"] == {"code": "invite_code_invalid", "name": None}
    assert calls == []


@pytest.mark.parametrize(
    "codes",
    [[], ["--code", CODE, "--code-file", "/dev/null"]],
)
def test_exactly_one_code_source_is_required(
    runner_home: Path,
    env_for: Callable[..., dict[str, str]],
    make_host: Callable[..., Any],
    codes: list[str],
) -> None:
    captured = make_host(env_for(runner_home))
    assert run_cli(["enroll", "--hub", URL, *codes], captured.host) == 2
    assert "agent-runner: error:" in captured.stderr.getvalue()
    assert "--code" in captured.stderr.getvalue()
    assert CODE not in captured.stdout.getvalue()
