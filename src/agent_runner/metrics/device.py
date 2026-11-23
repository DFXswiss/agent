"""Host values of the runner device for the hub sample (08-monitoring.md section 1, device column; section 3 last
paragraph: in Prometheus these come from the existing node_exporter, so the textfile does not repeat them).

| Value | Probe |
|---|---|
| CPU (`cpu_ratio`, 1.0 = one core) | `top -l 2 -n 0 -s 1` idle of the second sample, times `hw.ncpu` |
| memory used / total | `vm_stat` (active + wired + compressor) / `hw.memsize` |
| disk used / total | `df -k` of the volume holding `$LIMA_HOME` (default `~/.lima`) |
| network received / transmitted | `netstat -ib` (counters) |
| uptime | now - `kern.boottime` |

The probes run concurrently; each one that fails leaves its values None and clears `ok` (the device probe is then
reported failed), the others are still used.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from agent_runner.metrics import parsers
from agent_runner.metrics.probes import DF, NETSTAT, SYSCTL, TOP, VM_STAT, Tools, probe

SYSCTL_ARGV = (SYSCTL, "-n", "hw.memsize", "hw.ncpu", "kern.boottime")
TOP_ARGV = (TOP, "-l", "2", "-n", "0", "-s", "1")
VM_STAT_ARGV = (VM_STAT,)
NETSTAT_ARGV = (NETSTAT, "-ib")


def lima_home(env: Mapping[str, str]) -> str:
    """`$LIMA_HOME` of the runner user, else `~/.lima` (Lima's default)."""
    return env.get("LIMA_HOME") or str(Path(env.get("HOME") or os.path.expanduser("~")) / ".lima")


@dataclass(frozen=True)
class DeviceValues:
    cpu_ratio: float | None
    mem_used_bytes: int | None
    mem_total_bytes: int | None
    disk_used_bytes: int | None
    disk_total_bytes: int | None
    net_rx_bytes: int | None
    net_tx_bytes: int | None
    uptime_seconds: int | None
    ok: bool


async def sample(tools: Tools, env: Mapping[str, str], wall: float) -> DeviceValues:
    sysctl, idle, used, disk, net = await asyncio.gather(
        probe(tools, SYSCTL_ARGV, parsers.parse_sysctl),
        probe(tools, TOP_ARGV, parsers.parse_top_idle),
        probe(tools, VM_STAT_ARGV, parsers.parse_vm_stat),
        probe(tools, (DF, "-k", lima_home(env)), parsers.parse_df),
        probe(tools, NETSTAT_ARGV, parsers.parse_netstat),
    )
    memsize = ncpu = uptime = None
    if sysctl is not None:
        memsize, ncpu, boottime = sysctl
        uptime = max(int(wall) - boottime, 0)
    cpu = None if idle is None or ncpu is None else round((100.0 - idle) / 100.0 * ncpu, 4)
    return DeviceValues(
        cpu_ratio=cpu,
        mem_used_bytes=used,
        mem_total_bytes=memsize,
        disk_used_bytes=None if disk is None else disk[0],
        disk_total_bytes=None if disk is None else disk[1],
        net_rx_bytes=None if net is None else net[0],
        net_tx_bytes=None if net is None else net[1],
        uptime_seconds=uptime,
        ok=None not in (sysctl, idle, used, disk, net),
    )
