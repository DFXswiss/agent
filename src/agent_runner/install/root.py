"""The privileged helper of the runner package (ADR-074 sections 3, 5 and 6).

`python -I -m agent_runner.install.root <command>`, always as root and always from the root-owned payload:

| Command | Run by |
|---|---|
| `postinstall --version V` | the package's `postinstall` (Installer.app or `installer`) |
| `uninstall` | the one administrator dialog of `agent-platform device remove --this-computer` |
| `leftovers` | anyone with root, to check a removal |
| `maintain` | the maintenance LaunchDaemon every 15 minutes |

Output: one JSON document on stdout (`{"ok": true, ...}` or `{"ok": false, "error": {"code", "name"}}`); exit 0 or 1.
Values are codes, versions, IDs and paths of the installation; the enrollment code is never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, NoReturn

from agent_runner.errors import RunnerError
from agent_runner.install import daemons, hosttools, owner, preflight, release, runnerctl, update
from agent_runner.install.layout import MAINTENANCE_LABEL, PACKAGE_ID, RUNNER_LABEL, VM_PREFIX, Layout
from agent_runner.install.release import Manifest
from agent_runner.install.state import Maintenance, read_json, write_state
from agent_runner.install.system import System
from agent_runner.vm import golden

PKGUTIL = "/usr/sbin/pkgutil"
TMUTIL = "/usr/bin/tmutil"
RM = "/bin/rm"
MKDIR = "/bin/mkdir"
CHMOD = "/bin/chmod"
RMDIR = "/bin/rmdir"
LOGGER = "/usr/bin/logger"
OSASCRIPT = "/usr/bin/osascript"
SFLTOOL = "/usr/bin/sfltool"
MAX_REQUEST = 4096
# A booted-out runner still shows as loaded until its process (with its database) has ended: the check for what is
# left waits for that, at most UNLOAD_POLLS times UNLOAD_POLL_S (measured in a macOS guest: a few seconds).
# Lima's own directories below the owner's home, recorded in `created` when the installation found them missing
# (KOR-67): its configuration (`~/.lima/_config`: network settings and the SSH key Lima makes for its instances) and
# its download cache. A removal takes `_config` and then `~/.lima` itself only when nothing else is in it (an
# instance the owner made with Lima stays), and the cache.
LIMA_CONFIG = ".lima"
LIMA_CACHE = "Library/Caches/lima"
UNLOAD_POLLS = 30
UNLOAD_POLL_S = 1.0
# Right after an enrollment the runner is loaded but its store still starts, so its status cannot be read yet: the
# maintenance run that `postinstall` starts at once asks again, at most STATUS_POLLS times STATUS_POLL_S, so the golden
# of a new device is built at once and not by the next run 15 minutes later (KOR-67).
STATUS_POLLS = 60
STATUS_POLL_S = 2.0
CODE_PROMPT = (
    "To connect this Mac to your organization, paste the device code shown on the devices page of agent.dfx.swiss."
)
ASK_SCRIPT = (
    "on run argv\n"
    'set answer to display dialog (item 1 of argv) default answer "" with title "DFX Agent Runner" '
    'buttons {"Cancel", "Connect"} default button "Connect" with icon note\n'
    "return text returned of answer\n"
    "end run"
)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise RunnerError("usage")


def require_root(system: System) -> None:
    if system.geteuid() != 0:
        raise RunnerError("root_required")


def check_ancestors(system: System) -> None:
    """The payload and every directory above it (up to the layout's root) is owned by root and writable by nobody
    else, and none is a symbolic link: otherwise someone other than root could change what root runs."""
    layout = system.layout
    chain = [layout.payload, *(parent for parent in layout.payload.parents if parent.is_relative_to(layout.root))]
    for part in chain:
        try:
            found = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(found.st_mode) or found.st_uid != system.root_uid or found.st_mode & 0o022:
            raise RunnerError("payload_parent_unsafe", part.name.replace(" ", "_") or "root")


def _manifest(system: System, version: str, keys: Sequence[str], *, verify_tree: bool) -> Manifest:
    folder = system.layout.version_dir(version)
    if verify_tree:
        return release.read_manifest(folder, keys)
    try:
        data = (folder / release.MANIFEST).read_bytes()
        signature = (folder / (release.MANIFEST + release.SIGNATURE_SUFFIX)).read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        raise RunnerError("release_invalid", "missing") from None
    return release.manifest_from(release.verify(data, signature, keys))


def active_version(system: System) -> str | None:
    current = system.layout.current
    if not current.is_symlink():
        return None
    return Path(os.readlink(current)).name


def prune_versions(system: System, keep: set[str]) -> list[str]:
    removed: list[str] = []
    versions = system.layout.versions
    for folder in sorted(versions.iterdir()) if versions.is_dir() else []:
        if folder.name not in keep and folder.is_dir() and not folder.is_symlink():
            shutil.rmtree(folder)
            removed.append(folder.name)
    return removed


def runner_dirs(ctx: runnerctl.Context, memory: Maintenance) -> None:
    """The runner's home (0700) and its `log/`, created by the owner (root never creates below the owner's home); the
    parents it had to create (for example `~/.local/share`) are recorded, so a removal takes them away again."""
    home = ctx.home
    system = ctx.system
    for parent in reversed(home.relative_to(ctx.owner.home).parents[:-1]):
        path = ctx.owner.home / parent
        if not path.exists() and not path.is_symlink() and str(parent) not in memory.created:
            memory.created.append(str(parent))
    for lima in (LIMA_CONFIG, LIMA_CACHE):
        path = ctx.owner.home / lima
        if not path.exists() and not path.is_symlink() and lima not in memory.created:
            memory.created.append(lima)
    system.must(ctx.as_owner([MKDIR, "-p", "-m", "0700", str(home)]), "runner_home_failed")
    system.must(ctx.as_owner([CHMOD, "0700", str(home)]), "runner_home_failed")
    system.must(ctx.as_owner([MKDIR, "-p", "-m", "0700", str(home / "log")]), "runner_home_failed")


def read_request(path: Path) -> str | None:
    """The code in the spool (a regular 0600 file of root, at most 4 KiB); None when there is none. Always removed."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError:
        path.unlink(missing_ok=True)
        raise RunnerError("request_invalid") from None
    try:
        found = os.fstat(fd)
        if not stat.S_ISREG(found.st_mode) or found.st_mode & 0o077 or found.st_size > MAX_REQUEST:
            raise RunnerError("request_invalid")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            code = handle.read().strip()
    finally:
        if fd >= 0:
            os.close(fd)
        path.unlink(missing_ok=True)
    return code or None


