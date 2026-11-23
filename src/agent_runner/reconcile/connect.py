"""Connect hook `reconcile` (ADR-028, 03-api.md section 6.2): after every WebSocket (re)connect one reconcile run
completes before the outbox is pushed and the cursors are pulled."""

from __future__ import annotations

from typing import Any

from agent_runner.reconcile.engine import engine_for
from agent_runner.sync.extensions import ConnectHook


async def reconcile_now(context: Any) -> None:
    engine = engine_for(context)
    if engine is not None:
        await engine.run_once()


HOOKS = [ConnectHook("reconcile", "reconcile", reconcile_now)]
