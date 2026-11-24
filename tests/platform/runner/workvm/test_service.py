"""U-RUN-workvm service: production wiring of the work VMs (13-work-vm.md sections 6 and 7).

The daemon context is a fake (configuration with the `workvm_*` fields, host, log) and the session service is
replaced; `id` and the `sudo -n pfctl` reads of the boundary check are a fake `run`; the clone bases use a fake
`limactl` run behind a real `Lima`. `free_memory_mib` reads real rows of a migrated runner store. Nothing runs sudo,
limactl or pfctl for real.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import subprocess
import sys
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import agent_runner.sessions.service as sessions_service
from agent_runner.store.migrate import migrate
from agent_runner.store.pool import open_pool
from agent_runner.vm import records as vm_records
from agent_runner.vm.lima import Lima, LimactlRun
from agent_runner.workvm import firewall, records, service
from agent_runner.workvm.jobs import WorkVmError
from agent_runner.workvm.manager import Identity, Settings, WorkVms
from agent_runner.workvm.records import WorkVm
from agent_runner.workvm.service import Bases, Boundary
from agent_runner.workvm.shell import SlotCommand, VmShell

ROOT = Path(__file__).resolve().parents[4]
WALL = 1_800_000_000.0
SLOTS = [(0, "agentwvm00", 47200), (1, "agentwvm01", 47201)]
SUDO = ("/usr/bin/sudo", "-n")


# -- run_command -------------------------------------------------------------------------------------------------


def test_run_command_runs_and_fails_closed(tmp_path: Path) -> None:
    done = service.run_command([sys.executable, "-c", "print('hi')"], 20.0)
    assert (done.returncode, done.stdout) == (0, "hi\n")
    missing = service.run_command([str(tmp_path / "nowhere")], 5.0)
    assert (missing.returncode, missing.stdout, missing.stderr) == (1, "", "")
    slow = service.run_command([sys.executable, "-c", "import time; time.sleep(30)"], 0.3)
    assert (slow.returncode, slow.args) == (1, [sys.executable, "-c", "import time; time.sleep(30)"])


# -- Boundary ----------------------------------------------------------------------------------------------------


Answers = dict[tuple[str, ...], tuple[int, str | None]]


class FakeRun:
    def __init__(self, answers: Mapping[tuple[str, ...], tuple[int, str | None]]) -> None:
        self.answers = dict(answers)
        self.calls: list[tuple[tuple[str, ...], float]] = []

    def __call__(self, argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        key = tuple(argv)
        self.calls.append((key, timeout))
        code, out = self.answers.get(key, (0, ""))
        return subprocess.CompletedProcess(list(argv), code, out, "")


def healthy(egress: str | None = "utun4") -> Answers:
    slots = [firewall.Slot(index, account, 601 + index, port) for index, account, port in SLOTS]
    rendered = firewall.render(slots, egress, service.BANDWIDTH_MBIT)
    table = "".join(f"   {entry}\n" for entry in [*firewall.BLOCKED_V4, *firewall.BLOCKED_V6])
    return {
        ("/usr/bin/id", "-u", "agentwvm00"): (0, "601\n"),
        ("/usr/bin/id", "-u", "agentwvm01"): (0, "602\n"),
        (*SUDO, "/sbin/pfctl", "-s", "info"): (0, "Status: Enabled for 0 days 01:02:03\n"),
        (*SUDO, "/sbin/pfctl", "-a", "com.apple/000.agent.workvm", "-sr"): (0, rendered),
        (*SUDO, "/sbin/pfctl", "-sr"): (0, 'anchor "com.apple/*" all\nanchor "com.cloudflare.warp_svc" all\n'),
        (*SUDO, "/sbin/pfctl", "-s", "Interfaces", "-v"): (0, "lo0\nen0\n"),
        (*SUDO, "/sbin/pfctl", "-a", "com.apple/000.agent.workvm", "-t", "workvm_blocked", "-T", "show"): (0, table),
        (*SUDO, "/sbin/pfctl", "-a", "com.apple", "-sA"): (0, "  com.apple/000.agent.workvm\n"),
    }


def boundary_config(**changes: Any) -> SimpleNamespace:
    values: dict[str, Any] = {"workvm_slots_self": False, "test_mode": False}
    values.update(changes)
    return SimpleNamespace(**values)


def check(answers: Answers, **changes: Any) -> tuple[bool, FakeRun, Boundary]:
    run = FakeRun(answers)
    boundary = Boundary(boundary_config(**changes), SLOTS, run)
    ok = asyncio.run(boundary())
    return ok, run, boundary


def test_boundary_holds_on_the_healthy_filter() -> None:
    ok, run, boundary = check(healthy())
    assert (ok, boundary.internet) == (True, True)
    assert [argv[:3] for argv, _ in run.calls][:3] == [
        ("/usr/bin/id", "-u", "agentwvm00"),
        ("/usr/bin/id", "-u", "agentwvm01"),
        (*SUDO, "/sbin/pfctl"),
    ]
    assert all(timeout > 0 for _, timeout in run.calls)


def test_boundary_holds_without_internet_while_the_client_is_down() -> None:
    ok, _, boundary = check(healthy(None))
    assert (ok, boundary.internet) == (True, False)


def test_boundary_is_cached_for_the_ttl() -> None:
    run = FakeRun(healthy())
    boundary = Boundary(boundary_config(), SLOTS, run)

    async def twice() -> list[bool]:
        first = await boundary()
        count = len(run.calls)
        run.answers[(*SUDO, "/sbin/pfctl", "-s", "info")] = (1, "")
        second = await boundary()
        assert len(run.calls) == count
        boundary.checked_at -= service.BOUNDARY_TTL
        third = await boundary()
        assert len(run.calls) > count
        assert boundary.internet is False
        return [first, second, third]

    assert asyncio.run(twice()) == [True, True, False]


@pytest.mark.parametrize(("test_mode", "expected"), [(True, True), (False, False), (None, False)])
def test_boundary_in_self_mode_needs_no_filter(test_mode: bool | None, expected: bool) -> None:
    ok, run, boundary = check({}, workvm_slots_self=True, test_mode=test_mode)
    assert (ok, boundary.internet) == (expected, expected)
    assert run.calls == []


@pytest.mark.parametrize(
    "changes",
    [
        {("/usr/bin/id", "-u", "agentwvm01"): (1, "")},
        {("/usr/bin/id", "-u", "agentwvm00"): (0, "no such user\n")},
        {(*SUDO, "/sbin/pfctl", "-s", "info"): (0, "Status: Disabled\n")},
        {(*SUDO, "/sbin/pfctl", "-s", "info"): (1, None)},
        {("/usr/bin/id", "-u", "agentwvm01"): (0, "0\n")},
    ],
)
def test_boundary_fails_closed(changes: Answers) -> None:
    ok, _, boundary = check({**healthy(), **changes})
    assert (ok, boundary.internet) == (False, False)


# -- templates and bases -----------------------------------------------------------------------------------------


def test_image_disk_and_default_template(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert service.image_disk("wimg-1") == Path("/var/agent-workvm/images/wimg-1.img")
    monkeypatch.setattr(service, "IMAGES_DIR", tmp_path)
    assert service.image_disk("wimg-1") == tmp_path / "wimg-1.img"
    assert service.image_disk("wimg-1", Path("/e")) == Path("/e/wimg-1.img")
    assert service.default_template() == ROOT / "images" / "workvm.yaml"
    assert service.default_template().is_file()


def test_base_template_replaces_the_image_block() -> None:
    template = (
        "vmType: vz\n"
        "images:\n"
        '- location: "https://example.invalid/a.img"\n'
        "  arch: aarch64\n"
        '  digest: "sha256:00"\n'
        "- location: other\n"
        "# comment\n"
        "cpus: 4\n"
    )
    assert service.base_template(template, Path("/d/w.img")) == (
        'vmType: vz\nimages:\n- location: "/d/w.img"\n  arch: aarch64\n# comment\ncpus: 4\n'
    )
    assert service.base_template("a: 1\n  b: 2\n- c\n", Path("/x")) == "a: 1\n  b: 2\n- c\n"
    assert service.base_template("images:\n- location: x\n", Path("/x")) == (
        'images:\n- location: "/x"\n  arch: aarch64\n'
    )


def test_base_template_of_the_real_work_template() -> None:
    text = service.base_template((ROOT / "images" / "workvm.yaml").read_text(encoding="utf-8"), Path("/i/w.img"))
    assert '- location: "/i/w.img"' in text
    assert "https://" not in text
    assert "digest:" not in text
    assert "\ncpus: 4\n" in text


@dataclass
class FakeLimactl:
    """`limactl list --json NAME` and `create` of one slot's Lima home."""

    instances: set[str] = field(default_factory=set)
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        if argv[:2] == ["list", "--json"]:
            name = argv[2]
            if name not in self.instances:
                return subprocess.CompletedProcess(list(argv), 1, "", f'level=fatal msg="No instance matching {name}"')
            line = f'{{"name": "{name}", "status": "Stopped", "dir": "/l/{name}"}}\n'
            return subprocess.CompletedProcess(list(argv), 0, line, "")
        if argv[0] == "create":
            self.instances.add(argv[2].removeprefix("--name="))
        return subprocess.CompletedProcess(list(argv), 0, "", "")


