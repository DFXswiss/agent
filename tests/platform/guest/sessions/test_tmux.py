"""U-GST-sessions: the root tmux server and the pane launcher of ``tmux.py`` (05-runner.md section 7)."""

from __future__ import annotations

import json
import os
import re
import signal
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from agent_guest import tmux
from tests.platform.guest.sessions.conftest import SESSION, TmuxRunner

NAME = f"s-{SESSION}"


@pytest.fixture
def runner() -> TmuxRunner:
    return TmuxRunner()


@pytest.fixture
def restore_sighup() -> Iterator[None]:
    previous = signal.getsignal(signal.SIGHUP)
    try:
        yield
    finally:
        signal.signal(signal.SIGHUP, previous)


def test_session_name_is_s_and_the_session_id() -> None:
    assert tmux.session_name(SESSION) == NAME
    for bad in ("x", SESSION.upper(), f"{SESSION}:0", f"{SESSION} "):
        with pytest.raises(tmux.TmuxError):
            tmux.session_name(bad)


def test_new_session_is_detached_160_by_48_in_its_own_scope(runner: TmuxRunner) -> None:
    client = tmux.Tmux(socket="/run/agent-guest/tmux.sock", run=runner)
    client.new_session(NAME, ["/usr/bin/python3", "-I", tmux.HELPER, "exec", "/run/agent-guest/launch/x.json"])
    first, second = runner.calls
    assert first == [
        "systemd-run",
        "--scope",
        "--quiet",
        "--collect",
        first[4],
        "--",
        "tmux",
        "-S",
        "/run/agent-guest/tmux.sock",
        "new-session",
        "-d",
        "-s",
        NAME,
        "-x",
        "160",
        "-y",
        "48",
        "--",
        "/usr/bin/python3",
        "-I",
        tmux.HELPER,
        "exec",
        "/run/agent-guest/launch/x.json",
    ]
    assert second == ["tmux", "-S", "/run/agent-guest/tmux.sock", "set-option", "-t", NAME, "remain-on-exit", "off"]
    assert re.fullmatch(rf"--unit=agent-tmux-{SESSION}-[0-9a-f]{{8}}", first[4])
    client.new_session(NAME, ["true"])
    assert runner.calls[2][4] != first[4], "a resumed session needs a new scope name"


def test_new_session_without_scope_and_failures(runner: TmuxRunner) -> None:
    client = tmux.Tmux(socket="/s", run=runner, scope=False, binary="/usr/bin/tmux")
    client.new_session(NAME, ["true"])
    assert runner.calls[0][:4] == ["/usr/bin/tmux", "-S", "/s", "new-session"]
    runner.codes["new-session"] = 1
    with pytest.raises(tmux.TmuxError, match="status 1"):
        client.new_session(NAME, ["true"])
    for name, command in (("bad", ["true"]), (NAME, [])):
        with pytest.raises(tmux.TmuxError):
            client.new_session(name, command)
    runner.codes = {"set-option": 1}
    client.new_session(NAME, ["true"])


def test_session_queries_and_keys(runner: TmuxRunner) -> None:
    client = tmux.Tmux(socket="/s", run=runner, scope=False)
    assert client.has_session(NAME)
    assert client.kill_session(NAME)
    runner.codes = {"has-session": 1, "kill-session": 1}
    assert not client.has_session(NAME)
    assert not client.kill_session(NAME)
    client.send_keys(NAME, "Escape")
    client.send_keys(NAME, "hello world", literal=True)
    runner.stdout = "pane text\n"
    assert client.capture(NAME) == "pane text\n"
    assert runner.calls[0][-2:] == ["-t", f"={NAME}"]
    assert runner.calls[4][3:] == ["send-keys", "-t", f"={NAME}:", "--", "Escape"]
    assert runner.calls[5][3:] == ["send-keys", "-t", f"={NAME}:", "-l", "--", "hello world"]
    assert runner.calls[6][3:] == ["capture-pane", "-p", "-t", f"={NAME}:"]
    runner.codes = {"send-keys": 1}
    with pytest.raises(tmux.TmuxError):
        client.send_keys(NAME, "Enter")


