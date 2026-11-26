"""U-RUN-pool daemon tasks `vm-pool` and `vm-idle`, the shared manager, the uninstall cleaner and the token files
(05-runner.md sections 1.1, 1.2, 3.2, 3.5; ADR-027 section 4)."""

from __future__ import annotations

import asyncio
import io
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_runner import registry
from agent_runner.errors import RunnerError
from agent_runner.store.pool import open_pool
from agent_runner.sync import extensions, outbox
from agent_runner.sync.runtime import ATTRIBUTE, SyncRuntime
from agent_runner.vm import heartbeat, records, tasks, uninstall
from agent_runner.vm.capacity import Usage
from agent_runner.vm.lifecycle import VmManager
from agent_runner.vm.lima import Lima
from agent_runner.vm.pool import VmEnv
from agent_runner.vm.records import TokenStore

from .guests import GOLDEN, ORG, PREFIX, WG_A, Harness


def test_tasks_are_registered() -> None:
    names = [spec.name for spec in registry.tasks()]
    assert "vm-pool" in names
    assert "vm-idle" in names
    assert [c.name for c in registry.cleaners()] == ["vm-instances"]


class Stopper:
    """A fake manager that records calls and sets `stopping` after `rounds` loop turns."""

    def __init__(self, stopping: asyncio.Event, rounds: int) -> None:
        self.stopping = stopping
        self.rounds = rounds
        self.calls: list[str] = []
        self.reconciled = False
        self.golden_switch = self

    async def run_pass(self) -> list[str]:
        self.calls.append("switch")
        return []

    def turn(self, name: str) -> None:
        self.calls.append(name)
        if self.calls.count(name) >= self.rounds:
            self.stopping.set()

    async def reconcile(self) -> None:
        self.calls.append("reconcile")

    async def refill(self) -> int:
        self.turn("refill")
        return 0

    async def wait_refill(self, timeout: float) -> None:
        self.calls.append(f"wait:{timeout:g}")

    async def wait_reconciled(self) -> None:
        self.calls.append("reconciled")

    async def health_pass(self) -> None:
        self.turn("health")

    async def idle_pass(self) -> list[str]:
        self.calls.append("idle")
        return []


def context(fake_clock: Any, manager: Any = None) -> SimpleNamespace:
    ctx = SimpleNamespace(
        stopping=asyncio.Event(),
        host=SimpleNamespace(sleep=fake_clock.sleep, clock=fake_clock, env={"PATH": "/usr/bin:/bin"}),
        log=io.StringIO(),
    )
    if manager is not None:
        vars(ctx)[tasks.MANAGER_KEY] = manager
    return ctx


def test_pool_task_reconciles_then_refills(fake_clock: Any) -> None:
    async def body() -> list[str]:
        ctx = context(fake_clock)
        fake = Stopper(ctx.stopping, rounds=2)
        vars(ctx)[tasks.MANAGER_KEY] = fake
        await tasks.run_pool(ctx)
        return fake.calls

    # the golden switch (ADR-093) follows every refill
    assert asyncio.run(body()) == ["reconcile", "refill", "switch", "wait:60", "refill", "switch", "wait:60"]


def test_idle_task_probes_every_5_s_and_stops_idle_vms_every_60_s(fake_clock: Any) -> None:
    async def body() -> list[str]:
        ctx = context(fake_clock)
        fake = Stopper(ctx.stopping, rounds=13)
        vars(ctx)[tasks.MANAGER_KEY] = fake
        await tasks.run_idle(ctx)
        return fake.calls

    calls = asyncio.run(body())
    assert calls[0] == "reconciled"
    assert calls.count("health") == 13
    assert calls.count("idle") == 1
    assert calls.index("idle") == calls.index("health") + 12 + 1
    assert set(fake_clock.sleeps) == {5.0}


