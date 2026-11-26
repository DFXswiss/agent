"""The volume of the slot homes after a restart (HOT-25, `agent_runner.workvm.volume`): finding it in `diskutil list
-plist`, the `/etc/fstab` entry by volume UUID and mounting it back at `/var/agent-workvm`. `diskutil` is a recording
double (07-testing.md section 1); the install, uninstall and status paths are in `test_cli.py`."""

from __future__ import annotations

import plistlib
import subprocess
from collections.abc import Sequence
from typing import Any

import pytest

from agent_runner.errors import RunnerError
from agent_runner.workvm import volume
from agent_runner.workvm.accounts import DISKUTIL, HOME_ROOT, VOLUME_NAME

UUID = "0A1B2C3D-4E5F-4A6B-8C7D-9E0F1A2B3C4D"
ENTRY = f"UUID={UUID} /var/agent-workvm apfs rw"


def listing(*volumes: dict[str, Any]) -> str:
    return plistlib.dumps({"AllDisksAndPartitions": [{"APFSVolumes": list(volumes)}, {"Content": "EFI"}]}).decode()


def ours(**fields: Any) -> dict[str, Any]:
    return {"VolumeName": VOLUME_NAME, "DeviceIdentifier": "disk3s7", "VolumeUUID": UUID, **fields}


class Diskutil:
    def __init__(self, out: str, code: int = 0, failing: tuple[str, ...] = ()) -> None:
        self.out, self.code, self.failing = out, code, failing
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        self.calls.append(tuple(argv))
        if tuple(argv) == (DISKUTIL, "list", "-plist"):
            return subprocess.CompletedProcess(list(argv), self.code, self.out, "")
        return subprocess.CompletedProcess(list(argv), 1 if argv[1] in self.failing else 0, "", "")


def test_find_reads_the_volume_by_name() -> None:
    other = {"VolumeName": "Data", "DeviceIdentifier": "disk3s5", "VolumeUUID": UUID, "MountPoint": "/"}
    assert volume.find(Diskutil(listing(other, ours(MountPoint="/Volumes/AgentWorkVM")))) == volume.Volume(
        "disk3s7", UUID, "/Volumes/AgentWorkVM"
    )
    assert volume.find(Diskutil(listing(ours(DeviceIdentifier="disk3s1s1")))) == volume.Volume("disk3s1s1", UUID, "")
    assert volume.find(Diskutil(listing(other))) is None


@pytest.mark.parametrize(
    ("out", "code", "name"),
    [
        ("", 1, "diskutil"),
        ("not a plist", 0, "diskutil"),
        (plistlib.dumps(["a"]).decode(), 0, "diskutil"),
        (plistlib.dumps({"AllDisksAndPartitions": "x"}).decode(), 0, "diskutil"),
        (listing(ours(DeviceIdentifier="/dev/disk3s7")), 0, "diskutil"),
        (listing(ours(VolumeUUID="lower-case")), 0, "diskutil"),
        (listing(ours(MountPoint=7)), 0, "diskutil"),
        (listing(ours(), ours(DeviceIdentifier="disk4s1")), 0, "ambiguous"),
    ],
)
def test_find_refuses_answers_it_cannot_read(out: str, code: int, name: str) -> None:
    with pytest.raises(RunnerError) as caught:
        volume.find(Diskutil(out, code))
    assert (caught.value.code, caught.value.name) == ("workvm_volume", name)


def test_odd_entries_of_the_listing_are_skipped() -> None:
    data = {"AllDisksAndPartitions": ["x", {"APFSVolumes": ["y", ours()]}]}
    assert volume.find(Diskutil(plistlib.dumps(data).decode())) == volume.Volume("disk3s7", UUID, "")


@pytest.mark.parametrize(
    ("text", "result"),
    [
        ("", ENTRY + "\n"),
        (ENTRY + "\n", None),
        (f"# a comment naming {HOME_ROOT}\n{ENTRY}\n", None),
        ("LABEL=x /other apfs rw\n", f"LABEL=x /other apfs rw\n{ENTRY}\n"),
        (f"UUID={UUID} /elsewhere apfs rw\n", ENTRY + "\n"),
        ("UUID=OLD /private/var/agent-workvm apfs rw\n", ENTRY + "\n"),
        (f"{ENTRY}\n{ENTRY}\n", ENTRY + "\n"),
        (f"{ENTRY} \n", ENTRY + "\n"),
        ("\nonlyonefield\n", f"\nonlyonefield\n{ENTRY}\n"),
    ],
)
def test_the_fstab_entry_replaces_every_line_for_the_volume(text: str, result: str | None) -> None:
    assert volume.with_entry(text, UUID) == result


def test_the_fstab_entry_goes_with_the_volume() -> None:
    assert volume.without_entry("LABEL=x /other apfs rw\n") == "LABEL=x /other apfs rw\n"
    assert volume.without_entry("LABEL=x /other apfs rw") == "LABEL=x /other apfs rw"
    assert volume.without_entry(f"# keep\n{ENTRY}\nLABEL=x /other apfs rw") == "# keep\nLABEL=x /other apfs rw\n"


def test_repair_without_a_volume_changes_nothing() -> None:
    run = Diskutil(listing())
    written: list[str] = []
    assert volume.repair(run, lambda _p: "", lambda p, _c, _m: written.append(p)) is None
    assert (written, run.calls) == ([], [(DISKUTIL, "list", "-plist")])


def test_repair_writes_a_missing_fstab_and_mounts_the_volume_back() -> None:
    run = Diskutil(listing(ours(MountPoint="/Volumes/AgentWorkVM")))
    files: dict[str, tuple[str, int]] = {}

    def missing(path: str) -> str:
        raise FileNotFoundError(path)

    def write(path: str, content: str, mode: int) -> None:
        files[path] = (content, mode)

    assert volume.repair(run, missing, write, fstab="/x/fstab") == volume.Repair("added", "remounted")
    assert files == {"/x/fstab": (ENTRY + "\n", 0o644)}
    assert run.calls[1:] == [
        (DISKUTIL, "unmount", "disk3s7"),
        ("/bin/mkdir", "-p", HOME_ROOT),
        (DISKUTIL, "mount", "-mountPoint", HOME_ROOT, "disk3s7"),
    ]


def test_repair_of_a_volume_in_place_only_checks() -> None:
    run = Diskutil(listing(ours(MountPoint="/private/var/agent-workvm")))
    assert volume.repair(run, lambda _p: ENTRY + "\n", lambda *_a: None) == volume.Repair("present", "ok")
    assert run.calls == [(DISKUTIL, "list", "-plist")]


def test_a_failed_unmount_stops_the_repair() -> None:
    run = Diskutil(listing(ours(MountPoint="/Volumes/AgentWorkVM")), failing=("unmount",))
    with pytest.raises(RunnerError) as caught:
        volume.repair(run, lambda _p: ENTRY + "\n", lambda *_a: None)
    assert (caught.value.code, caught.value.name) == ("workvm_volume", "mount")
    assert run.calls[-1] == (DISKUTIL, "unmount", "disk3s7")
