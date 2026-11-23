"""U-RUN-metrics sampler and task (08-monitoring.md section 2, 05-runner.md section 14): fake Lima VMs with real guest
control processes answering `GET /v1/metrics` (the guest collector reads the recorded Linux files of the guest suite),
a real store, recorded host probe output; both sinks written, sampling independent of the hub."""

from __future__ import annotations

import asyncio
import io
import os
import stat
import time
import uuid
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent_guest import metrics as guest_metrics
from agent_guest.schema import SchemaSet
from agent_guest.state import Session
from agent_runner import registry
from agent_runner.metrics import sampler as sampler_module
from agent_runner.metrics import session as session_module
from agent_runner.metrics import tasks
from agent_runner.metrics.sampler import Sampler
from agent_runner.sync import outbox as sync_outbox
from agent_runner.sync.runtime import ATTRIBUTE, SyncRuntime
from agent_runner.vm import tasks as vm_tasks
from agent_runner.vm.lifecycle import VmManager
from agent_runner.vm.lima import LimaError
from agent_runner.vm.records import VmRow

from ..vm.guests import NOW, ORG, WG_A, WG_B, Harness
from .conftest import GUEST_FIXTURES, FakeRun, fixture, tools

REPO = Path(__file__).parents[4]
DEVICE_ID = "01930014-0000-7000-8000-000000000014"
WORKING = "01a1ee56-5e00-7003-8f1b-340691d99f03"  # has a cgroup in the guest fixtures
CHILD = "01a1ee56-5e00-7003-8f1b-340691d99f05"  # subagent session of WORKING (R-13.4)
RECENT = "01a1ee56-5e00-7003-8f1b-340691d99f06"  # stopped 5 min ago: still exported
OLD = "01a1ee56-5e00-7003-8f1b-340691d99f07"  # stopped 20 min ago: gone
FOREIGN = "01a1ee56-5e00-7003-8f1b-340691d99f08"  # reported by the guest, unknown to the store
TASK = "01930003-0000-7000-8000-000000000001"
OWNER = "01930004-0000-7000-8000-000000000001"
WALL = NOW.timestamp()
STORAGE_KEY = bytes(range(32))


class FakeNft:
    def __call__(self, argv: list[str], **kwargs: Any) -> Any:
        import subprocess  # noqa: PLC0415

        return subprocess.CompletedProcess(argv, 1, "", "nft: not here")


def guest_sources() -> guest_metrics.Sources:
    return guest_metrics.Sources(
        proc_root=str(GUEST_FIXTURES / "proc"),
        cgroup_root=str(GUEST_FIXTURES / "cgroup"),
        statvfs=lambda _path: os.statvfs_result((4096, 4096, 1000, 250, 200, 100, 50, 50, 0, 255)),
        clock_ticks=100,
        run=FakeNft(),
    )


def guest_session(session_id: str, state: str = "working") -> Session:
    return Session(session_id, -1, session_id.replace("-", "") * 2, state, f"/srv/sessions/{session_id}/work", None,
                   "2026-11-21T10:00:00.000Z")  # fmt: skip


def install_guest(harness: Harness, row: VmRow, *sessions: Session) -> None:
    service = harness.fake.farm.services[row.lima_name]  # type: ignore[union-attr]
    assert service.context is not None
    guest_metrics.install(service.context, guest_metrics.Collector(guest_sources()))
    for item in sessions:
        service.context.state.save_session(item)


def add_session(harness: Harness, session_id: str, vm_id: str | None, state: str, *, ended_minutes: int | None = None,
                parent: str | None = None) -> None:  # fmt: skip
    ended = None if ended_minutes is None else NOW - timedelta(minutes=ended_minutes)
    harness.sql(
        "INSERT INTO runner.session (id, task_id, org_id, wg_id, vm_id, parent_id, depth, state, credential_owner,"
        " created_at, ended_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (session_id, TASK, ORG, WG_A, vm_id, parent, 1 if parent else 0, state, OWNER, NOW - timedelta(minutes=30),
         ended),
    )  # fmt: skip


class FakePusher:
    def __init__(self) -> None:
        self.notified = 0

    def notify(self) -> None:
        self.notified += 1


def context_for(harness: Harness, manager: VmManager, textfile_dir: Path | None, *, enrolled: bool = True) -> Any:
    ctx = SimpleNamespace(
        config=SimpleNamespace(textfile_dir=textfile_dir),
        pool=manager.env.db,
        identity=SimpleNamespace(device=SimpleNamespace(device_id=DEVICE_ID)) if enrolled else None,
        host=SimpleNamespace(env={"LIMA_HOME": "/lima"}, clock=harness.clock, sleep=harness.clock.sleep),
        log=io.StringIO(),
        stopping=asyncio.Event(),
    )
    vars(ctx)[vm_tasks.MANAGER_KEY] = manager
    if enrolled:
        ws = SimpleNamespace(connected=True)
        setattr(ctx, ATTRIBUTE, SyncRuntime(client=None, pusher=FakePusher(), ws=ws, storage_key=STORAGE_KEY,  # type: ignore[arg-type]
                                            wall=time.time))  # fmt: skip
    return ctx


