"""Slot accounts of the work VMs (docs/platform/spec/13-work-vm.md section 6.1, ADR-059).

Pure builders of what `agent-runner workvm install` (root) runs: the number of slot accounts (`slot_count`: the RAM
decides), the slot account names and SSH ports, the `dscl`
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
- verified (Lima 2.3.0, test device): Lima keeps its user key in `$LIMA_HOME/_config/user` (ed25519), and the first
  `limactl start` writes `$LIMA_HOME/_config/networks.yaml` (0644) - with a read-only `_config` it fails with `open
  .../_config/networks.yaml: permission denied`. Besides `_config` it writes only the instance directory. `_config`
  therefore belongs to the slot account like in any Lima home (decision HOT-18); the key is generated at installation.
- assumed: a custom mount point of `addVolume` does not persist across reboots; an `/etc/fstab` entry by volume UUID
  is still needed (open point).
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
from collections.abc import Callable, Sequence
from xml.parsers.expat import ExpatError

from agent_runner.errors import RunnerError
from agent_runner.vm.capacity import host_reserve_mib
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


SMALLEST_VM_MIB = 8192
"""RAM of the smallest work VM (`standard`, `manager.SIZES`)."""


def slot_count(physical_mib: int, reserve_mib: int | None) -> int:
    """The number of slot accounts (ADR-059 addendum 4): as many as the RAM above the host reserve (`reserve_mib`, None:
    the rule of every device, `vm.capacity.host_reserve_mib`) could ever hold standard work VMs (at least 1, at most
    `MAX_SLOTS`, the range of the pinned ports and the store), so the accounts - isolation units - never limit before
    the free resources do. There is no setting for it."""
    room = physical_mib - host_reserve_mib(physical_mib, reserve_mib)
    return max(1, min(MAX_SLOTS, room // SMALLEST_VM_MIB))


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

    The home, `lima/` and `lima/_config` (0700 each) belong to the account, as in any Lima home: Lima writes
    `_config/networks.yaml` at the first start. `_config` holds the Lima user key generated here (0600).
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
        ["/usr/bin/install", "-d", "-o", account, "-g", group, "-m", "0700", config],
        ["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "lima", "-f", f"{config}/user"],
        ["/usr/sbin/chown", owner, f"{config}/user", f"{config}/user.pub"],
        ["/bin/chmod", "0600", f"{config}/user"],
    ]


LIMA_CONFIG_MODE = 0o700


def give_lima_config(home: str, uid: int, gid: int) -> str:
    """Gives `<home>/lima/_config` of a present slot account to the account (`uid`, `gid`, 0700) and creates it when
    it is missing: installations before HOT-18 made it root-owned, and Lima cannot start there. Returns "present" or
    "created".

    Root runs this inside a home the slot account controls, so `lima` and `_config` are opened with `O_NOFOLLOW` and
    changed through their descriptors: a symbolic link planted there (for example to another slot's `lima`) is
    refused (`workvm_install_failed`, `lima_config`) instead of being followed."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fds: list[int] = []
    try:
        fds.append(os.open(_path(home, "home"), flags))
        fds.append(os.open("lima", flags, dir_fd=fds[-1]))
        state = "present"
        try:
            fds.append(os.open("_config", flags, dir_fd=fds[-1]))
        except FileNotFoundError:
            os.mkdir("_config", LIMA_CONFIG_MODE, dir_fd=fds[-1])
            fds.append(os.open("_config", flags, dir_fd=fds[-1]))
            state = "created"
        os.fchown(fds[-1], _id(uid, "uid"), _id(gid, "gid"))
        os.fchmod(fds[-1], LIMA_CONFIG_MODE)
    except OSError:
        raise RunnerError("workvm_install_failed", "lima_config") from None
    finally:
        for fd in fds:
            os.close(fd)
    return state


def delete_command(account: str) -> list[str]:
    """The `dscl` command deleting a slot account (its home goes with the volume)."""
    return [DSCL, ".", "-delete", f"/Users/{_account(account)}"]


def delete_group_command(group: str) -> list[str]:
    return [DSCL, ".", "-delete", f"/Groups/{_account(group)}"]


def delete_volume_command(mountpoint: str = HOME_ROOT) -> list[str]:
    """Deletes the APFS volume mounted at `mountpoint` with every slot home and work VM disk (assumed: `diskutil apfs
    deleteVolume` takes a mount point like other diskutil verbs)."""
    return [DISKUTIL, "apfs", "deleteVolume", _path(mountpoint, "mountpoint")]


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