@dataclass(frozen=True)
class FakeCommand(SlotCommand):
    homes: Mapping[int, FakeLimactl] = field(default_factory=dict, compare=False)

    def sync_runner(self, slot: int) -> LimactlRun:
        return self.homes[slot]


def test_bases_in_self_mode_clone_the_image_itself(tmp_path: Path) -> None:
    bases = Bases(SlotCommand(()), "agent-", tmp_path / "missing.yaml", None)
    assert asyncio.run(bases(3, "agent-wimg-1")) == "agent-wimg-1"


def test_bases_with_slot_accounts_create_one_base_per_slot(tmp_path: Path) -> None:
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "agent-wimg-1.img").write_bytes(b"disk")
    template = tmp_path / "workvm.yaml"
    template.write_text('images:\n- location: "https://x"\n  arch: aarch64\ncpus: 4\n', encoding="utf-8")
    homes = {0: FakeLimactl(), 1: FakeLimactl()}
    bases = Bases(FakeCommand(("agentwvm00", "agentwvm01"), homes=homes), "agent-", template, tmp_path / "images")
    name = "agent-wbase-" + hashlib.sha256(b"agent-wimg-1").hexdigest()[:8]
    spec = tmp_path / "images" / "agent-wimg-1.yaml"

    assert asyncio.run(bases(0, "agent-wimg-1")) == name
    assert spec.read_text(encoding="utf-8") == (
        f'images:\n- location: "{tmp_path}/images/agent-wimg-1.img"\n  arch: aarch64\ncpus: 4\n'
    )
    assert spec.stat().st_mode & 0o777 == 0o644
    assert homes[0].calls == [["list", "--json", name], ["create", "-y", f"--name={name}", str(spec)]]

    # the base exists: nothing is created again
    assert asyncio.run(bases(0, "agent-wimg-1")) == name
    assert homes[0].calls[2:] == [["list", "--json", name]]

    # another slot creates its own base from the spec already written (kept as it is)
    spec.write_text("kept\n", encoding="utf-8")
    assert asyncio.run(bases(1, "agent-wimg-1")) == name
    assert homes[1].calls == [["list", "--json", name], ["create", "-y", f"--name={name}", str(spec)]]
    assert spec.read_text(encoding="utf-8") == "kept\n"


