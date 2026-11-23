"""Install profiles of the runner service and the `LOGIN_AUTOMATION` default (05-runner.md sections 1.3 and 11.3).

`agent-runner service install --profile platform|org`:

- `platform` (DFX platform devices): installs the `login` extra when it is missing (`pip install playwright>=1.65`
  with this installation's interpreter) and Chromium into `<runner home>/ms-playwright` (`playwright install
  chromium`, idempotent), both as the runner account when the caller is root, and writes `LOGIN_AUTOMATION=on` into
  the LaunchDaemon environment (the daemon finds that Chromium through `automation.use_browsers`);
- `org` (devices of an organisation): writes `LOGIN_AUTOMATION=off`;
- no profile: the default follows the device kind recorded at enrollment (`config.load_config`).

An explicit `AGENT_RUNNER_LOGIN_AUTOMATION` always wins over the profile. Under pytest no installer command runs (the
plist goes into the test home, pattern of `service.install`). A device that has the setting on but lacks the extra
falls back to the relay at run time (`login_automation_failed`).
"""

from __future__ import annotations

import dataclasses
import importlib.util
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from agent_runner import service
from agent_runner.config import Config
from agent_runner.errors import RunnerError
from agent_runner.host import CommandRunner, Host

PROFILES = ("platform", "org")
EXTRA_MODULE = "playwright"
EXTRA_REQUIREMENT = "playwright>=1.65"
BROWSERS_DIR = "ms-playwright"
INSTALL_TIMEOUT_S = 1800.0
ENV_PROGRAM = "/usr/bin/env"


def run_install(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run one installer step (list form, output captured, at most `INSTALL_TIMEOUT_S` for the browser download)."""
    try:
        return subprocess.run(list(argv), capture_output=True, text=True, check=False, timeout=INSTALL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(list(argv), 124, "", "timed out")


def extra_installed(find_spec: Callable[[str], Any] = importlib.util.find_spec) -> bool:
    """True when the `login` extra (Playwright for Python) can be imported."""
    return find_spec(EXTRA_MODULE) is not None


def install_steps(home: Path, user: str | None, *, with_package: bool) -> list[list[str]]:
    """The installer commands: Playwright for Python (when missing) and its Chromium into `home/ms-playwright`, run as
    the runner account `user` through `sudo -u` when the caller is root (None: as the caller)."""
    prefix = [] if user is None else ["sudo", "-u", user, "-H"]
    env = [ENV_PROGRAM, f"PLAYWRIGHT_BROWSERS_PATH={home / BROWSERS_DIR}"]
    steps = [[*prefix, *env, sys.executable, "-m", EXTRA_MODULE, "install", "chromium"]]
    if with_package:
        steps.insert(0, [*prefix, sys.executable, "-m", "pip", "install", EXTRA_REQUIREMENT])
    return steps


def install_extra(steps: list[list[str]], run: CommandRunner) -> None:
    """Run the installer steps; `RunnerError` when one fails."""
    for argv in steps:
        if run(argv).returncode != 0:
            raise RunnerError("login_extra_install_failed")


def apply(
    config: Config,
    profile: str | None,
    host: Host,
    *,
    user: str | None = None,
    run: CommandRunner = run_install,
    find_spec: Callable[[str], Any] = importlib.util.find_spec,
) -> Config:
    """`config` with the profile's `LOGIN_AUTOMATION` (unless set explicitly); `platform` installs the extra and its
    Chromium for the runner account `user` (resolved like `service install --user`)."""
    if profile is None:
        return config
    if profile not in PROFILES:
        raise RunnerError("config_invalid", "profile")
    automation = profile == "platform"
    if automation and not host.under_pytest:
        prepared, name = service.prepare(config, host, user)
        steps = install_steps(
            prepared.home, name if host.geteuid() == 0 else None, with_package=not extra_installed(find_spec)
        )
        install_extra(steps, run)
    if config.login_automation_set:
        return config
    return dataclasses.replace(config, login_automation=automation, login_automation_set=True)
