"""Daemon task `workvm` (docs/platform/spec/13-work-vm.md sections 6.4 and 7): reconciliation after a restart, then
every `INTERVAL` seconds (and at once after a pool entry was assigned) the boundary guard (every live VM and pool
entry goes when the host filter check fails), the idle pass and the warm pool refill. The task ends at once on a
device without work VMs."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from agent_runner.registry import TaskSpec
from agent_runner.workvm.service import workvms_for

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
    await workvms.reconcile()
    while not context.stopping.is_set():
        await workvms.guard_pass()
        await workvms.idle_pass()
        await workvms.pool_pass()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(_either(context.stopping, workvms.pool_wanted), INTERVAL)


TASKS = [TaskSpec("workvm", run_workvm)]