def ask_code(ctx: runnerctl.Context) -> str | None:
    """Ask the owner for the device code in a dialog of the owner's session (Installer.app only)."""
    system = ctx.system
    if system.env.get("COMMAND_LINE_INSTALL") or owner.console(system) != ctx.owner.name:
        return None
    argv = ["/bin/launchctl", "asuser", str(ctx.owner.uid), *ctx.as_owner([OSASCRIPT, "-e", ASK_SCRIPT, CODE_PROMPT])]
    done = system.run(argv)
    code = done.stdout.strip()
    return code if done.returncode == 0 and code else None


def enroll_pending(ctx: runnerctl.Context, enrolled: bool) -> dict[str, Any]:
    """Enroll from the spool (or, in Installer.app, from the owner's answer) unless already enrolled."""
    code = read_request(ctx.system.layout.request)
    if enrolled:
        return {"enrollment": "already" if code is None else "already_enrolled"}
    if code is None:
        code = ask_code(ctx)
    if code is None:
        return {"enrollment": "waiting"}
    try:
        found = runnerctl.enroll(ctx, code)
    except RunnerError as error:
        # The installation stays; the state names the reason and the code can be given again (ADR-074 section 3).
        return {"enrollment": "failed", "enrollment_error": error.name or error.code}
    return {"enrollment": "enrolled", "device_id": str(found.get("device_id", ""))}


def _safe_status(ctx: runnerctl.Context) -> runnerctl.RunnerStatus | None:
    try:
        return runnerctl.status(ctx)
    except RunnerError:
        return None


def _started_status(ctx: runnerctl.Context, sleep: Callable[[float], None]) -> runnerctl.RunnerStatus | None:
    """The runner's status, waiting while the runner is loaded but does not answer yet (`STATUS_POLLS`)."""
    found, polls = _safe_status(ctx), 0
    while found is None and polls < STATUS_POLLS and daemons.loaded(ctx.system, RUNNER_LABEL):
        sleep(STATUS_POLL_S)
        found, polls = _safe_status(ctx), polls + 1
    return found


def context(system: System, manifest: Manifest, recorded: str | None) -> runnerctl.Context:
    found = owner.resolve(system, recorded)
    env = runnerctl.runner_env(system, found, manifest, preflight.memory_reserve_mib(system))
    return runnerctl.Context(system, found, env)