def host_run(*rows_dirs: str) -> FakeRun:
    run = FakeRun()
    run.answers["lsof"] = (
        0,
        "".join(fixture("lsof.txt").replace("{VZ}", "92995").replace("{LIMA}", directory) for directory in rows_dirs),
    )
    run.answers["ps"] = (0, fixture("ps.txt"))
    return run


async def outbox_items(pool: Any) -> list[dict[str, Any]]:
    async with pool.connection() as conn:
        stored = await sync_outbox.load_batch(conn, STORAGE_KEY, 0, 100)
    return [item.body for item in stored if item.item_kind == "metrics"]


def test_the_metrics_task_is_registered() -> None:
    assert "metrics" in [spec.name for spec in registry.tasks()]


def check_textfile(path: Path, row: VmRow) -> None:
    text = path.read_text(encoding="utf-8")
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert f'agent_runner_vm_probe_ok{{vm="{row.id}",wg="{WG_A}"}} 1' in text
    assert f'agent_runner_session_oom_kills_total{{vm="{row.id}",session="{WORKING}"}} 1' in text
    assert f'agent_runner_session_runtime_seconds{{vm="{row.id}",session="{CHILD}"}}' in text
    assert f'session="{RECENT}"' in text
    assert OLD not in text
    assert FOREIGN not in text
    assert "agent_runner_device_hub_connected 1" in text
    assert "agent_runner_device_outbox_held 3\n" in text
    assert "agent_runner_device_outbox_held_scopes 2\n" in text


def check_outbox(items: list[dict[str, Any]], row: VmRow) -> None:
    schemas = SchemaSet(str(REPO / "contracts"))
    assert len(items) == 2
    for item in items:
        assert schemas.errors("outbox/metrics.json", item) == []
    kinds = [(sample["entity_kind"], sample["entity_id"]) for sample in items[0]["samples"]]
    assert ("device", DEVICE_ID) in kinds
    assert ("vm", row.id) in kinds
    assert ("session", RECENT) in kinds  # the final values once
    assert ("session", RECENT) not in [(s["entity_kind"], s["entity_id"]) for s in items[1]["samples"]]
    assert ("session", WORKING) in [(s["entity_kind"], s["entity_id"]) for s in items[1]["samples"]]


def test_sample_writes_the_textfile_and_the_outbox(harness: Harness, tmp_path: Path) -> None:
    harness.add_golden()
    textfile_dir = tmp_path / "textfile"
    textfile_dir.mkdir()

    async def body(manager: VmManager) -> tuple[Any, Any, Any, list[dict[str, Any]], Any]:
        row = await manager.ensure_vm(ORG, WG_A)
        install_guest(harness, row, guest_session(WORKING), guest_session(CHILD, "idle"), guest_session(FOREIGN))
        stopped_row = await manager.ensure_vm(ORG, WG_B)
        await manager.env.set_state(stopped_row, "stopped")
        add_session(harness, WORKING, row.id, "working")
        add_session(harness, CHILD, row.id, "idle", parent=WORKING)
        add_session(harness, RECENT, row.id, "stopped", ended_minutes=5)
        add_session(harness, OLD, row.id, "stopped", ended_minutes=20)
        add_session(harness, "01a1ee56-5e00-7003-8f1b-340691d99f09", None, "requested")
        # two scope queues (KOR-10, AUD-02 NB-09): items that wait outside the numbering are visible as metrics
        async with manager.env.db.connection() as conn:
            for scope_id in (WG_A, WG_A, WG_B):
                await conn.execute(
                    "INSERT INTO runner.outbox_held (scope, scope_id, item_kind, event_id, body_ct, created_at)"
                    " VALUES ('wg', %s, 'event', %s, '\\x00', now())",
                    (scope_id, str(uuid.uuid4())),
                )
        directory = harness.fake.instance_dir(row.lima_name)
        Path(directory, "ha.pid").write_text("92989\n", encoding="utf-8")
        Path(directory, "disk").write_bytes(b"\0" * 4096)
        ctx = context_for(harness, manager, textfile_dir)
        sampler = Sampler(ctx, tools(host_run(directory)), wall=lambda: WALL)
        first = await sampler.tick()
        harness.clock.now += 15
        second = await sampler.tick()
        return row, first, second, await outbox_items(manager.env.db), ctx

    row, first, second, items, ctx = harness.run(body)
    vm_entry = next(entry for entry in second.vms if entry.vm_id == row.id)
    assert vm_entry.probe_ok is True
    assert vm_entry.cpu_ratio == 0.0  # guest delta of two answers (the fixture does not move)
    assert vm_entry.memory_bytes == (1383216 + 143376) * 1024
    assert vm_entry.sessions == {"working": 1, "idle": 1, "stopped": 1}
    assert vm_entry.disk_used_bytes is not None
    assert vm_entry.net_rx_bytes == 2940810
    assert [entry.probe_ok for entry in second.vms if entry.vm_id != row.id] == [None]  # the stopped VM
    assert [entry.session_id for entry in second.sessions] == [WORKING, CHILD, RECENT]
    by_id = {entry.session_id: entry for entry in second.sessions}
    assert by_id[WORKING].values is not None
    assert by_id[WORKING].values["oom_kills"] == 1
    assert by_id[WORKING].cpu_ratio == 0.0
    assert first.sessions[0].cpu_ratio is None
    assert by_id[RECENT].values is None
    assert by_id[RECENT].runtime_seconds == 25 * 60
    assert second.device.hub_connected is True
    assert second.device.values.ok is True
    assert (second.device.held, second.device.held_scopes) == (3, 2)

    check_textfile(textfile_dir / "agent_runner.prom", row)
    check_outbox(items, row)
    assert getattr(ctx, ATTRIBUTE).pusher.notified == 2


