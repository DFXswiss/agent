"""The deterministic scheduler (03-api.md section 4.6.2, ADR-002, ADR-009, ADR-013, ADR-049, ADR-060).

`choose(candidates, vm_allowance=…, pinned=…)` picks the device for a new session from the devices assigned to the
workgroup (directly or through the organization). For each candidate the hub knows the workgroup's live VMs on that
device with their occupying sessions (`Vm`) and the sessions of the workgroup placed there without a VM yet
(`pending`). The *room* of a VM is `capacity.sessions_per_vm` minus its sessions (unbounded when the device reports no
limit, `sessions_per_vm = 0`). Pending sessions use the room of the running VMs first, then of VMs in another state,
then of stopped ones; what is left needs new VMs (one per `sessions_per_vm` sessions, a single one without a limit),
each taking a free slot and one VM of the workgroup's allowance.

- Only **online** candidates count; without one the answer is `NoPlacement("device_offline")`.
- Level 1: a running VM of the workgroup with room left. Level 2: a stopped VM with room (it needs a free slot only
  after the device **refused** a session of the workgroup, `Candidate.refused`, KOR-14, ADR-049). Level 3: a VM in
  another state with room (the same refusal rule), else a new VM: a ready pool VM or a free slot (after the pending
  sessions' new VMs),
  the workgroup's allowance of new VMs not used up (`vm_allowance`, from `AGENT_HUB_VMS_PER_WG`; None = no limit) and,
  when the device reports `disk_free_mib`, at least `NEW_VM_DISK_MIB` free. Otherwise the device is skipped.
- A device assigned only through the organization whose wrap of the workgroup's current key is missing
  (`Candidate.keys` False) is skipped as well (it could not open the task).
- The rest is ordered by (level, most free slots, most free VM memory budget, smallest device ID); the first one
  wins. Estimates: level 1 2500 ms, level 2 6500 ms, level 3 3000 ms with `pool_ready > 0` and 8000 ms without.
- Nothing usable: `capacity_exhausted`, or `wg_vm_limit` when a device had a free slot but the workgroup's allowance
  was used up, or `keys_pending` when only devices without the key had room. With a chosen device (`pinned`) the
  reasons are `placement_offline` and `placement_full`; with a chosen VM (`Candidate.pinned_vm`) only that VM's room
  counts.

The same inputs always give the same device (no clock, no randomness, no dictionary order). `Capacity.low` is the
capacity warning of every device list: fewer than 10 % of the VM slots are free (a device without slots is low).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

ESTIMATE_RUNNING_MS = 2500
ESTIMATE_STOPPED_MS = 6500
ESTIMATE_POOL_MS = 3000
ESTIMATE_CLONE_MS = 8000
NEW_VM_DISK_MIB = 20480
"""Free disk a new VM needs when the device reports `disk_free_mib` (the default `VM_DISK_GIB` of 20 GiB)."""
DEVICE_OFFLINE = "device_offline"
CAPACITY_EXHAUSTED = "capacity_exhausted"
WG_VM_LIMIT = "wg_vm_limit"
KEYS_PENDING = "keys_pending"
PLACEMENT_OFFLINE = "placement_offline"
PLACEMENT_FULL = "placement_full"
NO_DEVICE = "no_device"
PLATFORM_ACCESS_REQUIRED = "platform_access_required"
RUNNING, STOPPED, OTHER = "running", "stopped", "other"
CAPACITY_KEYS = ("vm_memory_budget_mib", "vm_memory_used_mib", "vm_slots_total", "vm_slots_free", "pool_ready")
OPTIONAL_KEYS = (
    "cpus",
    "memory_total_mib",
    "disk_free_mib",
    "vms_running",
    "sessions_active",
    "work_vms_running",
    "sessions_per_vm",
    "vms_max",
)
MAX_COUNT = 9007199254740991
"""The largest `count` of the contracts (2^53 - 1)."""
UNBOUNDED = MAX_COUNT
WAITING_FOR_CAPACITY = "waiting_for_capacity"
WAITING_FOR_DEVICE = "waiting_for_device"
CATEGORIES: Mapping[str, str] = {
    CAPACITY_EXHAUSTED: WAITING_FOR_CAPACITY,
    WG_VM_LIMIT: WAITING_FOR_CAPACITY,
    PLACEMENT_FULL: WAITING_FOR_CAPACITY,
    DEVICE_OFFLINE: WAITING_FOR_DEVICE,
    PLACEMENT_OFFLINE: WAITING_FOR_DEVICE,
    KEYS_PENDING: WAITING_FOR_DEVICE,
    NO_DEVICE: WAITING_FOR_DEVICE,
    PLATFORM_ACCESS_REQUIRED: WAITING_FOR_DEVICE,
}
"""Waiting reason -> category of the situation view (contracts/enums/task-states.json `scheduling_categories`)."""


def _count(value: object) -> int:
    """A reported counter as a non-negative integer (anything else counts as 0)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return min(max(value, 0), MAX_COUNT)


