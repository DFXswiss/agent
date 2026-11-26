"""The APFS volume of the slot homes after a restart of the device (docs/platform/spec/13-work-vm.md section 6.1,
HOT-25).

`workvm install` adds the volume `accounts.VOLUME_NAME` with `-mountpoint /var/agent-workvm`. That mount point does not
persist: after a restart macOS mounts the volume under `/Volumes/<name>`, the `limactl` wrapper finds no slot home and
every work VM call fails (observed after a power cut). The fix is an `/etc/fstab` entry by volume UUID
(`UUID=<uuid> /var/agent-workvm apfs rw`), which macOS reads when it mounts the volume at the next start (fstab(5)).

`repair(run, read_file, write_file)` (root) makes both true for an existing volume: the `fstab` entry is present
(other lines naming that UUID or that mount point are replaced, everything else is kept) and the volume hangs at
`HOME_ROOT` (unmounted from wherever it hangs and mounted there with `diskutil mount -mountPoint`). It returns None
when the volume does not exist. `workvm install` runs it after the volume step, so running `install` again on an
existing device - the update step of the work VM installation (runbook `work-vms.md`) - adds the entry and mounts the
volume back. The unprivileged daemon cannot mount anything or write `/etc/fstab`: it checks the mount point, offers no
work VM while it is wrong, and `doctor` reports it (check `workvm_volume`: fail `not_mounted`, warn `fstab_missing`).

macOS facts (verified = read in the man pages of macOS 27 or run on the build device; assumed = not run):
- verified (diskutil(8), DEVICES): a device is a disk identifier, device node, mount point or UUID - not a volume
  name; so the volume is looked up by `VolumeName` in `diskutil list -plist`, whose `AllDisksAndPartitions[]`
  `.APFSVolumes[]` carry `VolumeName`, `DeviceIdentifier` (`disk3s5`, a snapshot `disk3s1s1`), `VolumeUUID` and
  `MountPoint` (absent while unmounted; run on the build device).
- verified (diskutil(8)): `diskutil mount -mountPoint PATH DEVICE` mounts at an existing directory instead of
  `/Volumes/<name>`; `diskutil unmount DEVICE`.
- verified (fstab(5)): APFS volumes are named by `UUID=` or `LABEL=` only, never by device; vfstype `apfs`.
- assumed: macOS mounts an APFS volume with an `fstab` entry at that entry's path at the next start.
- `/var` is a link to `/private/var`, so diskutil may name the mount point either way; both count.
"""

from __future__ import annotations

import plistlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from xml.parsers.expat import ExpatError

from agent_runner.errors import RunnerError
from agent_runner.workvm.accounts import COMMAND_TIMEOUT, DISKUTIL, HOME_ROOT, VOLUME_NAME, Run

