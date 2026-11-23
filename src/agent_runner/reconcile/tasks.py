"""Daemon task `reconcile` (05-runner.md sections 1.1, 2.1 and 2.2): runs the engine every 30 s and on request.

While the device is `pending_certification` each run only polls `GET /v1/device/state` (every 30 s) until the hub
answers `active`. The task ends at once on a device that is not enrolled.
"""

from __future__ import annotations

from typing import Any

from agent_runner.reconcile.engine import engine_for
from agent_runner.registry import TaskSpec


async def run_reconcile(context: Any) -> None:
    engine = engine_for(context)
    if engine is None:
        context.log.write("runner.reconcile not_enrolled\n")
        context.log.flush()
        return
    await engine.loop(context.stopping)


TASKS = [TaskSpec("reconcile", run_reconcile)]
