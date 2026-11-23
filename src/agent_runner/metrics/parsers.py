"""Parsers of the probe commands of the metric sampler (08-monitoring.md section 1, Spike A `vm-metrics.sh`).

Every parser takes the text a command printed and returns plain values; anything it does not recognise raises
`ProbeError`, which the caller turns into a failed probe (`probe_ok 0`), never into a stopped sampler. The formats are
those of macOS 27 on the runner device (fixtures under `tests/platform/runner/metrics/fixtures/`).

| Command | Parser | Result |
|---|---|---|
| `sysctl -n hw.memsize hw.ncpu kern.boottime` | `parse_sysctl` | memory bytes, CPU count, boot time (epoch seconds) |
| `top -l 2 -n 0 -s 1` | `parse_top_idle` | idle percent of the last sample (the first one is since boot) |
| `vm_stat` | `parse_vm_stat` | used bytes: active + wired + occupied by the compressor |
| `df -k <dir>` | `parse_df` | used and total bytes of the volume |
| `netstat -ib` | `parse_netstat` | received/transmitted bytes of the active interfaces |
| `lsof -F pn <file>...` | `parse_lsof` | the processes holding each file |
| `ps -o pid=,%cpu=,rss=,etime= -p <pids>` | `parse_ps` | `ProcessStat` per PID |
| `nettop -P -L 1 -x -J bytes_in,bytes_out` | `parse_nettop` | received/transmitted bytes per PID |
| guest `GET /v1/metrics` | `parse_guest_metrics` | `GuestMetrics` (the guest is untrusted, 00-overview.md B4) |

`netstat -ib` counts only link-level rows (`<Link#n>`) of interfaces that are up (no `*` suffix), not loopback and
not tunnels (`utun`, `gif`, `stf`): tunnel traffic is counted again on the interface that carries it.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

MAX_COUNT = 2**53 - 1
KIB = 1024
_BOOTTIME_RE = re.compile(r"\{\s*sec\s*=\s*(\d+),\s*usec\s*=\s*\d+\s*\}")
_IDLE_RE = re.compile(r"^CPU usage: [0-9.]+% user, [0-9.]+% sys, ([0-9.]+)% idle")
_PAGE_SIZE_RE = re.compile(r"\(page size of (\d+) bytes\)")
_VM_STAT_RE = re.compile(r'^"?([A-Za-z -]+?)"?:\s+(\d+)\.$')
_DF_RE = re.compile(r"^(.+?)\s+(\d+)\s+(\d+)\s+(\d+)\s+\d+%\s")
_ETIME_RE = re.compile(r"^(?:(?:(\d+)-)?(\d{1,2}):)?(\d{1,2}):(\d{2})$")
_NETTOP_RE = re.compile(r"^.+\.(\d+),(\d+),(\d+),?$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
TUNNEL_PREFIXES = ("lo", "utun", "gif", "stf")
SESSION_STATES = frozenset(
    {
        "requested",
        "vm_assigning",
        "vm_ready",
        "sandbox_ready",
        "agent_starting",
        "prompt_delivering",
        "working",
        "idle",
        "suspended",
        "resuming",
        "stopping",
        "stopped",
        "failed",
    }
)
VM_FIELDS = (
    "cpu_usage_usec",
    "mem_used_bytes",
    "mem_total_bytes",
    "disk_used_bytes",
    "disk_total_bytes",
    "net_rx_bytes",
    "net_tx_bytes",
    "uptime_seconds",
)
SESSION_COUNTS = (
    "cpu_usage_usec",
    "mem_current_bytes",
    "tasks_current",
    "io_read_bytes",
    "io_write_bytes",
    "workspace_bytes",
)
SESSION_OPTIONAL = (
    "oom_kills",
    "hook_denies",
    "hook_errors",
    "egress_denied",
    "model_input_tokens",
    "model_output_tokens",
    "model_request_bytes",
    "model_response_bytes",
)
MAX_GUEST_SESSIONS = 64


class ProbeError(ValueError):
    """The output of a probe is not what the parser knows."""


def _int(text: str) -> int:
    if not text.isdigit():
        raise ProbeError("not a number")
    return int(text)


def parse_sysctl(text: str) -> tuple[int, int, int]:
    """`(memsize, ncpu, boottime)` from the three lines of `sysctl -n hw.memsize hw.ncpu kern.boottime`."""
    lines = text.splitlines()
    if len(lines) != 3:
        raise ProbeError("sysctl")
    match = _BOOTTIME_RE.match(lines[2].strip())
    if match is None:
        raise ProbeError("kern.boottime")
    memsize, ncpu = _int(lines[0].strip()), _int(lines[1].strip())
    if ncpu == 0:
        raise ProbeError("hw.ncpu")
    return memsize, ncpu, int(match.group(1))


def parse_top_idle(text: str) -> float:
    """Idle percent of the last `CPU usage` line (`top -l 2`: the first sample counts since boot)."""
    found = [match.group(1) for line in text.splitlines() if (match := _IDLE_RE.match(line.strip()))]
    if not found:
        raise ProbeError("top")
    idle = float(found[-1])
    if not 0 <= idle <= 100:
        raise ProbeError("top")
    return idle


def parse_vm_stat(text: str) -> int:
    lines = text.splitlines()
    size = _PAGE_SIZE_RE.search(lines[0]) if lines else None
    if size is None:
        raise ProbeError("vm_stat")
    pages: dict[str, int] = {}
    for line in lines[1:]:
        match = _VM_STAT_RE.match(line.strip())
        if match is not None:
            pages[match.group(1)] = int(match.group(2))
    try:
        used = pages["Pages active"] + pages["Pages wired down"] + pages["Pages occupied by compressor"]
    except KeyError:
        raise ProbeError("vm_stat") from None
    return used * int(size.group(1))


def parse_df(text: str) -> tuple[int, int]:
    """`(used, total)` bytes of the single volume line of `df -k`."""
    lines = text.splitlines()
    match = _DF_RE.match(lines[1]) if len(lines) == 2 else None
    if match is None:
        raise ProbeError("df")
    return int(match.group(3)) * KIB, int(match.group(2)) * KIB


def parse_netstat(text: str) -> tuple[int, int]:
    lines = text.splitlines()
    if not lines or not lines[0].split()[:1] == ["Name"]:
        raise ProbeError("netstat")
    received = transmitted = 0
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 10 or not parts[2].startswith("<Link#"):
            continue
        name = parts[0]
        if name.endswith("*") or name.startswith(TUNNEL_PREFIXES):
            continue
        _ipkts, _ierrs, ibytes, _opkts, _oerrs, obytes, _coll = (_int(item) for item in parts[-7:])
        received += ibytes
        transmitted += obytes
    return received, transmitted


def parse_lsof(text: str) -> dict[str, list[int]]:
    """`{file: [pid, ...]}` from `lsof -F pn` output (`p<pid>` starts a process, `n<name>` names a file)."""
    holders: dict[str, list[int]] = {}
    pid: int | None = None
    for line in text.splitlines():
        if line.startswith("p"):
            pid = _int(line[1:])
        elif line.startswith("n"):
            if pid is None:
                raise ProbeError("lsof")
            holders.setdefault(line[1:], []).append(pid)
    return holders


def parse_etime(text: str) -> int:
    """Seconds of a `ps` elapsed time `[[dd-]hh:]mm:ss`."""
    match = _ETIME_RE.match(text)
    if match is None:
        raise ProbeError("etime")
    days, hours, minutes, seconds = (int(group) if group else 0 for group in match.groups())
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


@dataclass(frozen=True)
class ProcessStat:
    cpu_percent: float
    rss_bytes: int
    etime_seconds: int


def parse_ps(text: str) -> dict[int, ProcessStat]:
    found: dict[int, ProcessStat] = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        if len(parts) != 4:
            raise ProbeError("ps")
        try:
            cpu = float(parts[1])
        except ValueError:
            raise ProbeError("ps") from None
        if not 0 <= cpu < math.inf:
            raise ProbeError("ps")
        found[_int(parts[0])] = ProcessStat(cpu, _int(parts[2]) * KIB, parse_etime(parts[3]))
    return found


def parse_nettop(text: str) -> dict[int, tuple[int, int]]:
    """`{pid: (bytes_in, bytes_out)}` from `nettop -P -L 1 -x -J bytes_in,bytes_out` (header line first)."""
    lines = text.splitlines()
    if not lines or lines[0].strip().strip(",") != "bytes_in,bytes_out":
        raise ProbeError("nettop")
    found: dict[int, tuple[int, int]] = {}
    for line in lines[1:]:
        match = _NETTOP_RE.match(line.strip())
        if match is None:
            continue
        pid, received, sent = (int(group) for group in match.groups())
        found[pid] = (received, sent)
    return found


# -- guest -------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class GuestSession:
    session_id: str
    state: str
    values: Mapping[str, int | None]


@dataclass(frozen=True)
class GuestMetrics:
    vm: Mapping[str, int]
    load1: float
    sessions: tuple[GuestSession, ...]


def _count(value: object) -> int:
    if type(value) is not int or not 0 <= value <= MAX_COUNT:
        raise ProbeError("guest")
    return value


def _session(item: object) -> GuestSession:
    if not isinstance(item, dict):
        raise ProbeError("guest")
    session_id, state = item.get("session_id"), item.get("state")
    if not isinstance(session_id, str) or not _UUID_RE.fullmatch(session_id) or state not in SESSION_STATES:
        raise ProbeError("guest")
    values: dict[str, int | None] = {name: _count(item.get(name)) for name in SESSION_COUNTS}
    limit = item.get("mem_max_bytes")
    values["mem_max_bytes"] = None if limit is None else _count(limit)
    for name in SESSION_OPTIONAL:
        values[name] = None if item.get(name) is None else _count(item.get(name))
    return GuestSession(session_id, str(state), values)


def parse_guest_metrics(data: Mapping[str, Any]) -> GuestMetrics:
    """The checked answer of `GET /v1/metrics`; any value outside the contract refuses the whole answer."""
    vm, sessions = data.get("vm"), data.get("sessions")
    if not isinstance(vm, dict) or not isinstance(sessions, list) or len(sessions) > MAX_GUEST_SESSIONS:
        raise ProbeError("guest")
    load = vm.get("load1")
    if not isinstance(load, int | float) or isinstance(load, bool) or not 0 <= load < math.inf:
        raise ProbeError("guest")
    parsed = tuple(_session(item) for item in sessions)
    if len({item.session_id for item in parsed}) != len(parsed):
        raise ProbeError("guest")
    return GuestMetrics({name: _count(vm.get(name)) for name in VM_FIELDS}, float(load), parsed)


def pids_text(pids: Sequence[int]) -> str:
    return ",".join(str(pid) for pid in sorted(set(pids)))