def test_sampling_goes_on_without_hub_textfile_or_lima(harness: Harness, tmp_path: Path) -> None:
    harness.add_golden()
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    readonly.chmod(0o500)

    async def body(manager: VmManager) -> tuple[Any, Any, list[dict[str, Any]], str]:
        row = await manager.ensure_vm(ORG, WG_A)
        add_session(harness, WORKING, row.id, "working")
        ctx = context_for(harness, manager, readonly)
        getattr(ctx, ATTRIBUTE).ws.connected = False
        failing = FakeRun({})
        sampler = Sampler(ctx, tools(failing), wall=lambda: WALL)
        first = await sampler.tick()  # guest has no collector installed: /proc missing on this host -> 503

        def broken_list() -> list[Any]:
            raise LimaError("lima_timeout", "list")

        manager.env.lima.list = broken_list  # type: ignore[method-assign]
        second = await sampler.tick()
        return first, second, await outbox_items(manager.env.db), ctx.log.getvalue()

    try:
        first, second, items, log = harness.run(body)
    finally:
        readonly.chmod(0o700)
    assert first.vms[0].probe_ok is False
    assert first.device.values.ok is False
    assert first.device.hub_connected is False
    assert second.vms[0].probe_ok is False
    assert second.vms[0].disk_limit_bytes is None
    assert "runner.metrics textfile_failed error=PermissionError" in log
    assert "runner.metrics lima_list_failed code=lima_timeout" in log
    assert items == []  # held in runner.metric_pending while the WebSocket is closed
    pending = harness.sql("SELECT jsonb_array_length(samples) AS n FROM runner.metric_pending ORDER BY id")
    assert [row["n"] for row in pending] == [3, 3]  # device, VM and session of each round


def test_not_enrolled_device_writes_only_the_textfile(harness: Harness, tmp_path: Path) -> None:
    harness.add_golden()

    async def body(manager: VmManager) -> tuple[Any, Any]:
        await manager.ensure_vm(ORG, WG_A)
        ctx = context_for(harness, manager, tmp_path, enrolled=False)
        snap = await Sampler(ctx, tools(FakeRun({})), wall=lambda: WALL).tick()
        return snap, await outbox_items(manager.env.db)

    snap, items = harness.run(body)
    assert snap.device.hub_connected is False
    assert items == []
    assert (tmp_path / "agent_runner.prom").exists()


def test_outbox_failure_is_logged(harness: Harness, tmp_path: Path) -> None:
    harness.add_golden()

    async def body(manager: VmManager) -> str:
        await manager.ensure_vm(ORG, WG_A)
        ctx = context_for(harness, manager, None)
        runtime = getattr(ctx, ATTRIBUTE)
        setattr(ctx, ATTRIBUTE, replace(runtime, storage_key=b"short"))
        await Sampler(ctx, tools(FakeRun({})), wall=lambda: WALL).tick()
        return str(ctx.log.getvalue())

    assert "runner.metrics outbox_failed error=" in harness.run(body)