FSTAB = "/etc/fstab"
FSTAB_MODE = 0o644
_DEVICE_RE = re.compile(r"^disk[0-9]+(?:s[0-9]+)+$")
_UUID_RE = re.compile(r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$")

ReadFile = Callable[[str], str]
WriteFile = Callable[[str, str, int], None]


@dataclass(frozen=True)
class Volume:
    device: str
    uuid: str
    mountpoint: str
    """Where it hangs now; empty while it is not mounted."""


@dataclass(frozen=True)
class Repair:
    fstab: str
    """`present` or `added`."""
    mount: str
    """`ok` or `remounted`."""


def has_entry(text: str, mountpoint: str = HOME_ROOT) -> bool:
    """True when `text` (an fstab) has an entry for `mountpoint` (`doctor`; the UUID is not readable without the
    volume listing)."""
    return any(_names(line, "", mountpoint) for line in text.splitlines())


def same_place(mountpoint: str, wanted: str = HOME_ROOT) -> bool:
    """True when `mountpoint` is `wanted`, also through the `/private` of `/var`."""
    return mountpoint in (wanted, f"/private{wanted}")


def find(run: Run, name: str = VOLUME_NAME) -> Volume | None:
    """The APFS volume named `name` (`diskutil list -plist`), None when there is none; `workvm_volume diskutil` for an
    answer it cannot read, `workvm_volume ambiguous` for two volumes of that name."""
    result = run([DISKUTIL, "list", "-plist"], COMMAND_TIMEOUT)
    if result.returncode != 0:
        raise RunnerError("workvm_volume", "diskutil")
    try:
        data = plistlib.loads(result.stdout.encode())
    except (ValueError, ExpatError):
        raise RunnerError("workvm_volume", "diskutil") from None
    disks = data.get("AllDisksAndPartitions") if isinstance(data, dict) else None
    if not isinstance(disks, list):
        raise RunnerError("workvm_volume", "diskutil")
    found = [
        _volume(entry)
        for disk in disks
        if isinstance(disk, dict)
        for entry in disk.get("APFSVolumes", [])
        if isinstance(entry, dict) and entry.get("VolumeName") == name
    ]
    if len(found) > 1:
        raise RunnerError("workvm_volume", "ambiguous")
    return found[0] if found else None


def _volume(entry: dict[str, object]) -> Volume:
    device, uuid, mountpoint = entry.get("DeviceIdentifier"), entry.get("VolumeUUID"), entry.get("MountPoint", "")
    if not isinstance(device, str) or not _DEVICE_RE.fullmatch(device):
        raise RunnerError("workvm_volume", "diskutil")
    if not isinstance(uuid, str) or not _UUID_RE.fullmatch(uuid) or not isinstance(mountpoint, str):
        raise RunnerError("workvm_volume", "diskutil")
    return Volume(device=device, uuid=uuid, mountpoint=mountpoint)


def fstab_line(uuid: str, mountpoint: str = HOME_ROOT) -> str:
    return f"UUID={uuid} {mountpoint} apfs rw"


def _names(line: str, uuid: str, mountpoint: str) -> bool:
    """True for an entry (not a comment) that names the volume's UUID or the mount point."""
    fields = line.split()
    if not fields or fields[0].startswith("#"):
        return False
    return fields[0] == f"UUID={uuid}" or (len(fields) > 1 and same_place(fields[1], mountpoint))


def with_entry(text: str, uuid: str, mountpoint: str = HOME_ROOT) -> str | None:
    """`text` with the volume's entry instead of every other line naming that UUID or mount point (other lines are
    kept); None when the entry is already the only such line."""
    wanted = fstab_line(uuid, mountpoint)
    lines = text.splitlines()
    if [line for line in lines if _names(line, uuid, mountpoint)] == [wanted]:
        return None
    return "\n".join([*(line for line in lines if not _names(line, uuid, mountpoint)), wanted]) + "\n"


def without_entry(text: str, mountpoint: str = HOME_ROOT) -> str:
    """`text` without the entries naming `mountpoint` (`workvm uninstall`); unchanged when there is none."""
    lines = text.splitlines()
    kept = [line for line in lines if not _names(line, "", mountpoint)]
    if kept == lines:
        return text
    return "".join(f"{line}\n" for line in kept)


def remount_commands(volume: Volume, mountpoint: str = HOME_ROOT) -> list[list[str]]:
    """Unmount the volume where it hangs (when it hangs anywhere) and mount it at `mountpoint`."""
    commands = [] if not volume.mountpoint else [[DISKUTIL, "unmount", volume.device]]
    return [
        *commands,
        ["/bin/mkdir", "-p", mountpoint],
        [DISKUTIL, "mount", "-mountPoint", mountpoint, volume.device],
    ]


def repair(run: Run, read_file: ReadFile, write_file: WriteFile, *, fstab: str = FSTAB) -> Repair | None:
    """The `fstab` entry and the mount point of the volume (module docstring); None without a volume."""
    volume = find(run)
    if volume is None:
        return None
    try:
        text = read_file(fstab)
    except FileNotFoundError:
        text = ""
    updated = with_entry(text, volume.uuid)
    if updated is not None:
        write_file(fstab, updated, FSTAB_MODE)
    mount = "ok"
    if not same_place(volume.mountpoint):
        for argv in remount_commands(volume):
            if run(argv, COMMAND_TIMEOUT).returncode != 0:
                raise RunnerError("workvm_volume", "mount")
        mount = "remounted"
    return Repair(fstab="present" if updated is None else "added", mount=mount)
