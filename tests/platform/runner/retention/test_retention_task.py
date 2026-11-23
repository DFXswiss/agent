"""U-RUN-retention: the daemon task `retention` (KOR-03, AUD-01 B-19) prunes the outbox and the prompt events in
batches of their own transactions every ten minutes, logs what it deleted and survives a failing round."""

from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace
from typing import Any

import pytest

from agent_runner import registry
from agent_runner.prompts import retention as prompt_retention
from agent_runner.retention import tasks
from agent_runner.store.pool import open_pool
from agent_runner.sync import outbox


def test_the_task_and_its_pruners_are_registered() -> None:
    assert "retention" in [spec.name for spec in registry.tasks()]
    assert (
        ("outbox", outbox.prune, outbox.PRUNE_BATCH),
        ("prompt_event", prompt_retention.prune, prompt_retention.PRUNE_BATCH),
    ) == tasks.PRUNERS


def _context(pool: Any, sleeps: list[float], stop_after: int) -> SimpleNamespace:
    context = SimpleNamespace(pool=pool, log=io.StringIO(), stopping=asyncio.Event())

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= stop_after:
            context.stopping.set()

    context.host = SimpleNamespace(sleep=sleep)
    return context


def test_rounds_prune_in_batches_log_counts_and_survive_failures(
    store_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = {"a": [2, 2, 1, 0, 0], "b": [0, 0]}
    calls: list[str] = []

    def pruner(name: str) -> Any:
        async def prune(conn: Any) -> int:
            calls.append(name)
            await conn.execute("SELECT 1")
            if not answers[name]:
                raise RuntimeError("boom")
            return answers[name].pop(0)

        return prune

    monkeypatch.setattr(tasks, "PRUNERS", (("a", pruner("a"), 2), ("b", pruner("b"), 2)))

    async def main() -> tuple[list[float], str]:
        pool = await open_pool(store_dsn)
        try:
            sleeps: list[float] = []
            context = _context(pool, sleeps, stop_after=4)
            await tasks.run_retention(context)
            return sleeps, context.log.getvalue()
        finally:
            await pool.close()

    sleeps, log = asyncio.run(main())
    assert sleeps == [tasks.INTERVAL_S] * 4
    # round 1: a in three batches (2, 2, 1), b once; round 2: nothing (no line); round 3: b fails; round 4: a fails
    assert calls == ["a", "a", "a", "b", "a", "b", "a", "b", "a"]
    assert log.splitlines() == [
        "runner.retention a=5 b=0",
        "runner.retention failed error=RuntimeError",
        "runner.retention failed error=RuntimeError",
    ]


def test_a_real_round_on_an_empty_store(store_dsn: str) -> None:
    async def main() -> dict[str, int]:
        pool = await open_pool(store_dsn)
        try:
            return await tasks.prune_all(SimpleNamespace(pool=pool))
        finally:
            await pool.close()

    assert asyncio.run(main()) == {"outbox": 0, "prompt_event": 0}