def test_bases_without_the_exported_disk_fail_with_its_path(tmp_path: Path) -> None:
    (tmp_path / "images").mkdir()
    home, lines = FakeLimactl(), list[str]()
    bases = Bases(FakeCommand(("agentwvm00",), homes={0: home}), "agent-", tmp_path / "w.yaml", tmp_path / "images")
    bases.log = lines.append
    with pytest.raises(WorkVmError) as caught:
        asyncio.run(bases(0, "agent-wimg-1"))
    assert (caught.value.code, caught.value.name) == ("workvm_image_missing", "agent-wimg-1")
    assert lines == [f"runner.workvm.base.failed slot=0 path={tmp_path}/images/agent-wimg-1.img"]
    assert [call[0] for call in home.calls] == ["list"]
    assert list((tmp_path / "images").iterdir()) == []


def test_bases_log_nothing_by_default(tmp_path: Path) -> None:
    bases = Bases(FakeCommand(("agentwvm00",), homes={0: FakeLimactl()}), "agent-", tmp_path / "w.yaml", tmp_path)
    with pytest.raises(WorkVmError):
        asyncio.run(bases(0, "agent-wimg-1"))


# -- workvms_for -------------------------------------------------------------------------------------------------


def config(home: Path, **changes: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "home": home,
        "memory_reserve_mib": 4096,
        "test_mode": False,
        "vm_prefix": "agent",
        "workvm_slots": 2,
        "workvm_slots_self": False,
        "workvm_max_size": "large",
        "workvm_idle_seconds": 14400,
        "workvm_pool_size": 1,
        "workvm_disk_quota_gib": 0,
        "workvm_queue_seconds": 900,
        "workvm_max_per_task": 4,
        "workvm_max_per_org": 16,
        "workvm_max_per_owner": 8,
        "workvm_ssh_port_base": 47200,
        "workvm_template": None,
    }
    values.update(changes)
    return SimpleNamespace(**values)


