"""Heartbeat fields of the VM area (05-runner.md section 3.4, 03-api.md section 6.2; `sync/extensions.py`).

- `capacity`: `{vm_memory_budget_mib, vm_memory_used_mib, vm_slots_total, vm_slots_free, pool_ready}` of the shared
  `VmManager` (VMs being cloned or restarted count as used) plus the counters, settings and features of ADR-075
  section 2 (`VmManager.report`).
- `vms`: `[{vm_id, state}]` of every VM that is not deleted, oldest first (all of them, ADR-088 section 3:
  `sync.ws.split_heartbeat` spreads a long list over several frames).
"""

from __future__ import annotations

from typing import Any

from agent_runner.sync.extensions import HeartbeatSection
from agent_runner.vm import records
from agent_runner.vm.tasks import manager_for


async def capacity(context: Any) -> dict[str, object]:
    return await manager_for(context).report()


async def vms(context: Any) -> list[dict[str, str]]:
    rows = await records.live(manager_for(context).env.db)
    return [{"vm_id": row.id, "state": row.state} for row in rows]


SECTIONS = [HeartbeatSection("capacity", capacity), HeartbeatSection("vms", vms)]
