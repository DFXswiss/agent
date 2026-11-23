"""U-RUN-service: LaunchDaemon plist and launchctl calls (05-runner.md section 1.1, 00-overview.md section 7)."""

from __future__ import annotations

import getpass
import os
import plistlib
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agent_runner import host as host_module
from agent_runner import service
from agent_runner.config import load_config
from agent_runner.errors import RunnerError
from agent_runner.host import Host, daemon_program, run_command, tool


def config_for(env: dict[str, str]) -> Any:
    return load_config(env, platform="darwin")


def read_plist(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        data = plistlib.load(handle)
    assert isinstance(data, dict)
    return data


def test_install_under_pytest_writes_into_home_without_launchctl(
    make_host: Callable[..., Any], env_for: Callable[..., dict[str, str]], runner_home: Path, fake_tools: Any
) -> None:
    env = env_for(runner_home, SUDO_USER="runneruser", AGENT_RUNNER_VM_PREFIX="wp-run01-")
    captured = make_host(env)
    cfg = config_for(env)
    path = service.install(cfg, captured.host)
    assert path == runner_home / "swiss.dfx.agent-runner.plist"
    assert fake_tools.calls == []
    data = read_plist(path)
    assert data["Label"] == "swiss.dfx.agent-runner"
    assert data["UserName"] == "runneruser"
    assert data["RunAtLoad"] is True
    assert data["KeepAlive"] is True
    assert data["ProgramArguments"] == [sys.executable, "-m", "agent_runner", "run"]
    assert data["StandardOutPath"] == str(runner_home / "log" / "runner.out.log")
    assert data["StandardErrorPath"] == str(runner_home / "log" / "runner.err.log")
    env_vars = data["EnvironmentVariables"]
    assert env_vars["AGENT_RUNNER_HOME"] == str(runner_home)
    assert env_vars["PATH"] == "/usr/bin:/bin"
    assert env_vars["AGENT_RUNNER_VM_PREFIX"] == "wp-run01-"
    assert env_vars["AGENT_RUNNER_HUB_URL"] == "http://127.0.0.1:9"
    assert "PYTEST_CURRENT_TEST" not in env_vars
    assert "SUDO_USER" not in env_vars
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert stat.S_IMODE(runner_home.stat().st_mode) == 0o700
    assert (runner_home / "log").is_dir()
    # Re-install replaces the plist atomically and leaves no temporary file.
    service.install(cfg, captured.host, user="other")
    assert read_plist(path)["UserName"] == "other"
    assert sorted(p.name for p in runner_home.iterdir()) == ["log", "swiss.dfx.agent-runner.plist"]
    assert service.uninstall(cfg, captured.host) is True
    assert not path.exists()
    assert service.uninstall(cfg, captured.host) is False
    assert fake_tools.calls == []


def test_install_outside_pytest_bootstraps_system_domain(
    make_host: Callable[..., Any], env_for: Callable[..., dict[str, str]], runner_home: Path, fake_tools: Any
) -> None:
    env = env_for(runner_home)
    del env["PYTEST_CURRENT_TEST"]
    del env["PATH"]
    captured = make_host(env)
    captured.host.launch_daemons_dir.mkdir()
    fake_tools.answers["launchctl"] = [(3, "", "Boot-out failed: 3: No such process"), (0, "", "")]
    cfg = config_for(env)
    path = service.install(cfg, captured.host)
    assert path == captured.host.launch_daemons_dir / "swiss.dfx.agent-runner.plist"
    assert fake_tools.calls == [
        ("launchctl", ["bootout", "system/swiss.dfx.agent-runner"]),
        ("launchctl", ["bootstrap", "system", str(path)]),
    ]
    data = read_plist(path)
    assert data["UserName"] == "runner"
    assert data["EnvironmentVariables"]["PATH"] == service.DEFAULT_PATH
    fake_tools.calls.clear()
    fake_tools.answers["launchctl"] = [(0, "", "")]
    assert service.uninstall(cfg, captured.host) is True
    assert fake_tools.calls == [("launchctl", ["bootout", "system/swiss.dfx.agent-runner"])]


def test_bootstrap_failure(
    make_host: Callable[..., Any], env_for: Callable[..., dict[str, str]], runner_home: Path, fake_tools: Any
) -> None:
    env = env_for(runner_home)
    del env["PYTEST_CURRENT_TEST"]
    captured = make_host(env)
    captured.host.launch_daemons_dir.mkdir()
    fake_tools.answers["launchctl"] = [(0, "", ""), (5, "", "Bootstrap failed: 5: Input/output error token=x")]
    with pytest.raises(RunnerError) as caught:
        service.install(config_for(env), captured.host)
    assert (caught.value.code, str(caught.value)) == ("service_bootstrap_failed", "service_bootstrap_failed")


def test_bootout_failure(
    make_host: Callable[..., Any], env_for: Callable[..., dict[str, str]], runner_home: Path, fake_tools: Any
) -> None:
    env = env_for(runner_home)
    del env["PYTEST_CURRENT_TEST"]
    captured = make_host(env)
    captured.host.launch_daemons_dir.mkdir()
    fake_tools.answers["launchctl"] = [(1, "Operation not permitted", "")]
    with pytest.raises(RunnerError) as caught:
        service.uninstall(config_for(env), captured.host)
    assert caught.value.code == "service_bootout_failed"
    fake_tools.answers["launchctl"] = [(113, "", "Could not find service in domain")]
    assert service.uninstall(config_for(env), captured.host) is False


def test_permission_errors(
    make_host: Callable[..., Any], env_for: Callable[..., dict[str, str]], runner_home: Path, fake_tools: Any
) -> None:
    env = env_for(runner_home)
    del env["PYTEST_CURRENT_TEST"]
    captured = make_host(env)
    daemons = captured.host.launch_daemons_dir
    daemons.mkdir()
    (daemons / service.PLIST_NAME).write_text("old", encoding="utf-8")
    os.chmod(daemons, 0o500)
    try:
        with pytest.raises(RunnerError) as caught:
            service.install(config_for(env), captured.host)
        assert caught.value.code == "service_permission"
        with pytest.raises(RunnerError) as caught:
            service.uninstall(config_for(env), captured.host)
        assert caught.value.code == "service_permission"
    finally:
        os.chmod(daemons, 0o700)


@pytest.mark.parametrize("user", ["root", "Bad User", "", "x" * 40])
def test_invalid_user(user: str, make_host: Callable[..., Any]) -> None:
    captured = make_host({}, user=lambda: user)
    with pytest.raises(RunnerError) as caught:
        service.runner_user(captured.host, None)
    assert caught.value.code == "service_user_invalid"


def test_user_precedence(make_host: Callable[..., Any]) -> None:
    captured = make_host({"SUDO_USER": "sudoer"})
    assert service.runner_user(captured.host, "explicit") == "explicit"
    assert service.runner_user(captured.host, None) == "sudoer"
    assert service.runner_user(make_host({}).host, None) == "runner"


def test_run_command_real_binaries() -> None:
    done = run_command([sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"])
    assert (done.returncode, done.stdout, done.stderr) == (3, "out\n", "err\n")
    missing = run_command(["agent-runner-test-no-such-binary"])
    assert missing.returncode == 127
    assert "not found" in missing.stderr


def test_run_command_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host_module, "COMMAND_TIMEOUT_S", 0.2)
    done = run_command([sys.executable, "-c", "import time; time.sleep(5)"])
    assert done.returncode == 124


def test_tool_prefixes_the_binary() -> None:
    done = tool(sys.executable)(["-c", "print('hi')"])
    assert done.stdout == "hi\n"
    assert done.args == [sys.executable, "-c", "print('hi')"]


def test_default_host() -> None:
    real = Host()
    assert real.platform == sys.platform
    assert real.launch_daemons_dir == Path("/Library/LaunchDaemons")
    assert real.package == "agent_runner"
    assert daemon_program() == [sys.executable, "-m", "agent_runner", "run"]
    assert real.under_pytest is True
    assert Host(env={}).under_pytest is False
    for name in ("launchctl", "limactl", "tmux", "ssh", "sudo"):
        assert callable(getattr(real, name))


def test_launchctl_default_is_the_real_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    Host().launchctl(["print", "system"])
    Host().limactl(["list"])
    assert seen == [["launchctl", "print", "system"], ["limactl", "list"]]


def test_install_as_root_creates_the_home_for_the_runner_account(
    make_host: Callable[..., Any],
    env_for: Callable[..., dict[str, str]],
    runner_home: Path,
    fake_tools: Any,
    tmp_path: Path,
) -> None:
    """sudo install without AGENT_RUNNER_HOME: home below the runner account's home, owned by that account."""
    env = env_for(runner_home, SUDO_USER="runner")
    del env["AGENT_RUNNER_HOME"]
    user_home = tmp_path / "users" / "runner"
    user_home.mkdir(parents=True)
    captured = make_host(env, geteuid=lambda: 0)
    cfg = load_config(env, platform="darwin", expand_user=lambda text: text.replace("~", "/nonexistent-root"))
    path = service.install(cfg, captured.host)
    home = user_home / ".local" / "share" / "agent-runner"
    assert path == home / service.PLIST_NAME
    assert read_plist(path)["EnvironmentVariables"]["AGENT_RUNNER_HOME"] == str(home)
    owned = [entry[0] for entry in fake_tools.chowns]
    assert owned == [
        user_home / ".local",
        user_home / ".local" / "share",
        home,
        home / "log",
        home,
        home / "log",
    ]
    assert {(uid, gid) for _, uid, gid in fake_tools.chowns} == {(501, 20)}
    for directory in (user_home / ".local", home, home / "log"):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    fake_tools.chowns.clear()
    service.install(cfg, captured.host)
    assert [entry[0] for entry in fake_tools.chowns] == [home, home / "log"]


def test_install_explicit_home_is_kept(
    make_host: Callable[..., Any], env_for: Callable[..., dict[str, str]], runner_home: Path, fake_tools: Any
) -> None:
    env = env_for(runner_home)
    captured = make_host(env, geteuid=lambda: 0)
    path = service.install(config_for(env), captured.host, user="runner")
    assert path == runner_home / service.PLIST_NAME
    assert [entry[0] for entry in fake_tools.chowns] == [
        runner_home,
        runner_home / "log",
        runner_home,
        runner_home / "log",
    ]


def test_install_default_home_of_the_account_must_be_private(
    make_host: Callable[..., Any], env_for: Callable[..., dict[str, str]], runner_home: Path, tmp_path: Path
) -> None:
    env = env_for(runner_home)
    del env["AGENT_RUNNER_HOME"]
    wide = tmp_path / "users" / "runner" / ".local" / "share" / "agent-runner"
    wide.mkdir(parents=True)
    os.chmod(wide, 0o755)
    captured = make_host(env)
    with pytest.raises(RunnerError) as caught:
        service.install(load_config(env, platform="darwin"), captured.host)
    assert (caught.value.code, caught.value.name) == ("config_invalid", "HOME")


def test_install_unknown_account(
    make_host: Callable[..., Any], env_for: Callable[..., dict[str, str]], runner_home: Path
) -> None:
    env = env_for(runner_home)
    with pytest.raises(RunnerError) as caught:
        service.install(config_for(env), make_host(env).host, user="ghost")
    assert caught.value.code == "service_user_invalid"


def test_read_plist(tmp_path: Path) -> None:
    assert service.read_plist(tmp_path / "missing") is None
    broken = tmp_path / "broken.plist"
    broken.write_text("not a plist", encoding="utf-8")
    assert service.read_plist(broken) is None
    array = tmp_path / "array.plist"
    array.write_bytes(plistlib.dumps(["x"]))
    assert service.read_plist(array) is None
    good = tmp_path / "good.plist"
    good.write_bytes(plistlib.dumps({"Label": "x"}))
    assert service.read_plist(good) == {"Label": "x"}


def test_account_lookup_and_chown(tmp_path: Path) -> None:
    me = host_module.lookup_account(getpass.getuser())
    assert me is not None
    assert me.uid == os.getuid()
    assert host_module.lookup_account("agent-runner-no-such-account") is None
    target = tmp_path / "f"
    target.write_text("x", encoding="utf-8")
    link = tmp_path / "l"
    link.symlink_to(target)
    host_module.chown(link, os.getuid(), os.getgid())
    assert host_module.cli_program() == [sys.executable, "-m", "agent_runner"]