async def host_sleep(seconds: float) -> None:
    return None


def context(home: Path, **changes: Any) -> SimpleNamespace:
    return SimpleNamespace(config=config(home, **changes), host=SimpleNamespace(sleep=host_sleep), log=io.StringIO())


def fake_service(monkeypatch: pytest.MonkeyPatch, db: Any = None, found: bool = True) -> list[Any]:
    seen: list[Any] = []

    def service_for(ctx: Any) -> Any:
        seen.append(ctx)
        return SimpleNamespace(env=SimpleNamespace(db=db, wall=lambda: WALL)) if found else None

    monkeypatch.setattr(sessions_service, "service_for", service_for)
    return seen


def test_no_work_vms_without_slots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen = fake_service(monkeypatch)
    assert service.workvms_for(context(tmp_path, workvm_slots=0)) is None
    assert seen == []


def test_no_work_vms_before_enrollment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen = fake_service(monkeypatch, found=False)
    ctx = context(tmp_path)
    assert service.workvms_for(ctx) is None
    assert seen == [ctx]
    assert service.existing(ctx) is None


def test_an_installed_instance_is_reused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen = fake_service(monkeypatch)
    ctx = context(tmp_path)
    ctx._workvms = "not work vms"
    assert service.existing(ctx) is None
    built = service.workvms_for(ctx)
    assert isinstance(built, WorkVms)
    assert service.existing(ctx) is built
    assert service.workvms_for(ctx) is built
    assert len(seen) == 1
    other = WorkVms(built.deps)
    assert service.install(ctx, other) is other
    assert service.workvms_for(ctx) is other


def test_wiring_with_slot_accounts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db = object()
    fake_service(monkeypatch, db)
    ctx = context(tmp_path)
    workvms = service.workvms_for(ctx)
    assert workvms is not None
    deps = workvms.deps
    assert deps.db is db
    assert deps.settings == Settings(
        slots=2,
        max_size="large",
        idle_seconds=14400,
        queue_seconds=900,
        max_per_task=4,
        max_per_org=16,
        max_per_owner=8,
        ssh_port_base=47200,
        test_mode=False,
        pool_size=1,
    )
    assert deps.now() == datetime.fromtimestamp(WALL, UTC)
    assert deps.sleep is host_sleep
    assert deps.scratch == tmp_path / "tmp"

    assert isinstance(deps.base_for, Bases)
    command = deps.base_for.command
    assert command == SlotCommand(("agentwvm00", "agentwvm01"))
    assert (deps.base_for.prefix, deps.base_for.template) == ("agent", service.default_template())
    assert deps.base_for.images == service.IMAGES_DIR

    lima = deps.lima_for(1)
    assert isinstance(lima, Lima)
    assert lima.prefix == "agent"

    vm = workvm_row("wvm-0000000a", slot=1, state="running", memory_mib=8192)
    assert deps.shell_for(vm) == VmShell(command, 1, "agent-wvm-0000000a")

    assert isinstance(deps.boundary_ok, Boundary)
    assert deps.boundary_ok.config is ctx.config
    assert deps.boundary_ok.slots == SLOTS

    deps.log("runner.workvm started")
    deps.log("second")
    assert ctx.log.getvalue() == "runner.workvm started\nsecond\n"


