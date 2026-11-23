"""Slot accounts of the work VMs (docs/platform/spec/13-work-vm.md section 6.1, ADR-059).

Pure builders of what `agent-runner workvm install` (root) runs: the slot account names and SSH ports, the `dscl`
commands of the group and the accounts, their homes on the APFS volume `AgentWorkVM`, the `limactl` wrapper and the
sudoers rule that lets the runner account run only that wrapper as a slot account. Commands that read the machine
take an injected `run(argv, timeout) -> CompletedProcess` (legacy injection style, 07-testing.md section 1).

The runner never passes an environment through sudo: the root-owned wrapper `WRAPPER_PATH` refuses every effective
user but the slot accounts, clears the environment itself (`env -i`) and sets `HOME`, `LIMA_HOME` and `PATH` from its
own fixed values, so the sudoers rule needs no wildcard in environment arguments (`HOME=*` would let the runner
choose any home, including another slot's Lima home). This replaces the `sudo ... /usr/bin/env -i HOME=...` command
line of spec section 6.1 by `sudo -n -u <account> WRAPPER limactl-args...` (open point for the spec).

macOS facts (verified = read in the man pages of macOS 27 on the build device; assumed = not run):
- verified (diskutil(8)): `diskutil apfs addVolume CONTAINER APFS NAME -quota SIZE -mountpoint PATH`; a size without
  suffix is an exact byte count; a custom mount point must already exist and needs root.
- verified (`diskutil info -plist /`): the key `APFSContainerReference` names the boot volume's container.
- verified (visudo(8)): `visudo -c -f -` checks a sudoers text read from standard input.
- assumed: the `dscl . -create` attribute names (`UserShell`, `RealName`, `UniqueID`, `PrimaryGroupID`,
  `NFSHomeDirectory`, `IsHidden`, `Password`) of the local directory node; short names with hyphens are valid.
- assumed: Lima keeps its user key in `$LIMA_HOME/_config/user` (ed25519); the root-owned `_config` therefore gets
  that key generated at installation, owned by the slot account.
- assumed: a custom mount point of `addVolume` does not persist across reboots; an `/etc/fstab` entry by volume UUID
  is still needed (open point).
"""

from __future__ import annotations

import plistlib
import re
import subprocess
from collections.abc import Callable, Sequence
from xml.parsers.expat import ExpatError

from agent_runner.errors import RunnerError
from agent_runner.workvm import firewall

Run = Callable[[Sequence[str], float], "subprocess.CompletedProcess[str]"]
RunInput = Callable[[Sequence[str], str, float], "subprocess.CompletedProcess[str]"]

MAX_SLOTS = 100
HOME_ROOT = "/var/agent-workvm"
VOLUME_NAME = "AgentWorkVM"
WRAPPER_PATH = "/usr/local/libexec/agent-runner-workvm-limactl"
SUDOERS_PATH = "/etc/sudoers.d/agent-runner-workvm"
DSCL = "/usr/bin/dscl"
DISKUTIL = "/usr/sbin/diskutil"
VISUDO = "/usr/sbin/visudo"
COMMAND_TIMEOUT = 30.0
FIRST_UID = 600
_MAX_ID = 2**31

_PREFIX_RE = re.compile(r"^(?:[a-z][a-z0-9-]{0,31})?$")
_ACCOUNT_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]{1,255}$")
_CONTAINER_RE = re.compile(r"^disk[0-9]+$")
_VOLUME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,63}$")
_MIN_PORT = 1024
_MAX_PORT = 65535
_MIN_UID = 501


def _invalid(field: str) -> RunnerError:
    return RunnerError("workvm_accounts_invalid", field)


def _account(name: str) -> str:
    if not _ACCOUNT_RE.fullmatch(name):
        raise _invalid("account")
    return name


def _path(path: str, field: str) -> str:
    if not _PATH_RE.fullmatch(path) or "/../" in f"{path}/" or "//" in path:
        raise _invalid(field)
    return path


