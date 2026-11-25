"""Work image build and registry (docs/platform/spec/13-work-vm.md section 4, ADR-059).

`Builder.build(...)` turns `images/workvm.yaml` (or the configured template; its directory is the `images/` tree)
into a stopped Lima instance in the runner's own Lima home:

1. `limactl` >= 2.3.0; the name `<prefix>wimg-<yyyymmdd>-<6 hex>` (or `<prefix>wimg-<NAME>`) must be free.
2. `limactl create --name=<prefix>wimg-build-<8 hex>` from the template + `start`.
3. `bundle`: the `images/` tree as a tar (root-owned entries, no hidden files or caches) copied in and extracted to
   `GUEST_BUNDLE_DIR`; `provision`: `sudo bash <bundle>/images/workvm/provision.sh`.
4. `smoke`: `python3 --version`, `node --version`, `workvm-job selftest`; `seal`: bundle removed, `sync`.
5. `stop`, the empty mode-000 `sock/` directory Lima leaves behind removed (it makes `limactl clone` fail),
   `rename` to the final name.
6. `export` (slot accounts only, `export_dir` set): the instance's writable disk (`shell.disk_path`: `disk` with
   Lima 2.x, `diffdisk` with older Lima) copied sparsely to `<export_dir>/<name>.img` (0644), the disk each slot
   account creates its base `wbase-<hash>` from (`service.Bases`). In self mode the stopped instance itself is the
   clone base and nothing is exported.
7. `register`: the injected callable records the image (`runner.workvm_image` `ready`).

Every step is announced as `workvm.image step=<step> name=<instance> seconds=<s>`. Any failure announces
`workvm.image.failed step=<step> name=<instance>` (plus `path=<path>` when a file operation names one), passes Lima's
stderr tail to `log`, deletes the instance and the partial export and re-raises; a failed file operation (`OSError`)
becomes `workvm_image_failed <step>` instead of an internal error. `list_images` reads the registry. All effects are
injected (Lima, clock, now, random source, disk copy, registration), so the tests need neither Lima nor root.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import re
import secrets
import shutil
import tarfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from agent_runner.errors import RunnerError
from agent_runner.store.pool import open_pool
from agent_runner.vm import golden
from agent_runner.vm.lima import Lima, LimaError
from agent_runner.workvm import records
from agent_runner.workvm.shell import disk_path

GUEST_BUNDLE = "/tmp/agent-workvm.tar"
GUEST_BUNDLE_DIR = "/tmp/agent-workvm"
PROVISION = "images/workvm/provision.sh"
PROVISION_TIMEOUT_S = 3600.0
SMOKE_TIMEOUT_S = 300.0
SMOKE_SCRIPT = (
    "python3 --version && node --version && /usr/local/sbin/workvm-job selftest"
    " && /usr/local/bin/workvm-browser selftest"
    " && { systemctl stop workvm-browser.service || true; }"
    " && rm -rf /var/lib/workvm-browser /run/workvm-browser.sock"
)
"""The smoke test of a new work image: the toolchains, the job helper and the platform browser (KOR-44); the browser
the selftest started is stopped and its profile removed, so no clone starts with a used profile."""
CHUNK = 1 << 20
_NAME_RE = re.compile(r"^[0-9a-z]+(?:[.-][0-9a-z]+)*$")

Announce = Callable[[str, dict[str, str | int | bool]], None]
Register = Callable[[str, datetime], None]


def bundle_members(images: Path) -> list[tuple[str, Path]]:
    """`(archive name, path)` of every file below `images/` (archive names `images/...`), sorted by name."""
    members: list[tuple[str, Path]] = []
    for directory, dirnames, filenames in os.walk(images):
        dirnames[:] = sorted(name for name in dirnames if name not in golden.BUNDLE_SKIP and not name.startswith("."))
        for filename in filenames:
            path = Path(directory) / filename
            if filename.startswith(".") or filename.endswith(".pyc") or path.is_symlink():
                continue
            members.append((f"images/{path.relative_to(images).as_posix()}", path))
    return sorted(members)


def write_bundle(images: Path, target: Path) -> None:
    """The bundle tar of `images/` (root-owned entries, mode 0755 for executables, else 0644)."""
    with tarfile.open(target, "w", format=tarfile.PAX_FORMAT) as archive:
        for name, path in bundle_members(images):
            data = path.read_bytes()
            info = tarfile.TarInfo(name)
            info.size, info.mtime = len(data), int(path.stat().st_mtime)
            info.mode = 0o755 if os.access(path, os.X_OK) else 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            archive.addfile(info, io.BytesIO(data))


def copy_sparse(source: Path, target: Path) -> None:
    """Copy `source` to `target` (created, never followed), leaving holes where `source` has zero blocks."""
    zero = bytes(CHUNK)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with source.open("rb") as reader, os.fdopen(descriptor, "wb") as writer:
        while chunk := reader.read(CHUNK):
            if chunk == zero[: len(chunk)]:
                writer.seek(len(chunk), os.SEEK_CUR)
            else:
                writer.write(chunk)
        writer.truncate()


@dataclass(frozen=True)
class Built:
    name: str
    seconds: float
    export: Path | None


@dataclass
class Builder:
    """One work image build with all collaborators injected (see the module docstring)."""

    lima: Lima
    template: Path
    scratch: Path
    register: Register
    export_dir: Path | None = None
    announce: Announce = field(default=lambda _name, _fields: None)
    log: Callable[[str], None] = field(default=lambda _text: None)
    clock: Callable[[], float] = time.monotonic
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    token_hex: Callable[[int], str] = secrets.token_hex
    copy_disk: Callable[[Path, Path], None] = copy_sparse

    @contextlib.contextmanager
    def _step(self, step: str, name: str) -> Iterator[None]:
        start = self.clock()
        try:
            yield
        except BaseException as exc:
            fields: dict[str, str | int | bool] = {"step": step, "name": name}
            if isinstance(exc, OSError) and isinstance(exc.filename, str):
                fields["path"] = exc.filename
            self.announce("workvm.image.failed", fields)
            if isinstance(exc, LimaError) and exc.detail:
                self.log(f"workvm image step {step}: limactl {exc.operation} exit {exc.returncode}\n{exc.detail}\n")
            if isinstance(exc, OSError):
                raise RunnerError("workvm_image_failed", step) from exc
            raise
        self.announce("workvm.image", {"step": step, "name": name, "seconds": f"{self.clock() - start:.3f}"})

    @property
    def images(self) -> Path:
        """The `images/` tree of the template (a configured template brings its own provisioning)."""
        return self.template.resolve().parent

    def _root(self, name: str, script: str, *args: str) -> None:
        self.lima.shell(name, ["sudo", "sh", "-c", script, "sh", *args])

    def build(self, suffix: str | None = None) -> Built:
        if suffix is not None and not _NAME_RE.fullmatch(suffix):
            raise RunnerError("usage", "name")
        if not (self.images / "workvm" / "provision.sh").is_file():
            raise RunnerError("workvm_image_source_missing")
        if self.export_dir is not None and not self.export_dir.is_dir():
            raise RunnerError("workvm_not_installed", "images")
        started = self.clock()
        self.lima.check_version()
        final = self.lima.name(f"wimg-{suffix or f'{self.now():%Y%m%d}-{self.token_hex(3)}'}")
        if self.lima.get(final) is not None:
            raise RunnerError("workvm_image_exists")
        build = self.lima.name(f"wimg-build-{self.token_hex(4)}")
        export = None if self.export_dir is None else self.export_dir / f"{final}.img"
        current = build
        try:
            self._provision(build)
            with self._step("stop", build):
                self.lima.stop(build)
                self._clear_socket_dir(build)
            with self._step("rename", build):
                self.lima.rename(build, final)
                current = final
            if export is not None:
                with self._step("export", final):
                    self._export(final, export)
            with self._step("register", final):
                self.register(final, self.now())
        except BaseException:
            with contextlib.suppress(LimaError):
                self.lima.delete(current)
            if export is not None:
                for leftover in (_part(export), export):
                    with contextlib.suppress(OSError):
                        leftover.unlink(missing_ok=True)
            raise
        return Built(name=final, seconds=round(self.clock() - started, 3), export=export)

    def _provision(self, name: str) -> None:
        with self._step("create", name):
            self.lima.create(name, str(self.template))
        with self._step("start", name):
            self.lima.start(name)
        with self._step("bundle", name):
            bundle = self.scratch / f"workvm-bundle-{self.token_hex(4)}.tar"
            try:
                write_bundle(self.images, bundle)
                self.lima.copy_to(str(bundle), name, GUEST_BUNDLE)
            finally:
                bundle.unlink(missing_ok=True)
            self._root(
                name,
                'rm -rf "$1" && mkdir -m 0700 "$1" && tar -xf "$2" -C "$1" --no-same-owner && rm -f "$2"',
                GUEST_BUNDLE_DIR,
                GUEST_BUNDLE,
            )
        with self._step("provision", name):
            self.lima.shell(name, ["sudo", "bash", f"{GUEST_BUNDLE_DIR}/{PROVISION}"], timeout=PROVISION_TIMEOUT_S)
        with self._step("smoke", name):
            self.lima.shell(name, ["sudo", "sh", "-c", SMOKE_SCRIPT], timeout=SMOKE_TIMEOUT_S)
        with self._step("seal", name):
            self._root(name, 'rm -rf "$1" && sync', GUEST_BUNDLE_DIR)

    def _export(self, name: str, export: Path) -> None:
        """Copy the stopped instance's writable disk to `export` (0644, through a hidden partial file)."""
        instance = self.lima.get(name)
        if instance is None:
            raise RunnerError("workvm_image_failed", "export")
        part = _part(export)
        part.unlink(missing_ok=True)
        self.copy_disk(Path(disk_path(instance.dir)), part)
        part.chmod(0o644)
        part.replace(export)

    def _clear_socket_dir(self, name: str) -> None:
        """Remove the empty mode-000 `<instance dir>/sock` Lima leaves behind (see `golden.Builder`)."""
        instance = self.lima.get(name)
        if instance is None:
            raise RunnerError("workvm_image_failed", "stop")
        directory = Path(instance.dir) / "sock"
        if directory.is_dir() and not directory.is_symlink():
            directory.chmod(0o700)
            shutil.rmtree(directory)


def _part(export: Path) -> Path:
    return export.with_name(f".{export.name}.part")


def register_ready(dsn: str, name: str, at: datetime) -> None:
    """Record `name` as a `ready` work image in the runner store."""

    async def main() -> None:
        db = await open_pool(dsn, min_size=1, max_size=1)
        try:
            await records.set_image(db, name, "ready", at)
        finally:
            await db.close()

    asyncio.run(main())


@dataclass(frozen=True)
class Image:
    name: str
    state: str
    built_at: datetime


def list_images(dsn: str) -> list[Image]:
    """The registered work images, oldest first."""
    with golden.connect(dsn) as conn:
        rows = conn.execute("SELECT name, state, built_at FROM runner.workvm_image ORDER BY built_at, name").fetchall()
    return [Image(name=row["name"], state=row["state"], built_at=row["built_at"]) for row in rows]