def test_wiring_in_self_mode_with_a_template(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_service(monkeypatch)
    template = tmp_path / "workvm.yaml"
    ctx = context(tmp_path, workvm_slots_self=True, test_mode=True, workvm_slots=1, workvm_template=template)
    workvms = service.workvms_for(ctx)
    assert workvms is not None
    bases = workvms.deps.base_for
    assert isinstance(bases, Bases)
    assert bases.command == SlotCommand(())
    assert bases.template == template
    assert bases.images is None

    async def boundary() -> bool:
        return await workvms.deps.boundary_ok()

    assert asyncio.run(boundary()) is True
    assert workvms.deps.settings.test_mode is True


# -- emit ------------------------------------------------------------------------------------------------------


@dataclass
class FakeEnv:
    """The session service env of `emit`: a transaction yielding a marker connection, an outbox and a clock."""

    db: Any = None
    outbox: object = field(default_factory=object)
    conn: object = field(default_factory=object)
    entered: list[str] = field(default_factory=list)

    def wall(self) -> float:
        return WALL

    def at(self) -> str:
        return "2026-11-23T10:00:00.000Z"

    @contextlib.asynccontextmanager
    async def transaction(self) -> AsyncIterator[object]:
        self.entered.append("begin")
        yield self.conn
        self.entered.append("commit")


def test_emit_writes_a_session_event_in_a_transaction(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env = FakeEnv()
    monkeypatch.setattr(sessions_service, "service_for", lambda ctx: SimpleNamespace(env=env))
    written: list[tuple[Any, ...]] = []

    async def session_event(conn: Any, outbox: Any, local: Any, *, kind: str, body: Any, ts: str) -> None:
        assert env.entered == ["begin"]
        written.append((conn, outbox, local, kind, dict(body), ts))

    monkeypatch.setattr(service, "session_event", session_event)
    workvms = service.workvms_for(context(tmp_path))
    assert workvms is not None
    identity = Identity(
        org_id="0192f3a4-0000-7000-8000-0000000000aa",
        wg_id="0192f3a4-0000-7000-8000-0000000000bb",
        task_id="0192f3a4-0000-7000-8000-000000000001",
        session_id="0192f3a4-0000-7000-8000-000000000002",
    )

    async def emit() -> None:
        await workvms.deps.emit(identity, "workvm.started", {"workvm_id": "wvm-0000000a"})

    asyncio.run(emit())
    assert env.entered == ["begin", "commit"]
    [(conn, outbox, local, kind, body, ts)] = written
    assert (conn, outbox, kind, body, ts) == (
        env.conn,
        env.outbox,
        "workvm.started",
        {"workvm_id": "wvm-0000000a"},
        "2026-11-23T10:00:00.000Z",
    )
    assert (local.id, local.task_id, local.org_id, local.wg_id) == (
        identity.session_id,
        identity.task_id,
        identity.org_id,
        identity.wg_id,
    )
    assert (local.vm_id, local.parent_id, local.depth, local.state) == (None, None, 0, "working")
    assert local.created_at == datetime.fromtimestamp(0, UTC)


# -- free memory against the store -------------------------------------------------------------------------------


@pytest.fixture
def store_dsn(runner_dsn: str) -> str:
    result = migrate(runner_dsn)
    assert result.status == "applied"
    return runner_dsn


AT = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)


def workvm_row(workvm_id: str, *, slot: int, state: str, memory_mib: int) -> WorkVm:
    n = int(workvm_id[-8:], 16)
    return WorkVm(
        id=workvm_id,
        lima_name="agent-" + workvm_id,
        slot=slot,
        scope="task",
        task_id=f"0192f3a4-0000-7000-8000-{n:012x}",
        session_id=None,
        requested_by="0192f3a4-0000-7000-8000-0000000000cc",
        implicit=False,
        org_id="0192f3a4-0000-7000-8000-0000000000aa",
        wg_id="0192f3a4-0000-7000-8000-0000000000bb",
        owner_id=None,
        size="standard",
        cpus=4,
        memory_mib=memory_mib,
        disk_gib=50,
        image="agent-wimg-1",
        state=state,
        created_at=AT,
        last_used_at=AT,
        stopped_at=None,
    )


async def insert_workvm(db: Any, vm: WorkVm) -> None:
    await records.insert(db, vm)


def test_free_memory_counts_held_workgroup_vms_and_work_vms(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store_dsn: str
) -> None:
    monkeypatch.setattr(service, "physical_memory_mib", lambda: 65536)

    async def run() -> tuple[int, int]:
        db = await open_pool(store_dsn)
        try:
            fake_service(monkeypatch, db)
            workvms = service.workvms_for(context(tmp_path))
            assert workvms is not None
            empty = await workvms.deps.free_memory_mib()
            vms = [("pool", 8192), ("assigning", 512), ("running", 4096), ("stopping", 1024), ("stopped", 2048)]
            vms.append(("deleted", 999))
            for n, (state, memory) in enumerate(vms):
                await vm_records.insert(
                    db,
                    vm_id=f"vm-0000000{n}",
                    lima_name=f"agent-vm-0000000{n}",
                    golden_version="g1",
                    state=state,
                    token_sha256="0" * 64,
                    cpus=2,
                    memory_mib=memory,
                    created_at=AT,
                )
            work = [("running", 8192), ("stopped", 16384), ("deleting", 4096), ("deleted", 32768)]
            for n, (state, memory) in enumerate(work):
                await insert_workvm(db, workvm_row(f"wvm-0000000{n}", slot=n, state=state, memory_mib=memory))
            return empty, await workvms.deps.free_memory_mib()
        finally:
            await db.close()

    empty, used = asyncio.run(run())
    assert empty == 65536 - 4096
    assert used == 65536 - 4096 - (8192 + 512 + 4096 + 1024) - (8192 + 4096)
