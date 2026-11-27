"""Room for one more task on a running workgroup VM, measured on the VM (05-runner.md section 3.3.1).

No fixed number of sessions per VM (decision of the client, 2026-11-26; the former `SESSIONS_PER_VM` is gone,
ADR-075 addendum 2). Before a **new task** is placed on a running VM, the runner measures that VM (HOT-32), read from
its guest (`GET /v1/metrics`, the same answer the monitoring samples, 08-monitoring.md section 2; 2 s). The new task
fits while one more task as large as the largest task on the VM now - its main session **and its subagents**
together, measured - still fits; that keeps the room the tasks already there need for their subagents (subagents run
in the VM of their task and are never refused for room, `subagents.limits`):

- RAM: `MemAvailable` of the VM (`mem_total_bytes - mem_used_bytes`) less what the largest task uses now (the sum of
  `memory.current` of its sessions that have not ended; sessions grouped by their task, `tasks`) still leaves
  `VM_FREE_FRACTION` of the VM's RAM for the guest itself. A VM without a measured session needs only that share.
- CPU: the VM's 1-minute load plus the share of the largest task (the load per open session times its sessions - the
  guest reports no CPU rate per session, so the load is shared evenly), per vCPU of the VM's **own** vCPUs
  (`runner.vm.cpus`), at most `VM_LOAD_MAX_PERCENT` (default 85 %). The finding of 2026-11-27 - 11 sessions, load 4.3
  on 4 vCPUs (108 % already before) - is full.

So the number of tasks a VM takes follows its fixed small size and what its tasks really use. A VM does not grow
(ADR-075 addendum 3); when no running VM has room, the order of `lifecycle` goes on - a stopped VM of the workgroup,
then a further VM on a free slot of the device (05-runner.md section 3.8), else `capacity_exhausted` and the hub
places the next attempt (ADR-013, ADR-049).

A VM whose guest does not answer within `GUEST_TIMEOUT_S` is **not measured** (`Room.measured` False): for a new task
that is no room (HOT-32: "first look whether there is room") - a guest that cannot answer its own metrics in 2 s is
the overloaded VM of the finding. The judgement is logged:
`runner.vm.session_room vm_id= fits=yes|no|unknown available_mib= need_mib= load_pct= after_pct=`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass
from typing import Any

from agent_runner.errors import RunnerError
from agent_runner.guestlink.errors import GuestError
from agent_runner.metrics import parsers
from agent_runner.metrics.parsers import GuestMetrics, ProbeError
from agent_runner.vm.records import VmRow

MIB = 1024 * 1024
VM_FREE_FRACTION = 0.1
"""RAM of the VM that stays free for the guest itself and short peaks."""
GUEST_TIMEOUT_S = 2.0
ENDED = frozenset({"stopped", "failed"})

TasksOf = Callable[[VmRow], Awaitable[Mapping[str, str]]]
"""The task of each session on a VM that has not ended (`records.session_tasks`)."""


@dataclass(frozen=True)
class Room:
    """The measured room of one running VM; `available_mib` None: not measured (its guest did not answer).
    `need_mib` is what the largest task uses, `load` the load per vCPU now, `after` with one more such task."""

    fits: bool
    available_mib: int | None = None
    need_mib: int | None = None
    load: float | None = None
    after: float | None = None

    @property
    def measured(self) -> bool:
        return self.available_mib is not None

    def fields(self) -> dict[str, str | int]:
        """Log fields of the judgement (`fits=unknown` without a measurement)."""
        if self.available_mib is None or self.need_mib is None or self.load is None:
            return {"fits": "unknown"}
        found: dict[str, str | int] = {
            "fits": "yes" if self.fits else "no",
            "available_mib": self.available_mib,
            "need_mib": self.need_mib,
            "load_pct": round(self.load * 100),
        }
        if self.after is not None:
            found["after_pct"] = round(self.after * 100)
        return found


UNMEASURED = Room(False)


def judge(metrics: GuestMetrics, cpus: int, load_max: float, tasks: Mapping[str, str] | None = None) -> Room:
    """Whether one more task as large as the largest task on the VM fits (module docstring); a session without a known
    task counts as a task of its own."""
    total, used = metrics.vm["mem_total_bytes"], metrics.vm["mem_used_bytes"]
    known = tasks or {}
    memory: dict[str, int] = {}
    count: dict[str, int] = {}
    for session in metrics.sessions:
        if session.state in ENDED:
            continue
        task = known.get(session.session_id, session.session_id)
        memory[task] = memory.get(task, 0) + (session.values.get("mem_current_bytes") or 0)
        count[task] = count.get(task, 0) + 1
    largest = max(memory, key=lambda task: (memory[task], count[task]), default=None)
    need = 0 if largest is None else memory[largest]
    share = 0.0 if largest is None else metrics.load1 / sum(count.values()) * count[largest]
    available = max(total - used, 0)
    vcpus = max(cpus, 1)
    load, after = metrics.load1 / vcpus, (metrics.load1 + share) / vcpus
    fits = available - need >= total * VM_FREE_FRACTION and after <= load_max
    return Room(fits, available // MIB, need // MIB, load, after)


async def measure(client_for: Callable[[VmRow], Awaitable[Any]], row: VmRow) -> GuestMetrics | None:
    """The guest's checked `GET /v1/metrics` (2 s); None on any failure."""
    try:
        client = await client_for(row)
    except (RunnerError, OSError):
        return None
    try:
        return parsers.parse_guest_metrics(await asyncio.wait_for(client.metrics(), GUEST_TIMEOUT_S))
    except (GuestError, TimeoutError, ProbeError):
        return None
    finally:
        await client.aclose()


def checker(env: Any, *, load_max: float, tasks_of: TasksOf) -> Callable[[VmRow], Coroutine[Any, Any, Room]]:
    """`VmEnv.session_room` of the daemon: measure the VM, judge it with its own vCPUs and the tasks of its sessions,
    log the judgement."""

    async def session_room(row: VmRow) -> Room:
        metrics = await measure(env.client, row)
        room = UNMEASURED if metrics is None else judge(metrics, row.cpus, load_max, await tasks_of(row))
        env.log("vm.session_room", {"vm_id": row.id, **room.fields()})
        return room

    return session_room
