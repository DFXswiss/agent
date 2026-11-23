"""Shutdown hook of the sync area (05-runner.md section 1.1: "flush outbox" on SIGTERM).

The supervisor awaits it within its stop budget before cancelling the tasks; a hub that cannot be reached or
refuses only ends the flush (the items stay queued for the next start).
"""

from __future__ import annotations

from typing import Any

from agent_runner.registry import ShutdownHook
from agent_runner.sync.http import HubRefusal, HubTransportError
from agent_runner.sync.push import SyncFatal
from agent_runner.sync.runtime import runtime


async def flush(context: Any) -> None:
    sync = runtime(context)
    if sync is None:
        return
    try:
        outcome = await sync.pusher.drain()
        sync.pusher.line(f"flushed waiting={outcome.waiting}")
    except (HubRefusal, HubTransportError, SyncFatal) as exc:
        sync.pusher.line(f"flush_failed error={type(exc).__name__}")
    finally:
        await sync.close()


HOOKS = [ShutdownHook("outbox-flush", flush)]
