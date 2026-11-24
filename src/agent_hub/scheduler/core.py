"""The deterministic scheduler (03-api.md section 4.6.2, ADR-002, ADR-009, ADR-013).

`choose(candidates)` picks the device for a new session from the devices assigned to the workgroup:

- Only **online** candidates count; without one the answer is `NoPlacement("device_offline")`.
- A device with `vm_slots_free = 0` and no VM of the workgroup is skipped. After a device **refused** a session of the
  workgroup for lack of capacity (`Candidate.refused`, KOR-14, ADR-049) a stopped or starting VM of the workgroup no
  longer exempts it: it needs `vm_slots_free > 0` (restarting the VM takes a slot), only a `running` VM of the
  workgroup needs none. When every online device is skipped the answer is `NoPlacement("capacity_exhausted")`.
- The rest is ordered by (1) a `running` VM of the workgroup, (2) a `stopped` VM of the workgroup, (3) the highest
  `capacity.vm_slots_free`, then the highest free VM memory budget (`vm_memory_budget_mib - vm_memory_used_mib`),
  (4) the smallest device ID; the first one wins. The levels are one lexicographic key, so (3) and (4) also break
  ties between devices of the same level (1) or (2).
- Estimates: level (1) 2500 ms, (2) 6500 ms, otherwise 3000 ms with `pool_ready > 0` and 8000 ms without.

The same inputs always give the same device (no clock, no randomness, no dictionary order). `Capacity.low` is the
capacity warning of every device list: fewer than 10 % of the VM slots are free (a device without slots is low).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

ESTIMATE_RUNNING_MS = 2500
ESTIMATE_STOPPED_MS = 6500
ESTIMATE_POOL_MS = 3000
ESTIMATE_CLONE_MS = 8000
DEVICE_OFFLINE = "device_offline"
CAPACITY_EXHAUSTED = "capacity_exhausted"
RUNNING, STOPPED, OTHER = "running", "stopped", "other"
CAPACITY_KEYS = ("vm_memory_budget_mib", "vm_memory_used_mib", "vm_slots_total", "vm_slots_free", "pool_ready")
MAX_COUNT = 9007199254740991
"""The largest `count` of the contracts (2^53 - 1)."""


def _count(value: object) -> int:
    """A reported counter as a non-negative integer (anything else counts as 0)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return min(max(value, 0), MAX_COUNT)


@dataclass(frozen=True)
class Capacity:
    """The capacity a device reports (02-data-model.md section 2.1, `device.capacity`)."""

    vm_memory_budget_mib: int = 0
    vm_memory_used_mib: int = 0
    vm_slots_total: int = 0
    vm_slots_free: int = 0
    pool_ready: int = 0

    @classmethod
    def of(cls, value: Mapping[str, Any] | None) -> Capacity:
        """The capacity of a stored `device.capacity` (missing or malformed counters are 0)."""
        found = value if isinstance(value, Mapping) else {}
        return cls(**{key: _count(found.get(key)) for key in CAPACITY_KEYS})

    @property
    def free_memory_mib(self) -> int:
        """The free part of the VM memory budget."""
        return max(self.vm_memory_budget_mib - self.vm_memory_used_mib, 0)

    @property
    def low(self) -> bool:
        """`vm_slots_free / vm_slots_total < 0.1` (exact integer form); a device without slots is low."""
        return self.vm_slots_total == 0 or self.vm_slots_free * 10 < self.vm_slots_total

    def json(self) -> dict[str, int]:
        """The capacity object of the API."""
        return {key: getattr(self, key) for key in CAPACITY_KEYS}


@dataclass(frozen=True)
class Candidate:
    """A device assigned to the workgroup: presence, capacity and the state of its VM of the workgroup."""

    device_id: str
    online: bool
    capacity: Capacity
    wg_vm: str | None = None
    """`running`, `stopped`, `other` (any other state but `deleted`) or None (no VM of the workgroup)."""
    refused: bool = False
    """The newest session of the workgroup on this device was refused with `capacity_exhausted`."""

    @property
    def level(self) -> int:
        """1 with a running VM of the workgroup, 2 with a stopped one, else 3."""
        return 1 if self.wg_vm == RUNNING else 2 if self.wg_vm == STOPPED else 3

    @property
    def usable(self) -> bool:
        """Online and not skipped (free slots, or a VM of the workgroup that needs none; after a refusal only a running
        one)."""
        if not self.online or self.capacity.vm_slots_free > 0 or self.wg_vm == RUNNING:
            return self.online
        return self.wg_vm is not None and not self.refused

    def key(self) -> tuple[int, int, int, str]:
        """The ordering key of section 4.6.2 (smallest first)."""
        return (self.level, -self.capacity.vm_slots_free, -self.capacity.free_memory_mib, self.device_id)

    @property
    def estimate_ms(self) -> int:
        """The countdown estimate when this device is chosen."""
        if self.level == 1:
            return ESTIMATE_RUNNING_MS
        if self.level == 2:
            return ESTIMATE_STOPPED_MS
        return ESTIMATE_POOL_MS if self.capacity.pool_ready > 0 else ESTIMATE_CLONE_MS


@dataclass(frozen=True)
class Placement:
    """The chosen device and the countdown estimate."""

    device_id: str
    estimate_ms: int


@dataclass(frozen=True)
class NoPlacement:
    """No device: the reason shown to the user (`device_offline` or `capacity_exhausted`)."""

    reason: str


Decision = Placement | NoPlacement


def order(candidates: Iterable[Candidate]) -> list[Candidate]:
    """The usable candidates in scheduler order."""
    return sorted((item for item in candidates if item.usable), key=Candidate.key)


def choose(candidates: Iterable[Candidate]) -> Decision:
    """The placement of a new session (module docstring)."""
    items = list(candidates)
    if not any(item.online for item in items):
        return NoPlacement(DEVICE_OFFLINE)
    ranked = order(items)
    if not ranked:
        return NoPlacement(CAPACITY_EXHAUSTED)
    return Placement(device_id=ranked[0].device_id, estimate_ms=ranked[0].estimate_ms)
