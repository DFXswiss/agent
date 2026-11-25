"""Work image build (docs/platform/spec/13-work-vm.md section 4): `image.Builder` against a stateful `limactl`
stand-in (`FakeLimactl.run`, the `LimactlRun` of a real `Lima`), a temporary `images/` tree and, for the registry, a
real PostgreSQL database. Nothing needs Lima or root."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tarfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agent_runner.errors import RunnerError
from agent_runner.store.migrate import migrate
from agent_runner.store.pool import open_pool
from agent_runner.vm.lima import Lima, LimaError
from agent_runner.workvm import image, records

PREFIX = "wp-t-"
DISK = b"head" + bytes(3 * image.CHUNK) + b"tail" + bytes(image.CHUNK)
AT = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)


@dataclass
class FakeLimactl:
    """Instances in a dict and directories below `home`; `fail[key]` answers one operation or guest step."""

    home: Path
    instances: dict[str, str] = field(default_factory=dict)
    fail: dict[str, tuple[int, str]] = field(default_factory=dict)
    vanish_on: str | None = None
    sock: str = "dir"
    disk: str | None = "disk"
    """The disk file a new instance gets: `disk` (Lima 2.x), `diffdisk` (older Lima) or none."""
    calls: list[list[str]] = field(default_factory=list)
    bundle: list[str] = field(default_factory=list)
    modes: dict[str, int] = field(default_factory=dict)

    def _list(self, names: Sequence[str]) -> tuple[int, str, str]:
        if names and names[0] not in self.instances:
            return 1, "", f"No instance matching {names[0]} found\n"
        shown = names or sorted(self.instances)
        lines = [
            json.dumps(
                {"name": n, "status": self.instances[n], "dir": str(self.home / n), "cpus": 4, "memory": 0, "disk": 0}
            )
            for n in shown
        ]
        return 0, "".join(line + "\n" for line in lines), ""

    @staticmethod
    def _shell_key(argv: Sequence[str]) -> str:
        text = " ".join(argv)
        for key, marker in (("provision", "provision.sh"), ("smoke", "selftest"), ("extract", "tar -xf")):
            if marker in text:
                return key
        return "seal"

    def _act(self, op: str, argv: list[str]) -> tuple[int, str, str]:
        if op == "--version":
            return 0, "limactl version 2.3.0\n", ""
        if op == "list":
            return self._list(argv[2:])
        if op == "create":
            name = argv[2].removeprefix("--name=")
            directory = self.home / name
            directory.mkdir(parents=True)
            if self.sock == "dir":
                (directory / "sock").mkdir()
                (directory / "sock").chmod(0o000)
            elif self.sock == "link":
                (directory / "sock").symlink_to(self.home)
            if self.disk is not None:
                (directory / self.disk).write_bytes(DISK)
            self.instances[name] = "Stopped"
        elif op == "start":
            self.instances[argv[-1]] = "Running"
        elif op == "copy":
            with tarfile.open(argv[1]) as archive:
                self.bundle = archive.getnames()
                self.modes = {member.name: member.mode for member in archive.getmembers()}
        elif op == "stop":
            self.instances[argv[1]] = "Stopped"
        elif op == "rename":
            (self.home / argv[1]).rename(self.home / argv[2])
            self.instances[argv[2]] = self.instances.pop(argv[1])
        elif op == "delete":
            if argv[2] not in self.instances:
                return 0, "", f"Ignoring non-existent instance {argv[2]}\n"
            del self.instances[argv[2]]
            shutil.rmtree(self.home / argv[2], ignore_errors=True)
        return 0, "", ""

    def run(self, argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        args = list(argv)
        self.calls.append(args)
        op = args[0]
        key = self._shell_key(args[3:]) if op == "shell" else op
        if key in self.fail:
            code, stderr = self.fail[key]
            return subprocess.CompletedProcess(args, code, "", stderr)
        code, out, err = self._act(op, args)
        if op == self.vanish_on:
            self.instances.clear()
        return subprocess.CompletedProcess(args, code, out, err)

    def lima(self) -> Lima:
        return Lima(self.run, prefix=PREFIX)


def make_images(root: Path) -> Path:
    """An `images/` tree with the files a bundle keeps and the ones it leaves out; returns the template."""
    images = root / "images"
    (images / "workvm" / "files").mkdir(parents=True)
    (images / "workvm" / "provision.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (images / "workvm" / "provision.sh").chmod(0o755)
    (images / "workvm" / "files" / "job").write_text("job\n", encoding="utf-8")
    (images / "versions.json").write_text("{}\n", encoding="utf-8")
    (images / "workvm.yaml").write_text("vmType: vz\n", encoding="utf-8")
    (images / ".DS_Store").write_text("x", encoding="utf-8")
    (images / "workvm" / "x.pyc").write_bytes(b"x")
    (images / "__pycache__").mkdir()
    (images / "__pycache__" / "y.txt").write_text("y", encoding="utf-8")
    (images / ".hidden").mkdir()
    (images / ".hidden" / "z.txt").write_text("z", encoding="utf-8")
    (images / "link").symlink_to(images / "versions.json")
    return images / "workvm.yaml"


@dataclass
class Rig:
    fake: FakeLimactl
    template: Path
    scratch: Path
    exports: Path
    facts: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    registered: list[tuple[str, datetime]] = field(default_factory=list)

    def builder(self, *, export: bool = False, **overrides: Any) -> image.Builder:
        fields: dict[str, Any] = {
            "lima": self.fake.lima(),
            "template": self.template,
            "scratch": self.scratch,
            "register": lambda name, at: self.registered.append((name, at)),
            "export_dir": self.exports if export else None,
            "announce": lambda name, values: self.facts.append((name, values)),
            "log": self.logs.append,
            "now": lambda: AT,
            "token_hex": lambda n: "ab" * n,
        }
        fields.update(overrides)
        return image.Builder(**fields)


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    for name in ("lima", "scratch", "exports"):
        (tmp_path / name).mkdir()
    return Rig(
        FakeLimactl(tmp_path / "lima"), make_images(tmp_path / "tree"), tmp_path / "scratch", tmp_path / "exports"
    )


STEPS = ["create", "start", "bundle", "provision", "smoke", "seal", "stop", "rename"]
BUILD = f"{PREFIX}wimg-build-abababab"
FINAL = f"{PREFIX}wimg-20261123-ababab"


def test_build_in_self_mode(rig: Rig) -> None:
    built = rig.builder().build()
    assert (built.name, built.export) == (FINAL, None)
    assert built.seconds >= 0
    names = [(name, values["step"], values["name"]) for name, values in rig.facts]
    expected = [("workvm.image", step, BUILD) for step in STEPS] + [("workvm.image", "register", FINAL)]
    assert names == expected
    assert rig.registered == [(FINAL, AT)]
    assert rig.fake.instances == {FINAL: "Stopped"}
    assert not (rig.fake.home / FINAL / "sock").exists()
    assert rig.fake.bundle == [
        "images/versions.json",
        "images/workvm.yaml",
        "images/workvm/files/job",
        "images/workvm/provision.sh",
    ]
    assert rig.fake.modes["images/workvm/provision.sh"] == 0o755
    assert rig.fake.modes["images/workvm.yaml"] == 0o644
    provision = [call for call in rig.fake.calls if call[0] == "shell" and "bash" in call]
    assert provision == [
        ["shell", "--workdir", "/", BUILD, "sudo", "bash", "/tmp/agent-workvm/images/workvm/provision.sh"]
    ]
    assert ["create", "-y", f"--name={BUILD}", str(rig.template)] in rig.fake.calls
    assert list(rig.scratch.iterdir()) == []
    assert list(rig.exports.iterdir()) == []


@pytest.mark.parametrize("disk", ["disk", "diffdisk"])
def test_build_with_slot_accounts_exports_the_disk(rig: Rig, disk: str) -> None:
    rig.fake.disk = disk
    built = rig.builder(export=True).build("v1")
    assert built.name == f"{PREFIX}wimg-v1"
    assert built.export == rig.exports / f"{PREFIX}wimg-v1.img"
    assert built.export.read_bytes() == DISK
    assert built.export.stat().st_mode & 0o777 == 0o644
    assert sorted(path.name for path in rig.exports.iterdir()) == [f"{PREFIX}wimg-v1.img"]
    assert [values["step"] for _, values in rig.facts][-2:] == ["export", "register"]


@pytest.mark.parametrize(
    ("suffix", "setup", "error"),
    [
        ("Bad Name", None, ("usage", "name")),
        (None, "no_source", ("workvm_image_source_missing", None)),
        (None, "no_exports", ("workvm_not_installed", "images")),
        ("v1", "exists", ("workvm_image_exists", None)),
    ],
)
def test_build_refusals(rig: Rig, suffix: str | None, setup: str | None, error: tuple[str, str | None]) -> None:
    if setup == "no_source":
        (rig.template.parent / "workvm" / "provision.sh").unlink()
    if setup == "no_exports":
        rig.exports.rmdir()
    if setup == "exists":
        rig.fake.instances[f"{PREFIX}wimg-v1"] = "Stopped"
    with pytest.raises(RunnerError) as caught:
        rig.builder(export=True).build(suffix)
    assert (caught.value.code, caught.value.name) == error
    assert not any(call[0] == "create" for call in rig.fake.calls)


@pytest.mark.parametrize("key", ["create", "start", "copy", "extract", "provision", "smoke", "seal", "stop", "rename"])
def test_failed_step_deletes_the_instance(rig: Rig, key: str) -> None:
    rig.fake.fail[key] = (1, f"boom {key}\n")
    with pytest.raises(LimaError):
        rig.builder(export=True).build()
    assert rig.fake.instances == {}
    assert rig.facts[-1][0] == "workvm.image.failed"
    assert rig.logs
    assert f"boom {key}" in rig.logs[-1]
    assert rig.registered == []
    assert list(rig.exports.iterdir()) == []
    assert list(rig.scratch.iterdir()) == []


def test_failure_without_lima_detail_is_not_logged(rig: Rig) -> None:
    def refuse(name: str, at: datetime) -> None:
        raise RunnerError("store_unavailable")

    with pytest.raises(RunnerError, match="store_unavailable"):
        rig.builder(export=True, register=refuse).build("v1")
    assert rig.facts[-1] == ("workvm.image.failed", {"step": "register", "name": f"{PREFIX}wimg-v1"})
    assert rig.logs == []
    assert rig.fake.instances == {}
    assert list(rig.exports.iterdir()) == []


def test_lima_error_without_detail_is_not_logged(rig: Rig) -> None:
    rig.fake.fail["stop"] = (1, "")
    with pytest.raises(LimaError):
        rig.builder().build()
    assert rig.logs == []


@pytest.mark.parametrize(("vanish_on", "step"), [("stop", "stop"), ("rename", "export")])
def test_instance_gone(rig: Rig, vanish_on: str, step: str) -> None:
    rig.fake.vanish_on = vanish_on
    with pytest.raises(RunnerError) as caught:
        rig.builder(export=True).build()
    assert (caught.value.code, caught.value.name) == ("workvm_image_failed", step)


def test_failed_export_removes_the_partial_copy(rig: Rig) -> None:
    def broken(source: Path, target: Path) -> None:
        target.write_bytes(b"partial")
        raise OSError("disk full")

    with pytest.raises(RunnerError) as caught:
        rig.builder(export=True, copy_disk=broken).build("v1")
    assert (caught.value.code, caught.value.name) == ("workvm_image_failed", "export")
    assert str(caught.value.__cause__) == "disk full"
    assert rig.facts[-1] == ("workvm.image.failed", {"step": "export", "name": f"{PREFIX}wimg-v1"})
    assert list(rig.exports.iterdir()) == []
    assert rig.fake.instances == {}


def test_missing_disk_fails_the_export_with_its_path(rig: Rig) -> None:
    rig.fake.disk = None
    with pytest.raises(RunnerError) as caught:
        rig.builder(export=True).build("v1")
    assert (caught.value.code, caught.value.name) == ("workvm_image_failed", "export")
    path = str(rig.fake.home / f"{PREFIX}wimg-v1" / "disk")
    assert rig.facts[-1] == ("workvm.image.failed", {"step": "export", "name": f"{PREFIX}wimg-v1", "path": path})
    assert list(rig.exports.iterdir()) == []
    assert rig.fake.instances == {}


def test_cleanup_tolerates_a_failing_delete(rig: Rig) -> None:
    rig.fake.fail["provision"] = (1, "")
    rig.fake.fail["delete"] = (1, "busy\n")
    with pytest.raises(LimaError) as caught:
        rig.builder().build()
    assert caught.value.operation == "shell"
    assert rig.fake.instances == {BUILD: "Running"}


def test_copy_sparse(tmp_path: Path) -> None:
    source = tmp_path / "disk"
    source.write_bytes(DISK)
    target = tmp_path / "copy"
    image.copy_sparse(source, target)
    assert target.read_bytes() == DISK
    assert target.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        image.copy_sparse(source, target)
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    image.copy_sparse(empty, tmp_path / "empty-copy")
    assert (tmp_path / "empty-copy").read_bytes() == b""


def test_registry(runner_dsn: str) -> None:
    assert migrate(runner_dsn).status == "applied"
    assert image.list_images(runner_dsn) == []
    image.register_ready(runner_dsn, f"{PREFIX}wimg-b", AT.replace(hour=11))
    image.register_ready(runner_dsn, f"{PREFIX}wimg-a", AT)
    assert image.list_images(runner_dsn) == [
        image.Image(f"{PREFIX}wimg-a", "ready", AT),
        image.Image(f"{PREFIX}wimg-b", "ready", AT.replace(hour=11)),
    ]

    async def ready() -> str | None:
        db = await open_pool(runner_dsn, min_size=1, max_size=1)
        try:
            return await records.ready_image(db)
        finally:
            await db.close()

    assert asyncio.run(ready()) == f"{PREFIX}wimg-b"


def test_bundle_members_skip_links_hidden_and_caches(tmp_path: Path) -> None:
    template = make_images(tmp_path)
    names = [name for name, _ in image.bundle_members(template.parent)]
    assert names == [
        "images/versions.json",
        "images/workvm.yaml",
        "images/workvm/files/job",
        "images/workvm/provision.sh",
    ]
    assert os.path.islink(template.parent / "link")


@pytest.mark.parametrize("sock", ["link", "none"])
def test_socket_dir_only_removed_when_a_real_directory(rig: Rig, sock: str) -> None:
    rig.fake.sock = sock
    built = rig.builder().build("v1")
    assert (rig.fake.home / built.name / "sock").is_symlink() == (sock == "link")


def test_the_smoke_test_checks_the_job_helper_and_the_platform_browser() -> None:
    steps = image.SMOKE_SCRIPT.split(" && ")
    assert steps[-2:] == ["/usr/local/sbin/workvm-job selftest", "/usr/local/bin/workvm-browser selftest"]
