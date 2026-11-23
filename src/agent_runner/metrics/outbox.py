"""Sink B: outbox items `metrics` for the hub (08-monitoring.md sections 2 and 4, 03-api.md section 6.1,
02-data-model.md section 2.8).

`samples_of(snapshot, device_id, include)` turns a snapshot into `metric sample` objects
(`contracts/outbox/objects.json#/$defs/metric_sample`):

- device: CPU from `top`, memory from `vm_stat`/`hw.memsize`, disk from `df`, network from `netstat -ib`, uptime
  from the boot time; `reachable` true, `state` `active` (`sync_fatal` while pushing is stopped); free VM slots and
  free VM memory budget from the capacity.
- vm: CPU (guest delta or VZ `%cpu`), host RSS / configured memory, disk file blocks / disk size, hostagent bytes,
  VZ elapsed time; `reachable` = the guest answered; `state` = the VM state.
- session: cgroup CPU delta, `memory.current` / `memory.max`, workspace bytes (no limit), model response / request
  bytes as received / sent, seconds since created; `reachable` = the guest reported it; `state` = the session state.

Sampling never waits for the hub. `OutboxSink.offer` stores the samples of every round in `runner.metric_pending`
(migration 0005, one row per round) and moves rows into the outbox only while the device WebSocket is open and the
outbox backlog is below `BACKLOG_LIMIT`: oldest rows first, at most `MOVE_SAMPLES` samples per round, at most
`MAX_SAMPLES` per item, rows deleted in the same transaction, then the pusher is woken. Before that, rows older than
`MAX_AGE_S` (48 h) are deleted and their samples counted (`agent_runner_device_metrics_dropped_total`). The outbox
itself cannot drop anything (its `origin_seq` is gapless and an item may already be on the way), so the age rule acts
before it. The buffer survives a daemon restart and is bounded by the age rule; a device that is not enrolled has no
hub and stores nothing.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from agent_runner.metrics.session import SessionEntry
from agent_runner.metrics.snapshot import Snapshot
from agent_runner.sync.runtime import SyncRuntime
from agent_runner.vm.pool import format_time

MAX_AGE_S = 48 * 3600
MAX_SAMPLES = 1000
BACKLOG_LIMIT = 1000
MOVE_SAMPLES = 20000
MOVE_ROWS = 1000
MIB = 1024 * 1024

Sample = dict[str, Any]


def _sample(kind: str, entity_id: str, ts: str, **values: Any) -> Sample:
    base: Sample = {
        "entity_kind": kind,
        "entity_id": entity_id,
        "ts": ts,
        "cpu_ratio": None,
        "mem_used_bytes": None,
        "mem_limit_bytes": None,
        "disk_used_bytes": None,
        "disk_limit_bytes": None,
        "net_rx_bytes": None,
        "net_tx_bytes": None,
        "uptime_seconds": None,
        "reachable": True,
        "state": "active",
        "free_vm_slots": None,
        "free_mem_bytes": None,
    }
    base.update(values)
    return base


def _session(entry: SessionEntry, ts: str) -> Sample:
    values = entry.values or {}
    return _sample(
        "session",
        entry.session_id,
        ts,
        cpu_ratio=entry.cpu_ratio,
        mem_used_bytes=values.get("mem_current_bytes"),
        mem_limit_bytes=values.get("mem_max_bytes"),
        disk_used_bytes=values.get("workspace_bytes"),
        net_rx_bytes=values.get("model_response_bytes"),
        net_tx_bytes=values.get("model_request_bytes"),
        uptime_seconds=entry.runtime_seconds,
        reachable=entry.reachable,
        state=entry.state,
    )


def samples_of(snapshot: Snapshot, device_id: str, sessions: Iterable[SessionEntry]) -> list[Sample]:
    ts = format_time(snapshot.at)
    device, capacity = snapshot.device, snapshot.device.capacity
    values = device.values
    found = [
        _sample(
            "device",
            device_id,
            ts,
            cpu_ratio=values.cpu_ratio,
            mem_used_bytes=values.mem_used_bytes,
            mem_limit_bytes=values.mem_total_bytes,
            disk_used_bytes=values.disk_used_bytes,
            disk_limit_bytes=values.disk_total_bytes,
            net_rx_bytes=values.net_rx_bytes,
            net_tx_bytes=values.net_tx_bytes,
            uptime_seconds=values.uptime_seconds,
            state="sync_fatal" if device.sync_fatal else "active",
            free_vm_slots=capacity.slots_free,
            free_mem_bytes=max(capacity.budget_mib - capacity.used_mib, 0) * MIB,
        )
    ]
    for vm in snapshot.vms:
        found.append(
            _sample(
                "vm",
                vm.vm_id,
                ts,
                cpu_ratio=vm.cpu_ratio,
                mem_used_bytes=vm.memory_bytes,
                mem_limit_bytes=vm.memory_limit_bytes,
                disk_used_bytes=vm.disk_used_bytes,
                disk_limit_bytes=vm.disk_limit_bytes,
                net_rx_bytes=vm.net_rx_bytes,
                net_tx_bytes=vm.net_tx_bytes,
                uptime_seconds=vm.uptime_seconds,
                reachable=vm.probe_ok is True,
                state=vm.state,
            )
        )
    found.extend(_session(entry, ts) for entry in sessions)
    return found


class OutboxSink:
    """Samples waiting for the hub (module docstring); `dropped` counts since the daemon started."""

    def __init__(self) -> None:
        self.dropped = 0

    async def offer(
        self, pool: Any, sync: SyncRuntime | None, wall: float, samples: list[Sample], *, connected: bool, backlog: int
    ) -> int:
        """Store `samples` (taken at `wall`); move held rows to the outbox when the hub is reachable. Returns the number
        of samples written to the outbox."""
        if sync is None:
            return 0
        taken = datetime.fromtimestamp(wall, UTC)
        async with pool.connection() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO runner.metric_pending (taken_at, samples) VALUES (%s, %s)", (taken, Jsonb(samples))
            )
            cur = await conn.execute(
                "DELETE FROM runner.metric_pending WHERE taken_at < %s RETURNING jsonb_array_length(samples)",
                (taken - timedelta(seconds=MAX_AGE_S),),
            )
            self.dropped += sum(int(row[0]) for row in await cur.fetchall())
        if not connected or backlog >= BACKLOG_LIMIT:
            return 0
        async with pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "SELECT id, samples FROM runner.metric_pending ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED",
                (MOVE_ROWS,),
            )
            held: list[Sample] = []
            moved: list[int] = []
            for row_id, batch in await cur.fetchall():
                if held and len(held) + len(batch) > MOVE_SAMPLES:
                    break
                held.extend(batch)
                moved.append(row_id)
            for start in range(0, len(held), MAX_SAMPLES):
                await sync.enqueue(conn, "metrics", {"samples": held[start : start + MAX_SAMPLES]})
            await conn.execute("DELETE FROM runner.metric_pending WHERE id = ANY(%s)", (moved,))
        sync.pusher.notify()
        return len(held)
