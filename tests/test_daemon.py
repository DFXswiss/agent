from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_cli.daemon import (
    SERVICE_LABEL,
    acquire_lock,
    install_and_start_service,
    run_supervisor,
    service_unit_text,
)
from agent_cli.main import main
from agent_cli.store import StoreError


pytestmark_no_pg = pytest.mark.no_pg


class _FakeProc:
    def __init__(self, argv: list[str]) -> None:
        self.argv = list(argv)
        self.returncode: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        if self.returncode is None:
            self.returncode = -15


class _StopLoop(Exception):
    pass


def _child_name(argv: list[str]) -> str:
    if "knock" in argv:
        return "knock"
    if "dashboard" in argv:
        return "dashboard"
    if "sync" in argv:
        return "sync"
    return "other"


@pytest.mark.no_pg
def test_service_unit_text_darwin_contains_label_and_daemon() -> None:
    text = service_unit_text(
        program=["/usr/bin/agent", "daemon"],
        home=Path("/tmp/agent-home"),
        platform="darwin",
    )
    assert f"<string>{SERVICE_LABEL}</string>" in text
    assert "<key>KeepAlive</key>" in text
    assert "<true/>" in text
    assert "<key>AGENT_HOME</key>" in text
    assert "<string>/tmp/agent-home</string>" in text
    assert "<string>daemon</string>" in text
    assert "ProgramArguments" in text
    assert "<key>PATH</key>" not in text


@pytest.mark.no_pg
def test_service_unit_text_darwin_extra_env_path() -> None:
    path_value = "/opt/homebrew/bin:/usr/bin:/bin"
    text = service_unit_text(
        program=["/usr/bin/agent", "daemon"],
        home=Path("/tmp/agent-home"),
        platform="darwin",
        extra_env={"PATH": path_value},
    )
    assert "<key>PATH</key>" in text
    assert f"<string>{path_value}</string>" in text
    assert "<key>AGENT_HOME</key>" in text


@pytest.mark.no_pg
def test_service_unit_text_linux_restart_and_home() -> None:
    text = service_unit_text(
        program=["/usr/bin/agent", "daemon"],
        home=Path("/tmp/agent-home"),
        platform="linux",
    )
    assert "Restart=always" in text
    assert "Environment=AGENT_HOME=/tmp/agent-home" in text
    assert "ExecStart=" in text
    assert "daemon" in text
    assert "Environment=PATH=" not in text


@pytest.mark.no_pg
def test_service_unit_text_linux_extra_env_path() -> None:
    path_value = "/opt/homebrew/bin:/usr/bin:/bin"
    text = service_unit_text(
        program=["/usr/bin/agent", "daemon"],
        home=Path("/tmp/agent-home"),
        platform="linux",
        extra_env={"PATH": path_value},
    )
    assert f"Environment=PATH={path_value}" in text
    assert "Environment=AGENT_HOME=/tmp/agent-home" in text


@pytest.mark.no_pg
def test_service_unit_text_win32_raises() -> None:
    with pytest.raises(StoreError, match="unsupported on win32"):
        service_unit_text(
            program=["agent", "daemon"],
            home=Path("/tmp/x"),
            platform="win32",
        )


@pytest.mark.no_pg
def test_run_supervisor_restarts_sync_twice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    procs: dict[str, _FakeProc] = {}
    sync_starts = {"n": 0}
    ticks = {"n": 0}

    def fake_popen(argv: list[str], *args: object, **kwargs: object) -> _FakeProc:
        name = _child_name(argv)
        proc = _FakeProc(argv)
        procs[name] = proc
        if name == "sync":
            sync_starts["n"] += 1
            if sync_starts["n"] <= 2:
                proc.returncode = 1
        return proc

    def fake_sleep(_seconds: float) -> None:
        ticks["n"] += 1
        if sync_starts["n"] >= 3 and ticks["n"] >= 3:
            raise _StopLoop()

    with pytest.raises(_StopLoop):
        run_supervisor(
            home=tmp_path,
            argv_prefix=["agent"],
            popen=fake_popen,
            monotonic=lambda: float(ticks["n"]),
            sleep=fake_sleep,
        )
    err = capsys.readouterr().err
    assert "daemon sync restart 1" in err
    assert "daemon sync restart 2" in err
    assert procs["knock"].returncode is None
    assert procs["dashboard"].returncode is None


