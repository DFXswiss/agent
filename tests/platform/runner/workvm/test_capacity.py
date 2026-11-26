"""Work VMs in the device capacity (ADR-075 section 2, `workvm/capacity.py:USAGE` and `RECLAIM`; ADR-059 addendum 4:
the task and session VMs and the pool yield to workgroup VMs)."""

from __future__ import annotations

import asyncio
import dataclasses
from types import SimpleNamespace
from typing import Any

import pytest

from agent_runner import registry
from agent_runner.vm.capacity import Usage
from agent_runner.workvm import capacity, records

from .kit import START, TASK_B
from .test_records import ROW, dsn, run

__all__ = ["dsn"]


def test_live_work_vms_and_pool_entries_count_against_the_budget(dsn: str) -> None:
    async def body(db: Any) -> list[Usage]:
        context = SimpleNamespace(pool=db)
        empty = await capacity.usage(context)
        await records.insert(db, ROW)
        await records.insert(
            db,
            dataclasses.replace(
                ROW, id="wvm-0000000c", lima_name="wp-t-c", slot=2, task_id=TASK_B, requested_by=TASK_B, state="stopped"
            ),
        )
        await records.insert(
            db, dataclasses.replace(ROW, id="wvm-0000000d", lima_name="wp-t-d", slot=3, scope="run", session_id=ROW.task_id)
        )
        await records.pool_add(db, records.Pooled("wp-t-wvm-0000000b", 1, "img", 4096, "ready", START))
        return [empty, await capacity.usage(context)]

    assert run(dsn, body) == [
        Usage(0, 0),
        # the run VM is firm (its test run owns it); the task VM and the pool entry yield
        Usage(vms=3, memory_mib=2 * 8192 + 4096, yielding_vms=2, yielding_mib=8192 + 4096),
    ]


def test_reclaim_asks_the_work_vms_to_give_way(monkeypatch: pytest.MonkeyPatch) -> None:
    class Fake:
        async def give_way(self, need_mib: int) -> int:
            return need_mib - 1

    assert asyncio.run(capacity.reclaim(SimpleNamespace(), 8192)) == 0  # no work VMs on this device
    monkeypatch.setattr(capacity, "existing", lambda context: Fake())
    assert asyncio.run(capacity.reclaim(SimpleNamespace(), 8192)) == 8191


def test_the_provider_is_registered() -> None:
    assert capacity.USAGE in registry.capacity_usages()
    assert capacity.RECLAIM in registry.capacity_reclaimers()
