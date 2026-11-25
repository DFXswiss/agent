"""The agent's own Mac as an execution device (ADR-074 sections 3 to 5; 11-agent-access.md section 8.4).

- `device add [--org ORG] [--wg WG ...] [--this-computer]`: checks this Mac (macOS, Apple silicon, virtualization,
  memory, free space; what the user can fix is opened, E-05), fetches the release of the hub's download area and
  verifies it with the release keys of this installation (`latest.json` signature, expiry, package digest), creates
  a device invitation of the organization (`device invite`: the given workgroups, else the default workgroup), writes
  the code into a 0600 request file in a private directory and runs the one administrator step
  (`agent_runner.install.admin`): macOS shows one dialog. Then it waits for the installation's public state and reports
  the device, its assignment, the golden image (`ready`; until it is, `next` says that tasks wait for it and start by
  themselves; with `--wait` it waits up to `GOLDEN_WAIT_S` for it, KOR-67) and the plaintext notice of A7.
- `device remove --this-computer`: unpairs this Mac's device in its organization (`remove` with the rotations, as the
  device screen does), then runs the helper's removal in one dialog (cancelled: the maintenance daemon removes the
  installation itself after the verified `remove`); prints what was removed and what is left.

The administrator step is the `osascript` dialog, unless the caller already has the rights without one (E-27): a
root caller runs the script directly, a caller whose `sudo -n true` succeeds runs it under `sudo -n` (`admin_way`; the
output names the way as `admin`: `dialog`, `sudo` or `root`). The runner's owner is the owner of the request file,
so on these ways too it is the calling account, also without anybody logged in at the screen; `device add` therefore
refuses a caller that cannot own a device (`invalid_argument`: `owner_root`, `owner_not_regular` below user ID 501):
it is run as the account that is to own the device. Tests
and the E2E suite replace the step with the program named in
`AGENT_PLATFORM_DEVICE_ADMIN`, and the layout's root with `AGENT_PLATFORM_DEVICE_ROOT` (both only together; the program
gets the same three arguments as the root script); with them `AGENT_PLATFORM_DEVICE_KEYS` (comma-separated) adds
release keys of a test release. Nothing here runs as root itself.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import hashlib
import inspect
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
from agent_platform.state import over_ssh
from agent_runner.errors import RunnerError
from agent_runner.install import admin, owner, preflight, release, update
from agent_runner.install.layout import Layout
from agent_runner.install.state import read_json
from agent_runner.install.system import System, fetch_url, run_argv

ADMIN_ENV = "AGENT_PLATFORM_DEVICE_ADMIN"
ROOT_ENV = "AGENT_PLATFORM_DEVICE_ROOT"
KEYS_ENV = "AGENT_PLATFORM_DEVICE_KEYS"
INVITE_HOURS = 1
WAIT_S = 900.0
# The golden of a new device is built right after its enrollment (`maintain`, KOR-67): `device add --wait` waits for it.
GOLDEN_WAIT_S = 1800.0
READY_GOLDEN = ("ready", "built")
NEXT_PREPARING = (
    "This computer builds the image for its tasks now (a few minutes); tasks on it wait and then start by themselves. "
    "`agent-platform device add --this-computer --wait` waits until it is ready."
)
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
DIALOG, SUDO, ROOT = "dialog", "sudo", "root"


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


def admin_way(machine: Machine) -> str:
    """How the administrator step runs: `root` for a root caller, `sudo` when `sudo -n true` succeeds (the caller may
    act as administrator without a password now), else `dialog` (also for the test seam, which stands in for it).
    Over ssh macOS shows no dialog: there the step needs `sudo` (`install_failed`, `admin_dialog_over_ssh`, before
    anything is downloaded or changed; KOR-67, rule E-27)."""
    if machine.admin is not None:
        return DIALOG
    if machine.system.geteuid() == machine.system.root_uid:
        return ROOT
    if machine.system.run([admin.SUDO, "-n", "true"]).returncode == 0:
        return SUDO
    if over_ssh(machine.system.env):
        raise PlatformError("install_failed", reason="admin_dialog_over_ssh")
    return DIALOG


def _admin_argv(machine: Machine, install: bool, arguments: tuple[str, str, str], way: str) -> list[str]:
    if machine.admin is not None:
        return [*machine.admin, "install" if install else "uninstall", *arguments]
    if way != DIALOG:
        script = admin.install_script(machine.layout) if install else admin.uninstall_script(machine.layout)
        return admin.direct_argv(script, arguments, root=way == ROOT)
    if install:
        return admin.install_argv(*arguments, layout=machine.layout)
    return admin.uninstall_argv(machine.layout)


def run_admin(machine: Machine, install: bool, arguments: tuple[str, str, str], way: str = DIALOG) -> str:
    """Run the one administrator step the `way` of `admin_way`; its standard output. A cancelled dialog is
    `admin_cancelled`."""
    done = machine.system.run(_admin_argv(machine, install, arguments, way))
    if done.returncode != 0:
        if "-128" in done.stderr or "User canceled" in done.stderr:
            raise PlatformError("admin_cancelled")
        raise PlatformError("install_failed", reason="admin_step" if install else "removal_step")
    return done.stdout


def wait_for(
    machine: Machine, done: Callable[[dict[str, Any]], bool], started: str, seconds: float = WAIT_S
) -> dict[str, Any]:
    """The public state once it was written after `started` and `done` accepts it; `install_failed` on time-out."""
    deadline = machine.clock() + seconds
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


def check_caller(machine: Machine) -> None:
    """The runner runs as the owner of the request file, the calling account: it must be a regular account (never
    root, a user ID of at least 501), else the installation would fall back to whoever is at the screen."""
    if machine.admin is not None:
        return
    uid = machine.system.geteuid()
    if uid == machine.system.root_uid:
        raise PlatformError("invalid_argument", reason="owner_root")
    if uid < owner.MIN_UID:
        raise PlatformError("invalid_argument", reason="owner_not_regular")


def device_add(
    account: Account, machine: Machine, org: str | None, wgs: list[str], *, wait: bool = False
) -> dict[str, Any]:
    check_caller(machine)
    admin_way(machine)  # over ssh without sudo: refused before the download (asked again right before the step)
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
        way = admin_way(machine)  # asked right before the step: cached sudo rights may expire during the download
        try:
            run_admin(machine, True, (str(package_path), feed.package_sha256, str(request)), way)
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
    if wait and final.get("golden") not in READY_GOLDEN:
        with contextlib.suppress(PlatformError):  # not ready in time: the result says so (`ready`, `next`)
            final = wait_for(machine, lambda found: found.get("golden") in READY_GOLDEN, started, GOLDEN_WAIT_S)
    ready = final.get("golden") in READY_GOLDEN
    return {
        "device_id": final.get("device_id", ""),
        "org_id": chosen_org,
        "workgroups": chosen,
        "version": final.get("version", feed.version),
        "daemon": final.get("daemon", "unknown"),
        "golden": final.get("golden", "unknown"),
        "ready": ready,
        **({} if ready else {"next": NEXT_PREPARING}),
        "owner": final.get("owner", ""),
        "checks": checks,
        "admin": way,
        "dialog": admin.INSTALL_SENTENCE if way == DIALOG else admin.DIRECT_INSTALL_SENTENCE,
        "notice": NOTICE,
    }


def device_remove(account: Account, machine: Machine) -> dict[str, Any]:
    """Unpair first, then remove the installation in one dialog. A cancelled or failed dialog still leaves nothing
    behind for long: the maintenance daemon removes the installation itself after the verified `remove`."""
    state = read_state(machine)
    if state is None:
        raise PlatformError("not_installed")
    way = admin_way(machine)  # before the unpairing, which cannot be undone
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
    output = run_admin(machine, False, ("", "", ""), way)
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
        "admin": way,
        "dialog": admin.UNINSTALL_SENTENCE if way == DIALOG else admin.DIRECT_UNINSTALL_SENTENCE,
    }


# --- command line ---


def _add(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return device_add(account, machine_for(context.environ), args.org, list(args.wg), wait=args.wait)


def _remove(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return device_remove(account, machine_for(context.environ))


ADD_LOOKUP = {
    "example": "agent-platform device add --this-computer [--org <org_id>] [--wg <wg_id> ...]",
    "words": "eigener mac rechner computer gerät anbinden installieren dialog ohne dialog sudo ssh fernzugriff "
    "own mac computer device connect install dialog without dialog sudo ssh remote headless",
}
REMOVE_LOOKUP = {
    "example": "agent-platform device remove --this-computer",
    "words": "eigener mac rechner gerät entfernen deinstallieren dialog ohne dialog sudo ssh "
    "own mac device remove uninstall dialog without dialog sudo ssh headless",
}


def lookup(tree: Tree, fields: dict[str, str]) -> dict[str, Any]:
    """The search words and example of a command, for a tree that describes its commands with them (KOR-63)."""
    known = inspect.signature(tree.command).parameters
    return {name: value for name, value in fields.items() if name in known}


def register(tree: Tree) -> None:
    group = tree.group("device", "execution devices")
    sub = tree.command(
        group,
        "add",
        _add,
        "make this Mac an execution device of the organization (one macOS dialog; none with sudo rights)",
        writes=True,
        **lookup(tree, ADD_LOOKUP),
    )
    sub.add_argument("--org", help="the organization (default: the only one)")
    sub.add_argument("--wg", action="append", default=[], help="workgroup to assign, repeatable (default: default)")
    sub.add_argument("--this-computer", action="store_true", help="the computer this command runs on (the default)")
    sub.add_argument("--wait", action="store_true", help="also wait until the device is ready for tasks (its image)")
    sub = tree.command(
        group,
        "remove",
        _remove,
        "unpair this Mac and remove the installation (one macOS dialog; none with sudo rights)",
        writes=True,
        **lookup(tree, REMOVE_LOOKUP),
    )
    sub.add_argument("--this-computer", action="store_true", required=True, help="the computer this command runs on")
