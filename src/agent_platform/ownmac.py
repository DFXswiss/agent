"""The agent's own Mac as an execution device (ADR-074 sections 3 to 5; 11-agent-access.md section 8.4).

- `device add [--org ORG] [--wg WG ...] [--this-computer]`: checks this Mac (macOS, Apple silicon, virtualization,
  memory, free space; what the user can fix is opened, E-05), fetches the release of the hub's download area and
  verifies it with the release keys of this installation (`latest.json` signature, expiry, package digest), creates
  a device invitation of the organization (`device invite`: the given workgroups, else the default workgroup), writes
  the code into a 0600 request file in a private directory and runs the one administrator step
  (`agent_runner.install.admin`): macOS shows one dialog. Then it waits for the installation's public state and reports
  the device, its assignment, the golden image and the plaintext notice of A7.
- `device remove --this-computer`: unpairs this Mac's device in its organization (`remove` with the rotations, as the
  device screen does), then runs the helper's removal in one dialog (cancelled: the maintenance daemon removes the
  installation itself after the verified `remove`); prints what was removed and what is left.

The administrator step is `osascript`; tests and the E2E suite replace it with the program named in
`AGENT_PLATFORM_DEVICE_ADMIN`, and the layout's root with `AGENT_PLATFORM_DEVICE_ROOT` (both only together; the program
gets the same three arguments as the root script); with them `AGENT_PLATFORM_DEVICE_KEYS` (comma-separated) adds
release keys of a test release. Nothing here runs as root itself.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import shutil
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_platform import devices, membership
from agent_platform.account import Account
from agent_platform.commands import Context, Tree
from agent_platform.errors import PlatformError
from agent_platform.orgs import find_org
from agent_runner.errors import RunnerError
from agent_runner.install import admin, preflight, release, update
from agent_runner.install.layout import Layout
from agent_runner.install.state import read_json
from agent_runner.install.system import System, fetch_url, run_argv

ADMIN_ENV = "AGENT_PLATFORM_DEVICE_ADMIN"
ROOT_ENV = "AGENT_PLATFORM_DEVICE_ROOT"
KEYS_ENV = "AGENT_PLATFORM_DEVICE_KEYS"
INVITE_HOURS = 1
WAIT_S = 900.0
POLL_S = 2.0
MAX_PACKAGE = 2 * 1024**3
OPEN = "/usr/bin/open"
OPENERS = {
    "macos_too_old": "x-apple.systempreferences:com.apple.Software-Update-Settings.extension",
    "disk_too_full": "x-apple.systempreferences:com.apple.settings.Storage",
}
NOTICE = (
    "Whoever administers this Mac can read the content of every task this device runs for the organization - also of "
    "workgroups you are not a member of - including what is kept on the Mac after a task ran."
)
LOGIN_ITEMS = "x-apple.systempreferences:com.apple.LoginItems-Settings.extension"


@dataclass
class Machine:
    """This Mac as the commands see it; tests replace the programs, the clock and the root of the layout."""

    system: System
    admin: list[str] | None = None
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    keys: Callable[[], list[str]] = release.packaged_keys
    user: Callable[[], str] = getpass.getuser
    temp: Callable[[], str] = field(default_factory=lambda: lambda: tempfile.mkdtemp(prefix="dfx-agent-runner-"))

    @property
    def layout(self) -> Layout:
        return self.system.layout


def machine_for(environ: Mapping[str, str]) -> Machine:
    seam, root = environ.get(ADMIN_ENV), environ.get(ROOT_ENV)
    if (seam is None) != (root is None):
        raise PlatformError("invalid_argument", reason="device_seam")
    layout = Layout(Path(root)) if root is not None else Layout()
    system = System(layout=layout, run=run_argv, fetch=fetch_url, env=environ)
    if seam is None:
        return Machine(system)
    extra = [key for key in environ.get(KEYS_ENV, "").split(",") if key]
    return Machine(system, admin=[seam], keys=lambda: [*release.packaged_keys(), *extra])


def check_mac(machine: Machine) -> list[dict[str, str | bool]]:
    """The checks of this Mac; the first failure ends the command (what the user can fix is opened first)."""
    checks = preflight.run_checks(machine.system)
    for check in checks:
        if not check.ok:
            target = OPENERS.get(check.code)
            if target is not None:
                machine.system.run([OPEN, target])
            raise PlatformError("mac_unsupported", reason=check.code, opened=target is not None)
    return [check.fields() for check in checks]


def fetch_release(machine: Machine, origin: str) -> tuple[release.Feed, bytes]:
    """The verified feed and package of the hub's download area."""
    try:
        feed = update.read_feed(machine.system, origin, machine.keys())
        now = machine.system.now()
        if feed.expired(now):
            raise PlatformError("release_unavailable", reason="feed_expired")
        version = preflight.macos_version(machine.system)
        if version is not None and version < _macos(feed.min_macos):
            raise PlatformError("mac_unsupported", reason="macos_too_old", opened=False)
        package = machine.system.fetch(update.feed_url(origin, feed.package), MAX_PACKAGE)
    except RunnerError as error:
        raise PlatformError("release_unavailable", reason=error.code) from None
    if hashlib.sha256(package).hexdigest() != feed.package_sha256:
        raise PlatformError("release_unavailable", reason="package_digest_mismatch")
    return feed, package


