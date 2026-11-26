"""The work VMs in the device's capacity (ADR-075 section 2, `<area>/capacity.py:USAGE` and `RECLAIM`).

A work VM (docs/platform/spec/13-work-vm.md) holds host memory like a workgroup VM: the live work VMs that hold their
RAM (`records.MEMORY_STATES`) and the warm pool entries count against the VM budget and are reported as
`work_vms_running`. Sessions rank first in the device's one admission (05-runner.md section 3.8): the warm pool entries
are reported as *yielding* (they take no workgroup slot, a workgroup VM gets their room at once), and `RECLAIM` gives a
workgroup VM the room of the pool and of the idle task and session VMs (`WorkVms.give_way`). A work VM with running
work keeps its RAM - running work is never ended - so it counts as held.
"""

from __future__ import annotations

from typing import Any

from agent_runner.vm.capacity import Usage
from agent_runner.workvm import records
from agent_runner.workvm.service import existing


async def usage(context: Any) -> Usage:
    """VM count and memory of the work VMs and of the warm pool, and the part that yields (the pool)."""
    db = context.pool
    live = await records.live(db, *records.MEMORY_STATES)
    pooled = await records.pool(db)
    return Usage(
        vms=len(live) + len(pooled),
        memory_mib=await records.memory_in_use(db),
        yielding_vms=len(pooled),
        yielding_mib=sum(entry.memory_mib for entry in pooled),
    )


async def reclaim(context: Any, need_mib: int) -> int:
    """Give `need_mib` of RAM back to a workgroup VM (stopped work VMs keep their disks); returns the MiB given back."""
    workvms = existing(context)
    return 0 if workvms is None else await workvms.give_way(need_mib)


USAGE = usage
RECLAIM = reclaim