@dataclass(frozen=True)
class Capacity:
    """The capacity a device reports (02-data-model.md section 2.1, `device.capacity`; ADR-060 section 2)."""

    vm_memory_budget_mib: int = 0
    vm_memory_used_mib: int = 0
    vm_slots_total: int = 0
    vm_slots_free: int = 0
    pool_ready: int = 0
    sessions_per_vm: int = 0
    """Occupying sessions per VM; 0 = no limit (also for a runner that does not report it)."""
    disk_free_mib: int | None = None
    optional: Mapping[str, int] = field(default_factory=dict)
    """The reported optional counters and settings (for the device lists)."""
    features: tuple[str, ...] = ()

    @classmethod
    def of(cls, value: Mapping[str, Any] | None) -> Capacity:
        """The capacity of a stored `device.capacity` (missing or malformed counters are 0)."""
        found = value if isinstance(value, Mapping) else {}
        optional = {key: _count(found[key]) for key in OPTIONAL_KEYS if key in found}
        features = found.get("features")
        return cls(
            **{key: _count(found.get(key)) for key in CAPACITY_KEYS},
            sessions_per_vm=optional.get("sessions_per_vm", 0),
            disk_free_mib=optional.get("disk_free_mib"),
            optional=optional,
            features=tuple(item for item in features if isinstance(item, str)) if isinstance(features, list) else (),
        )

    @property
    def free_memory_mib(self) -> int:
        """The free part of the VM memory budget."""
        return max(self.vm_memory_budget_mib - self.vm_memory_used_mib, 0)

    @property
    def low(self) -> bool:
        """`vm_slots_free / vm_slots_total < 0.1` (exact integer form); a device without slots is low."""
        return self.vm_slots_total == 0 or self.vm_slots_free * 10 < self.vm_slots_total

    @property
    def disk_ok(self) -> bool:
        """Room on the disk for a new VM (always when the device does not report its free disk)."""
        return self.disk_free_mib is None or self.disk_free_mib >= NEW_VM_DISK_MIB

    def room(self, sessions: int) -> int:
        """The room of a VM with `sessions` occupying sessions."""
        return UNBOUNDED if self.sessions_per_vm == 0 else max(self.sessions_per_vm - sessions, 0)

    def json(self) -> dict[str, Any]:
        """The capacity object of the API (the optional counters and the features only when reported)."""
        found: dict[str, Any] = {key: getattr(self, key) for key in CAPACITY_KEYS}
        found.update(self.optional)
        if self.features:
            found["features"] = list(self.features)
        return found


@dataclass(frozen=True)
class Vm:
    """A live VM of the workgroup on a candidate: `running`, `stopped` or `other`, and its occupying sessions."""

    vm_id: str
    state: str
    sessions: int = 0


@dataclass(frozen=True)
class Fit:
    """How a candidate can take one more session: its level (1-3, 0 = not at all), whether that needs a new VM, the
    free slots after the pending sessions' new VMs, and the new VMs the pending sessions need."""

    level: int
    new_vm: bool
    slots_free: int
    pending_new_vms: int
    blocked_by_allowance: bool = False


_ORDER = {RUNNING: 0, OTHER: 1, STOPPED: 2}