def _macos(text: str) -> tuple[int, int]:
    major, _, minor = text.partition(".")
    return int(major), int(minor or 0)


def read_state(machine: Machine) -> dict[str, Any] | None:
    return read_json(machine.layout.state_file)


def _admin_argv(machine: Machine, install: bool, arguments: tuple[str, str, str]) -> list[str]:
    if machine.admin is not None:
        return [*machine.admin, "install" if install else "uninstall", *arguments]
    if install:
        return admin.install_argv(*arguments, layout=machine.layout)
    return admin.uninstall_argv(machine.layout)


def run_admin(machine: Machine, install: bool, arguments: tuple[str, str, str]) -> str:
    """Run the one administrator step; its standard output. A cancelled dialog is `admin_cancelled`."""
    done = machine.system.run(_admin_argv(machine, install, arguments))
    if done.returncode != 0:
        if "-128" in done.stderr or "User canceled" in done.stderr:
            raise PlatformError("admin_cancelled")
        raise PlatformError("install_failed", reason="admin_step" if install else "removal_step")
    return done.stdout


def wait_for(machine: Machine, done: Callable[[dict[str, Any]], bool], started: str) -> dict[str, Any]:
    """The public state once it was written after `started` and `done` accepts it; `install_failed` on time-out."""
    deadline = machine.clock() + WAIT_S
    while True:
        state = read_state(machine)
        if state is not None and str(state.get("updated_at", "")) >= started and done(state):
            return state
        if machine.clock() >= deadline:
            raise PlatformError("install_failed", reason="timeout")
        machine.sleep(POLL_S)


def _workgroups(account: Account, org: str | None, wgs: list[str]) -> tuple[str, list[str]]:
    if wgs:
        found = org or devices_org_of(account, wgs[0])
        return found, wgs
    orgs = [str(item["org_id"]) for item in account.me().get("orgs", [])]
    if org is None:
        if len(orgs) != 1:
            raise PlatformError("no_org") if not orgs else PlatformError("invalid_argument", reason="org_ambiguous")
        org = orgs[0]
    return org, [account.org_log(org).state.default_wg]


def devices_org_of(account: Account, wg: str) -> str:
    return find_org(account, membership.require_id(wg))


