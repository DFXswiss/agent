"""Running the probe commands of the metric sampler (08-monitoring.md section 1; legacy injection style, 07-testing.md
section 1).

Every command has a fixed argv (absolute macOS paths, no shell) and `PROBE_TIMEOUT_S`; `Tools` carries the injected
callables (command runner, file reader, `stat`, the thread offloader). `probe(tools, argv, parse)` returns the parsed
value or None: a missing binary (127), a timeout (124), any other non-zero exit and a `ProbeError` of the parser all
mean "probe failed" and never raise.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from agent_runner.metrics.parsers import ProbeError

PROBE_TIMEOUT_S = 5.0
SYSCTL = "/usr/sbin/sysctl"
TOP = "/usr/bin/top"
VM_STAT = "/usr/bin/vm_stat"
DF = "/bin/df"
NETSTAT = "/usr/sbin/netstat"
LSOF = "/usr/sbin/lsof"
PS = "/bin/ps"
NETTOP = "/usr/bin/nettop"

ProbeRun = Callable[[Sequence[str], float], "subprocess.CompletedProcess[str]"]


def run_probe(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """Run `argv` (list form) with `timeout`; a missing binary answers 127, a timeout 124 (the child is killed)."""
    try:
        return subprocess.run(list(argv), capture_output=True, text=True, check=False, timeout=timeout)
    except FileNotFoundError:
        return subprocess.CompletedProcess(list(argv), 127, "", f"{argv[0]}: not found")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(list(argv), 124, "", f"{argv[0]}: timed out")


def read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except (OSError, ValueError):
        return None


@dataclass
class Tools:
    run: ProbeRun = run_probe
    read_text: Callable[[str], str | None] = read_text
    stat: Callable[[str], os.stat_result] = os.stat
    blocking: Callable[..., Awaitable[Any]] = asyncio.to_thread


async def probe[T](
    tools: Tools, argv: Sequence[str], parse: Callable[[str], T], accept: frozenset[int] = frozenset({0})
) -> T | None:
    """`parse(stdout)` when the exit code is in `accept`, else None (also for a `ProbeError`)."""
    result: subprocess.CompletedProcess[str] = await tools.blocking(tools.run, list(argv), PROBE_TIMEOUT_S)
    if result.returncode not in accept:
        return None
    try:
        return parse(result.stdout)
    except ProbeError:
        return None
