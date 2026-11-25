"""One sampling round of the `metrics` task (08-monitoring.md section 2, 05-runner.md section 14).

`Sampler.tick()`:

1. collects device values, `limactl list --json`, the host side of every running instance and the guest's
   `GET /v1/metrics` of every VM in `pool`/`assigning`/`running` (concurrently, each probe bounded by its timeout),
   the capacity (05 section 3.4), the local sessions, the sync state (WebSocket open, `sync_fatal`, backlog) and the
   work VMs (KOR-29, `agent_runner.metrics.workvm`);
2. builds one entry per entity (`Snapshot`);
3. writes sink A (textfile, only with `AGENT_RUNNER_TEXTFILE_DIR`) and sink B (outbox, only when enrolled).

VM CPU: the guest's `cpu_usage_usec` delta (exact, 1.0 = one core) when two answers exist, else the VZ process'
`%cpu` from `ps`. Uptime: the VZ process' elapsed time, else the guest's uptime. A sink that fails is logged
(`runner.metrics textfile_failed|outbox_failed error=<type>`) and the round still counts; nothing here depends on the
hub being reachable.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from agent_runner.metrics import counters, device, outbox, textfile, vm, workvm
from agent_runner.metrics.parsers import GuestMetrics
from agent_runner.metrics.probes import Tools
from agent_runner.metrics.session import SessionEntry, SessionTracker, session_rows
from agent_runner.metrics.snapshot import PROBED_STATES, DeviceEntry, Snapshot, VmEntry
from agent_runner.model.operator import Summary, pool_for
from agent_runner.quota.openrouter import spend_metrics
from agent_runner.sync import outbox as sync_outbox
from agent_runner.sync.runtime import runtime
from agent_runner.vm import records as vm_records
from agent_runner.vm.lima import Instance, LimaError
from agent_runner.vm.records import VmRow
from agent_runner.vm.tasks import manager_for

MIB = 1024 * 1024
USEC = 1_000_000


class Sampler:
    def __init__(self, context: Any, tools: Tools, wall: Callable[[], float] = time.time) -> None:
        self.context = context
        self.tools = tools
        self.wall = wall
        self.sessions = SessionTracker()
        self.sink = outbox.OutboxSink()
        self.vm_cpu: dict[str, tuple[int, float]] = {}
        self.final_sent: set[str] = set()

    def line(self, text: str) -> None:
        self.context.log.write(f"runner.metrics {text}\n")
        self.context.log.flush()

    async def _instances(self, manager: Any) -> dict[str, Instance]:
        try:
            listed: list[Instance] = await manager.env.blocking(manager.env.lima.list)
        except LimaError as exc:
            self.line(f"lima_list_failed code={exc.code}")
            return {}
        for instance in listed:
            manager.env.dirs.setdefault(instance.name, instance.dir)
        return {instance.name: instance for instance in listed}

    def _vm_cpu(self, row: VmRow, guest: GuestMetrics | None, host: vm.HostVm | None, clock: float) -> float | None:
        ratio: float | None = None
        if guest is not None:
            usage = guest.vm["cpu_usage_usec"]
            previous = self.vm_cpu.get(row.id)
            if previous is not None and usage >= previous[0] and clock > previous[1]:
                ratio = round((usage - previous[0]) / USEC / (clock - previous[1]), 4)
            self.vm_cpu[row.id] = (usage, clock)
        if ratio is None and host is not None:
            ratio = host.cpu_ratio
        return ratio

    def _vm_entry(
        self,
        *,
        row: VmRow,
        instance: Instance | None,
        host: vm.HostVm | None,
        guest: GuestMetrics | None,
        clock: float,
        states: Mapping[str, int],
    ) -> VmEntry:
        uptime = host.uptime_seconds if host is not None else None
        if uptime is None and guest is not None:
            uptime = guest.vm["uptime_seconds"]
        return VmEntry(
            vm_id=row.id,
            wg_id=row.wg_id,
            state=row.state,
            probe_ok=(guest is not None) if row.state in PROBED_STATES else None,
            cpu_ratio=self._vm_cpu(row, guest, host, clock),
            memory_bytes=host.memory_bytes if host is not None else None,
            memory_limit_bytes=row.memory_mib * MIB,
            disk_used_bytes=host.disk_used_bytes if host is not None else None,
            disk_limit_bytes=instance.disk_bytes if instance is not None else None,
            uptime_seconds=uptime,
            net_rx_bytes=host.net_rx_bytes if host is not None else None,
            net_tx_bytes=host.net_tx_bytes if host is not None else None,
            sessions=dict(states),
        )

    async def snapshot(self) -> Snapshot:
        context = self.context
        wall = self.wall()
        at = datetime.fromtimestamp(wall, UTC)
        manager = manager_for(context)
        rows = await vm_records.live(manager.env.db)
        instances = await self._instances(manager)
        probed = [
            row
            for row in rows
            if row.state in PROBED_STATES and row.lima_name in instances and instances[row.lima_name].running
        ]
        own = [instances[row.lima_name] for row in rows if row.lima_name in instances]
        values, host, answers = await asyncio.gather(
            device.sample(self.tools, context.host.env, wall),
            vm.host_values(self.tools, own),
            asyncio.gather(*(vm.guest_metrics(manager, row) for row in probed)),
        )
        clock = context.host.clock()
        guests = {row.id: answer for row, answer in zip(probed, answers, strict=True)}
        local = await session_rows(manager.env.db, at)
        entries = self.sessions.entries(local, guests, at, clock)
        by_vm: dict[str, Counter[str]] = {}
        for entry in entries:
            by_vm.setdefault(entry.vm_id, Counter())[entry.state] += 1
        vms = tuple(
            self._vm_entry(
                row=row,
                instance=instances.get(row.lima_name),
                host=host.get(row.lima_name),
                guest=guests.get(row.id),
                clock=clock,
                states=by_vm.get(row.id, Counter()),
            )
            for row in rows
        )
        async with context.pool.connection() as conn:
            fatal = await sync_outbox.sync_fatal(conn)
            backlog = await sync_outbox.backlog(conn)
            # the scope queues of KOR-08 (KOR-10, AUD-02 NB-09): items and scopes, one indexed count
            cur = await conn.execute("SELECT count(*), count(DISTINCT (scope, scope_id)) FROM runner.outbox_held")
            held = await cur.fetchone()
            assert held is not None
        sync = runtime(context)
        summary = DeviceEntry(
            values=values,
            capacity=await manager.capacity(),
            hub_connected=sync is not None and sync.ws.connected,
            sync_fatal=fatal is not None,
            backlog=backlog,
            counters=counters.device_values(context, self.sink.dropped),
            held=int(held[0]),
            held_scopes=int(held[1]),
        )
        workvms = await workvm.entries(context, self.tools, at, self.line)
        return Snapshot(wall=wall, at=at, device=summary, vms=vms, sessions=tuple(entries), workvms=workvms)

    def hub_sessions(self, entries: tuple[SessionEntry, ...]) -> list[SessionEntry]:
        """Sessions for the hub: running ones every round, an ended one once (its final values)."""
        chosen = [entry for entry in entries if not entry.ended or entry.session_id not in self.final_sent]
        self.final_sent = {entry.session_id for entry in entries if entry.ended}
        return chosen

    def operator(self) -> Summary | None:
        """The operator's subscriptions (KOR-23) when they are configured: scanned now."""
        pool = pool_for(self.context)
        if pool is None or pool.directory is None:  # shared subscriptions only: no token files to show
            return None
        pool.scan()
        return pool.summary()

    async def tick(self) -> Snapshot:
        snap = await self.snapshot()
        directory = self.context.config.textfile_dir
        if directory is not None:
            try:
                await self.tools.blocking(
                    textfile.write_atomic,
                    directory,
                    textfile.render(
                        textfile.metrics_of(snap, spend=spend_metrics(self.context), operator=self.operator())
                    ),
                )
            except OSError as exc:
                self.line(f"textfile_failed error={type(exc).__name__}")
        sync = runtime(self.context)
        identity = self.context.identity
        chosen = self.hub_sessions(snap.sessions)
        if sync is not None and identity is not None:
            samples = outbox.samples_of(snap, identity.device.device_id, chosen)
            try:
                await self.sink.offer(
                    self.context.pool,
                    sync,
                    snap.wall,
                    samples,
                    connected=snap.device.hub_connected,
                    backlog=snap.device.backlog,
                )
            except Exception as exc:
                self.line(f"outbox_failed error={type(exc).__name__}")
        return snap