def _id(value: int, field: str) -> int:
    if not _MIN_UID <= value <= _MAX_ID:
        raise _invalid(field)
    return value


def _prefix(prefix: str) -> str:
    if not _PREFIX_RE.fullmatch(prefix):
        raise _invalid("prefix")
    return prefix


def slot_accounts(prefix: str, count: int, ssh_port_base: int) -> list[tuple[int, str, int]]:
    """`(index, account, ssh_port)` of every slot: `<prefix>wvm00` ... with port `ssh_port_base + index`."""
    _prefix(prefix)
    if not 0 <= count <= MAX_SLOTS:
        raise _invalid("count")
    if ssh_port_base < _MIN_PORT or ssh_port_base + max(count - 1, 0) > _MAX_PORT:
        raise _invalid("ssh_port_base")
    return [(i, _account(f"{prefix}wvm{i:02d}"), ssh_port_base + i) for i in range(count)]


def group_name(prefix: str) -> str:
    """The dedicated primary group of the slot accounts."""
    return _account(f"{_prefix(prefix)}workvm")


def free_uid(existing: set[int], start: int = FIRST_UID) -> int:
    """The smallest id `>= start` not in `existing` (UIDs and GIDs alike)."""
    candidate = _id(start, "start")
    while candidate in existing:
        candidate += 1
    return _id(candidate, "start")


def group_commands(prefix: str, gid: int) -> list[list[str]]:
    """`dscl` commands creating the slot accounts' group."""
    record = f"/Groups/{group_name(prefix)}"
    _id(gid, "gid")
    return [
        [DSCL, ".", "-create", record],
        [DSCL, ".", "-create", record, "PrimaryGroupID", str(gid)],
        [DSCL, ".", "-create", record, "RealName", "Agent work VM slots"],
        [DSCL, ".", "-create", record, "Password", "*"],
    ]


