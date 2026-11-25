"""Test kit of the work VM manager: fake Lima, fake job helper, fake guest (docs/platform/spec/13-work-vm.md)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import tarfile
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent_runner.sessions.records import LocalSession
from agent_runner.store.pool import open_pool
from agent_runner.vm.lima import Instance, LimaError
from agent_runner.workvm import browser_helper, jobs, sync
from agent_runner.workvm.jobs import ShellResult
from agent_runner.workvm.manager import Deps, Settings, WorkVms
from agent_runner.workvm.records import WorkVm

TASK = "0193000a-0000-7000-8000-0000000000a1"
TASK_B = "0193000a-0000-7000-8000-0000000000b1"
SESSION = "0193000a-0000-7000-8000-0000000000a2"
CHILD = "0193000a-0000-7000-8000-0000000000a3"
ORG = "0193000a-0000-7000-8000-0000000000a4"
WG = "0193000a-0000-7000-8000-0000000000a5"
OWNER = "0193000a-0000-7000-8000-0000000000a6"
VM_ID = "vm-0000000a"
START = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)


def session(
    session_id: str = SESSION,
    *,
    task_id: str = TASK,
    owner: str | None = OWNER,
    vm_id: str = VM_ID,
    parent_id: str | None = None,
) -> LocalSession:
    return LocalSession(
        id=session_id,
        task_id=task_id,
        org_id=ORG,
        wg_id=WG,
        vm_id=vm_id,
        parent_id=parent_id,
        depth=0 if parent_id is None else 1,
        state="working",
        claude_session_id=None,
        model_access=None,
        model_id=None,
        owner_id=owner,
        created_at=START,
        ready_at=START,
        ended_at=None,
    )


class FakeLima:
    """In-memory `Lima` of one slot; `fail` names operations that raise `LimaError`."""

    def __init__(self, world: World, slot: int) -> None:
        self.world = world
        self.slot = slot
        self.prefix = "wp-t-"

    def name(self, suffix: str) -> str:
        return self.prefix + suffix

    def _check(self, operation: str) -> None:
        self.world.calls.append((operation, self.slot))
        if operation in self.world.fail:
            raise LimaError("lima_failed", operation, returncode=1, detail=self.world.lima_stderr)

    def clone(self, source: str, name: str, *, sets: Sequence[str] = ()) -> None:
        self._check("clone")
        self.world.sets[name] = list(sets)
        self.world.instances[name] = "Stopped"

    def start(self, name: str, **_: Any) -> None:
        self._check("start")
        self.world.instances[name] = "Running"

    def stop(self, name: str) -> None:
        self._check("stop")
        self.world.instances[name] = "Stopped"

    def delete(self, name: str, *, missing_ok: bool = True) -> bool:
        self._check("delete")
        return self.world.instances.pop(name, None) is not None

    def get(self, name: str) -> Instance | None:
        status = self.world.instances.get(name)
        if status is None:
            return None
        return Instance(name=name, status=status, dir="/x", cpus=4, memory_bytes=0, disk_bytes=0, raw={})

    def list(self) -> list[Instance]:
        return [instance for name in list(self.world.instances) if (instance := self.get(name)) is not None]


@dataclass
class Job:
    command: str
    output: bytes = b""
    state: str = "running"
    exit_code: int | None = None


JPEG = b"\xff\xd8\xff\xe0fake-jpeg"
PAGE_URL = "https://example.test/"


def browser_answer(**changes: Any) -> dict[str, Any]:
    """A valid `workvm-browser call` answer (section 13.3) with `changes`."""
    answer: dict[str, Any] = {
        "ok": True,
        "error": None,
        "notice": None,
        "url": PAGE_URL,
        "title": "Example",
        "tab": 0,
        "tabs": [{"index": 0, "url": PAGE_URL, "title": "Example", "active": True}],
        "snapshot": '- heading "Example" [ref=e1]',
        "masked": False,
        "result": None,
        "image": {"width": 960, "height": 600, "jpeg": base64.b64encode(JPEG).decode()},
        "file": None,
    }
    answer.update(changes)
    return answer


BrowserScript = dict[str, Any] | ShellResult | Callable[[dict[str, Any]], dict[str, Any] | ShellResult]


class FakeBrowser:
    """`workvm-browser` of every fake VM: `call` answers from `script` (else `browser_answer()`), `selftest` from
    `probes` (else ok); a VM in `World.dead` answers nothing; the upload copy (`/bin/sh -c ...`) is recorded."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.script: list[BrowserScript] = []
        self.probes: list[ShellResult] = []
        self.uploads: list[tuple[str, dict[str, bytes]]] = []
        self.upload_result = ShellResult(0, b"", False)

    async def __call__(self, vm: WorkVm, argv: Sequence[str], *, stdin: bytes | Path | None) -> ShellResult:
        if vm.id in self.world.dead:
            return ShellResult(255, b"", False)
        if argv[2] == "/bin/sh":
            assert isinstance(stdin, Path)
            with tarfile.open(stdin) as archive:
                files = {}
                for member in archive.getmembers():
                    reader = archive.extractfile(member)
                    assert reader is not None
                    files[member.name] = reader.read()
            self.uploads.append((argv[-1], files))
            return self.upload_result
        command = argv[3]
        self.world.shell_calls.append((vm.id, "browser." + command))
        if command == "selftest":
            return self.probes.pop(0) if self.probes else ShellResult(0, b'{"ok": true}\n', False)
        assert command == "call"
        assert isinstance(stdin, bytes)
        request = json.loads(stdin)
        self.requests.append((vm.id, request))
        step: BrowserScript = self.script.pop(0) if self.script else browser_answer()
        if callable(step):
            step = step(request)
        if isinstance(step, ShellResult):
            return step
        return ShellResult(0, json.dumps(step).encode() + b"\n", False)


