"""Daemon tasks of the VM area (docs/platform/spec/05-runner.md section 1.1, ADR-027 section 4).

| Task | Loop |
|---|---|
| `vm-pool` | startup reconciliation (once, before other VM work), then refill after every assignment and every 60 s |
| `vm-idle` | health probe of the running VMs every 5 s, idle stop every 60 s (after the reconciliation) |

Both tasks share one `VmManager` per daemon context: `manager_for(context)` builds it on first use from the
configuration (`limactl` from `PATH`, the daemon's store pool, `$AGENT_RUNNER_HOME/vm/` for the tokens) and keeps it
on the context, so the session start (RUN-04) calls `manager_for(context).ensure_vm(...)` on the same instance; the
manager gets the retire hooks of `vm.extensions` bound to the context (KOR-33).
Every VM state change becomes an outbox item `vm_state` (RUN-02 `runtime(context).enqueue`, then the pusher is
woken) and brings the next heartbeat forward (`WsClient.beat_soon`: the capacity, e.g. `pool_ready`, changed); a
device that is not enrolled has no outbox, its `vm_state` items are written as fact lines to the log. Once the daemon
is stopping (`context.stopping`, set on SIGTERM before the shutdown hooks run) no pool or hot-path clone is started
(decisions KOR-03).
"""

from __future__ import annotations

import functools
import json
import os
from typing import Any, cast

from agent_runner import registry
from agent_runner.errors import RunnerError
from agent_runner.metrics.device import lima_home
from agent_runner.registry import TaskSpec
from agent_runner.sync.runtime import runtime
from agent_runner.vm import lima
from agent_runner.vm.capacity import NO_USAGE, Usage, disk_free_mib, physical_memory_mib
from agent_runner.vm.extensions import Retire, retire_hooks
from agent_runner.vm.lifecycle import VmManager
from agent_runner.vm.pool import VmEnv, VmSettings
from agent_runner.vm.records import TokenStore

POOL_INTERVAL_S = 60.0
HEALTH_INTERVAL_S = 5.0
IDLE_INTERVAL_S = 60.0
MANAGER_KEY = "_vm_manager"


def _line(context: Any, name: str, fields: dict[str, str | int]) -> None:
    text = " ".join(f"{key}={value}" for key, value in fields.items())
    context.log.write(f"runner.{name}{' ' if text else ''}{text}\n")
    context.log.flush()


def usage_of(context: Any, providers: list[Any]) -> Any:
    """The sum of the other areas' `USAGE` (ADR-075 section 2) for this daemon context."""

    async def other_usage() -> Usage:
        total = NO_USAGE
        for provide in providers:
            found = await provide(context)
            if not isinstance(found, Usage):
                raise RunnerError("registry_invalid", "capacity")
            total = total + found
        return total

    return other_usage


def build_manager(context: Any) -> VmManager:
    config = context.config
    log = functools.partial(_line, context)
    home = lima_home(context.host.env)

    async def sink(body: dict[str, Any]) -> None:
        sync = runtime(context)
        if sync is None:
            # Not enrolled: there is no outbox to write to.
            _line(context, "vm.state", {"body": json.dumps(body, sort_keys=True, separators=(",", ":"))})
            return
        async with context.pool.connection() as conn, conn.transaction():
            await sync.enqueue(conn, "vm_state", body)
        sync.pusher.notify()
        sync.ws.beat_soon()

    env = VmEnv(
        lima=lima.Lima(functools.partial(lima.run_limactl, env=dict(context.host.env)), prefix=config.vm_prefix),
        db=context.pool,
        settings=VmSettings.from_config(config),
        tokens=TokenStore(config.home / "vm"),
        physical_mib=physical_memory_mib,
        sink=sink,
        log=log,
        sleep=context.host.sleep,
        clock=context.host.clock,
        stopping=context.stopping.is_set,
        other_usage=usage_of(context, registry.capacity_usages()),
        cpus=os.cpu_count,
        disk_free_mib=functools.partial(disk_free_mib, home),
    )
    return VmManager(env, retire=Retire(context, retire_hooks(), log))


def manager_for(context: Any) -> VmManager:
    """The VM manager of this daemon context (built once)."""
    state = vars(context)
    manager = state.get(MANAGER_KEY)
    if manager is None:
        manager = build_manager(context)
        state[MANAGER_KEY] = manager
    return cast(VmManager, manager)


async def run_pool(context: Any) -> None:
    manager = manager_for(context)
    # Once per daemon: after a restart of this task other VM work may be in flight, which a second
    # reconciliation would mistake for crash leftovers.
    if not manager.reconciled:
        await manager.reconcile()
    while not context.stopping.is_set():
        await manager.refill()
        await manager.wait_refill(POOL_INTERVAL_S)


async def run_idle(context: Any) -> None:
    manager = manager_for(context)
    await manager.wait_reconciled()
    sleep, clock = context.host.sleep, context.host.clock
    last_idle = clock()
    while not context.stopping.is_set():
        await manager.health_pass()
        if clock() - last_idle >= IDLE_INTERVAL_S:
            last_idle = clock()
            await manager.idle_pass()
        await sleep(HEALTH_INTERVAL_S)


TASKS = [TaskSpec("vm-pool", run_pool), TaskSpec("vm-idle", run_idle)]
