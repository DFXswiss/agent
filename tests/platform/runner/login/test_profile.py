"""U-RUN-login (profile): the `LOGIN_AUTOMATION` default per device kind (05-runner.md section 1.3) and
`agent-runner service install --profile platform|org` (login extra, plist environment; explicit setting wins)."""

from __future__ import annotations

import dataclasses
import os
import plistlib
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from agent_runner.cli import run_cli
from agent_runner.config import Config, DeviceKind, load_config
from agent_runner.errors import RunnerError
from agent_runner.host import Account, Host
from agent_runner.login import profile

ENV = {"AGENT_RUNNER_HUB_URL": "https://hub.example", "AGENT_RUNNER_PLATFORM_ROOT_KEY": "A" * 43}


def config(extra: dict[str, str] | None = None, kind: DeviceKind | None = None) -> Config:
    return load_config({**ENV, **(extra or {})}, platform="darwin", device_kind=kind)


@pytest.mark.parametrize(
    ("kind", "explicit", "expected"),
    [
        ("platform", None, True),
        ("org", None, False),
        (None, None, False),
        ("platform", "off", False),
        ("org", "on", True),
    ],
)
def test_the_automation_default_follows_the_device_kind_and_an_explicit_value_wins(
    kind: DeviceKind | None, explicit: str | None, expected: bool
) -> None:
    extra = {} if explicit is None else {"AGENT_RUNNER_LOGIN_AUTOMATION": explicit}
    assert config(extra, kind).login_automation is expected


class Runs:
    def __init__(self, codes: Sequence[int] = (0, 0)) -> None:
        self.codes = list(codes)
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), self.codes.pop(0), "", "")


def test_no_profile_keeps_the_configuration() -> None:
    base = config()
    assert profile.apply(base, None, Host(env={})) is base


def test_an_unknown_profile_is_refused() -> None:
    with pytest.raises(RunnerError) as refused:
        profile.apply(config(), "lab", Host(env={}))
    assert (refused.value.code, refused.value.name) == ("config_invalid", "profile")


def home_config(tmp_path: Path) -> Config:
    """A configuration whose runner home lies below `tmp_path` (never the default home of the test user)."""
    base = tmp_path / "base"
    base.mkdir(mode=0o700, exist_ok=True)
    return load_config({**ENV, "AGENT_RUNNER_HOME": str(base / "home")}, platform="darwin")


def host_for(tmp_path: Path, euid: int = 501, **env: str) -> Host:
    """A host outside pytest mode with a private runner home below `tmp_path` (account `svc`, owner calls recorded)."""
    base = tmp_path / "base"
    base.mkdir(mode=0o700, exist_ok=True)
    return Host(
        env={"AGENT_RUNNER_HOME": str(base / "home"), **env},
        geteuid=lambda: euid,
        user=lambda: "svc",
        account=lambda _n: Account(uid=os.getuid(), gid=os.getgid(), home=tmp_path),
        chown=lambda *_a: None,
    )


def browsers(tmp_path: Path) -> str:
    return f"PLAYWRIGHT_BROWSERS_PATH={tmp_path / 'base' / 'home' / 'ms-playwright'}"


def test_the_platform_profile_installs_a_missing_extra_and_enables_the_automation(tmp_path: Path) -> None:
    runs = Runs()
    applied = profile.apply(home_config(tmp_path), "platform", host_for(tmp_path), run=runs, find_spec=lambda _n: None)
    assert (applied.login_automation, applied.login_automation_set) == (True, True)
    assert runs.calls == [
        [sys.executable, "-m", "pip", "install", "playwright>=1.65"],
        ["/usr/bin/env", browsers(tmp_path), sys.executable, "-m", "playwright", "install", "chromium"],
    ]


def test_as_root_the_installer_runs_as_the_runner_account(tmp_path: Path) -> None:
    runs = Runs((0,))
    host = host_for(tmp_path, euid=0)
    profile.apply(home_config(tmp_path), "platform", host, user="svc", run=runs, find_spec=lambda _n: object())
    assert runs.calls == [
        [
            "sudo",
            "-u",
            "svc",
            "-H",
            "/usr/bin/env",
            browsers(tmp_path),
            sys.executable,
            "-m",
            "playwright",
            "install",
            "chromium",
        ]
    ]
    assert profile.extra_installed() is True


@pytest.mark.parametrize("codes", [(1,), (0, 1)])
def test_a_failed_installation_stops_the_service_install(tmp_path: Path, codes: tuple[int, ...]) -> None:
    with pytest.raises(RunnerError) as failed:
        profile.apply(home_config(tmp_path), "platform", host_for(tmp_path), run=Runs(codes), find_spec=lambda _n: None)
    assert failed.value.code == "login_extra_install_failed"


def test_the_installer_reports_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    # the step that ends runs under the real limit: a Python start alone can take longer than 0.2 s on a loaded device
    assert profile.run_install([sys.executable, "-c", "print('ok')"]).returncode == 0
    monkeypatch.setattr(profile, "INSTALL_TIMEOUT_S", 0.2)
    assert profile.run_install([sys.executable, "-c", "import time; time.sleep(5)"]).returncode == 124


def test_under_pytest_no_installer_runs() -> None:
    runs = Runs()
    host = Host(env={"PYTEST_CURRENT_TEST": "x"})
    assert profile.apply(config(), "platform", host, run=runs, find_spec=lambda _n: None).login_automation
    assert runs.calls == []


def test_the_org_profile_disables_and_an_explicit_value_wins(tmp_path: Path) -> None:
    org = profile.apply(config(kind="platform"), "org", Host(env={}))
    assert (org.login_automation, org.login_automation_set) == (False, True)
    explicit = dataclasses.replace(home_config(tmp_path), login_automation=False, login_automation_set=True)
    kept = profile.apply(explicit, "platform", host_for(tmp_path), run=Runs((0,)), find_spec=lambda _n: object())
    assert kept is explicit


def test_service_install_with_a_profile_writes_the_setting(
    make_host: Callable[..., Any], env_for: Callable[..., dict[str, str]], runner_home: Path
) -> None:
    plist = runner_home / "swiss.dfx.agent-runner.plist"
    for name, value in (("platform", "on"), ("org", "off")):
        captured = make_host(env_for(runner_home))
        assert run_cli(["service", "install", "--profile", name, "--user", "svc"], captured.host) == 0
        assert plistlib.loads(plist.read_bytes())["EnvironmentVariables"]["AGENT_RUNNER_LOGIN_AUTOMATION"] == value
