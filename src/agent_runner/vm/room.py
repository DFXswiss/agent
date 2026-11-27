"""Room for one more session on a running workgroup VM, measured on the VM (05-runner.md section 3.3 step 2).

No fixed number of sessions per VM (decision of the client, 2026-11-26; the former `SESSIONS_PER_VM` is gone,
ADR-075 addendum 2). Before a session is placed on a running VM, the runner measures that VM (HOT-32): it takes one
more session while its own free resources last, read from its guest (`GET /v1/metrics`, the same answer the monitoring
samples, 08-monitoring.md section 2; 2 s):

- RAM: `MemAvailable` of the VM (`mem_total_bytes - mem_used_bytes`) less what one more session needs still leaves
  `VM_FREE_FRACTION` of the VM's RAM for the guest itself. What one more session needs is **measured**, no fixed
  number: the largest `memory.current` among the VM's sessions that have not ended (a new session grows to what the
  others use; the measurement in the DFX workgroup VM on 2026-11-26: 350-510 MB per session at about 3 % of a CPU).
  A VM without a measured session needs only the guest's own share;
- CPU: the VM's 1-minute load per vCPU (`load1` / the VM's **own** vCPUs, `runner.vm.cpus`: a VM of an older size
  keeps its own count) at most `VM_LOAD_MAX_PERCENT` (default 85 %). The finding of 2026-11-27 - 11 sessions, load 4.3
  on 4 vCPUs (108 %) - is full.

So the number of sessions a VM takes follows its fixed size and what its sessions really use (about 13-15 sessions of
about 0.5 GB in a VM of 8 GiB). A VM does not grow: Lima with Apple's Virtualization framework fixes a VM's RAM at its
start (`limactl edit` only on a stopped instance, no memory hot-plug), so growing would end the running sessions
(ADR-075 addendum 3). When no running VM has room, the existing order of `lifecycle` goes on instead - a stopped VM of
the workgroup, then a further VM of the workgroup on a free slot of the device (the device admission, 05-runner.md
section 3.8), else `capacity_exhausted` and the hub places the next attempt (ADR-013, ADR-049).

A VM whose guest does not answer within `GUEST_TIMEOUT_S` is **not measured** (`Room.measured` False): for a new
session that is no room (HOT-32: "first look whether there is room") - a guest that cannot answer its own metrics in
2 s is the overloaded VM of the finding, not a VM with room. The pre-check of a subagent on its parent's VM
(`subagents.limits`) refuses only a measured full VM. The judgement is logged:
`runner.vm.session_room vm_id= fits=yes|no|unknown available_mib= need_mib= load_pct=`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
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


@dataclass(frozen=True)
class Room:
    """The measured room of one running VM; `available_mib` None: not measured (its guest did not answer)."""

    fits: bool
    available_mib: int | None = None
    need_mib: int | None = None
    load: float | None = None

    @property
    def measured(self) -> bool:
        return self.available_mib is not None

    def fields(self) -> dict[str, str | int]:
        """Log fields of the judgement (`fits=unknown` without a measurement)."""
        if self.available_mib is None or self.need_mib is None or self.load is None:
            return {"fits": "unknown"}
        return {
            "fits": "yes" if self.fits else "no",
            "available_mib": self.available_mib,
            "need_mib": self.need_mib,
            "load_pct": round(self.load * 100),
        }


UNMEASURED = Room(False)


def judge(metrics: GuestMetrics, cpus: int, load_max: float) -> Room:
    """Whether one more session fits the VM measured in `metrics` (module docstring)."""
    total, used = metrics.vm["mem_total_bytes"], metrics.vm["mem_used_bytes"]
    current = [
        session.values.get("mem_current_bytes") or 0 for session in metrics.sessions if session.state not in ENDED
    ]
    need = max(current, default=0)  # measured: what the largest session of this VM uses now
    available = max(total - used, 0)
    load = metrics.load1 / max(cpus, 1)
    fits = available - need >= total * VM_FREE_FRACTION and load <= load_max
    return Room(fits, available // MIB, need // MIB, load)


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


def checker(env: Any, *, load_max: float) -> Callable[[VmRow], Coroutine[Any, Any, Room]]:
    """`VmEnv.session_room` of the daemon: measure the VM, judge it with its own vCPUs, log the judgement."""

    async def session_room(row: VmRow) -> Room:
        metrics = await measure(env.client, row)
        room = UNMEASURED if metrics is None else judge(metrics, row.cpus, load_max)
        env.log("vm.session_room", {"vm_id": row.id, **room.fields()})
        return room

    return session_room
