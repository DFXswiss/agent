"""Daemon task `workvm` (docs/platform/spec/13-work-vm.md sections 6.4, 7 and 12): reconciliation after a restart (the
live VMs are taken first; the test runs still `running` end `error`, with the log of a run VM that still runs, which
goes after its capture; then every VM taken goes, ADR-087 section 5), then every `INTERVAL` seconds (and at once after a
pool entry was assigned) the boundary guard (every live VM and pool entry goes when the host filter check fails), the
idle pass of the platform browser sessions (`browser_idle`) and of the work VMs, the sweep of failed deletions and
orphan instances, and the warm pool refill. A volume of the slot homes that does not hang at its mount point is logged
at the start (`volume_missing`, HOT-25); no work VM is offered until it does. The task ends at once on a device without
work VMs."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from agent_runner.registry import TaskSpec
from agent_runner.workvm.service import browsers_of, runs_of, workvms_for

INTERVAL = 60.0


async def _either(first: asyncio.Event, second: asyncio.Event) -> None:
    waits = [asyncio.ensure_future(first.wait()), asyncio.ensure_future(second.wait())]
    try:
        await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for wait in waits:
            wait.cancel()


async def run_workvm(context: Any) -> None:
    workvms = workvms_for(context)
    if workvms is None:
        return
    await workvms.check_volume()
    left = await workvms.left_over()
    await runs_of(context, workvms).reconcile()
    await workvms.reconcile(left)
    browsers = browsers_of(context, workvms)
    while not context.stopping.is_set():
        await workvms.guard_pass()
        await browsers.idle_pass()
        await workvms.idle_pass()
        await workvms.sweep()
        await workvms.pool_pass()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(_either(context.stopping, workvms.pool_wanted), INTERVAL)


TASKS = [TaskSpec("workvm", run_workvm)]
