"""Work VMs in the device capacity (ADR-075 section 2, `workvm/capacity.py:USAGE`)."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import Any

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
        await records.pool_add(db, records.Pooled("wp-t-wvm-0000000b", 1, "img", 4096, "ready", START))
        return [empty, await capacity.usage(context)]

    assert run(dsn, body) == [Usage(0, 0), Usage(vms=2, memory_mib=8192 + 4096)]


def test_the_provider_is_registered() -> None:
    assert capacity.USAGE in registry.capacity_usages()