@pytest.mark.no_pg
def test_run_supervisor_sync_restart_limit(tmp_path: Path) -> None:
    procs: dict[str, _FakeProc] = {}
    sync_starts = {"n": 0}
    clock = {"t": 0.0}

    def fake_popen(argv: list[str], *args: object, **kwargs: object) -> _FakeProc:
        name = _child_name(argv)
        proc = _FakeProc(argv)
        procs[name] = proc
        if name == "sync":
            sync_starts["n"] += 1
            proc.returncode = 1
        return proc

    def fake_sleep(_seconds: float) -> None:
        clock["t"] += 0.01

    with pytest.raises(SystemExit, match="daemon sync restart limit"):
        run_supervisor(
            home=tmp_path,
            argv_prefix=["agent"],
            popen=fake_popen,
            monotonic=lambda: clock["t"],
            sleep=fake_sleep,
        )
    assert procs["knock"].terminated is True
    assert procs["dashboard"].terminated is True
    assert sync_starts["n"] == 10


@pytest.mark.no_pg
def test_run_supervisor_knock_exit_terminates_others(tmp_path: Path) -> None:
    procs: dict[str, _FakeProc] = {}
    ticks = {"n": 0}

    def fake_popen(argv: list[str], *args: object, **kwargs: object) -> _FakeProc:
        name = _child_name(argv)
        proc = _FakeProc(argv)
        procs[name] = proc
        return proc

    def fake_sleep(_seconds: float) -> None:
        ticks["n"] += 1
        if ticks["n"] == 1:
            procs["knock"].returncode = 3

    with pytest.raises(SystemExit, match="daemon child knock exited 3"):
        run_supervisor(
            home=tmp_path,
            argv_prefix=["agent"],
            popen=fake_popen,
            monotonic=lambda: float(ticks["n"]),
            sleep=fake_sleep,
        )
    assert procs["sync"].terminated is True
    assert procs["dashboard"].terminated is True


@pytest.mark.no_pg
def test_acquire_lock_second_raises(tmp_path: Path) -> None:
    first = acquire_lock(tmp_path)
    try:
        with pytest.raises(StoreError, match="agent daemon already running"):
            acquire_lock(tmp_path)
    finally:
        first.close()  # type: ignore[attr-defined]


def run(home: Path, argv: list[str]) -> None:
    os.environ["AGENT_HOME"] = str(home)
    main(argv)


def test_daemon_install_writes_service_under_pytest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert os.environ.get("PYTEST_CURRENT_TEST")
    path_value = "/opt/homebrew/bin:/usr/bin:/bin"
    monkeypatch.setenv("PATH", path_value)

    def boom(_argv: list[str]) -> Any:
        raise AssertionError("run_argv must not be called under pytest")

    monkeypatch.setattr("agent_cli.daemon._default_run_argv", boom)
    monkeypatch.setattr("agent_cli.runtime.run_argv", boom)
    run(tmp_path, ["daemon", "--install"])
    path = tmp_path / "daemon.service"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "daemon" in text
    assert str(tmp_path) in text
    if sys.platform == "darwin":
        assert "<key>PATH</key>" in text
        assert f"<string>{path_value}</string>" in text
    elif sys.platform.startswith("linux"):
        assert f"Environment=PATH={path_value}" in text
    else:
        assert path_value in text


def test_init_writes_daemon_service_under_pytest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(_argv: list[str]) -> Any:
        raise AssertionError("run_argv must not be called under pytest")

    monkeypatch.setattr("agent_cli.daemon._default_run_argv", boom)
    run(tmp_path, ["init"])
    out = capsys.readouterr().out
    assert "daemon=installed" in out
    assert (tmp_path / "daemon.service").is_file()


def test_knock_daemon_tick_skips_pr_merged_without_die(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    run(tmp_path, ["init"])
    capsys.readouterr()
    listen_calls = {"n": 0}

    def fake_poll_due(*_args: object, **_kwargs: object) -> bool:
        return True

    def fake_scan_usage(_store: object) -> str:
        return "usage-1"

    def fake_scan_merged(_store: object, _runner: object) -> tuple[list[str], int]:
        return [], 2

    def fake_listen(*_args: object, **_kwargs: object) -> None:
        listen_calls["n"] += 1
        raise SystemExit("stop")

    monkeypatch.setattr("agent_cli.main.usage_poll_due", fake_poll_due)
    monkeypatch.setattr("agent_cli.main.scan_usage", fake_scan_usage)
    monkeypatch.setattr("agent_cli.main.scan_merged", fake_scan_merged)
    monkeypatch.setattr("agent_cli.main.knock_listen", fake_listen)
    with pytest.raises(SystemExit, match="stop"):
        run(tmp_path, ["knock"])
    captured = capsys.readouterr()
    assert "usage.snapshot usage-1" in captured.out
    assert "watch skipped 2 pr.open rows" in captured.err