@dataclass(frozen=True)
class Candidate:
    """A device assigned to the workgroup: presence, capacity, the workgroup's VMs there and its pending sessions."""

    device_id: str
    online: bool
    capacity: Capacity
    vms: tuple[Vm, ...] = ()
    pending: int = 0
    """Sessions of the workgroup placed on this device that have no VM yet."""
    refused: bool = False
    """The newest session of the workgroup on this device was refused with `capacity_exhausted`."""
    keys: bool = True
    """False for a device assigned only through the organization that lacks a wrap it needs (workgroup, org or owner
    credential key)."""
    org_only: bool = False
    """Assigned only through the organization (its keys come by catch-up)."""
    pinned_vm: str | None = None
    """A chosen VM (only its room counts); None when no VM is chosen or the chosen one is gone."""

    def considered(self) -> list[Vm]:
        """The VMs whose room counts, in the order pending sessions use it (running, other, stopped; then by ID)."""
        found = [vm for vm in self.vms if self.pinned_vm is None or vm.vm_id == self.pinned_vm]
        return sorted(found, key=lambda vm: (_ORDER[vm.state], vm.vm_id))

    def fit(self, allowance: int | None) -> Fit:
        """The fit of one more session (module docstring); `allowance` = new VMs the workgroup may still create."""
        capacity = self.capacity
        remaining = self.pending
        left: dict[str, int] = {RUNNING: 0, OTHER: 0, STOPPED: 0}
        for vm in self.considered():
            room = capacity.room(vm.sessions)
            used = min(room, remaining)
            remaining -= used
            left[vm.state] += room - used
        pending_new = 0
        if remaining > 0 and self.pinned_vm is None:
            per_vm = capacity.sessions_per_vm
            pending_new = 1 if per_vm == 0 else -(-remaining // per_vm)
            left[OTHER] += UNBOUNDED if per_vm == 0 else pending_new * per_vm - remaining
        slots = capacity.vm_slots_free - pending_new
        exempt = not self.refused
        if left[RUNNING] > 0:
            return Fit(1, False, slots, pending_new)
        if left[STOPPED] > 0 and (exempt or slots > 0):
            return Fit(2, False, slots, pending_new)
        if left[OTHER] > 0 and (exempt or slots > 0):
            return Fit(3, False, slots, pending_new)
        # a new VM takes a ready pool VM (no further slot) or a free slot
        if self.pinned_vm is not None or slots + capacity.pool_ready <= 0 or not capacity.disk_ok:
            return Fit(0, False, slots, pending_new)
        if allowance is not None and allowance <= 0:
            return Fit(0, False, slots, pending_new, blocked_by_allowance=True)
        return Fit(3, True, slots, pending_new)

    def estimate_ms(self, fit: Fit) -> int:
        """The countdown estimate when this device is chosen with `fit`."""
        if fit.level == 1:
            return ESTIMATE_RUNNING_MS
        if fit.level == 2:
            return ESTIMATE_STOPPED_MS
        return ESTIMATE_POOL_MS if self.capacity.pool_ready > 0 else ESTIMATE_CLONE_MS


@dataclass(frozen=True)
class Placement:
    """The chosen device and the countdown estimate."""

    device_id: str
    estimate_ms: int


@dataclass(frozen=True)
class NoPlacement:
    """No device: the reason shown to the user (03-api.md section 4.6.2)."""

    reason: str


Decision = Placement | NoPlacement


def allowance_left(candidates: Iterable[Candidate], vm_allowance: int | None) -> int | None:
    """The new VMs the workgroup may still create after the VMs its pending sessions need."""
    if vm_allowance is None:
        return None
    return vm_allowance - sum(item.fit(None).pending_new_vms for item in candidates if item.online)


def ranked(candidates: Iterable[Candidate], vm_allowance: int | None = None) -> list[tuple[Candidate, Fit]]:
    """The usable candidates with their fit, in scheduler order."""
    items = list(candidates)
    allowance = allowance_left(items, vm_allowance)
    found = [(item, item.fit(allowance)) for item in items if item.online and item.keys]
    usable = [(item, fit) for item, fit in found if fit.level > 0]
    return sorted(
        usable,
        key=lambda pair: (pair[1].level, -pair[1].slots_free, -pair[0].capacity.free_memory_mib, pair[0].device_id),
    )


def order(candidates: Iterable[Candidate], vm_allowance: int | None = None) -> list[Candidate]:
    """The usable candidates in scheduler order."""
    return [item for item, _fit in ranked(candidates, vm_allowance)]


def _reason(items: list[Candidate], vm_allowance: int | None, *, pinned: bool) -> str:
    online = [item for item in items if item.online]
    if not online:
        return PLACEMENT_OFFLINE if pinned else DEVICE_OFFLINE
    if pinned:
        return KEYS_PENDING if not any(item.keys for item in online) else PLACEMENT_FULL
    allowance = allowance_left(items, vm_allowance)
    if any(item.keys and item.fit(allowance).blocked_by_allowance for item in online):
        return WG_VM_LIMIT
    if any(not item.keys and item.fit(allowance).level > 0 for item in online):
        return KEYS_PENDING
    return CAPACITY_EXHAUSTED


def choose(candidates: Iterable[Candidate], vm_allowance: int | None = None, *, pinned: bool = False) -> Decision:
    """The placement of a new session (module docstring)."""
    items = list(candidates)
    found = ranked(items, vm_allowance)
    if not found:
        return NoPlacement(_reason(items, vm_allowance, pinned=pinned))
    item, fit = found[0]
    return Placement(device_id=item.device_id, estimate_ms=item.estimate_ms(fit))
