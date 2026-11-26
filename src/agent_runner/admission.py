"""The one admission of the device for every kind of VM (05-runner.md section 3.8, 16-ci-runners.md section 6.6).

Every VM a device starts passes this admission: workgroup VMs with their sessions (`SESSION`), work, browser and test
VMs (`WORK`), and later the CI VMs of 16-ci-runners.md (`CI`), which plug into it without a second mechanism. It
admits only what is physically free - RAM above the host's base need (4 GiB measured, or `MEMORY_RESERVE_MIB`), each
VM with its host share (`vm.capacity.vm_footprint_mib`), the host's CPU load (about 1-2 cores stay free, or
`CPU_LOAD_MAX_PERCENT`), disk, at most `MACOS_VMS_MAX` macOS VMs per Mac - in one transaction: `lock` is held from the
check to the claim (the row in the store, or `pending` while a workgroup VM is being cloned), by every area.

There is no fixed number and no fixed share for any kind, CI included (decision of the client, 2026-11-26: "wenn es
nur ci runnerbedarf gibt, dann darf es auch 100% sein ... immer der von dem es am wenigsten aktive gibt"): a kind
alone on a device may fill it. When requests of several kinds compete, **the kind with the fewest active instances on
this device goes first**, on a tie the older request (`Ticket.since`). Active instances per kind come from the areas
(`counters`: the workgroup VMs that hold a slot, the work VMs that are not stopped). A request waits with a `Ticket`
(`waits`); a session the device refused for lack of capacity keeps a hold (`refused`, at most `REFUSAL_HOLD_S`, its age
from the first refusal) until the hub's next attempt starts here (`started`). A new request may start only with the
RAM that the waiting requests ahead of it leave (`ahead_mib`; reason `turn`), so freed room goes to them first. Within
one kind its own queue keeps the order (work VMs: FIFO); requests of one kind that carry a `group` (CI: the GitHub
organisation and class) follow the same rule between the groups (`counters` keyed by the group).

Running work is never ended by the admission. The verdict names the first reason that refuses (`cpu`, `memory`,
`disk`, `macos`, `turn`); the caller waits in its queue or answers `capacity_exhausted` (the hub places the session on
another device where it fits). The waiting requests are mirrored in `runner.workvm_wait` with their kind for
`agent-runner status` (`mirror`, display only).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import time
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from agent_runner.vm.capacity import cpu_load_per_core, host_cpu_ratio

SESSION = "session"
WORK = "work"
CI = "ci"
KINDS = (SESSION, WORK, CI)
MACOS_VMS_MAX = 2
"""Apple allows at most two macOS VMs per Mac (macOS licence, Virtualization.framework)."""
REFUSAL_HOLD_S = 120.0
"""How long a session refused for lack of capacity keeps its place (the hub retries it)."""
ATTRIBUTE = "_admission"


@dataclass(frozen=True)
class Demand:
    """What one new VM needs; `group` orders requests within a kind (CI: organisation and class)."""

    kind: str
    memory_mib: int
    disk_gib: int = 0
    macos: bool = False
    group: str | None = None


@dataclass(frozen=True)
class Device:
    """What the device has, measured by the caller inside the transaction."""

    free_mib: int
    """RAM above the host reserve that no VM holds (and no admitted VM in flight)."""
    disk_free_gib: int | None = None
    """Disk left for VMs (None: not bounded)."""
    macos_vms: int = 0


@dataclass(frozen=True)
class Verdict:
    admitted: bool
    reason: str | None = None


@dataclass
class Ticket:
    """A request that waits: its kind, the RAM it needs and since when it waits (the clock of the admission)."""

    kind: str
    memory_mib: int
    since: float
    group: str | None = None
    ticket: str = field(default_factory=lambda: secrets.token_hex(8))
    until: float | None = None
    """A hold (a refused session): it ends then unless the next attempt comes back."""


async def host_cpu_load() -> float:
    return cpu_load_per_core()


def _nothing() -> int:
    return 0


class Admission:
    def __init__(
        self,
        *,
        cpu_load: Callable[[], Awaitable[float]] = host_cpu_load,
        cpu_load_max: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.lock = asyncio.Lock()
        """The one transaction of the device: held from a check to its claim by every area."""
        self.cpu_load = cpu_load
        self.cpu_load_max = cpu_load_max
        self.clock = clock
        self.tickets: list[Ticket] = []
        self.counters: dict[str, Callable[[], Awaitable[int]]] = {}
        """Active instances per kind (or group) on this device, registered by the areas."""
        self.pending: Callable[[], int] = _nothing
        """MiB admitted for workgroup VMs that are not in the store yet (clones in flight; set by the VM manager)."""
        self.store: Any = None
        """The store for the mirror of the waiting requests (set by the VM area); None: no mirror."""

    # -- the waiting requests --------------------------------------------------------------------------------------

    def live(self) -> list[Ticket]:
        """The waiting requests; holds that ran out are dropped (their mirror rows go with `mirror`)."""
        now = self.clock()
        gone = [ticket for ticket in self.tickets if ticket.until is not None and ticket.until <= now]
        for ticket in gone:
            self.tickets.remove(ticket)
        return list(self.tickets)

    @contextlib.asynccontextmanager
    async def waits(self, kind: str, memory_mib: int, *, group: str | None = None) -> AsyncIterator[Ticket]:
        """A request of `kind` waits while inside; the ticket says since when (module docstring)."""
        ticket = Ticket(kind, memory_mib, self.clock(), group)
        self.tickets.append(ticket)
        try:
            yield ticket
        finally:
            self.tickets.remove(ticket)

    def hold(self, kind: str) -> Ticket | None:
        """The hold of a refused request of `kind`, if it still runs."""
        return next((ticket for ticket in self.live() if ticket.kind == kind and ticket.until is not None), None)

    async def refused(self, kind: str, memory_mib: int) -> None:
        """A request of `kind` was refused for lack of capacity; its next attempt keeps the age of the first."""
        found = self.hold(kind)
        if found is None:
            found = Ticket(kind, memory_mib, self.clock())
            self.tickets.append(found)
        found.until = self.clock() + REFUSAL_HOLD_S
        await self.mirror()

    async def started(self, kind: str) -> None:
        found = self.hold(kind)
        if found is not None:
            self.tickets.remove(found)
            await self.mirror()

    async def mirror(self) -> None:
        """Mirror the holds in `runner.workvm_wait` (kind, since, until) for the status; failures are ignored."""
        if self.store is None:
            return
        now = self.clock()
        wall = datetime.now(UTC)
        holds = [ticket for ticket in self.live() if ticket.until is not None]
        rows = [
            (
                ticket.ticket,
                ticket.kind,
                wall - timedelta(seconds=now - ticket.since),
                wall + timedelta(seconds=(ticket.until or now) - now),
            )
            for ticket in holds
        ]
        with contextlib.suppress(Exception):
            async with self.store.connection() as conn:
                await conn.execute("DELETE FROM runner.workvm_wait WHERE until IS NOT NULL")
                for row in rows:
                    await conn.execute(
                        "INSERT INTO runner.workvm_wait (ticket, kind, since, until) VALUES (%s, %s, %s, %s)", row
                    )

    # -- the decision --------------------------------------------------------------------------------------------------

    async def active(self) -> Counter[str]:
        """Active instances per registered kind or group."""
        return Counter({key: await count() for key, count in self.counters.items()})

    async def waiting(self) -> Counter[str]:
        """Waiting requests per kind."""
        return Counter(ticket.kind for ticket in self.live())

    async def ahead_mib(self, demand: Demand, own: Ticket | None = None) -> int:
        """The RAM of the waiting requests that go before `demand` (fewest active first, then the older)."""
        since = own.since if own is not None else self.clock()
        active = await self.active()
        ahead = 0
        for ticket in self.live():
            if ticket is own:
                continue
            if ticket.kind != demand.kind:
                first = (active[ticket.kind], ticket.since) < (active[demand.kind], since)
            elif ticket.group is not None and demand.group is not None and ticket.group != demand.group:
                first = (active[ticket.group], ticket.since) < (active[demand.group], since)
            else:
                first = False  # the same kind and group: its own queue keeps the order
            ahead += ticket.memory_mib if first else 0
        return ahead

    async def cpu_ok(self) -> bool:
        return await self.cpu_load() <= self.cpu_load_max

    async def judge(self, demand: Demand, device: Device, own: Ticket | None = None) -> Verdict:
        """Whether `demand` may start now on `device` (module docstring); call it under `lock`."""
        if not await self.cpu_ok():
            return Verdict(False, "cpu")
        short = self.short(demand, device)
        if short is not None:
            return Verdict(False, short)
        if device.free_mib - await self.ahead_mib(demand, own) < demand.memory_mib:
            return Verdict(False, "turn")
        return Verdict(True)

    @staticmethod
    def short(demand: Demand, device: Device) -> str | None:
        """The first resource `demand` lacks on `device` (`memory`, `disk`, `macos`), None when it fits."""
        if device.free_mib < demand.memory_mib:
            return "memory"
        if device.disk_free_gib is not None and device.disk_free_gib < demand.disk_gib:
            return "disk"
        if demand.macos and device.macos_vms >= MACOS_VMS_MAX:
            return "macos"
        return None

    async def summary(self) -> Mapping[str, tuple[int, int]]:
        """`{kind: (active, waiting)}` for every kind (status and metrics)."""
        active, waiting = await self.active(), await self.waiting()
        return {kind: (active[kind], waiting[kind]) for kind in KINDS}


def cpu_load_max(percent: int | None, cores: Callable[[], int | None] = os.cpu_count) -> float:
    """The load per core up to which new VMs start: `CPU_LOAD_MAX_PERCENT` when set, else about 1-2 cores stay free
    for macOS, the runner and the VMs' input and output (`host_cpu_ratio`, no fixed reserve)."""
    return percent / 100 if percent is not None else host_cpu_ratio(cores())


def of(context: Any) -> Admission:
    """The device's one admission (created once per daemon context, `cpu_load_max` of its configuration)."""
    found = vars(context).get(ATTRIBUTE)
    if isinstance(found, Admission):
        return found
    created = Admission(cpu_load_max=cpu_load_max(context.config.cpu_load_max_percent))
    vars(context)[ATTRIBUTE] = created
    return created
