"""The account the runner of an own device runs as: the Mac's owner (ADR-074 section 3).

macOS 27 does not let any root context without Full Disk Access delete a local account (the installer's scripts, a
LaunchDaemon and an administrator's `sysadminctl -deleteUser` all end with `eDSPermissionError`, measured in a fresh
macOS guest, KOR-59), so a dedicated service account would stay behind after a removal. The runner therefore runs as
the person who installed it, which an own device belongs to anyway:

- the agent way names the owner through the request file (the administrator script records the file's owner in
  the spool as `owner`),
- Installer.app takes the user logged in at the screen,
- an update or a second installation keeps the owner recorded in `maintenance.json`.

Only a regular account (UID ≥ 501, a plain name, a home directory) can be the owner; root never is.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from agent_runner.errors import RunnerError
from agent_runner.install.system import System

DSCL = "/usr/bin/dscl"
STAT = "/usr/bin/stat"
MIN_UID = 501
OWNER_FILE = "owner"
_NAME_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$")
_FIELD_RE = re.compile(r"^(UniqueID|PrimaryGroupID|NFSHomeDirectory):\s*(\S.*)$")
_NOT_PEOPLE = frozenset({"root", "loginwindow", "_mbsetupuser"})


@dataclass(frozen=True)
class Owner:
    name: str
    uid: int
    gid: int
    home: Path


def valid_name(name: str) -> bool:
    return _NAME_RE.fullmatch(name) is not None and name not in _NOT_PEOPLE


def lookup(system: System, name: str) -> Owner | None:
    """The regular account `name` (its home mapped below the layout's root), None for anything else."""
    if not valid_name(name):
        return None
    done = system.run([DSCL, ".", "-read", f"/Users/{name}", "UniqueID", "PrimaryGroupID", "NFSHomeDirectory"])
    if done.returncode != 0:
        return None
    fields: dict[str, str] = {}
    for line in done.stdout.splitlines():
        match = _FIELD_RE.fullmatch(line.strip())
        if match is not None:
            fields[match.group(1)] = match.group(2).strip()
    uid, gid, home = (fields.get(key, "") for key in ("UniqueID", "PrimaryGroupID", "NFSHomeDirectory"))
    if not uid.isdigit() or not gid.isdigit() or int(uid) < MIN_UID or not home.startswith("/") or home == "/":
        return None
    return Owner(name=name, uid=int(uid), gid=int(gid), home=system.layout.root / home.lstrip("/"))


def console(system: System) -> str | None:
    """The name of the user logged in at the screen; None at the login window or without a session."""
    done = system.run([STAT, "-f", "%Su", "/dev/console"])
    name = done.stdout.strip()
    return name if done.returncode == 0 and valid_name(name) else None


def take_spooled(system: System) -> str | None:
    """The owner the administrator script recorded next to the request (root, 0600); always removed."""
    path = system.layout.spool / OWNER_FILE
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        _discard(path)
        return None
    try:
        info = os.fstat(fd)
        text = os.read(fd, 64).decode("ascii", "replace").strip() if stat.S_ISREG(info.st_mode) else ""
    finally:
        os.close(fd)
        _discard(path)
    return text if valid_name(text) else None


def _discard(path: Path) -> None:
    """Remove whatever is at `path` (a directory there is left; the spool is root's alone)."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return


def resolve(system: System, recorded: str | None) -> Owner:
    """The recorded owner, else the spooled one, else the user at the screen; `owner_unknown` when none exists."""
    spooled = take_spooled(system)
    for name in (recorded, spooled, console(system)):
        found = lookup(system, name) if name else None
        if found is not None:
            return found
    raise RunnerError("owner_unknown")