def test_task_loop_samples_every_15_s_and_survives_failures(fake_clock: Any) -> None:
    class Flaky:
        def __init__(self, stopping: asyncio.Event) -> None:
            self.stopping = stopping
            self.rounds = 0
            self.lines: list[str] = []

        async def tick(self) -> None:
            self.rounds += 1
            fake_clock.now += 4.0
            if self.rounds == 2:
                raise RuntimeError("secret detail")
            if self.rounds == 3:
                self.stopping.set()

        def line(self, text: str) -> None:
            self.lines.append(text)

    ctx = SimpleNamespace(stopping=asyncio.Event(), host=SimpleNamespace(clock=fake_clock, sleep=fake_clock.sleep))
    flaky = Flaky(ctx.stopping)
    vars(ctx)[tasks.SAMPLER_KEY] = flaky
    asyncio.run(tasks.run(ctx))
    assert flaky.rounds == 3
    assert flaky.lines == ["sample_failed error=RuntimeError"]
    assert fake_clock.sleeps == [11.0, 11.0, 11.0]


def test_sampler_for_builds_once_with_options() -> None:
    options = tasks.MetricsOptions(wall=lambda: 5.0)
    ctx = SimpleNamespace(metrics_options=options)
    built = tasks.sampler_for(ctx)
    assert tasks.sampler_for(ctx) is built
    assert built.wall() == 5.0
    plain = tasks.sampler_for(SimpleNamespace())
    assert plain.wall is time.time
    assert isinstance(plain, sampler_module.Sampler)


def test_session_tracker_cpu_ratio_and_resets() -> None:
    from agent_runner.metrics.parsers import GuestMetrics, GuestSession  # noqa: PLC0415
    from agent_runner.sessions.records import LocalSession  # noqa: PLC0415

    row = LocalSession(WORKING, TASK, ORG, WG_A, "vm-0a1b2c3d", None, 0, "working", None, None, None, OWNER,
                       NOW - timedelta(minutes=1), None, None)  # fmt: skip

    def answer(usage: int) -> dict[str, GuestMetrics | None]:
        values: dict[str, int | None] = {"cpu_usage_usec": usage}
        session = GuestSession(WORKING, "working", values)
        return {"vm-0a1b2c3d": GuestMetrics({}, 0.0, (session,))}

    tracker = session_module.SessionTracker()
    assert tracker.entries([row], answer(1_000_000), NOW, 10.0)[0].cpu_ratio is None
    assert tracker.entries([row], answer(4_000_000), NOW, 12.0)[0].cpu_ratio == 1.5
    assert tracker.entries([row], answer(100), NOW, 14.0)[0].cpu_ratio is None  # counter reset
    assert tracker.entries([row], answer(200), NOW, 14.0)[0].cpu_ratio is None  # no time passed
    unreachable = tracker.entries([row], {"vm-0a1b2c3d": None}, NOW, 16.0)[0]
    assert (unreachable.reachable, unreachable.values) == (False, None)  # running and not reported: no stale values
    ended = replace(row, state="stopped", ended_at=NOW)
    final = tracker.entries([ended], {"vm-0a1b2c3d": None}, NOW, 16.0)[0]
    assert (final.values, final.cpu_ratio, final.runtime_seconds) == ({"cpu_usage_usec": 200}, None, 60)
    assert tracker.entries([], {}, NOW, 18.0) == []
    assert tracker.last == {}
    assert tracker.cpu == {}


def test_session_rows_retention(harness: Harness) -> None:
    async def body(manager: VmManager) -> list[str]:
        add_session(harness, WORKING, "vm-0a1b2c3d", "working")
        add_session(harness, RECENT, "vm-0a1b2c3d", "failed", ended_minutes=14)
        add_session(harness, OLD, "vm-0a1b2c3d", "failed", ended_minutes=16)
        add_session(harness, CHILD, "vm-0a1b2c3d", "stopped")  # ended without a time: not kept
        rows = await session_module.session_rows(manager.env.db, NOW)
        return [row.id for row in rows]

    assert harness.run(body) == [WORKING, RECENT]


def test_vm_uptime_falls_back_to_the_guest() -> None:
    from agent_runner.metrics.parsers import GuestMetrics  # noqa: PLC0415

    row = VmRow("vm-0a1b2c3d", "wp-test-arp-1", "g1", WG_A, ORG, "running", "x", 4, 8192, NOW, NOW, NOW)
    guest = GuestMetrics({"cpu_usage_usec": 5, "uptime_seconds": 1234}, 0.1, ())
    sampler = Sampler(SimpleNamespace(), tools(FakeRun({})))
    entry = sampler._vm_entry(row=row, instance=None, host=None, guest=guest, clock=1.0, states={})
    assert (entry.uptime_seconds, entry.cpu_ratio, entry.probe_ok) == (1234, None, True)
