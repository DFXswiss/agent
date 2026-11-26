"""VM capacity of the runner device (docs/platform/spec/05-runner.md section 3.4, 03-api.md section 4.6.2, ADR-009).

    budget_mib     = physical RAM - MEMORY_RESERVE_MIB
    vm_slots_total = floor(budget_mib / VM_MEMORY_MIB), at most MAX_VMS when set (ADR-075)
    vm_slots_free  = vm_slots_total - (VMs in pool, assigning, running, stopping) - slots of other areas

Stopped VMs use no slot (the host gets their RAM back on stop). VMs being cloned for the pool count as well (they
are not in the store yet), so a refill never overbooks. VMs of other runner areas (e.g. work VMs) are reported by
their `<area>/capacity.py:USAGE` providers as a `Usage` (VM count and memory); they hold their memory and
`max(count, ceil(memory / VM_MEMORY_MIB))` slots (ADR-075 section 2, decisions/KOR-41.md) - except the part they
report as yielding (work VMs, ADR-059 addendum 4): workgroup VMs come first, so `slots_free` does not count it and a
workgroup VM that needs that room gets it through the areas' `RECLAIM` (`slots_yielding`, `slots_free_now`; the pool
refill uses only `slots_free_now`). `Capacity.heartbeat()` is
the base `capacity` object of the device row (02-data-model.md section 2.1, also the enrollment body);
`capacity_low` is the warning threshold of 03-api.md section 4.6.2.

`lru_victim(...)` picks the VM to stop when a session needs a slot and none is free: the least recently used
running VM of *another* workgroup that has no active session and no suspended session younger than
`VM_IDLE_SECONDS`.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

# VM states that occupy a slot (05 section 3.4).
SLOT_STATES = frozenset({"pool", "assigning", "running", "stopping"})
LOW_FRACTION = 0.1
MIB = 1024 * 1024


@dataclass(frozen=True)
class Usage:
    """VMs and memory held by another runner area (`<area>/capacity.py:USAGE`)."""

    vms: int = 0
    memory_mib: int = 0
    yielding_vms: int = 0
    """Of `vms`, those the area gives back when a workgroup VM needs the room (its `RECLAIM`, ADR-059 addendum 4)."""
    yielding_mib: int = 0
    """Their memory (part of `memory_mib`)."""

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.vms + other.vms,
            self.memory_mib + other.memory_mib,
            self.yielding_vms + other.yielding_vms,
            self.yielding_mib + other.yielding_mib,
        )


NO_USAGE = Usage()


@dataclass(frozen=True)
class Capacity:
    budget_mib: int
    used_mib: int
    slots_total: int
    slots_free: int
    pool_ready: int
    other_vms: int = 0
    """VMs of other runner areas (`work_vms_running`)."""
    slots_yielding: int = 0
    """Slots of `slots_free` that VMs of other areas still hold but give back on demand (work VMs, ADR-059 addendum 4):
    a workgroup VM may take them after `RECLAIM`; a speculative start (the pool) uses `slots_free_now`."""

    @property
    def slots_free_now(self) -> int:
        """Free slots without asking another area to give room back."""
        return max(self.slots_free - self.slots_yielding, 0)

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
    max_vms: int = 0,
    other: Usage = NO_USAGE,
) -> Capacity:
    """Capacity from the VM rows `(state, memory_mib)` plus `in_flight` VMs being created for the pool, at most
    `max_vms` slots (0: the budget alone), less the slots of `other` areas."""
    budget = max(physical_mib - reserve_mib, 0)
    total = budget // vm_memory_mib
    if max_vms:
        total = min(total, max_vms)
    occupying = [memory for state, memory in vms if state in SLOT_STATES]
    pool_ready = sum(1 for state, _ in vms if state == "pool")
    used = sum(occupying) + in_flight * vm_memory_mib + other.memory_mib
    other_slots = max(other.vms, -(-other.memory_mib // vm_memory_mib))
    firm = max(other.vms - other.yielding_vms, -(-(other.memory_mib - other.yielding_mib) // vm_memory_mib))
    free = max(total - len(occupying) - in_flight - firm, 0)
    now = max(total - len(occupying) - in_flight - other_slots, 0)
    return Capacity(
        budget_mib=budget,
        used_mib=used,
        slots_total=total,
        slots_free=free,
        pool_ready=pool_ready,
        other_vms=other.vms,
        slots_yielding=free - now,
    )


async def own_usage(_context: Any) -> Usage:
    """The VM area's own `USAGE`: nothing beyond its `runner.vm` rows, which `compute` counts itself."""
    return NO_USAGE


USAGE = own_usage


async def own_reclaim(_context: Any, _need_mib: int) -> int:
    """The VM area's own `RECLAIM`: its VMs are the ones the room is given back to."""
    return 0


RECLAIM = own_reclaim


def disk_free_mib(path: str, usage: Callable[[str], Any] = shutil.disk_usage) -> int | None:
    """Free MiB of the volume holding `path` (`$LIMA_HOME`), None when it cannot be read."""
    try:
        return int(usage(path).free) // MIB
    except OSError:
        return None


def physical_memory_mib(sysconf: Callable[[str], int] = os.sysconf) -> int:
    """Installed RAM of this machine in MiB."""
    return sysconf("SC_PHYS_PAGES") * sysconf("SC_PAGE_SIZE") // MIB


def cpu_load_per_core(
    loadavg: Callable[[], tuple[float, float, float]] = os.getloadavg, cpus: Callable[[], int | None] = os.cpu_count
) -> float:
    """The host's CPU load: the 1-minute load average per core (1.0 = every core busy; work VMs, ADR-059 addendum 4)."""
    return loadavg()[0] / (cpus() or 1)


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