def postinstall(system: System, version: str, keys: Sequence[str]) -> dict[str, Any]:
    require_root(system)
    check_ancestors(system)
    layout = system.layout
    manifest = _manifest(system, version, keys, verify_tree=True)
    if manifest.version != version:
        raise RunnerError("release_invalid", "version")
    for folder in (layout.payload, layout.versions, layout.log_dir):
        folder.mkdir(mode=0o755, parents=True, exist_ok=True)
        folder.chmod(0o755)
    layout.spool.mkdir(mode=0o700, exist_ok=True)
    layout.spool.chmod(0o700)
    memory = Maintenance.load(layout.maintenance_file)
    before = active_version(system)
    update.switch_current(system, version)
    if before is not None and before != version:
        memory.previous = before
    pruned = prune_versions(system, {version, *([memory.previous] if memory.previous else [])})
    try:
        tools = hosttools.ensure(system, manifest)
    except RunnerError as error:
        tools = error.code
    daemons.install_maintenance(system)
    ctx = context(system, manifest, memory.owner)
    memory.owner = ctx.owner.name
    memory.save(layout.maintenance_file)
    runner_dirs(ctx, memory)
    memory.save(layout.maintenance_file)
    system.run(ctx.as_owner([TMUTIL, "addexclusion", str(ctx.home)]))
    found = _safe_status(ctx)
    result = enroll_pending(ctx, found is not None and found.enrolled)
    if result["enrollment"] in ("enrolled", "already", "already_enrolled"):
        daemons.install_runner(system, ctx.owner, ctx.env)
        # The maintenance run builds the golden at once instead of at its next interval.
        daemons.kickstart(system, MAINTENANCE_LABEL)
    publish(ctx, manifest, memory, host_tools=tools, enrollment=result)
    return {
        "version": version,
        "previous": memory.previous,
        "pruned": pruned,
        "host_tools": tools,
        "owner": ctx.owner.name,
        **result,
    }


