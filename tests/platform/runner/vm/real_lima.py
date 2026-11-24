"""Integration run of the VM management against real Lima on the runner device (VM-02 acceptance, PERF smoke).

Not collected by pytest (no `test_` prefix); run it on the runner device from the repository root:

    python tests/platform/runner/vm/real_lima.py --golden <lima name of a stopped golden> [--prefix wp-x-] [--runs 10]

It creates an own PostgreSQL cluster in a temporary directory, registers the given golden (an own one or a shared
`cache-golden-*`) as the ready golden and then, with real `limactl` and the real guest of the golden:

1. fills the warm pool (clone, start with token parameter, health, sandbox self-test) and times each fill;
2. assigns pool VMs `--runs` times (`ensure_vm` on the pool path; the pool is refilled outside the timing);
3. creates VMs on the clone path `--runs` times (`ensure_vm` with an empty pool: clone + start + health + assign)
   and times clone + start until the guest health says `unassigned` separately;
4. stops and restarts a workgroup VM (`ensure_vm` on the restart path), stops it on idle, deletes it;
5. checks that the startup reconciliation deletes an unknown pool clone.

Every created instance starts with the prefix and is deleted at the end (also on failure). Output: one JSON line
per measurement and a summary with min/median/max per path.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from agent_cli.pg import create_database, ensure_cluster, stop_cluster
from agent_runner.store.migrate import migrate
from agent_runner.store.pool import open_pool
from agent_runner.vm import lima
from agent_runner.vm.capacity import physical_memory_mib
from agent_runner.vm.lifecycle import VmManager
from agent_runner.vm.pool import VmEnv, VmSettings
from agent_runner.vm.records import GoldenRef, TokenStore, VmRow

ORG = "01930001-0000-7000-8000-000000000001"


def uuid7() -> str:
    """A UUIDv7-shaped random ID (the guest validates workgroup IDs against the contract pattern)."""
    raw = bytearray(uuid.uuid4().bytes)
    raw[6] = 0x70 | (raw[6] & 0x0F)
    raw[8] = 0x80 | (raw[8] & 0x3F)
    return str(uuid.UUID(bytes=bytes(raw)))


def emit(kind: str, **fields: Any) -> None:
    print(json.dumps({"kind": kind, **fields}, sort_keys=True), flush=True)


def summary(name: str, values: list[float]) -> None:
    if values:
        emit("summary", path=name, runs=len(values), min=round(min(values), 3),
             median=round(statistics.median(values), 3), max=round(max(values), 3))  # fmt: skip


async def pool_runs(manager: VmManager, golden: GoldenRef, runs: int) -> tuple[list[float], list[float]]:
    fills, assigns = [], []
    for run in range(runs):
        start = time.monotonic()
        await manager.pool.fill_one(golden)
        fills.append(time.monotonic() - start)
        wg = uuid7()
        start = time.monotonic()
        row = await manager.ensure_vm(ORG, wg)
        assigns.append(time.monotonic() - start)
        emit("measure", path="pool_assign", run=run + 1, seconds=round(assigns[-1], 3), fill=round(fills[-1], 3))
        async with await manager.client(row) as client:
            health = await client.health()
        emit("check", name="assigned", ok=health.state == "assigned" and health.wg_id == wg)
        await manager.delete_workgroup(wg)
    return fills, assigns


async def clone_runs(manager: VmManager, runs: int) -> tuple[list[float], list[float], VmRow]:
    clones: list[float] = []
    provisions: list[float] = []
    original = manager.provisioner._provision

    async def timed(*a: Any, **k: Any) -> None:
        begin = time.monotonic()
        await original(*a, **k)
        provisions.append(time.monotonic() - begin)

    manager.provisioner._provision = timed  # type: ignore[method-assign]
    row: VmRow | None = None
    for run in range(runs):
        if row is not None:
            assert row.wg_id is not None
            await manager.delete_workgroup(row.wg_id)
        start = time.monotonic()
        row = await manager.ensure_vm(ORG, uuid7())
        clones.append(time.monotonic() - start)
        emit("measure", path="clone", run=run + 1, seconds=round(clones[-1], 3),
             clone_start_health=round(provisions[-1], 3))  # fmt: skip
    assert row is not None
    return clones, provisions, row


async def lifecycle_checks(manager: VmManager, row: VmRow, golden_name: str) -> list[float]:
    env, machine = manager.env, manager.env.lima
    assert row.wg_id is not None
    restarts = []
    for run in range(3):
        await manager.stop_vm(row.id)
        start = time.monotonic()
        again = await manager.ensure_vm(ORG, row.wg_id)
        restarts.append(time.monotonic() - start)
        emit("measure", path="restart", run=run + 1, seconds=round(restarts[-1], 3), same=again.id == row.id)
    env.settings = VmSettings(**{**env.settings.__dict__, "vm_idle_seconds": 1})
    await asyncio.sleep(2)
    stopped = await manager.idle_pass()
    emit("check", name="idle_stop", stopped=stopped, ok=stopped == [row.id])
    deleted = await manager.delete_workgroup(row.wg_id)
    gone = machine.get(row.lima_name) is None
    emit("check", name="delete", ok=[item.state for item in deleted] == ["deleted"] and gone)
    stray = machine.name("arp-0000abcd")
    machine.clone(golden_name, stray)
    counts = await VmManager(env).reconcile()
    emit("check", name="reconcile_unknown", counts=counts, ok=counts["unknown"] == 1 and machine.get(stray) is None)
    return restarts


async def scenario(args: argparse.Namespace, dsn: str, home: Path) -> None:
    machine = lima.Lima(lima.run_limactl, prefix=args.prefix)
    db = await open_pool(dsn)
    async with db.connection() as conn:
        await conn.execute(
            "INSERT INTO runner.golden (version, lima_name, claude_version, guest_version, image_digest, built_at,"
            " state) VALUES ('real', %s, '2.1.330', '0.1.0', 'sha256:real', now(), 'ready')",
            (args.golden,),
        )
    settings = VmSettings(cpus=4, memory_mib=8192, disk_gib=20, pool_size=1, memory_reserve_mib=32768,
                          vm_idle_seconds=1800)  # fmt: skip

    def log(name: str, fields: dict[str, str | int]) -> None:
        emit("log", name=name, **fields)

    env = VmEnv(lima=machine, db=db, settings=settings, tokens=TokenStore(home / "vm"),
                physical_mib=physical_memory_mib, log=log)  # fmt: skip
    manager = VmManager(env)
    try:
        await manager.reconcile()
        fills, assigns = await pool_runs(manager, GoldenRef(version="real", lima_name=args.golden), args.runs)
        env.settings = VmSettings(**{**settings.__dict__, "pool_size": 0})
        clones, provisions, row = await clone_runs(manager, args.runs)
        restarts = await lifecycle_checks(manager, row, args.golden)
        summary("pool_fill", fills)
        summary("pool_assign", assigns)
        summary("clone_start_health", provisions)
        summary("clone_ensure_vm", clones)
        summary("restart", restarts)
        emit("start_failures", count=env.start_failures)
    finally:
        for instance in machine.list():
            if instance.name.startswith(args.prefix + "arp-"):
                machine.delete(instance.name)
        await db.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--golden", required=True)
    parser.add_argument("--prefix", default="wp-vm02-")
    parser.add_argument("--runs", type=int, default=10)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="vm02-real-") as scratch:
        base = Path(scratch)
        admin = ensure_cluster(base / "pg")
        try:
            dsn = create_database(admin, "vm02")
            assert migrate(dsn).status == "applied"
            asyncio.run(scenario(args, dsn, base / "home"))
        finally:
            stop_cluster(base / "pg")
    return 0


if __name__ == "__main__":
    sys.exit(main())