def test_manager_for_builds_one_manager(fake_clock: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = SimpleNamespace(
        vm_prefix=PREFIX, vm_cpus=4, vm_memory_mib=8192, vm_disk_gib=20, pool_size=2, memory_reserve_mib=32768,
        vm_idle_seconds=1800, home=tmp_path / "home", sessions_per_vm=8, max_vms=5,
    )  # fmt: skip
    ctx = context(fake_clock)
    ctx.config = config
    ctx.pool = object()
    ctx.identity = None
    monkeypatch.setattr(registry, "capacity_usages", lambda: [])  # no store behind this context
    manager = tasks.manager_for(ctx)
    assert tasks.manager_for(ctx) is manager
    assert isinstance(manager, VmManager)
    assert manager.env.lima.prefix == PREFIX
    assert manager.env.tokens.directory == tmp_path / "home" / "vm"
    assert manager.env.settings.pool_size == 2
    assert (manager.env.settings.sessions_per_vm, manager.env.settings.max_vms) == (8, 5)
    assert manager.env.cpus() == os.cpu_count()

    async def usage() -> Usage:
        return await manager.env.other_usage()

    assert asyncio.run(usage()) == Usage()
    manager.env.log("vm.test", {"a": 1})
    asyncio.run(manager.env.sink({"vm_id": "vm-00000001", "state": "pool"}))  # type: ignore[arg-type]
    manager.env.log("vm.empty", {})
    lines = ctx.log.getvalue().splitlines()
    assert lines[0] == "runner.vm.test a=1"
    assert lines[1] == 'runner.vm.state body={"state":"pool","vm_id":"vm-00000001"}'
    assert lines[2] == "runner.vm.empty"
    assert manager.env.physical_mib() > 0
    # The daemon's stop flag reaches the VM area (no clone once stopping, decisions KOR-03).
    assert manager.env.stopping() is False
    ctx.stopping.set()
    assert manager.env.stopping() is True


# -- uninstall --------------------------------------------------------------------------------------------------


def test_uninstall_deletes_own_instances(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = harness.settings.__class__(**{**harness.settings.__dict__, "pool_size": 1})

    async def setup(manager: VmManager) -> Any:
        await manager.refill()
        return await manager.ensure_vm(ORG, WG_A)

    harness.run(setup)
    harness.fake.add(f"{PREFIX}arp-0000abcd")
    harness.fake.add(f"{PREFIX}something-else")
    harness.fake.add("cache-golden-0123456789ab")
    ctx = SimpleNamespace(config=SimpleNamespace(vm_prefix=PREFIX), host=SimpleNamespace(env={}), dsn=harness.dsn)
    deleted = uninstall.delete_instances(ctx, wrapper=Lima(harness.fake, prefix=PREFIX))
    assert f"{PREFIX}golden-{GOLDEN}" in deleted
    assert f"{PREFIX}arp-0000abcd" in deleted
    assert len(deleted) == 3
    assert sorted(harness.fake.instances) == ["cache-golden-0123456789ab", f"{PREFIX}something-else"]
    # Without a store only the unknown pool clones are recognised.
    harness.fake.add(f"{PREFIX}arp-0000abce")
    no_store = SimpleNamespace(config=ctx.config, host=ctx.host, dsn=None)
    assert uninstall.delete_instances(no_store, wrapper=Lima(harness.fake, prefix=PREFIX)) == [f"{PREFIX}arp-0000abce"]
    assert uninstall.delete_instances(no_store, wrapper=Lima(harness.fake, prefix=PREFIX)) == []


def test_uninstall_without_lima(tmp_path: Path) -> None:
    ctx = SimpleNamespace(
        config=SimpleNamespace(vm_prefix=""), host=SimpleNamespace(env={"PATH": str(tmp_path)}), dsn=None
    )
    assert uninstall.delete_instances(ctx) == []
    uninstall.CLEANERS[0].run(ctx)


# -- token files ------------------------------------------------------------------------------------------------


def test_token_store(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "vm")
    assert store.known() == set()
    store.write("vm-0a1b2c3d", "token-value")
    path = tmp_path / "vm" / "vm-0a1b2c3d.token"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "vm").stat().st_mode) == 0o700
    assert store.read("vm-0a1b2c3d", records.token_hash("token-value")) == "token-value"
    with pytest.raises(RunnerError) as exists:
        store.write("vm-0a1b2c3d", "other")
    assert (exists.value.code, exists.value.name) == ("vm_token_exists", "vm-0a1b2c3d")
    with pytest.raises(RunnerError) as wrong:
        store.read("vm-0a1b2c3d", records.token_hash("other"))
    assert wrong.value.code == "vm_token_invalid"
    with pytest.raises(RunnerError):
        store.read("vm-0000beef", "x")
    os.symlink(path, tmp_path / "vm" / "vm-0000cafe.token")
    with pytest.raises(RunnerError):
        store.read("vm-0000cafe", records.token_hash("token-value"))
    (tmp_path / "vm" / "notes.txt").write_text("x", encoding="ascii")
    (tmp_path / "vm" / "vm-zz.token").write_text("x", encoding="ascii")
    assert store.known() == {"vm-0a1b2c3d", "vm-0000cafe"}
    with pytest.raises(RunnerError) as invalid:
        store.path("../x")
    assert invalid.value.code == "vm_id_invalid"
    store.remove("vm-0a1b2c3d")
    store.remove("vm-0a1b2c3d")
    assert not path.exists()
    assert json.dumps(sorted(store.known())) == '["vm-0000cafe"]'


def test_default_sink_log_and_unreachable_handler(harness: Harness) -> None:
    harness.add_golden()
    row = harness.run(lambda manager: manager.ensure_vm(ORG, WG_A))

    async def body(manager: VmManager) -> Any:
        env = manager.env
        plain = VmManager(
            VmEnv(lima=env.lima, db=env.db, settings=env.settings, tokens=env.tokens, physical_mib=env.physical_mib)
        )
        await plain.env.emit(row)
        plain.env.log("vm.nothing", {})
        await plain.reconcile()
        await plain.on_unreachable(row)
        return plain.env.dirs

    assert harness.run(body)


# -- outbox sink and heartbeat (RUN-02 extension points) ---------------------------------------------------------


class FakePusher:
    def __init__(self) -> None:
        self.notified = 0

    def notify(self) -> None:
        self.notified += 1


