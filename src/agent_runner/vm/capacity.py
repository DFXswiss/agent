"""VM capacity of the runner device (docs/platform/spec/05-runner.md section 3.4, 03-api.md section 4.6.2, ADR-009).

    budget_mib     = physical RAM - MEMORY_RESERVE_MIB
    vm_slots_total = floor(budget_mib / VM_MEMORY_MIB)
    vm_slots_free  = vm_slots_total - (VMs in pool, assigning, running, stopping)

Stopped VMs use no slot (the host gets their RAM back on stop). VMs being cloned for the pool count as well (they
are not in the store yet), so a refill never overbooks. `Capacity.heartbeat()` is the `capacity` object of the
device row (02-data-model.md section 2.1); `capacity_low` is the warning threshold of 03-api.md section 4.6.2.

`lru_victim(...)` picks the VM to stop when a session needs a slot and none is free: the least recently used
running VM of *another* workgroup that has no active session and no suspended session younger than
`VM_IDLE_SECONDS`.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

# VM states that occupy a slot (05 section 3.4).
SLOT_STATES = frozenset({"pool", "assigning", "running", "stopping"})
LOW_FRACTION = 0.1
MIB = 1024 * 1024


@dataclass(frozen=True)
class Capacity:
    budget_mib: int
    used_mib: int
    slots_total: int
    slots_free: int
    pool_ready: int

    @property
    def capacity_low(self) -> bool:
        """True when less than 10 % of the slots are free (UI badge, alert rule)."""
        return self.slots_total == 0 or self.slots_free / self.slots_total < LOW_FRACTION

    def heartbeat(self) -> dict[str, int]:
        return {
            "vm_memory_budget_mib": self.budget_mib,
            "vm_memory_used_mib": self.used_mib,
            "vm_slots_total": self.slots_total,
            "vm_slots_free": self.slots_free,
            "pool_ready": self.pool_ready,
        }


def compute(
    *,
    physical_mib: int,
    reserve_mib: int,
    vm_memory_mib: int,
    vms: Sequence[tuple[str, int]],
    in_flight: int = 0,
) -> Capacity:
    """Capacity from the VM rows `(state, memory_mib)` plus `in_flight` VMs being created for the pool."""
    budget = max(physical_mib - reserve_mib, 0)
    total = budget // vm_memory_mib
    occupying = [memory for state, memory in vms if state in SLOT_STATES]
    pool_ready = sum(1 for state, _ in vms if state == "pool")
    used = sum(occupying) + in_flight * vm_memory_mib
    free = max(total - len(occupying) - in_flight, 0)
    return Capacity(budget_mib=budget, used_mib=used, slots_total=total, slots_free=free, pool_ready=pool_ready)


def physical_memory_mib(sysconf: Callable[[str], int] = os.sysconf) -> int:
    """Installed RAM of this machine in MiB."""
    return sysconf("SC_PHYS_PAGES") * sysconf("SC_PAGE_SIZE") // MIB


@dataclass(frozen=True)
class Candidate:
    """A running VM considered for an LRU stop."""

    vm_id: str
    wg_id: str | None
    last_used: datetime
    active_sessions: int
    newest_suspended: datetime | None


def lru_victim(
    candidates: Iterable[Candidate], *, exclude_wg: str | None, now: datetime, idle_seconds: int
) -> Candidate | None:
    """The least recently used idle VM of another workgroup, or None."""
    limit = now - timedelta(seconds=idle_seconds)
    eligible = [
        item
        for item in candidates
        if item.wg_id != exclude_wg
        and item.active_sessions == 0
        and (item.newest_suspended is None or item.newest_suspended <= limit)
    ]
    return min(eligible, key=lambda item: (item.last_used, item.vm_id), default=None)
