"""Daemon task `operations` (05-runner.md sections 1.1 and 12): the operation pipeline (`Pipeline.loop`) and a
listener for `NOTIFY runner_operation` on its own connection, sent by the journal handler inside the journal page's
transaction and therefore delivered after the commit; a missed notification costs at most `pipeline.PERIOD_S`. The
task ends at once on a device that is not enrolled."""

from __future__ import annotations

import asyncio
from typing import Any

import psycopg

from agent_runner.operations.journal import CHANNEL
from agent_runner.operations.pipeline import PERIOD_S, Pipeline, pipeline_for
from agent_runner.registry import TaskSpec

LISTEN_RETRY_S = 1.0


def _line(context: Any, text: str) -> None:
    context.log.write(f"runner.operations {text}\n")
    context.log.flush()


async def listen(context: Any, pipeline: Pipeline) -> None:
    """Wake `pipeline` on every `NOTIFY runner_operation`; reconnects after a database failure."""
    while True:
        try:
            async with await psycopg.AsyncConnection.connect(context.dsn, autocommit=True) as conn:
                await conn.execute(f"LISTEN {CHANNEL}")
                pipeline.wake()
                while True:
                    async for _ in conn.notifies(timeout=PERIOD_S, stop_after=1):
                        pipeline.wake()
        except psycopg.Error as exc:
            _line(context, f"listen_failed error={type(exc).__name__}")
        await context.host.sleep(LISTEN_RETRY_S)


async def run_operations(context: Any) -> None:
    pipeline = pipeline_for(context)
    if pipeline is None:
        _line(context, "not_enrolled")
        return
    listener = asyncio.ensure_future(listen(context, pipeline))
    try:
        await pipeline.loop(context.stopping)
    finally:
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)


TASKS = [TaskSpec("operations", run_operations)]
