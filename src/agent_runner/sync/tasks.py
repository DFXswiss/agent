"""Daemon tasks of the sync area (05-runner.md sections 1.1 and 2.3, ADR-027 section 4).

- `sync`: pushes the outbox (`Pusher.run`) and listens for `NOTIFY runner_outbox` on its own connection to wake the
  push at once after an item was committed (a missed notification costs at most `push.IDLE_POLL_S`). After a fatal
  condition (`origin_seq_conflict`) the task ends and is not restarted.
- `ws`: keeps the device WebSocket connected (`WsClient.run`).

Both end at once on a device that is not enrolled yet.
"""

from __future__ import annotations

import asyncio
from typing import Any

import psycopg

from agent_runner.registry import TaskSpec
from agent_runner.sync.outbox import CHANNEL
from agent_runner.sync.push import IDLE_POLL_S, Pusher
from agent_runner.sync.runtime import runtime

LISTEN_RETRY_S = 1.0


def _line(context: Any, text: str) -> None:
    context.log.write(f"runner.sync {text}\n")
    context.log.flush()


async def listen(context: Any, pusher: Pusher) -> None:
    """Wake `pusher` on every `NOTIFY runner_outbox`; reconnects after a database failure."""
    while True:
        try:
            async with await psycopg.AsyncConnection.connect(context.dsn, autocommit=True) as conn:
                await conn.execute(f"LISTEN {CHANNEL}")
                pusher.notify()
                while True:
                    async for _ in conn.notifies(timeout=IDLE_POLL_S, stop_after=1):
                        pusher.notify()
        except psycopg.Error as exc:
            _line(context, f"listen_failed error={type(exc).__name__}")
        await context.host.sleep(LISTEN_RETRY_S)


async def run_sync(context: Any) -> None:
    sync = runtime(context)
    if sync is None:
        _line(context, "not_enrolled task=sync")
        return
    listener = asyncio.ensure_future(listen(context, sync.pusher))
    try:
        await sync.pusher.run(context.stopping)
    finally:
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)


async def run_ws(context: Any) -> None:
    sync = runtime(context)
    if sync is None:
        _line(context, "not_enrolled task=ws")
        return
    await sync.ws.run(context.stopping)


TASKS = [TaskSpec("sync", run_sync), TaskSpec("ws", run_ws)]
