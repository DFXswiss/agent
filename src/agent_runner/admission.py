"""The one admission of the device for every kind of VM (05-runner.md section 3.8, 16-ci-runners.md section 6.6).

Every VM a device starts passes this admission: workgroup VMs with their sessions, work, browser and test VMs, and
later the CI VMs of 16-ci-runners.md, which plug into it without a second mechanism. It decides by what is physically
free - RAM above the host reserve (`MEMORY_RESERVE_MIB`), the host's CPU load (`CPU_LOAD_MAX_PERCENT`) and disk - in one
transaction: `lock` is held from the check to the claim (the row in the store, or `pending` while a workgroup VM is
being cloned), by every area. There is no fixed number and no fixed share for the kinds of agent (ADR-075 addendum 2):

- **Rank** (`RANK`): sessions (workgroup VMs) before work, browser and test VMs (`WORK`) before CI. A new request of a
  lower rank does not start while one of a higher rank waits (`waits`, or a session refused for lack of capacity
  within `REFUSAL_HOLD_S`, `refused`). Running work is never ended silently by the admission: a VM of a lower rank
  gives way only when it is idle (`workvm.WorkVms.give_way`).
- **Bounds per kind** (`caps`): only for CI (16 section 6.6: its share of the device's RAM and CPUs, `MAX_SHARE`); the
  kinds of agent have none.
- **macOS VMs**: at most `MACOS_VMS_MAX` per Mac - Apple's limit of the licence and the virtualization framework, the
  one fixed number (only CI runs macOS VMs).

The verdict names the first reason that refuses (`rank`, `cpu`, `memory`, `disk`, `cap`, `macos`); the caller waits in
its queue or answers `capacity_exhausted` (the hub places the session on another device).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from agent_runner.vm.capacity import cpu_load_per_core

SESSION = "session"
WORK = "work"
CI = "ci"
RANK: Mapping[str, int] = {SESSION: 0, WORK: 1, CI: 2}
MACOS_VMS_MAX = 2
"""Apple allows at most two macOS VMs per Mac (macOS licence, Virtualization.framework)."""
REFUSAL_HOLD_S = 120.0
"""How long a session refused for lack of capacity keeps lower ranks from starting (the hub retries it)."""
ATTRIBUTE = "_admission"


@dataclass(frozen=True)
class Cap:
    """The bound of one kind (only CI): its share of the device's RAM and CPUs."""

    memory_share: float
    cpu_share: float


@dataclass(frozen=True)
class Demand:
    """What one new VM needs."""

    kind: str
    memory_mib: int
    cpus: int = 0
    disk_gib: int = 0
    macos: bool = False


@dataclass(frozen=True)
class Device:
    """What the device has, measured by the caller inside the transaction."""

    free_mib: int
    """RAM above the host reserve that no VM holds (and no admitted VM in flight)."""
    budget_mib: int = 0
    """RAM above the host reserve (the base of a kind's share)."""
    cpus: int = 0
    """Cores of the host (the base of a kind's share)."""
    disk_free_gib: int | None = None
    """Disk left for VMs (None: not bounded)."""
    held_mib: Mapping[str, int] = field(default_factory=dict)
    held_cpus: Mapping[str, int] = field(default_factory=dict)
    macos_vms: int = 0


@dataclass(frozen=True)
class Verdict:
    admitted: bool
    reason: str | None = None


async def host_cpu_load() -> float:
    return cpu_load_per_core()


class Admission:
    def __init__(
        self,
        *,
        cpu_load: Callable[[], Awaitable[float]] = host_cpu_load,
        cpu_load_max: float = 1.0,
        caps: Mapping[str, Cap] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.lock = asyncio.Lock()
        """The one transaction of the device: held from a check to its claim by every area."""
        self.cpu_load = cpu_load
        self.cpu_load_max = cpu_load_max
        self.caps: dict[str, Cap] = dict(caps or {})
        self.clock = clock
        self.waiting: Counter[str] = Counter()
        self.refused_until: dict[str, float] = {}
        self.pending: Callable[[], int] = _nothing
        """MiB admitted for workgroup VMs that are not in the store yet (clones in flight; set by the VM manager)."""

    @contextlib.contextmanager
    def waits(self, kind: str) -> Iterator[None]:
        """A request of `kind` waits while inside (lower ranks do not start meanwhile)."""
        self.waiting[kind] += 1
        try:
            yield
        finally:
            self.waiting -= Counter({kind: 1})

    def refused(self, kind: str) -> None:
        """A request of `kind` was refused for lack of capacity; it is retried (the hub places it again)."""
        self.refused_until[kind] = self.clock() + REFUSAL_HOLD_S

    def started(self, kind: str) -> None:
        self.refused_until.pop(kind, None)

    def ahead(self, kind: str) -> str | None:
        """A kind of a higher rank that waits now, else None."""
        now = self.clock()
        for other, rank in RANK.items():
            if rank < RANK[kind] and (self.waiting[other] or self.refused_until.get(other, 0.0) > now):
                return other
        return None

    async def cpu_ok(self) -> bool:
        return await self.cpu_load() <= self.cpu_load_max

    async def judge(self, demand: Demand, device: Device) -> Verdict:
        """Whether `demand` may start now on `device` (module docstring); call it under `lock`."""
        if self.ahead(demand.kind) is not None:
            return Verdict(False, "rank")
        if not await self.cpu_ok():
            return Verdict(False, "cpu")
        short = self.short(demand, device)
        return Verdict(False, short) if short is not None else Verdict(True)

    def short(self, demand: Demand, device: Device) -> str | None:
        """The first resource `demand` lacks on `device` (`memory`, `disk`, `cap`, `macos`), None when it fits."""
        if device.free_mib < demand.memory_mib:
            return "memory"
        if device.disk_free_gib is not None and device.disk_free_gib < demand.disk_gib:
            return "disk"
        cap = self.caps.get(demand.kind)
        if cap is not None and (
            device.held_mib.get(demand.kind, 0) + demand.memory_mib > cap.memory_share * device.budget_mib
            or device.held_cpus.get(demand.kind, 0) + demand.cpus > cap.cpu_share * device.cpus
        ):
            return "cap"
        if demand.macos and device.macos_vms >= MACOS_VMS_MAX:
            return "macos"
        return None


def _nothing() -> int:
    return 0


def of(context: Any) -> Admission:
    """The device's one admission (created once per daemon context, `CPU_LOAD_MAX_PERCENT` of its configuration)."""
    found = vars(context).get(ATTRIBUTE)
    if isinstance(found, Admission):
        return found
    created = Admission(cpu_load_max=context.config.cpu_load_max_percent / 100)
    vars(context)[ATTRIBUTE] = created
    return created