def device_add(account: Account, machine: Machine, org: str | None, wgs: list[str]) -> dict[str, Any]:
    checks = check_mac(machine)
    state = read_state(machine)
    if state is not None and state.get("enrolled") is True:
        raise PlatformError("device_installed", device_id=str(state.get("device_id", "")))
    owner = str(state.get("owner", "")) if state is not None else ""
    if owner and owner != machine.user():
        # The installation runs as another account: enrolling it would give that account this user's org keys.
        raise PlatformError("device_installed", reason="other_owner")
    feed, package = fetch_release(machine, account.origin)
    chosen_org, chosen = _workgroups(account, org, wgs)
    folder = Path(machine.temp())
    try:
        os.chmod(folder, 0o700)
        package_path = folder / feed.package
        package_path.write_bytes(package)
        invitation = devices.device_invite(account, chosen_org, chosen, INVITE_HOURS)
        request = folder / "enroll.request"
        fd = os.open(request, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(str(invitation["code"]) + "\n")
        started = machine.system.now().strftime(update.TIME_FORMAT)
        try:
            run_admin(machine, True, (str(package_path), feed.package_sha256, str(request)))
        except PlatformError:
            # A cancelled or failed dialog leaves no open invitation behind (best effort; it expires in an hour).
            membership.follow_up(lambda: membership.invite_revoke(account, chosen_org, str(invitation["invite_id"])))
            raise
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    final = wait_for(machine, lambda found: found.get("enrollment") != "waiting", started)
    if final.get("enrollment") != "enrolled":
        raise PlatformError("install_failed", reason=str(final.get("enrollment_error", final.get("enrollment"))))
    if final.get("disabled_in_login_items") is True:
        machine.system.run([OPEN, LOGIN_ITEMS])
    return {
        "device_id": final.get("device_id", ""),
        "org_id": chosen_org,
        "workgroups": chosen,
        "version": final.get("version", feed.version),
        "daemon": final.get("daemon", "unknown"),
        "golden": final.get("golden", "unknown"),
        "owner": final.get("owner", ""),
        "checks": checks,
        "dialog": admin.INSTALL_SENTENCE,
        "notice": NOTICE,
    }


def device_remove(account: Account, machine: Machine) -> dict[str, Any]:
    """Unpair first, then remove the installation in one dialog. A cancelled or failed dialog still leaves nothing
    behind for long: the maintenance daemon removes the installation itself after the verified `remove`."""
    state = read_state(machine)
    if state is None:
        raise PlatformError("not_installed")
    device = str(state.get("device_id", ""))
    unpaired: dict[str, Any] = {"device_id": device, "unpaired": False}
    if state.get("enrolled") is True and device:
        try:
            org = devices._device_org(account, device)
        except PlatformError as error:
            if error.code != "not_found":
                raise
        else:
            unpaired = {**membership.member_remove(account, org, device), "device_id": device, "unpaired": True}
    output = run_admin(machine, False, ("", "", ""))
    lines = [line for line in output.strip().splitlines() if line.startswith("{")]
    try:
        removal = json.loads(lines[-1]) if lines else {}
    except ValueError:
        removal = {}
    if removal.get("ok") is not True:
        raise PlatformError("install_failed", reason="removal_step")
    return {
        **unpaired,
        "removed": removal.get("removed", []),
        "leftovers": removal.get("leftovers", []),
        "dialog": admin.UNINSTALL_SENTENCE,
    }


# --- command line ---


def _add(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return device_add(account, machine_for(context.environ), args.org, list(args.wg))


def _remove(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return device_remove(account, machine_for(context.environ))


def register(tree: Tree) -> None:
    group = tree.group("device", "execution devices")
    sub = tree.command(
        group, "add", _add, "make this Mac an execution device of the organization (one macOS dialog)", writes=True
    )
    sub.add_argument("--org", help="the organization (default: the only one)")
    sub.add_argument("--wg", action="append", default=[], help="workgroup to assign, repeatable (default: default)")
    sub.add_argument("--this-computer", action="store_true", help="the computer this command runs on (the default)")
    sub = tree.command(
        group, "remove", _remove, "unpair this Mac and remove the installation (one macOS dialog)", writes=True
    )
    sub.add_argument("--this-computer", action="store_true", required=True, help="the computer this command runs on")
