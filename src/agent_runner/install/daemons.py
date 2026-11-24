"""The two LaunchDaemons of an own device (ADR-074 sections 3 and 6; 05-runner.md section 1.1).

- `swiss.dfx.agent-runner`: the runner as the owner of the Mac, `RunAtLoad`, `KeepAlive`, its settings in
  `EnvironmentVariables`, logs in the runner's `log/` (as written by `agent-runner service install`).
- `swiss.dfx.agent-runner.maintenance`: root, every 15 minutes and at load, logs in the payload's `log/`.

Both start the payload's launcher `libexec/dfx-agent-runner` through `current`, so a version switch needs no new
plist. Plists are written atomically with mode 0644 (launchd refuses others).
"""

from __future__ import annotations

import os
import plistlib
import re
import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent_runner.errors import RunnerError
from agent_runner.install.layout import MAINTENANCE_LABEL, RUNNER_LABEL, Layout
from agent_runner.install.owner import Owner
from agent_runner.install.system import System

LAUNCHCTL = "/bin/launchctl"
DOMAIN = "system"
MAINTENANCE_INTERVAL_S = 900
_STATE_RE = re.compile(r"^\s*state = (\S+)\s*$", re.MULTILINE)
_UNLOADED = ("no such process", "could not find service", "not loaded", "not found")


def runner_plist(system: System, owner: Owner, env: Mapping[str, str]) -> dict[str, Any]:
    layout = system.layout
    log = Layout.runner_home(owner.home) / "log"
    return {
        "Label": RUNNER_LABEL,
        "ProgramArguments": [str(layout.launcher()), "agent_runner", "run"],
        "UserName": owner.name,
        "RunAtLoad": True,
        "KeepAlive": True,
        "EnvironmentVariables": {"PATH": layout.runner_path(), **dict(env)},
        "StandardOutPath": str(log / "runner.out.log"),
        "StandardErrorPath": str(log / "runner.err.log"),
    }


def maintenance_plist(system: System) -> dict[str, Any]:
    layout = system.layout
    return {
        "Label": MAINTENANCE_LABEL,
        "ProgramArguments": [str(layout.launcher()), "agent_runner.install.root", "maintain"],
        "RunAtLoad": True,
        "StartInterval": MAINTENANCE_INTERVAL_S,
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        "StandardOutPath": str(layout.log_dir / "maintenance.log"),
        "StandardErrorPath": str(layout.log_dir / "maintenance.log"),
    }


def write_plist(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "wb") as handle:
        os.fchmod(handle.fileno(), 0o644)
        plistlib.dump(data, handle, sort_keys=True)
    os.replace(temporary, path)


def _unloaded(text: str) -> bool:
    lowered = text.lower()
    return any(needle in lowered for needle in _UNLOADED)


def loaded(system: System, label: str) -> bool:
    return system.run([LAUNCHCTL, "print", f"{DOMAIN}/{label}"]).returncode == 0


def state(system: System, label: str) -> str:
    """`running`, `waiting`, … as launchd reports it; `absent` when the service is not loaded."""
    done = system.run([LAUNCHCTL, "print", f"{DOMAIN}/{label}"])
    if done.returncode != 0:
        return "absent"
    match = _STATE_RE.search(done.stdout)
    return match.group(1) if match is not None else "unknown"


def disabled(system: System, label: str) -> bool:
    """True when the service is disabled (for example switched off by the user in Login Items)."""
    done = system.run([LAUNCHCTL, "print-disabled", DOMAIN])
    for line in done.stdout.splitlines():
        text = line.strip()
        if text.startswith(f'"{label}"') and ("disabled" in text.split("=>")[-1] or "true" in text.split("=>")[-1]):
            return True
    return False


def bootout(system: System, label: str) -> None:
    done = system.run([LAUNCHCTL, "bootout", f"{DOMAIN}/{label}"])
    if done.returncode != 0 and not _unloaded(done.stdout + done.stderr):
        raise RunnerError("service_bootout_failed", label)


def bootstrap(system: System, path: Path, label: str) -> None:
    """(Re)load the daemon of `path`: boot out a loaded one first, then bootstrap."""
    bootout(system, label)
    if system.run([LAUNCHCTL, "bootstrap", DOMAIN, str(path)]).returncode != 0:
        raise RunnerError("service_bootstrap_failed", label)


def kickstart(system: System, label: str) -> None:
    """Start a loaded daemon now unless it runs (`kickstart` without `-k`); best effort."""
    system.run([LAUNCHCTL, "kickstart", f"{DOMAIN}/{label}"])


def install_runner(system: System, owner: Owner, env: Mapping[str, str]) -> None:
    write_plist(system.layout.runner_plist, runner_plist(system, owner, env))
    bootstrap(system, system.layout.runner_plist, RUNNER_LABEL)


def install_maintenance(system: System) -> None:
    """Write the maintenance plist; load it only when it is not loaded (an update runs inside it)."""
    write_plist(system.layout.maintenance_plist, maintenance_plist(system))
    if not loaded(system, MAINTENANCE_LABEL):
        bootstrap(system, system.layout.maintenance_plist, MAINTENANCE_LABEL)


def remove(system: System, label: str, path: Path, *, unload: bool = True) -> bool:
    """Unload (unless `unload` is false) and delete the plist; whether a plist was deleted."""
    if unload:
        bootout(system, label)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True