class FakeWs:
    def __init__(self) -> None:
        self.beats = 0

    def beat_soon(self) -> None:
        self.beats += 1


def test_vm_state_goes_to_the_outbox_and_the_heartbeat(harness: Harness, fake_clock: Any, tmp_path: Path) -> None:
    harness.add_golden()
    key = bytes(range(32))

    async def body() -> Any:
        db = await open_pool(harness.dsn)
        try:
            ctx = context(fake_clock)
            pusher, ws = FakePusher(), FakeWs()
            ctx.pool = db
            ctx.config = SimpleNamespace(
                vm_prefix=PREFIX, vm_cpus=4, vm_memory_mib=8192, vm_disk_gib=20, pool_size=1,
                memory_reserve_mib=32768, vm_idle_seconds=1800, home=tmp_path / "home", sessions_per_vm=16, max_vms=0,
            )  # fmt: skip
            ctx.identity = None
            manager = tasks.manager_for(ctx)
            manager.env.lima = Lima(harness.fake, prefix=PREFIX)
            manager.env.physical_mib = lambda: 32768 + 4 * 8192
            await manager.reconcile()
            await manager.refill()
            unenrolled = ctx.log.getvalue()
            vars(ctx)[ATTRIBUTE] = SyncRuntime(client=None, pusher=pusher, ws=ws, storage_key=key, wall=lambda: 0.0)  # type: ignore[arg-type]
            row = await manager.ensure_vm(ORG, WG_A)
            async with db.connection() as conn:
                cursor = await conn.execute(
                    "SELECT origin_seq, item_kind, body_ct FROM runner.outbox ORDER BY origin_seq"
                )
                items = [outbox.open_item(key, seq, kind, bytes(blob)) for seq, kind, blob in await cursor.fetchall()]
            return (
                unenrolled,
                (pusher.notified, ws.beats),
                items,
                row,
                await heartbeat.capacity(ctx),
                await heartbeat.vms(ctx),
            )
        finally:
            await db.close()

    unenrolled, notified, items, row, capacity, vms = asyncio.run(body())
    assert "runner.vm.state body=" in unenrolled
    assert [item["state"] for item in items] == ["assigning", "running"]
    assert items[-1]["vm_id"] == row.id
    assert items[-1]["wg_id"] == WG_A
    assert notified == (2, 2)  # pushed and the heartbeat brought forward per state change
    assert capacity == {"vm_memory_budget_mib": capacity["vm_memory_budget_mib"], "vm_memory_used_mib": 8192,
                        "vm_slots_total": capacity["vm_slots_total"], "vm_slots_free": capacity["vm_slots_total"] - 1,
                        "pool_ready": 0, "cpus": os.cpu_count(), "memory_total_mib": 32768 + 4 * 8192,
                        "vms_running": 1, "sessions_active": 0, "work_vms_running": 0, "sessions_per_vm": 16,
                        "vms_max": 0,
                        **{key: capacity[key] for key in ("disk_free_mib",) if key in capacity}}  # fmt: skip
    assert vms == [{"vm_id": row.id, "state": "running"}]
    sections = extensions.heartbeat_sections()
    assert (sections["capacity"].provide, sections["vms"].provide) == (heartbeat.capacity, heartbeat.vms)


def test_the_usage_of_other_areas_is_summed_and_checked() -> None:
    async def work(context: Any) -> Usage:
        return Usage(vms=1, memory_mib=context)

    async def wrong(_context: Any) -> object:
        return (1, 2)

    assert asyncio.run(tasks.usage_of(4096, [work, work])()) == Usage(vms=2, memory_mib=8192)
    assert asyncio.run(tasks.usage_of(4096, [])()) == Usage()
    with pytest.raises(RunnerError) as caught:
        asyncio.run(tasks.usage_of(None, [wrong])())
    assert (caught.value.code, caught.value.name) == ("registry_invalid", "capacity")


def test_the_other_areas_give_room_back_in_turn_until_it_is_enough() -> None:
    """ADR-059 addendum 4: a workgroup VM asks the areas' `RECLAIM` hooks for the RAM it needs."""
    asked: list[tuple[str, int]] = []

    def area(name: str, gives: int) -> Any:
        async def give(context: Any, need_mib: int) -> int:
            asked.append((name, need_mib))
            return gives

        return give

    reclaim = tasks.reclaim_of("ctx", [area("vm", 0), area("workvm", 6144), area("other", 4096), area("last", 1)])
    assert asyncio.run(reclaim(8192)) == 10240
    assert asked == [("vm", 8192), ("workvm", 8192), ("other", 2048)]
    assert asyncio.run(tasks.reclaim_of("ctx", [])(8192)) == 0
    assert registry.capacity_reclaimers()  # the vm and workvm areas register one each


def test_pool_task_reconciles_only_once(fake_clock: Any) -> None:
    async def body() -> list[str]:
        ctx = context(fake_clock)
        fake = Stopper(ctx.stopping, rounds=1)
        fake.reconciled = True
        vars(ctx)[tasks.MANAGER_KEY] = fake
        await tasks.run_pool(ctx)
        return fake.calls

    assert asyncio.run(body()) == ["refill", "switch", "wait:60"]
