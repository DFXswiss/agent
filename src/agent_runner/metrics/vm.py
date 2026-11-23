"""VM values: host side of every running Lima instance and the guest's `GET /v1/metrics` (08-monitoring.md section 1,
VM column; Spike A `vm-metrics.sh`).

Host side, three commands for all instances together (so the cost does not grow with the number of VMs):

- `lsof -F pn <dir>/disk ...`: the processes holding each instance's disk file; the VZ process is the holder that is
  not the hostagent (`<dir>/ha.pid`). `lsof` exits 1 when one of the files has no holder; its output still counts.
- `ps -o pid=,%cpu=,rss=,etime= -p <VZ and hostagent PIDs>`: `cpu_ratio` = VZ `%cpu` / 100 (1.0 = one core),
  `memory_bytes` = VZ RSS + hostagent RSS (what the VM costs the device; the VZ footprint is a high-water mark),
  `uptime_seconds` = VZ elapsed time. `ps` exits 1 when one PID is gone; the others still count.
- `nettop -P -L 1 -x -J bytes_in,bytes_out`: bytes of the hostagent process (usernet runs inside it) as the VM's
  network counters.

`disk_used_bytes` are the allocated blocks of `<dir>/disk` (`stat`; a clone shares blocks with its golden, which
cannot be separated, Spike A).

Guest: `GET /v1/metrics` over the control socket with a 2 s limit (`GUEST_TIMEOUT_S`); every failure (no token, socket,
timeout, refusal, an answer outside the contract) is None and the VM's probe counts as failed.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from dataclasses import dataclass

from agent_runner.errors import RunnerError
from agent_runner.guestlink.errors import GuestError
from agent_runner.metrics import parsers
from agent_runner.metrics.parsers import GuestMetrics, ProbeError
from agent_runner.metrics.probes import LSOF, NETTOP, PS, Tools, probe
from agent_runner.vm.lifecycle import VmManager
from agent_runner.vm.lima import Instance
from agent_runner.vm.records import VmRow

GUEST_TIMEOUT_S = 2.0
NETTOP_ARGV = (NETTOP, "-P", "-L", "1", "-x", "-J", "bytes_in,bytes_out")
PS_FORMAT = "pid=,%cpu=,rss=,etime="
BLOCK = 512
ANY_EXIT_0_1 = frozenset({0, 1})


@dataclass(frozen=True)
class HostVm:
    vz_pid: int | None
    hostagent_pid: int | None
    cpu_ratio: float | None
    memory_bytes: int | None
    uptime_seconds: int | None
    disk_used_bytes: int | None
    net_rx_bytes: int | None
    net_tx_bytes: int | None


def _pid(text: str | None) -> int | None:
    value = (text or "").strip()
    return int(value) if value.isdigit() and int(value) > 0 else None


def _disk(instance: Instance) -> str:
    return os.path.join(instance.dir, "disk")


def _allocated(tools: Tools, path: str) -> int | None:
    try:
        return tools.stat(path).st_blocks * BLOCK
    except OSError:
        return None


async def host_values(tools: Tools, instances: Sequence[Instance]) -> dict[str, HostVm]:
    """Host-side values of the running `instances`, by Lima name."""
    running = [instance for instance in instances if instance.running]
    if not running:
        return {}
    agents = {instance.name: _pid(tools.read_text(os.path.join(instance.dir, "ha.pid"))) for instance in running}
    holders = await probe(tools, (LSOF, "-F", "pn", *(_disk(i) for i in running)), parsers.parse_lsof, ANY_EXIT_0_1)
    vz: dict[str, int | None] = {}
    for instance in running:
        pids = [pid for pid in (holders or {}).get(_disk(instance), []) if pid != agents[instance.name]]
        vz[instance.name] = pids[0] if pids else None
    wanted = [pid for pid in [*vz.values(), *agents.values()] if pid is not None]
    stats = (
        await probe(tools, (PS, "-o", PS_FORMAT, "-p", parsers.pids_text(wanted)), parsers.parse_ps, ANY_EXIT_0_1)
        if wanted
        else None
    ) or {}
    traffic = (await probe(tools, NETTOP_ARGV, parsers.parse_nettop) if any(agents.values()) else None) or {}
    found: dict[str, HostVm] = {}
    for instance in running:
        agent, process = agents[instance.name], vz[instance.name]
        own = stats.get(process) if process is not None else None
        helper = stats.get(agent) if agent is not None else None
        net = traffic.get(agent) if agent is not None else None
        found[instance.name] = HostVm(
            vz_pid=process,
            hostagent_pid=agent,
            cpu_ratio=None if own is None else round(own.cpu_percent / 100.0, 4),
            memory_bytes=None if own is None else own.rss_bytes + (helper.rss_bytes if helper is not None else 0),
            uptime_seconds=None if own is None else own.etime_seconds,
            disk_used_bytes=await tools.blocking(_allocated, tools, _disk(instance)),
            net_rx_bytes=None if net is None else net[0],
            net_tx_bytes=None if net is None else net[1],
        )
    return found


async def guest_metrics(manager: VmManager, row: VmRow) -> GuestMetrics | None:
    """`GET /v1/metrics` of the VM's guest (2 s), checked against the contract; None on any failure."""
    try:
        client = await manager.env.client(row)
    except (RunnerError, OSError):
        return None
    try:
        data = await asyncio.wait_for(client.metrics(), GUEST_TIMEOUT_S)
        return parsers.parse_guest_metrics(data)
    except (GuestError, TimeoutError, ProbeError):
        return None
    finally:
        await client.aclose()