class FakeHelper:
    """`workvm-job` of every fake VM (one job table for the whole world)."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.jobs: dict[str, Job] = {}
        self.applied: list[tuple[str, bytes]] = []
        self.pack: bytes = b""

    async def __call__(  # noqa: PLR0911 - one branch per helper command
        self,
        vm: WorkVm,
        argv: Sequence[str],
        *,
        stdin: bytes | Path | None,
        timeout: float,
        limit: int,
        out: Path | None = None,
    ) -> ShellResult:
        if list(argv[:3]) in (["sudo", "-n", browser_helper.HELPER], ["sudo", "-n", "/bin/sh"]):
            return await self.world.browser(vm, argv, stdin=stdin)
        assert list(argv[:3]) == ["sudo", "-n", jobs.HELPER]
        command, args = argv[3], list(argv[4:])
        self.world.shell_calls.append((vm.id, command))
        if command in self.world.helper_fail:
            return self.world.helper_fail[command]
        if command == "list":
            listed = [
                {
                    "job_id": job_id,
                    "command": job.command,
                    "state": job.state,
                    "exit_code": job.exit_code,
                    "started_at": 1700000000000,
                    "duration_ms": 5,
                }
                for job_id, job in self.jobs.items()
            ]
            return ShellResult(0, json.dumps(listed).encode(), False)
        if command == "start":
            assert isinstance(stdin, bytes)
            spec = json.loads(stdin)
            job = Job(command=spec["command"])
            self.world.started.append(spec | {"cwd": args[1], "timeout": args[2]})
            script = self.world.scripts.get(spec["command"])
            if script is not None:
                job.output, job.state, job.exit_code = script
            self.jobs[args[0]] = job
            return ShellResult(0, b"", False)
        if command == "wait":
            job_id, offset, size = args[0], int(args[1]), int(args[2])
            found = self.jobs.get(job_id)
            if found is None:
                return ShellResult(0, b'{"error": "not_found"}\n', False)
            start = max(0, len(found.output) - size) if offset < 0 else offset
            body = found.output[start : start + size]
            header = {
                "state": found.state,
                "exit_code": found.exit_code,
                "output_bytes": len(found.output),
                "output_start": start,
                "body_bytes": len(body),
                "duration_ms": 7,
            }
            return ShellResult(0, json.dumps(header).encode() + b"\n" + body, False)
        if command == "kill":
            job = self.jobs[args[0]]
            job.state, job.exit_code = "killed", 143
            return ShellResult(0, b"{}", False)
        if command == "apply":
            assert isinstance(stdin, Path)
            self.applied.append((args[0], stdin.read_bytes()))
            return ShellResult(0, b'{"files": 1, "bytes": 1, "deleted": 0}', False)
        assert command in ("pack", "packfile")
        assert out is not None
        if command == "packfile":
            self.world.packed.append(args[0])
            out.write_bytes(pack_tar({args[1]: b"\x89PNG"}))
        else:
            out.write_bytes(self.pack)
        return ShellResult(0, b"", False)


def export_tar(files: Mapping[str, bytes]) -> bytes:
    """A guest export (`files/export`): the manifest first, then the files."""
    entries = [{"path": p, "size": len(d), "sha256": hashlib.sha256(d).hexdigest()} for p, d in sorted(files.items())]
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        manifest = json.dumps({"files": entries, "skipped": []}, separators=(",", ":"), sort_keys=True).encode()
        info = tarfile.TarInfo(sync.EXPORT_MANIFEST)
        info.size = len(manifest)
        archive.addfile(info, io.BytesIO(manifest))
        for path, data in sorted(files.items()):
            info = tarfile.TarInfo(path)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def pack_tar(files: Mapping[str, bytes], excluded: Sequence[str] = ()) -> bytes:
    """A `workvm-job pack` answer: the pack manifest first, then the files."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        manifest = json.dumps({"files": sorted(files), "excluded": list(excluded)}).encode()
        info = tarfile.TarInfo(sync.PACK_MANIFEST)
        info.size = len(manifest)
        archive.addfile(info, io.BytesIO(manifest))
        for path, data in sorted(files.items()):
            info = tarfile.TarInfo(path)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class FakeGuest:
    def __init__(self, world: World) -> None:
        self.world = world

    async def __aenter__(self) -> FakeGuest:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def export_files(self, session_id: str, directory: str, write: Callable[[bytes], object]) -> str:
        self.world.exports.append(directory)
        if self.world.export_error is not None:
            raise self.world.export_error
        files = (
            self.world.workspace
            if directory == "."
            else {
                path[len(directory) + 1 :]: data
                for path, data in self.world.workspace.items()
                if path.startswith(directory + "/")
            }
        )
        write(export_tar(files))
        return "sha"

    async def import_files(
        self, session_id: str, directory: str, data: Any, *, replace: bool, size: int
    ) -> dict[str, int]:
        if self.world.import_error is not None:
            raise self.world.import_error
        chunks = [chunk async for chunk in data]
        self.world.imports.append((directory, b"".join(chunks), replace))
        return {"files": 0, "bytes": 0}