def publish(
    ctx: runnerctl.Context,
    manifest: Manifest,
    memory: Maintenance,
    *,
    host_tools: str = "",
    golden: str = "",
    enrollment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write `state.json` from the runner's status and launchd; returns what was written.

    When the status cannot be read (the runner's store is starting: right after an enrollment, at boot), the device
    stays as last known, and an enrollment of this run names its device: an unreadable status is no removal. `status`
    says whether the device fields come from a status read now (`read`) or are kept (`unreadable`)."""
    system = ctx.system
    layout = system.layout
    found = _safe_status(ctx)
    old = read_json(layout.state_file) or {}
    if found is not None:
        enrolled, device = found.enrolled, found.device_id if found.enrolled else ""
    else:
        enrolled = old.get("enrolled") is True
        device = str(old.get("device_id", "")) if enrolled else ""
    if enrollment is not None and enrollment.get("enrollment") == "enrolled" and enrollment.get("device_id"):
        enrolled, device = True, str(enrollment["device_id"])
    fields: dict[str, Any] = {
        "version": manifest.version,
        "hub": manifest.hub,
        "owner": ctx.owner.name,
        "enrolled": enrolled,
        "device_id": device,
        "status": "read" if found is not None else "unreadable",
        "connected": bool(found and found.last_connect != "never"),
        "revoked": found.revoked if found else "unknown",
        "removed": found.removed if found else "unknown",
        "daemon": daemons.state(system, RUNNER_LABEL),
        "disabled_in_login_items": daemons.disabled(system, RUNNER_LABEL),
        "update": {"feed": memory.feed, "pending": memory.pending is not None, "bad": list(memory.bad)},
        "updated_at": system.now().strftime(update.TIME_FORMAT),
    }
    fields["golden"] = golden or old.get("golden", "unknown")
    fields["host_tools"] = host_tools or old.get("host_tools", "unknown")
    last = enrollment or {key: old[key] for key in ("enrollment", "enrollment_error") if key in old}
    fields["enrollment"] = last.get("enrollment", "unknown")
    if "enrollment_error" in last:
        fields["enrollment_error"] = last["enrollment_error"]
    write_state(layout.state_file, fields)
    return fields


def vm_dirs(found: owner.Owner) -> list[Path]:
    """The owner's Lima instances of the runner (prefix `dfx-`); nothing when `~/.lima` is a link (never followed)."""
    lima = found.home / ".lima"
    if lima.is_symlink() or not lima.is_dir():
        return []
    return sorted(path for path in lima.iterdir() if path.name.startswith(VM_PREFIX))


def leftovers(system: System, found: owner.Owner | None) -> list[str]:
    """What an installation left behind: paths (payload, plists, the runner's home and VMs of the owner), launchd
    services and the package receipt. No account is created, so none can be left."""
    layout = system.layout
    paths = [layout.payload, layout.runner_plist, layout.maintenance_plist]
    if found is not None:
        paths += [Layout.runner_home(found.home), *vm_dirs(found)]
    result = [f"path:{path}" for path in paths if path.exists() or path.is_symlink()]
    result += [f"service:{label}" for label in (RUNNER_LABEL, MAINTENANCE_LABEL) if daemons.loaded(system, label)]
    if system.run([PKGUTIL, "--pkg-info", PACKAGE_ID]).returncode == 0:
        result.append(f"receipt:{PACKAGE_ID}")
    return result


def background_items(system: System) -> int:
    """Entries of the background-items database that still name the runner (informational, macOS keeps history)."""
    done = system.run([SFLTOOL, "dumpbtm"])
    return sum(1 for line in done.stdout.splitlines() if RUNNER_LABEL in line)


def recorded_owner(system: System) -> owner.Owner | None:
    name = Maintenance.load(system.layout.maintenance_file).owner
    return owner.lookup(system, name) if name else None


def _remove_owner_dirs(system: System, found: owner.Owner, removed: list[str]) -> list[Path]:
    """Remove the runner's home and what the installation created below the owner's home (innermost first; Lima's
    directories as `LIMA_CONFIG` says); returns Lima's directories it had created, for the leftovers."""
    ctx = runnerctl.Context(system, found, {})
    home = ctx.home
    if home.exists() or home.is_symlink():
        system.must(ctx.as_owner([RM, "-rf", str(home)]), "uninstall_failed", "runner_home")
        removed.append("runner_home")
    created = Maintenance.load(system.layout.maintenance_file).created
    for parent in sorted(created, key=lambda item: item.count("/"), reverse=True):
        path = found.home / parent
        if parent == LIMA_CACHE:  # only downloads: removed with its content
            if path.exists() and system.run(ctx.as_owner([RM, "-rf", str(path)])).returncode == 0:
                removed.append(f"created:{parent}")
            continue
        if parent == LIMA_CONFIG and (path / "_config").exists():
            system.run(ctx.as_owner([RM, "-rf", str(path / "_config")]))
        if system.run(ctx.as_owner([RMDIR, str(path)])).returncode == 0:
            removed.append(f"created:{parent}")
    return [found.home / name for name in (LIMA_CONFIG, LIMA_CACHE) if name in created]


def uninstall(system: System, keys: Sequence[str], *, self_removal: bool = False) -> dict[str, Any]:
    """Remove the installation completely; returns what was removed and what is left (must be empty)."""
    require_root(system)
    check_ancestors(system)
    layout = system.layout
    removed: list[str] = []
    notes: list[str] = []
    if daemons.remove(system, RUNNER_LABEL, layout.runner_plist):
        removed.append("runner_daemon")
    found = recorded_owner(system)
    version = active_version(system)
    if found is not None and version is not None and layout.python().is_file():
        try:
            manifest = _manifest(system, version, keys, verify_tree=False)
            env = runnerctl.runner_env(system, found, manifest, preflight.memory_reserve_mib(system))
            runnerctl.uninstall(runnerctl.Context(system, found, env))
            removed.append("runner_data")
        except RunnerError as error:
            notes.append(f"runner_uninstall:{error.code}")
    lima = _remove_owner_dirs(system, found, removed) if found is not None else []
    if daemons.remove(system, MAINTENANCE_LABEL, layout.maintenance_plist, unload=not self_removal):
        removed.append("maintenance_daemon")
    if system.run([PKGUTIL, "--forget", PACKAGE_ID]).returncode == 0:
        removed.append("receipt")
    if layout.payload.exists():
        shutil.rmtree(layout.payload)
        removed.append("payload")
    left = _left_after_unloading(system, found, self_removal=self_removal)
    left += [f"path:{path}" for path in lima if path.exists() or path.is_symlink()]
    result = {"removed": removed, "leftovers": left, "notes": notes, "background_items": background_items(system)}
    if self_removal:
        system.run([LOGGER, "-t", "dfx-agent-runner", json.dumps(result, sort_keys=True)])
    return result


def _left_after_unloading(system: System, found: owner.Owner | None, *, self_removal: bool) -> list[str]:
    """`leftovers`, asked again while only services are left that may still be ending (the maintenance daemon running
    its own removal stays loaded until it exits and is not counted then)."""

    def now() -> list[str]:
        left = leftovers(system, found)
        return [item for item in left if item != f"service:{MAINTENANCE_LABEL}"] if self_removal else left

    left, polls = now(), 0
    while left and all(item.startswith("service:") for item in left) and polls < UNLOAD_POLLS:
        time.sleep(UNLOAD_POLL_S)
        left, polls = now(), polls + 1
    return left


def _golden_wanted(system: System) -> tuple[str, str] | None:
    try:
        source = golden.load_source(system.layout.golden_template())
    except RunnerError:
        return None
    return str(source.versions["claude_code"]), source.guest_version


def ensure_golden(ctx: runnerctl.Context) -> str:
    """`ready` when a ready golden has the payload's pins, else build one as the owner."""
    wanted = _golden_wanted(ctx.system)
    if wanted is None:
        return "source_missing"
    images = runnerctl.goldens(ctx)
    for image in images:
        if image.get("state") == "ready" and (image.get("claude_version"), image.get("guest_version")) == wanted:
            return "ready"
    document = runnerctl.call(ctx, ["image", "build"])
    return "built" if document.get("ok") is True else "build_failed"


def exclude_vms(ctx: runnerctl.Context) -> None:
    """Keep the runner's VM disks out of Time Machine (they hold task content in plaintext, ADR-074 section 8)."""
    for path in vm_dirs(ctx.owner):
        ctx.system.run(ctx.as_owner([TMUTIL, "addexclusion", str(path)]))


def detach_bootout(label: str) -> None:
    """Unload the maintenance daemon from a detached process: the last step of a self-removal."""
    subprocess.Popen(
        ["/bin/launchctl", "bootout", f"system/{label}"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def maintain(
    system: System,
    keys: Sequence[str],
    *,
    sleep: Callable[[float], None] = time.sleep,
    detach: Callable[[str], None] = detach_bootout,
) -> dict[str, Any]:
    require_root(system)
    check_ancestors(system)
    layout = system.layout
    version = active_version(system)
    if version is None:
        return {"result": "not_installed"}
    manifest = _manifest(system, version, keys, verify_tree=False)
    memory = Maintenance.load(layout.maintenance_file)
    ctx = context(system, manifest, memory.owner)
    found = _started_status(ctx, sleep)
    if found is not None and found.removed != "no":
        result = uninstall(system, keys, self_removal=True)
        detach(MAINTENANCE_LABEL)
        return {"result": "removed", **result}
    try:
        tools = hosttools.ensure(system, manifest)
    except RunnerError as error:
        tools = error.code
    exclude_vms(ctx)
    golden_state = "not_enrolled"
    outcome = update.Outcome("not_enrolled")
    try:
        if found is not None and found.enrolled and found.revoked == "no":
            try:
                golden_state = ensure_golden(ctx)
            except RunnerError as error:
                golden_state = error.code
        if memory.pending is not None:
            outcome = update.resume(ctx, memory)
        elif found is not None and found.enrolled:
            outcome = update.check(
                ctx,
                memory,
                hub=manifest.hub,
                active=version,
                keys=list(keys),
                busy=found.busy_sessions,
                sleep=sleep,
            )
    finally:
        memory.save(layout.maintenance_file)
    active = active_version(system) or version
    publish(ctx, _manifest(system, active, keys, verify_tree=False), memory, host_tools=tools, golden=golden_state)
    return {"result": "maintained", "golden": golden_state, "host_tools": tools, "update": outcome.result}


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="agent-runner-root", add_help=False)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    post = commands.add_parser("postinstall", add_help=False)
    post.add_argument("--version", required=True)
    commands.add_parser("uninstall", add_help=False)
    left = commands.add_parser("leftovers", add_help=False)
    left.add_argument("--owner")
    commands.add_parser("maintain", add_help=False)
    return parser


def run(argv: Sequence[str], system: System, keys: Callable[[], list[str]] = release.packaged_keys) -> int:
    try:
        args = build_parser().parse_args(list(argv))
        if args.command == "postinstall":
            result = postinstall(system, args.version, keys())
        elif args.command == "uninstall":
            result = uninstall(system, keys())
        elif args.command == "leftovers":
            require_root(system)
            named = owner.lookup(system, args.owner) if args.owner else recorded_owner(system)
            result = {"leftovers": leftovers(system, named)}
        else:
            result = maintain(system, keys())
    except RunnerError as error:
        system.stdout.write(json.dumps({"ok": False, "error": {"code": error.code, "name": error.name}}) + "\n")
        return 1
    system.stdout.write(json.dumps({"ok": True, **result}, sort_keys=True) + "\n")
    return 0


def main() -> int:
    return run(sys.argv[1:], System())


if __name__ == "__main__":
    raise SystemExit(main())