def create_commands(account: str, uid: int, gid: int, group: str, home: str) -> list[list[str]]:
    """Commands creating one hidden slot account without login and its home with the Lima home.

    The home (0700) and `lima/` belong to the account; `lima/_config` is root-owned and read-only for it, holding the
    Lima user key generated here (owned by the account, 0600), so the host agent cannot add an `override.yaml`.
    """
    record = f"/Users/{_account(account)}"
    _account(group)
    _id(uid, "uid")
    _id(gid, "gid")
    _path(home, "home")
    lima = f"{home}/lima"
    config = f"{lima}/_config"
    owner = f"{account}:{group}"
    return [
        [DSCL, ".", "-create", record],
        [DSCL, ".", "-create", record, "UserShell", "/usr/bin/false"],
        [DSCL, ".", "-create", record, "RealName", f"Agent work VM slot {account}"],
        [DSCL, ".", "-create", record, "UniqueID", str(uid)],
        [DSCL, ".", "-create", record, "PrimaryGroupID", str(gid)],
        [DSCL, ".", "-create", record, "NFSHomeDirectory", home],
        [DSCL, ".", "-create", record, "IsHidden", "1"],
        [DSCL, ".", "-create", record, "Password", "*"],
        ["/usr/bin/install", "-d", "-o", account, "-g", group, "-m", "0700", home],
        ["/usr/bin/install", "-d", "-o", account, "-g", group, "-m", "0700", lima],
        ["/usr/bin/install", "-d", "-o", "root", "-g", "wheel", "-m", "0755", config],
        ["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "lima", "-f", f"{config}/user"],
        ["/usr/sbin/chown", owner, f"{config}/user", f"{config}/user.pub"],
        ["/bin/chmod", "0600", f"{config}/user"],
    ]


def wrapper_script(
    accounts: Sequence[str], home_root: str = HOME_ROOT, limactl: str = "/opt/homebrew/bin/limactl"
) -> str:
    """The root-owned (0755) POSIX sh wrapper at `WRAPPER_PATH` that runs `limactl` for a slot account."""
    if not accounts:
        raise _invalid("accounts")
    names = "|".join(_account(a) for a in accounts)
    _path(home_root, "home_root")
    _path(limactl, "limactl")
    bindir = limactl.rsplit("/", 1)[0] or "/"
    return f"""#!/bin/sh
# agent-runner work VM limactl wrapper, generated by agent-runner workvm install (spec 11 section 6.1).
set -eu
umask 077
user=$(/usr/bin/id -un)
case "$user" in
  {names}) ;;
  *) echo "agent-runner-workvm-limactl: refused for this account" >&2; exit 126 ;;
esac
home='{home_root}'/"$user"
cd "$home"
exec /usr/bin/env -i HOME="$home" LIMA_HOME="$home/lima" PATH='{bindir}:/usr/bin:/bin' '{limactl}' "$@"
"""


def sudoers(runner_account: str, accounts: Sequence[str], wrapper: str = WRAPPER_PATH) -> str:
    """`/etc/sudoers.d/agent-runner-workvm`: the runner may run only the wrapper, only as a slot account, and as root
    only the exact read-only `pfctl` commands of the boundary check (`firewall.READ_COMMANDS`)."""
    _account(runner_account)
    if not accounts:
        raise _invalid("accounts")
    names = ", ".join(_account(a) for a in accounts)
    _path(wrapper, "wrapper")
    reads = ", ".join(" ".join(argv) for argv in firewall.READ_COMMANDS)
    return (
        "# agent-runner work VM slots, generated by agent-runner workvm install (spec 11 section 6.1).\n"
        f"Defaults!{wrapper} env_reset\n"
        f"Runas_Alias AGENT_WORKVM = {names}\n"
        f"{runner_account} ALL = (AGENT_WORKVM) NOPASSWD: {wrapper}\n"
        f"{runner_account} ALL = (root) NOPASSWD: {reads}\n"
    )


def sudoers_check(run: RunInput, text: str) -> bool:
    """True when `visudo -c -f -` accepts `text` (read from standard input)."""
    return run([VISUDO, "-c", "-f", "-"], text, COMMAND_TIMEOUT).returncode == 0


def apfs_container(run: Run) -> str:
    """The APFS container of the boot volume (`APFSContainerReference` of `diskutil info -plist /`)."""
    result = run([DISKUTIL, "info", "-plist", "/"], COMMAND_TIMEOUT)
    if result.returncode != 0:
        raise RunnerError("workvm_volume_container", "diskutil")
    try:
        data = plistlib.loads(result.stdout.encode())
    except (ValueError, ExpatError):
        raise RunnerError("workvm_volume_container", "diskutil") from None
    container = data.get("APFSContainerReference") if isinstance(data, dict) else None
    if not isinstance(container, str) or not _CONTAINER_RE.fullmatch(container):
        raise RunnerError("workvm_volume_container", "diskutil")
    return container


def apfs_volume_commands(
    container: str, quota_gib: int, volume_name: str = VOLUME_NAME, mountpoint: str = HOME_ROOT
) -> list[list[str]]:
    """Commands adding the quota-bounded APFS volume of the work VM homes, mounted at `mountpoint`."""
    if not _CONTAINER_RE.fullmatch(container):
        raise _invalid("container")
    if not 0 < quota_gib <= 1_000_000:
        raise _invalid("quota_gib")
    if not _VOLUME_RE.fullmatch(volume_name):
        raise _invalid("volume_name")
    _path(mountpoint, "mountpoint")
    quota = str(quota_gib * 2**30)
    return [
        ["/bin/mkdir", "-p", mountpoint],
        [DISKUTIL, "apfs", "addVolume", container, "APFS", volume_name, "-quota", quota, "-mountpoint", mountpoint],
        ["/usr/sbin/chown", "root:wheel", mountpoint],
        ["/bin/chmod", "0755", mountpoint],
    ]