@dataclass
class World:
    dsn: str
    tmp: Path
    instances: dict[str, str] = field(default_factory=dict)
    sets: dict[str, list[str]] = field(default_factory=dict)
    calls: list[tuple[str, int]] = field(default_factory=list)
    fail: set[str] = field(default_factory=set)
    lima_stderr: str = ""
    """Lima's stderr tail of a failing operation (`LimaError.detail`)."""
    shell_calls: list[tuple[str, str]] = field(default_factory=list)
    helper_fail: dict[str, ShellResult] = field(default_factory=dict)
    started: list[dict[str, Any]] = field(default_factory=list)
    scripts: dict[str, tuple[bytes, str, int | None]] = field(default_factory=dict)
    workspace: dict[str, bytes] = field(default_factory=dict)
    exports: list[str] = field(default_factory=list)
    imports: list[tuple[str, bytes, bool]] = field(default_factory=list)
    export_error: Exception | None = None
    import_error: Exception | None = None
    logs: list[str] = field(default_factory=list)
    events: list[tuple[Any, str, dict[str, Any]]] = field(default_factory=list)
    emit_error: Exception | None = None
    now: datetime = START
    boundary: bool = True
    free_mib: int = 1_000_000
    image: str | None = "wp-t-wimg-1"
    settings: Settings = field(
        default_factory=lambda: Settings(
            slots=2,
            max_size="large",
            idle_seconds=600,
            queue_seconds=10,
            max_per_task=4,
            max_per_org=16,
            max_per_owner=8,
            ssh_port_base=47200,
            test_mode=True,
        )
    )
    sleeps: list[float] = field(default_factory=list)
    on_sleep: Callable[[World], None] | None = None
    exhausted: set[str] = field(default_factory=set)
    """Organizations whose credit is used up (`Deps.exhausted`)."""
    capture: Any = None
    """`Deps.capture` (None: long job output is not kept as a log)."""
    dead: set[str] = field(default_factory=set)
    """Work VMs that answer no shell call (a killed VM)."""
    packed: list[str] = field(default_factory=list)
    """VM paths `workvm-job packfile` brought out of a VM."""
    helper: FakeHelper = field(init=False)
    browser: FakeBrowser = field(init=False)

    def __post_init__(self) -> None:
        self.helper = FakeHelper(self)
        self.browser = FakeBrowser(self)

    def guest(self) -> FakeGuest:
        return FakeGuest(self)

    def run[T](self, body: Callable[[WorkVms], Coroutine[Any, Any, T]]) -> T:
        async def main() -> T:
            db = await open_pool(self.dsn)
            try:
                if self.image is not None:
                    async with db.connection() as conn:
                        await conn.execute(
                            "INSERT INTO runner.workvm_image (name, state, built_at) VALUES (%s, 'ready', %s)"
                            " ON CONFLICT DO NOTHING",
                            (self.image, START),
                        )
                return await body(WorkVms(self.deps(db)))
            finally:
                await db.close()

        return asyncio.run(main())

    def deps(self, db: Any) -> Deps:
        world = self

        async def base_for(slot: int, image: str) -> str:
            return image

        async def boundary_ok() -> bool:
            return world.boundary

        async def free_memory_mib() -> int:
            return world.free_mib

        async def sleep(seconds: float) -> None:
            world.sleeps.append(seconds)
            world.now += timedelta(seconds=seconds)
            if world.on_sleep is not None:
                world.on_sleep(world)

        def shell_for(vm: WorkVm) -> Any:
            async def call(argv: Sequence[str], **kwargs: Any) -> ShellResult:
                return await world.helper(vm, argv, **kwargs)

            return call

        async def emit(identity: Any, kind: str, body: Mapping[str, Any]) -> None:
            if world.emit_error is not None:
                raise world.emit_error
            world.events.append((identity, kind, dict(body)))

        return Deps(
            emit=emit,
            db=db,
            settings=self.settings,
            now=lambda: world.now,
            lima_for=lambda slot: FakeLima(world, slot),  # type: ignore[arg-type,return-value]
            shell_for=shell_for,
            base_for=base_for,
            boundary_ok=boundary_ok,
            free_memory_mib=free_memory_mib,
            log=self.logs.append,
            sleep=sleep,
            scratch=self.tmp,
            exhausted=lambda org_id: org_id in world.exhausted,
            capture=world.capture,
        )
