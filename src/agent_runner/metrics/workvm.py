"""Work VM values for the monitoring (docs/platform/spec/13-work-vm.md section 10, 08-monitoring.md section 1; KOR-29).

Every work VM in state `starting` or `running` (`runner.workvm`) is one entry per sampling round, attributed to the
session it belongs to: its own session (scope `session`), else the session that requested the task VM. The values come
from the same host probes as the workgroup VMs (`agent_runner.metrics.vm.host_values`: VZ `%cpu` and RSS, disk file
blocks, hostagent bytes) over the instances of each slot's Lima home (`limactl list` through the slot's command). The
runner measures only from outside: nothing runs in the work VM for the monitoring (ADR-059 addendum 1).

With slot accounts the runner may not be able to read a slot's Lima home or its processes (homes are 0700, `lsof` sees
only the runner's own processes); the probes then answer None and the entry still carries the configured memory and
disk sizes, the state and the runtime since the VM was created (`uptime_seconds`, KONZEPT idea 46). A slot whose
listing fails is left out of the host values (`runner.metrics workvm_list_failed slot=<n> code=<code>`).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from agent_runner.metrics import vm
from agent_runner.metrics.probes import Tools
from agent_runner.vm.lima import Instance, LimaError
from agent_runner.workvm import records
from agent_runner.workvm.service import existing

LIST_TIMEOUT_S = 10.0
MIB = 1024 * 1024
GIB = 1024 * MIB
SAMPLED_STATES = ("starting", "running")


@dataclass(frozen=True)
class WorkvmEntry:
    workvm_id: str
    session_id: str
    state: str
    cpu_ratio: float | None
    memory_bytes: int | None
    memory_limit_bytes: int
    disk_used_bytes: int | None
    disk_limit_bytes: int
    uptime_seconds: int
    net_rx_bytes: int | None
    net_tx_bytes: int | None


async def entries(context: Any, tools: Tools, at: datetime, line: Any) -> tuple[WorkvmEntry, ...]:
    """The entries of the live work VMs (module docstring); none without the daemon's work VMs."""
    workvms = existing(context)
    if workvms is None:
        return ()
    rows = await records.live(workvms.deps.db, *SAMPLED_STATES)
    if not rows:
        return ()
    instances: dict[str, Instance] = {}  # by name: in self mode every slot lists the same Lima home
    for slot in sorted({row.slot for row in rows}):
        lima = workvms.deps.lima_for(slot)
        try:
            listed = await asyncio.wait_for(asyncio.to_thread(lima.list), LIST_TIMEOUT_S)
        except LimaError as exc:
            line(f"workvm_list_failed slot={slot} code={exc.code}")
            continue
        except TimeoutError:  # a hanging `limactl list` must not hold up the round (the thread ends by itself)
            line(f"workvm_list_failed slot={slot} code=timeout")
            continue
        instances.update((instance.name, instance) for instance in listed)
    known = {row.lima_name for row in rows}
    host = await vm.host_values(tools, [instance for name, instance in instances.items() if name in known])
    found = []
    for row in rows:
        values = host.get(row.lima_name)
        runtime = max(0, int((at - row.created_at).total_seconds()))
        found.append(
            WorkvmEntry(
                workvm_id=row.id,
                session_id=row.session_id or row.requested_by,
                state=row.state,
                cpu_ratio=None if values is None else values.cpu_ratio,
                memory_bytes=None if values is None else values.memory_bytes,
                memory_limit_bytes=row.memory_mib * MIB,
                disk_used_bytes=None if values is None else values.disk_used_bytes,
                disk_limit_bytes=row.disk_gib * GIB,
                uptime_seconds=runtime,
                net_rx_bytes=None if values is None else values.net_rx_bytes,
                net_tx_bytes=None if values is None else values.net_tx_bytes,
            )
        )
    return tuple(found)
