"""The work VMs in the device's capacity (ADR-075 section 2, `<area>/capacity.py:USAGE` and `RECLAIM`).

A work VM (docs/platform/spec/13-work-vm.md) holds host memory like a workgroup VM: the live work VMs that hold their
RAM (`records.MEMORY_STATES`) and the warm pool entries count against the VM budget and are reported as
`work_vms_running`. Workgroup VMs with their sessions always come first (ADR-059 addendum 4): the task and session
VMs and the pool entries are reported as *yielding* - they take no slot of a workgroup VM, so the hub places sessions
as if they were not there - and `RECLAIM` gives their RAM back when a workgroup VM needs it (`WorkVms.give_way`). Run
and browser VMs belong to one test run or browser session and are not yielding.
"""

from __future__ import annotations

from typing import Any

from agent_runner.vm.capacity import Usage
from agent_runner.workvm import records
from agent_runner.workvm.manager import OWNED_SCOPES
from agent_runner.workvm.service import existing


async def usage(context: Any) -> Usage:
    """VM count and memory of the work VMs and of the warm pool, and the part of them that yields."""
    db = context.pool
    live = await records.live(db, *records.MEMORY_STATES)
    pooled = await records.pool(db)
    yielding = [vm for vm in live if vm.state == "running" and vm.scope not in OWNED_SCOPES]
    return Usage(
        vms=len(live) + len(pooled),
        memory_mib=await records.memory_in_use(db),
        yielding_vms=len(yielding) + len(pooled),
        yielding_mib=sum(vm.memory_mib for vm in yielding) + sum(entry.memory_mib for entry in pooled),
    )


async def reclaim(context: Any, need_mib: int) -> int:
    """Give `need_mib` of RAM back to a workgroup VM (stopped work VMs keep their disks); returns the MiB given back."""
    workvms = existing(context)
    return 0 if workvms is None else await workvms.give_way(need_mib)


USAGE = usage
RECLAIM = reclaim