def test_launch_file_is_private_and_status_round_trips(tmp_path: Path) -> None:
    launch = tmp_path / "launch" / "x.json"
    tmux.write_launch(str(launch), ["systemd-run", "--pty"], "/run/x.status", {"TOKEN": "t"})
    assert json.loads(launch.read_text()) == {
        "argv": ["systemd-run", "--pty"],
        "status": "/run/x.status",
        "env": {"TOKEN": "t"},
    }
    assert stat.S_IMODE(launch.stat().st_mode) == 0o600
    assert stat.S_IMODE(launch.parent.stat().st_mode) == 0o700
    status = tmp_path / "x.status"
    assert tmux.read_status(str(status)) is None
    for text, expected in (("0\n", 0), ("126\n", 126), ("-15\n", -15), ("garbage", None), ("12345", None)):
        status.write_text(text)
        assert tmux.read_status(str(status)) == expected
    status.write_bytes(b"\xff")
    assert tmux.read_status(str(status)) is None


def test_failed_launch_write_leaves_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(source: str, target: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError, match="disk full"):
        tmux.write_launch(str(tmp_path / "l" / "x.json"), ["a"], "/s")
    assert os.listdir(tmp_path / "l") == []


def test_exec_runs_the_command_and_records_its_status(tmp_path: Path, restore_sighup: None) -> None:
    launch, status = tmp_path / "x.json", tmp_path / "x.status"
    tmux.write_launch(str(launch), ["claude", "--", "a prompt"], str(status), {"PLATFORM_SESSION_TOKEN": "t"})
    seen: list[list[str]] = []

    def call(command: list[str], env: dict[str, str]) -> int:
        seen.append(command)
        assert env == {**os.environ, "PLATFORM_SESSION_TOKEN": "t"}
        assert signal.getsignal(signal.SIGHUP) is tmux._hangup
        return 126

    assert tmux.main(["exec", str(launch)], call=call) == 126
    assert seen == [["claude", "--", "a prompt"]]
    assert not launch.exists()
    assert status.read_text() == "126\n"
    tmux._hangup(signal.SIGHUP, None)


@pytest.mark.parametrize(
    "content",
    [
        None,
        "not json",
        "[]",
        json.dumps({"argv": []}),
        json.dumps({"argv": [], "status": "/s", "env": {}}),
        json.dumps({"argv": ["a", 1], "status": "/s", "env": {}}),
        json.dumps({"argv": ["a"], "status": "relative", "env": {}}),
        json.dumps({"argv": ["a"], "status": 5, "env": {}}),
        json.dumps({"argv": ["a"], "status": "/s"}),
        json.dumps({"argv": ["a"], "status": "/s", "env": []}),
        json.dumps({"argv": ["a"], "status": "/s", "env": {"A": 1}}),
    ],
)
def test_exec_refuses_unusable_launch_files(tmp_path: Path, content: str | None, restore_sighup: None) -> None:
    launch = tmp_path / "x.json"
    if content is not None:
        launch.write_text(content)

    def call(command: list[str], env: dict[str, str]) -> int:
        raise AssertionError("must not run")

    assert tmux.main(["exec", str(launch)], call=call) == tmux.EXIT_UNREADABLE


def test_exec_usage(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    assert tmux.main(["run", "x"]) == 2
    monkeypatch.setattr(sys, "argv", ["tmux.py"])
    assert tmux.main() == 2
    assert "usage" in capsys.readouterr().err


def test_exec_as_a_script_with_isolated_python(tmp_path: Path) -> None:
    """The pane runs ``python3 -I tmux.py exec <file>``: no package on the path, only the standard library."""
    launch, status = tmp_path / "x.json", tmp_path / "x.status"
    tmux.write_launch(str(launch), ["/bin/sh", "-c", 'exit "$CODE"'], str(status), {"CODE": "7"})
    result = subprocess.run(
        ["/usr/bin/env", "python3", "-I", tmux.HELPER, "exec", str(launch)], capture_output=True, check=False
    )
    assert result.returncode == 7
    assert status.read_text() == "7\n"


def test_the_default_call_passes_the_environment() -> None:
    assert tmux._call(["/bin/sh", "-c", 'exit "$CODE"'], {"CODE": "3"}) == 3
