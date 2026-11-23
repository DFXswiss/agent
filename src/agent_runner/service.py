"""LaunchDaemon of the runner (docs/platform/spec/05-runner.md section 1.1, 00-overview.md section 7).

`/Library/LaunchDaemons/swiss.dfx.agent-runner.plist` with `UserName=<runner user>`, `RunAtLoad`, `KeepAlive`,
`EnvironmentVariables` (`AGENT_RUNNER_HOME`, `PATH` and the other non-default runner settings, see
decisions/RUN-01.md) and logs in `$AGENT_RUNNER_HOME/log/runner.{out,err}.log`. Install and uninstall run
`launchctl bootout` / `bootstrap system` (root required). Under pytest the plist is written into the runner home
and launchctl is never called (pattern of `agent_cli.daemon.install_and_start_service`).
"""

from __future__ import annotations

import dataclasses
import os
import plistlib
import re
import secrets
from pathlib import Path
from typing import Any

from agent_runner.config import DEFAULT_HOME_RELATIVE, PREFIX, Config, check_home, environment
from agent_runner.errors import RunnerError
from agent_runner.host import Account, Host
from agent_runner.identity import ensure_private_dir

LABEL = "swiss.dfx.agent-runner"
PLIST_NAME = f"{LABEL}.plist"
DOMAIN = "system"
DEFAULT_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
_USER_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$")
_UNLOADED = ("no such process", "could not find service", "not loaded", "not found")


def plist_path(config: Config, host: Host) -> Path:
    """Where the plist goes: the system LaunchDaemons directory, or the runner home under pytest."""
    if host.under_pytest:
        return config.home / PLIST_NAME
    return host.launch_daemons_dir / PLIST_NAME


def runner_user(host: Host, requested: str | None) -> str:
    """The unprivileged account that runs the daemon: `--user`, else `SUDO_USER`, else the current user."""
    name = requested or host.env.get("SUDO_USER") or host.user()
    if not _USER_RE.fullmatch(name) or name == "root":
        raise RunnerError("service_user_invalid")
    return name


def plist_data(config: Config, host: Host, user: str) -> dict[str, Any]:
    env = environment(config)
    env["PATH"] = host.env.get("PATH") or DEFAULT_PATH
    return {
        "Label": LABEL,
        "ProgramArguments": host.program(),
        "UserName": user,
        "RunAtLoad": True,
        "KeepAlive": True,
        "EnvironmentVariables": env,
        "StandardOutPath": str(config.log_dir / "runner.out.log"),
        "StandardErrorPath": str(config.log_dir / "runner.err.log"),
    }


def _write_plist(path: Path, data: dict[str, Any]) -> None:
    """Replace `path` atomically with mode 0644 (launchd refuses group- or world-writable plists)."""
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    except PermissionError:
        raise RunnerError("service_permission") from None
    with os.fdopen(fd, "wb") as handle:
        os.fchmod(handle.fileno(), 0o644)
        plistlib.dump(data, handle, sort_keys=True)
    os.replace(temporary, path)


def _unloaded(stdout: str, stderr: str) -> bool:
    text = f"{stdout}\n{stderr}".lower()
    return any(needle in text for needle in _UNLOADED)


def _bootout(host: Host) -> None:
    done = host.launchctl(["bootout", f"{DOMAIN}/{LABEL}"])
    if done.returncode != 0 and not _unloaded(done.stdout, done.stderr):
        raise RunnerError("service_bootout_failed")


def _owned_dirs(config: Config, account: Account, host: Host) -> None:
    """As root: create the home and `log/` (and missing parents below the account's home) owned by the account."""
    missing: list[Path] = []
    cursor = config.log_dir
    while not cursor.exists() and not cursor.is_symlink():
        missing.append(cursor)
        cursor = cursor.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        host.chown(directory, account.uid, account.gid)
    for directory in (config.home, config.log_dir):
        ensure_private_dir(directory)
        host.chown(directory, account.uid, account.gid)


def prepare(config: Config, host: Host, user: str | None) -> tuple[Config, str]:
    """Resolve the runner account and its home; create home and `log/` owned by that account.

    Without an explicit `AGENT_RUNNER_HOME` the home is the default below the runner account's home directory, not
    below the home of the (root) caller.
    """
    name = runner_user(host, user)
    account = host.account(name)
    if account is None:
        raise RunnerError("service_user_invalid")
    if PREFIX + "HOME" not in host.env:
        config = dataclasses.replace(config, home=check_home(account.home / DEFAULT_HOME_RELATIVE))
    if host.geteuid() == 0:
        _owned_dirs(config, account, host)
    else:
        for directory in (config.home, config.log_dir):
            ensure_private_dir(directory)
    return config, name


def install(config: Config, host: Host, *, user: str | None = None) -> Path:
    """Write the plist and (re)load the daemon; returns the plist path."""
    config, name = prepare(config, host, user)
    path = plist_path(config, host)
    _write_plist(path, plist_data(config, host, name))
    if host.under_pytest:
        return path
    _bootout(host)
    done = host.launchctl(["bootstrap", DOMAIN, str(path)])
    if done.returncode != 0:
        raise RunnerError("service_bootstrap_failed")
    return path


def read_plist(path: Path) -> dict[str, Any] | None:
    """The installed plist, None when it does not exist or cannot be parsed."""
    try:
        with path.open("rb") as handle:
            data = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    return data if isinstance(data, dict) else None


def uninstall(config: Config, host: Host) -> bool:
    """Unload the daemon and remove the plist; returns whether a plist was removed."""
    path = plist_path(config, host)
    if not host.under_pytest:
        _bootout(host)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except PermissionError:
        raise RunnerError("service_permission") from None
    return True
