"""Daemon task `contexts` (ADR-072 sections 4 and 8; review KOR-33 no. 2): two loops side by side, so a long upload
never delays an export -

- exports: due checkpoints (`checkpoint.Checkpointer.round`) and the pull-request registrations (`prlinks.round`),
  every `POLL_S` or at once when a checkpoint became due (`ContextEnv.wake`);
- uploads: sealed objects to the hub and the cache (`Checkpointer.upload_round`), every `POLL_S` or at once when an
  object was sealed (`ContextEnv.upload_wake`).

A failing pass is logged and the next one runs after the poll interval. The task ends at once on a device that is
not enrolled."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from agent_runner.contexts import prlinks
from agent_runner.contexts.checkpoint import Checkpointer
from agent_runner.contexts.env import ContextEnv, contexts_for
from agent_runner.identity import ensure_private_dir
from agent_runner.registry import TaskSpec

POLL_S = 15.0


async def _repeat(
    env: ContextEnv,
    stopping: asyncio.Event,
    wake: asyncio.Event,
    name: str,
    step: Callable[[], Awaitable[object]],
    *,
    poll: float,
) -> None:
    while not stopping.is_set():
        wake.clear()
        try:
            await step()
        except Exception as exc:
            env.line(f"{name}_failed error={type(exc).__name__}")
        waiters = [asyncio.ensure_future(stopping.wait()), asyncio.ensure_future(wake.wait())]
        await asyncio.wait(waiters, timeout=poll, return_when=asyncio.FIRST_COMPLETED)
        for waiter in waiters:
            waiter.cancel()


async def loop(env: ContextEnv, stopping: asyncio.Event, poll: float = POLL_S) -> None:
    ensure_private_dir(env.cache.root)
    worker = Checkpointer(env)

    async def exports() -> None:
        await worker.round()
        await prlinks.round(env)

    await asyncio.gather(
        _repeat(env, stopping, env.wake, "round", exports, poll=poll),
        _repeat(env, stopping, env.upload_wake, "upload_round", worker.upload_round, poll=poll),
    )


async def run_contexts(context: Any) -> None:
    env = contexts_for(context)
    if env is None:
        context.log.write("runner.contexts not_enrolled\n")
        context.log.flush()
        return
    await loop(env, context.stopping)


TASKS = [TaskSpec("contexts", run_contexts)]
