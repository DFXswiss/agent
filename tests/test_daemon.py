from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_cli.daemon import (
    SERVICE_LABEL,
    _terminate,
    acquire_lock,
    adopt_kept_agent_pg_bin,
    child_specs,
    existing_service_agent_pg_bin,
    hub_configured,
    kept_service_agent_pg_bin,
    run_supervisor,
    service_extra_env,
    service_unit_text,
)
from agent_cli.main import main
from agent_cli.store import Store, StoreError

_next_fake_pid = 4242


class _FakeProc:
    def __init__(self, argv: list[str]) -> None:
        global _next_fake_pid
        self.argv = list(argv)
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.pid = _next_fake_pid
        _next_fake_pid += 1

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        if self.returncode is None:
            self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        if self.returncode is None:
            self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is not None:
            return self.returncode
        if timeout is not None:
            raise subprocess.TimeoutExpired(self.argv, timeout)
        self.returncode = -15
        return self.returncode


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


def _write_hub_config(home: Path) -> None:
    (home / "device.json").write_text(
        json.dumps({"hub_url": "https://hub.example", "device_token": "tok"}),
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _block_real_killpg(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never deliver FakeProc pids to the real process table."""
    monkeypatch.setattr("agent_cli.daemon.os.killpg", lambda *_a, **_k: None)


def _patch_killpg(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    calls: list[tuple[int, int]] = []

    def fake_killpg(pid: int, sig: int) -> None:
        calls.append((pid, sig))

    monkeypatch.setattr("agent_cli.daemon.os.killpg", fake_killpg)
    return calls


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
def test_service_extra_env_omits_empty_agent_pg_bin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.delenv("AGENT_PG_BIN", raising=False)
    env = service_extra_env()
    assert env == {"PATH": "/usr/bin:/bin"}
    monkeypatch.setenv("AGENT_PG_BIN", "  ")
    assert "AGENT_PG_BIN" not in service_extra_env()


@pytest.mark.no_pg
def test_service_extra_env_persists_agent_pg_bin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("AGENT_PG_BIN", " /opt/pg/bin ")
    env = service_extra_env()
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["AGENT_PG_BIN"] == "/opt/pg/bin"


@pytest.mark.no_pg
def test_service_extra_env_keeps_existing_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.delenv("AGENT_PG_BIN", raising=False)
    env = service_extra_env(existing_pg_bin="/opt/pg/bin")
    assert env["AGENT_PG_BIN"] == "/opt/pg/bin"


@pytest.mark.no_pg
def test_service_extra_env_empty_clears_existing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("AGENT_PG_BIN", "  ")
    env = service_extra_env(existing_pg_bin="/opt/pg/bin")
    assert "AGENT_PG_BIN" not in env


@pytest.mark.no_pg
def test_existing_service_agent_pg_bin_roundtrip() -> None:
    darwin = service_unit_text(
        program=["/usr/bin/agent", "daemon"],
        home=Path("/tmp/agent-home"),
        platform="darwin",
        extra_env={"PATH": "/usr/bin:/bin", "AGENT_PG_BIN": "/opt/pg/bin"},
    )
    assert existing_service_agent_pg_bin(darwin, "darwin") == "/opt/pg/bin"
    linux = service_unit_text(
        program=["/usr/bin/agent", "daemon"],
        home=Path("/tmp/agent-home"),
        platform="linux",
        extra_env={"PATH": "/usr/bin:/bin", "AGENT_PG_BIN": "/opt/pg/bin"},
    )
    assert existing_service_agent_pg_bin(linux, "linux") == "/opt/pg/bin"
    assert existing_service_agent_pg_bin(darwin, "linux") is None


@pytest.mark.no_pg
def test_existing_service_agent_pg_bin_unescapes() -> None:
    special = "/opt/pg & bin"
    darwin = service_unit_text(
        program=["/usr/bin/agent", "daemon"],
        home=Path("/tmp/agent-home"),
        platform="darwin",
        extra_env={"PATH": "/usr/bin:/bin", "AGENT_PG_BIN": special},
    )
    assert existing_service_agent_pg_bin(darwin, "darwin") == special
    spaced = "/opt/pg bin"
    linux = service_unit_text(
        program=["/usr/bin/agent", "daemon"],
        home=Path("/tmp/agent-home"),
        platform="linux",
        extra_env={"PATH": "/usr/bin:/bin", "AGENT_PG_BIN": spaced},
    )
    assert existing_service_agent_pg_bin(linux, "linux") == spaced


@pytest.mark.no_pg
def test_service_unit_text_darwin_includes_agent_pg_bin() -> None:
    text = service_unit_text(
        program=["/usr/bin/agent", "daemon"],
        home=Path("/tmp/agent-home"),
        platform="darwin",
        extra_env={"PATH": "/usr/bin:/bin", "AGENT_PG_BIN": "/opt/pg/bin"},
    )
    assert "<key>AGENT_PG_BIN</key>" in text
    assert "<string>/opt/pg/bin</string>" in text


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
def test_hub_configured_missing_file(tmp_path: Path) -> None:
    assert hub_configured(tmp_path) is False


@pytest.mark.no_pg
def test_hub_configured_incomplete_object(tmp_path: Path) -> None:
    (tmp_path / "device.json").write_text(
        json.dumps({"device_id": "dev-1", "hub_url": "https://hub.example"}),
        encoding="utf-8",
    )
    assert hub_configured(tmp_path) is False
    (tmp_path / "device.json").write_text(
        json.dumps({"device_token": "tok"}),
        encoding="utf-8",
    )
    assert hub_configured(tmp_path) is False
    (tmp_path / "device.json").write_text(
        json.dumps({"hub_url": "", "device_token": "tok"}),
        encoding="utf-8",
    )
    assert hub_configured(tmp_path) is False


@pytest.mark.no_pg
def test_hub_configured_both_keys(tmp_path: Path) -> None:
    _write_hub_config(tmp_path)
    assert hub_configured(tmp_path) is True


@pytest.mark.no_pg
def test_hub_configured_broken_json(tmp_path: Path) -> None:
    (tmp_path / "device.json").write_text("{not-json", encoding="utf-8")
    with pytest.raises(StoreError, match="device.json"):
        hub_configured(tmp_path)


@pytest.mark.no_pg
def test_run_supervisor_restarts_sync_twice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_hub_config(tmp_path)
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
            # Still alive during sync restarts; finally reaps after loop exit.
            assert procs["knock"].returncode is None
            assert procs["dashboard"].returncode is None
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


@pytest.mark.no_pg
def test_run_supervisor_sync_restart_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_hub_config(tmp_path)
    killpg_calls = _patch_killpg(monkeypatch)
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
    knock_pid = procs["knock"].pid
    dash_pid = procs["dashboard"].pid
    assert (knock_pid, signal.SIGTERM) in killpg_calls
    assert (dash_pid, signal.SIGTERM) in killpg_calls
    assert sync_starts["n"] == 10


@pytest.mark.no_pg
def test_run_supervisor_knock_exit_terminates_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_hub_config(tmp_path)
    killpg_calls = _patch_killpg(monkeypatch)
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
    sync_pid = procs["sync"].pid
    dash_pid = procs["dashboard"].pid
    assert (sync_pid, signal.SIGTERM) in killpg_calls
    assert (dash_pid, signal.SIGTERM) in killpg_calls


@pytest.mark.no_pg
def test_terminate_escalates_sigterm_to_sigkill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killpg_calls = _patch_killpg(monkeypatch)
    proc = _FakeProc(["agent", "sync", "--follow"])
    assert proc.returncode is None
    _terminate(proc)
    assert killpg_calls == [
        (proc.pid, signal.SIGTERM),
        (proc.pid, signal.SIGKILL),
    ]


@pytest.mark.no_pg
def test_run_supervisor_unpaired_starts_knock_dashboard_only(tmp_path: Path) -> None:
    procs: dict[str, _FakeProc] = {}
    started: list[list[str]] = []
    ticks = {"n": 0}

    def fake_popen(argv: list[str], *args: object, **kwargs: object) -> _FakeProc:
        started.append(list(argv))
        name = _child_name(argv)
        proc = _FakeProc(argv)
        procs[name] = proc
        return proc

    def fake_sleep(_seconds: float) -> None:
        ticks["n"] += 1
        if ticks["n"] >= 1:
            raise _StopLoop()

    with pytest.raises(_StopLoop):
        run_supervisor(
            home=tmp_path,
            argv_prefix=["agent"],
            popen=fake_popen,
            monotonic=lambda: float(ticks["n"]),
            sleep=fake_sleep,
        )
    assert "sync" not in procs
    assert all("sync" not in argv for argv in started)
    assert procs["knock"].returncode is None
    assert procs["dashboard"].returncode is None
    expected = [argv for name, argv in child_specs(["agent"]) if name != "sync"]
    assert started == expected


@pytest.mark.no_pg
def test_run_supervisor_paired_starts_sync(tmp_path: Path) -> None:
    _write_hub_config(tmp_path)
    started: list[list[str]] = []
    ticks = {"n": 0}

    def fake_popen(argv: list[str], *args: object, **kwargs: object) -> _FakeProc:
        started.append(list(argv))
        return _FakeProc(argv)

    def fake_sleep(_seconds: float) -> None:
        ticks["n"] += 1
        if ticks["n"] >= 1:
            raise _StopLoop()

    with pytest.raises(_StopLoop):
        run_supervisor(
            home=tmp_path,
            argv_prefix=["agent"],
            popen=fake_popen,
            monotonic=lambda: float(ticks["n"]),
            sleep=fake_sleep,
        )
    specs = dict(child_specs(["agent"]))
    assert started == [specs["knock"], specs["dashboard"], specs["sync"]]
    assert any("sync" in argv and "--follow" in argv for argv in started)


@pytest.mark.no_pg
def test_run_supervisor_starts_sync_after_pair(tmp_path: Path) -> None:
    procs: dict[str, _FakeProc] = {}
    started: list[list[str]] = []
    ticks = {"n": 0}

    def fake_popen(argv: list[str], *args: object, **kwargs: object) -> _FakeProc:
        started.append(list(argv))
        name = _child_name(argv)
        proc = _FakeProc(argv)
        procs[name] = proc
        return proc

    def fake_sleep(_seconds: float) -> None:
        ticks["n"] += 1
        if ticks["n"] == 1:
            assert "sync" not in procs
            _write_hub_config(tmp_path)
        if ticks["n"] >= 3 and "sync" in procs:
            raise _StopLoop()

    with pytest.raises(_StopLoop):
        run_supervisor(
            home=tmp_path,
            argv_prefix=["agent"],
            popen=fake_popen,
            monotonic=lambda: float(ticks["n"]),
            sleep=fake_sleep,
        )
    assert "sync" in procs
    assert any("sync" in argv and "--follow" in argv for argv in started)


@pytest.mark.no_pg
def test_run_supervisor_does_not_restart_sync_after_unpair(tmp_path: Path) -> None:
    _write_hub_config(tmp_path)
    started: list[list[str]] = []
    ticks = {"n": 0}

    def fake_popen(argv: list[str], *args: object, **kwargs: object) -> _FakeProc:
        started.append(list(argv))
        proc = _FakeProc(argv)
        if "sync" in argv:
            proc.returncode = 1
        return proc

    def fake_sleep(_seconds: float) -> None:
        ticks["n"] += 1
        if ticks["n"] == 1:
            (tmp_path / "device.json").write_text("{}", encoding="utf-8")
        if ticks["n"] >= 4:
            raise _StopLoop()

    with pytest.raises(_StopLoop):
        run_supervisor(
            home=tmp_path,
            argv_prefix=["agent"],
            popen=fake_popen,
            monotonic=lambda: float(ticks["n"]),
            sleep=fake_sleep,
        )
    sync_starts = [argv for argv in started if "sync" in argv]
    assert len(sync_starts) == 1


@pytest.mark.no_pg
def test_run_supervisor_unpair_after_sync_deaths_keeps_knock(
    tmp_path: Path,
) -> None:
    _write_hub_config(tmp_path)
    procs: dict[str, _FakeProc] = {}
    sync_starts = {"n": 0}
    ticks = {"n": 0}

    def fake_popen(argv: list[str], *args: object, **kwargs: object) -> _FakeProc:
        name = _child_name(argv)
        proc = _FakeProc(argv)
        procs[name] = proc
        if name == "sync":
            sync_starts["n"] += 1
            proc.returncode = 1
        return proc

    def fake_sleep(_seconds: float) -> None:
        ticks["n"] += 1
        if sync_starts["n"] >= 9:
            (tmp_path / "device.json").write_text("{}", encoding="utf-8")
        if ticks["n"] >= 20:
            raise _StopLoop()

    with pytest.raises(_StopLoop):
        run_supervisor(
            home=tmp_path,
            argv_prefix=["agent"],
            popen=fake_popen,
            monotonic=lambda: float(ticks["n"]),
            sleep=fake_sleep,
        )
    assert procs["knock"].returncode is None
    assert procs["dashboard"].returncode is None


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


def test_daemon_install_keeps_agent_pg_bin_on_reinstall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert os.environ.get("PYTEST_CURRENT_TEST")

    def boom(_argv: list[str]) -> Any:
        raise AssertionError("run_argv must not be called under pytest")

    monkeypatch.setattr("agent_cli.daemon._default_run_argv", boom)
    monkeypatch.setattr("agent_cli.runtime.run_argv", boom)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("AGENT_PG_BIN", "/opt/pg/bin")
    run(tmp_path, ["daemon", "--install"])
    path = tmp_path / "daemon.service"
    first = path.read_text(encoding="utf-8")
    plat = "darwin" if sys.platform == "darwin" else "linux"
    if not sys.platform.startswith("linux") and sys.platform != "darwin":
        plat = sys.platform
    assert existing_service_agent_pg_bin(first, plat) == "/opt/pg/bin"
    monkeypatch.delenv("AGENT_PG_BIN", raising=False)
    run(tmp_path, ["daemon", "--install"])
    second = path.read_text(encoding="utf-8")
    assert existing_service_agent_pg_bin(second, plat) == "/opt/pg/bin"


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


def test_daemon_uninstall_removes_service_under_pytest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert os.environ.get("PYTEST_CURRENT_TEST")

    def boom(_argv: list[str]) -> Any:
        raise AssertionError("run_argv must not be called under pytest")

    monkeypatch.setattr("agent_cli.daemon._default_run_argv", boom)
    monkeypatch.setattr("agent_cli.runtime.run_argv", boom)
    run(tmp_path, ["init"])
    path = tmp_path / "daemon.service"
    assert path.is_file()
    run(tmp_path, ["daemon", "--uninstall"])
    assert not path.exists()


def test_knock_tick_with_hub_url_and_device_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    run(tmp_path, ["init"])
    store = Store(tmp_path)
    store.set_meta("hub_url", "https://hub.example")
    store.set_meta("device_token", "tok")
    store.close()
    capsys.readouterr()

    def fake_poll_due(*_args: object, **_kwargs: object) -> bool:
        return True

    def fake_scan_usage(_store: object) -> str:
        return "usage-1"

    def fake_scan_merged(_store: object, _runner: object) -> tuple[list[str], int]:
        return [], 0

    def fake_scan_pending(_store: object, _hub: object) -> list[str]:
        return ["pending line-1"]

    def fake_listen(*_args: object, **_kwargs: object) -> None:
        raise SystemExit("stop")

    monkeypatch.setattr("agent_cli.main.usage_poll_due", fake_poll_due)
    monkeypatch.setattr("agent_cli.main.scan_usage", fake_scan_usage)
    monkeypatch.setattr("agent_cli.main.scan_merged", fake_scan_merged)
    monkeypatch.setattr("agent_cli.pending.scan_pending", fake_scan_pending)
    monkeypatch.setattr("agent_cli.main.knock_listen", fake_listen)
    with pytest.raises(SystemExit, match="stop"):
        run(tmp_path, ["knock"])
    captured = capsys.readouterr()
    assert "pending line-1" in captured.out


@pytest.mark.no_pg
def test_run_supervisor_popen_fails_on_dashboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    killpg_calls = _patch_killpg(monkeypatch)
    procs: dict[str, _FakeProc] = {}

    def fake_popen(argv: list[str], *args: object, **kwargs: object) -> _FakeProc:
        name = _child_name(argv)
        if name == "dashboard":
            raise OSError("dashboard start failed")
        proc = _FakeProc(argv)
        procs[name] = proc
        return proc

    with pytest.raises(SystemExit):
        run_supervisor(
            home=tmp_path,
            argv_prefix=["agent"],
            popen=fake_popen,
            monotonic=lambda: 0.0,
            sleep=lambda _s: None,
        )
    assert "sync" not in procs
    assert (procs["knock"].pid, signal.SIGTERM) in killpg_calls
    second = acquire_lock(tmp_path)
    second.close()  # type: ignore[attr-defined]


@pytest.mark.no_pg
def test_run_supervisor_child_specs_argv(tmp_path: Path) -> None:
    started: list[list[str]] = []
    ticks = {"n": 0}

    def fake_popen(argv: list[str], *args: object, **kwargs: object) -> _FakeProc:
        started.append(list(argv))
        return _FakeProc(argv)

    def fake_sleep(_seconds: float) -> None:
        ticks["n"] += 1
        if ticks["n"] >= 1:
            raise _StopLoop()

    with pytest.raises(_StopLoop):
        run_supervisor(
            home=tmp_path,
            argv_prefix=["agent"],
            popen=fake_popen,
            monotonic=lambda: float(ticks["n"]),
            sleep=fake_sleep,
        )
    expected = [argv for name, argv in child_specs(["agent"]) if name != "sync"]
    assert started == expected


@pytest.mark.no_pg
def test_kept_service_agent_pg_bin_reads_installed_unit(tmp_path: Path) -> None:
    unit = service_unit_text(
        program=["/usr/bin/agent", "daemon"],
        home=tmp_path,
        platform=sys.platform,
        extra_env={"PATH": "/usr/bin", "AGENT_PG_BIN": "/opt/pg/bin"},
    )
    (tmp_path / "daemon.service").write_text(unit, encoding="utf-8")
    assert kept_service_agent_pg_bin(tmp_path) == "/opt/pg/bin"
    assert kept_service_agent_pg_bin(tmp_path / "nowhere") is None
    (tmp_path / "daemon.service").write_bytes(b"\xff\xfe\x00")
    assert kept_service_agent_pg_bin(tmp_path) is None


@pytest.mark.no_pg
def test_adopt_kept_agent_pg_bin_exports_when_env_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unit = service_unit_text(
        program=["/usr/bin/agent", "daemon"],
        home=tmp_path,
        platform=sys.platform,
        extra_env={"PATH": "/usr/bin", "AGENT_PG_BIN": "/opt/pg/bin"},
    )
    (tmp_path / "daemon.service").write_text(unit, encoding="utf-8")
    monkeypatch.delenv("AGENT_PG_BIN", raising=False)
    assert adopt_kept_agent_pg_bin(tmp_path) == "/opt/pg/bin"
    assert os.environ["AGENT_PG_BIN"] == "/opt/pg/bin"
    monkeypatch.setenv("AGENT_PG_BIN", "/env/bin")
    assert adopt_kept_agent_pg_bin(tmp_path) is None
    assert os.environ["AGENT_PG_BIN"] == "/env/bin"
    monkeypatch.setenv("AGENT_PG_BIN", "")
    assert adopt_kept_agent_pg_bin(tmp_path) is None
    assert os.environ["AGENT_PG_BIN"] == ""


@pytest.mark.no_pg
def test_open_store_adopts_kept_agent_pg_bin_before_cluster_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENT_PG_DSN", raising=False)
    monkeypatch.delenv("AGENT_PG_BIN", raising=False)
    unit = service_unit_text(
        program=["/usr/bin/agent", "daemon"],
        home=tmp_path,
        platform=sys.platform,
        extra_env={"PATH": "/usr/bin", "AGENT_PG_BIN": "/opt/pg/bin"},
    )
    (tmp_path / "daemon.service").write_text(unit, encoding="utf-8")
    pg_data = tmp_path / "pg" / "data"
    pg_data.mkdir(parents=True)
    (pg_data / "PG_VERSION").write_text("17", encoding="utf-8")

    seen: list[str | None] = []

    def fake_ensure_cluster(*args: object, **kwargs: object) -> Any:
        seen.append(os.environ.get("AGENT_PG_BIN"))
        raise SystemExit("agent: stop here")

    monkeypatch.setattr("agent_cli.main.ensure_cluster", fake_ensure_cluster)

    seen.clear()
    monkeypatch.delenv("AGENT_PG_BIN", raising=False)
    with pytest.raises(SystemExit, match="stop here"):
        run(tmp_path, ["daemon", "--install"])
    assert seen == ["/opt/pg/bin"]

    seen.clear()
    monkeypatch.delenv("AGENT_PG_BIN", raising=False)
    with pytest.raises(SystemExit, match="stop here"):
        run(tmp_path, ["init"])
    assert seen == ["/opt/pg/bin"]

    seen.clear()
    monkeypatch.delenv("AGENT_PG_BIN", raising=False)
    with pytest.raises(SystemExit, match="stop here"):
        run(tmp_path, ["status"])
    assert seen == ["/opt/pg/bin"]


@pytest.mark.no_pg
def test_kept_service_agent_pg_bin_ignores_other_homes_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unit = service_unit_text(
        program=["/usr/bin/agent", "daemon"],
        home=Path("/elsewhere"),
        platform=sys.platform,
        extra_env={"PATH": "/usr/bin", "AGENT_PG_BIN": "/opt/pg/bin"},
    )
    (tmp_path / "daemon.service").write_text(unit, encoding="utf-8")
    assert kept_service_agent_pg_bin(tmp_path) is None
    monkeypatch.delenv("AGENT_PG_BIN", raising=False)
    assert adopt_kept_agent_pg_bin(tmp_path) is None
    assert "AGENT_PG_BIN" not in os.environ


@pytest.mark.no_pg
def test_kept_service_agent_pg_bin_accepts_symlinked_own_home(tmp_path: Path) -> None:
    real = tmp_path / "realhome"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    unit = service_unit_text(
        program=["/usr/bin/agent", "daemon"],
        home=link,
        platform=sys.platform,
        extra_env={"PATH": "/usr/bin", "AGENT_PG_BIN": "/opt/pg/bin"},
    )
    (real / "daemon.service").write_text(unit, encoding="utf-8")
    assert kept_service_agent_pg_bin(real) == "/opt/pg/bin"


@pytest.mark.no_pg
def test_existing_service_agent_pg_bin_linux_whole_assignment_quoting() -> None:
    text = "[Service]\nEnvironment=PATH=/usr/bin\nEnvironment='AGENT_PG_BIN=/opt/Custom Postgres/bin'\n"
    assert existing_service_agent_pg_bin(text, "linux") == "/opt/Custom Postgres/bin"


@pytest.mark.no_pg
def test_existing_service_agent_pg_bin_linux_last_assignment_wins_and_reset() -> None:
    text = "[Service]\nEnvironment=AGENT_PG_BIN=/old\nEnvironment='AGENT_PG_BIN=/new bin'\n"
    assert existing_service_agent_pg_bin(text, "linux") == "/new bin"
    text2 = "[Service]\nEnvironment=AGENT_PG_BIN=/old\nEnvironment=\n"
    assert existing_service_agent_pg_bin(text2, "linux") is None
    text3 = "[Service]\nEnvironment=AGENT_PG_BIN='/unterminated\n"
    assert existing_service_agent_pg_bin(text3, "linux") is None
