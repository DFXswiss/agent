"""The work VMs in the device's capacity (ADR-075 section 2, `<area>/capacity.py:USAGE`).

A work VM (docs/platform/spec/13-work-vm.md) holds host memory like a workgroup VM: the live work VMs that hold their
RAM (`records.MEMORY_STATES`) and the warm pool entries count against the VM budget and are reported as
`work_vms_running`, so the hub schedules with the room that is really left.
"""

from __future__ import annotations

from typing import Any

from agent_runner.vm.capacity import Usage
from agent_runner.workvm import records


async def usage(context: Any) -> Usage:
    """VM count and memory of the work VMs and of the warm pool."""
    db = context.pool
    live = await records.live(db, *records.MEMORY_STATES)
    pooled = await records.pool(db)
    return Usage(vms=len(live) + len(pooled), memory_mib=await records.memory_in_use(db))


USAGE = usage
