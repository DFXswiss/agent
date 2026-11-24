"""Work VM rows (migration 0012, docs/platform/spec/13-work-vm.md section 8)."""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import psycopg
import pytest

from agent_runner.errors import RunnerError
from agent_runner.store.migrate import migrate
from agent_runner.store.pool import open_pool
from agent_runner.workvm import records
from agent_runner.workvm.records import WorkVm

from .kit import ORG, START, TASK, TASK_B, WG

ROW = WorkVm(
    id="wvm-0000000a",
    lima_name="wp-t-wvm-0000000a",
    slot=0,
    scope="task",
    task_id=TASK,
    session_id=None,
    requested_by=TASK,
    implicit=False,
    org_id=ORG,
    wg_id=WG,
    owner_id=None,
    size="standard",
    cpus=4,
    memory_mib=8192,
    disk_gib=50,
    image="img",
    state="running",
    created_at=START,
    last_used_at=START,
    stopped_at=None,
)


def run(dsn: str, body: Any) -> Any:
    async def main() -> Any:
        db = await open_pool(dsn)
        try:
            return await body(db)
        finally:
            await db.close()

    return asyncio.run(main())


@pytest.fixture
def dsn(runner_dsn: str) -> str:
    assert migrate(runner_dsn).status == "applied"
    return runner_dsn


def test_one_live_vm_per_task_scope_and_slot(dsn: str) -> None:
    async def body(db: Any) -> list[str]:
        await records.insert(db, ROW)
        codes = []
        for duplicate in (
            dataclasses.replace(ROW, id="wvm-0000000b", lima_name="b", slot=1),
            dataclasses.replace(ROW, id="wvm-0000000c", lima_name="c", task_id=TASK_B),
        ):
            try:
                await records.insert(db, duplicate)
            except RunnerError as error:
                codes.append(error.code)
        return codes

    assert run(dsn, body) == ["workvm_exists", "workvm_exists"]


def test_memory_in_use_counts_only_vms_that_hold_ram(dsn: str) -> None:
    async def body(db: Any) -> list[int]:
        found = [await records.memory_in_use(db)]
        await records.insert(db, ROW)
        found.append(await records.memory_in_use(db))
        await records.update(db, ROW.id, state="stopped")
        found.append(await records.memory_in_use(db))
        missing = await records.update(db, "wvm-ffffffff", state="stopped")
        assert missing is None
        assert await records.update(db, ROW.id, expect=("running",), state="deleted") is None
        return found

    assert run(dsn, body) == [0, 8192, 0]


def test_images_newest_ready_wins(dsn: str) -> None:
    async def body(db: Any) -> list[str | None]:
        found = [await records.ready_image(db)]
        await records.set_image(db, "old", "ready", START)
        await records.set_image(db, "new", "building", START.replace(hour=11))
        found.append(await records.ready_image(db))
        await records.set_image(db, "new", "ready", START.replace(hour=11))
        found.append(await records.ready_image(db))
        return found

    assert run(dsn, body) == [None, "old", "new"]


def test_active_sessions_of_a_task(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        for sid, state in (
            ("0193000a-0000-7000-8000-0000000000c1", "working"),
            ("0193000a-0000-7000-8000-0000000000c2", "stopped"),
            ("0193000a-0000-7000-8000-0000000000c3", "idle"),
        ):
            conn.execute(
                "INSERT INTO runner.session (id, task_id, org_id, wg_id, state, depth, created_at)"
                " VALUES (%s, %s, %s, %s, %s, 0, %s)",
                (sid, TASK, ORG, WG, state, START),
            )

    async def body(db: Any) -> list[int]:
        return [
            await records.active_sessions(db, TASK),
            await records.active_sessions(db, TASK, except_id="0193000a-0000-7000-8000-0000000000c1"),
            await records.active_sessions(db, TASK_B),
        ]

    assert run(dsn, body) == [2, 1, 0]


def test_pool_entries_hold_slots_and_ram_and_turn_into_rows(dsn: str) -> None:
    entry = records.Pooled("wp-t-wvm-0000000b", 1, "img", 8192, "starting", START)

    async def body(db: Any) -> None:
        await records.pool_add(db, entry)
        with pytest.raises(RunnerError, match="workvm_exists"):
            await records.pool_add(db, dataclasses.replace(entry, lima_name="wp-t-wvm-0000000c"))
        assert await records.used_slots(db) == {1}
        assert await records.memory_in_use(db) == 8192

        def build(lima_name: str, slot: int) -> WorkVm:
            return dataclasses.replace(ROW, lima_name=lima_name, slot=slot)

        assert await records.pool_assign(db, "img", 8192, build) is None  # still starting
        assert await records.pool_ready(db, entry.lima_name, START) is True
        assert await records.pool_ready(db, "wp-t-wvm-00000099", START) is False
        assert await records.pool_assign(db, "other", 8192, build) is None
        assert await records.pool_assign(db, "img", 16384, build) is None
        vm = await records.pool_assign(db, "img", 8192, build)
        assert vm is not None
        assert (vm.lima_name, vm.slot, vm.state) == (entry.lima_name, 1, "running")
        assert await records.pool(db) == []

        await records.pool_add(db, dataclasses.replace(entry, lima_name="wp-t-wvm-0000000d", slot=2, state="ready"))
        with pytest.raises(RunnerError, match="workvm_exists"):  # the task already has its live task VM
            await records.pool_assign(
                db, "img", 8192, lambda name, slot: dataclasses.replace(build(name, slot), id="wvm-0000000e")
            )
        [kept] = await records.pool(db)
        assert kept.lima_name == "wp-t-wvm-0000000d"
        assert await records.pool_remove(db, kept.lima_name) is True
        assert await records.pool_remove(db, kept.lima_name) is False
        assert await records.pool(db) == []

    run(dsn, body)


def test_two_concurrent_assignments_never_take_the_same_entry(dsn: str) -> None:
    async def body(db: Any) -> list[str | None]:
        for index, name in enumerate(("wp-t-wvm-000000a1", "wp-t-wvm-000000a2")):
            await records.pool_add(db, records.Pooled(name, index, "img", 8192, "ready", START))

        def build_for(task: str, vm_id: str) -> Any:
            return lambda name, slot: dataclasses.replace(ROW, id=vm_id, task_id=task, lima_name=name, slot=slot)

        first, second, third = await asyncio.gather(
            records.pool_assign(db, "img", 8192, build_for(TASK, "wvm-000000b1")),
            records.pool_assign(db, "img", 8192, build_for(TASK_B, "wvm-000000b2")),
            records.pool_assign(db, "img", 8192, build_for(ORG, "wvm-000000b3")),
        )
        return [None if vm is None else vm.lima_name for vm in (first, second, third)]

    names = run(dsn, body)
    taken = [name for name in names if name is not None]
    assert sorted(taken) == ["wp-t-wvm-000000a1", "wp-t-wvm-000000a2"]
    assert names.count(None) == 1


def test_disk_in_use_counts_live_vms_and_pool_entries(dsn: str) -> None:
    async def body(db: Any) -> tuple[int, int]:
        empty = await records.disk_in_use(db, 50)
        await records.insert(db, ROW)
        await records.insert(
            db, dataclasses.replace(ROW, id="wvm-0000000b", lima_name="x-b", slot=1, task_id=TASK_B, state="deleted")
        )
        await records.pool_add(db, records.Pooled("wp-t-wvm-000000c1", 2, "img", 8192, "ready", START))
        return empty, await records.disk_in_use(db, 50)

    assert run(dsn, body) == (0, 50 + 50)
