"""U-RUN-model (KOR-35, ADR-086 section 3): the usage deltas of a session are merged over 10 s into one outbox item
`usage`; the totals the hub counts stay the same, and the platform budget sees the pending cost."""

from __future__ import annotations

import asyncio
import io
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from agent_runner.model import merge, uncounted, usage
from agent_runner.model.tasks import TASKS

from ..sessions.kit import desired, session_id
from .kit import FLASH, OPUS, Env, attach
from .test_usage import TOKENS, consume, record, run, started

START = 1_790_000_000.0


class Wall:
    def __init__(self) -> None:
        self.value = START

    def __call__(self) -> float:
        return self.value


def test_merged_adds_the_deltas_and_keeps_the_newest_fields() -> None:
    old = {"session_id": "s", "delta": {"requests": 2, "input_tokens": 5}, "last_provider": "A", "cost_basis": "x"}
    new = {"session_id": "s", "delta": {"requests": 1, "input_tokens": 2**53 - 3}, "last_provider": None}
    assert merge.merged(old, new) == {
        "session_id": "s",
        "delta": {"requests": 3, "input_tokens": 2**53 - 1},  # capped at the largest contract count
        "last_provider": None,
    }


async def pending(env: Env) -> list[tuple[str, dict[str, Any], float]]:
    async with env.pool.connection() as conn:
        cur = await conn.execute("SELECT session_id::text, item, first_wall FROM runner.usage_pending")
        return [(str(sid), item, float(first)) for sid, item, first in await cur.fetchall()]


async def flush(found: Any, every: bool = False) -> int:
    async with found.session_env.transaction() as conn:
        return await merge.flush(conn, found.session_env, every)


def test_requests_within_10_s_become_one_item_with_the_same_totals(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        found, reader = await started(env, "api_key")
        wall = Wall()
        found.session_env.wall = wall
        sid = session_id()
        records = [
            record(OPUS, **TOKENS),
            record(OPUS, input_tokens=40_000, output_tokens=7, status=429, context_window_tokens=0),
            record(OPUS, input_tokens=60_000, thinking_tokens=9),
        ]
        for value in records[:2]:
            found.guest.emit(sid, "usage", value)
        await consume(found, reader)
        assert await found.of_kind("usage") == []
        ((row_sid, item, first),) = await pending(env)
        assert (row_sid, item["delta"]["requests"], first) == (sid, 2, START)
        wall.value += 9.5
        found.guest.emit(sid, "usage", records[2])
        await consume(found, reader)
        assert await found.of_kind("usage") == []
        wall.value += 0.5  # the first delta is 10 s old: the next one writes the item
        found.guest.emit(sid, "usage", record(OPUS))
        await consume(found, reader)
        (written,) = await found.of_kind("usage")
        assert await pending(env) == []
        session = SimpleNamespace(id=sid, model_access="api_key")
        catalog = found.session_env.catalog
        single = [usage.usage_item(session, value, catalog, None) for value in [*records, record(OPUS)]]  # type: ignore[arg-type]
        assert written["delta"] == {
            name: sum(one["delta"][name] for one in single) for name in written["delta"]
        }  # the hub's totals are unchanged
        assert written["delta"]["requests"] == 4
        assert (written["context_used_tokens"], written["context_window_tokens"]) == (60_000, 1_000_000)
        await env.sync.pusher.drain()  # the hub checks the merged item against contracts/outbox/usage.json
        assert [i["item_kind"] for i in env.hub.items()].count("usage") == 1

    run(store_dsn, body)


def test_due_and_ended_sessions_are_flushed(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        found, reader = await started(env, "platform")
        wall = Wall()
        found.session_env.wall = wall
        sid = session_id()
        found.guest.emit(sid, "usage", record(FLASH, cost_usd=0.25))
        await consume(found, reader)
        assert await flush(found) == 0
        wall.value += 10
        assert await flush(found) == 1
        found.guest.emit(sid, "usage", record(FLASH, cost_usd=0.5))
        await consume(found, reader)
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.session SET state = 'stopped' WHERE id = %s", (sid,))
        assert await flush(found) == 1  # the session ended: at once
        found.guest.emit(sid, "usage", record(FLASH, cost_usd=1.0))
        await consume(found, reader)
        assert await flush(found, every=True) == 1
        assert [item["delta"]["cost_micro_usd"] for item in await found.of_kind("usage")] == [
            250_000,
            500_000,
            1_000_000,
        ]
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.session SET state = 'working' WHERE id = %s", (sid,))
            await conn.execute(
                "INSERT INTO runner.usage_pending (session_id, item, first_wall) VALUES (%s, %s, %s)",
                (str(uuid.uuid4()), Jsonb({"session_id": "x"}), wall.value),
            )
        assert len(await pending(env)) == 1
        found.session_env.outbox = SimpleNamespace(enqueue=fake_enqueue)
        assert await flush(found) == 1  # a row of a session the store no longer knows is written too
        assert await pending(env) == []

    run(store_dsn, body)


async def fake_enqueue(conn: Any, item_kind: str, body: Any) -> int:
    assert (item_kind, body) == ("usage", {"session_id": "x"})
    return 1


def test_the_platform_budget_counts_the_pending_cost(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        found, reader = await started(env, "platform")
        found.session_env.wall = Wall()
        sid = session_id()
        found.guest.emit(sid, "usage", record(FLASH, cost_usd=0.002))
        found.guest.emit(sid, "usage", record(FLASH, cost_usd=0.001))
        await consume(found, reader)
        assert await found.of_kind("usage") == []
        async with env.pool.connection() as conn:
            assert await uncounted.uncounted_micro(conn, env.sync.storage_key, desired().org_id, 0) == 3000
        await flush(found, every=True)
        async with env.pool.connection() as conn:
            assert await uncounted.uncounted_micro(conn, env.sync.storage_key, desired().org_id, 0) == 3000

    run(store_dsn, body)


def test_the_daemon_task_flushes_every_second(store_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    assert [task.name for task in TASKS] == ["operator_subscriptions", "model_fallback", "usage_merge"]

    async def body(env: Env) -> None:
        found, reader = await started(env, "platform")
        wall = Wall()
        found.session_env.wall = wall
        found.guest.emit(session_id(), "usage", record(FLASH))
        await consume(found, reader)
        await merge.tick(env.context)  # no session service yet: nothing
        attach(found)
        await merge.tick(env.context)
        assert await found.of_kind("usage") == []
        wall.value += 10
        await merge.tick(env.context)
        assert len(await found.of_kind("usage")) == 1

    run(store_dsn, body)

    calls: list[float] = []
    stopping = asyncio.Event()

    async def sleep(seconds: float) -> None:
        calls.append(seconds)
        if len(calls) == 2:
            stopping.set()

    async def failing(context: Any) -> None:
        if not calls:
            raise RuntimeError("store gone")

    monkeypatch.setattr(merge, "tick", failing)
    log = io.StringIO()
    context = SimpleNamespace(stopping=stopping, host=SimpleNamespace(sleep=sleep), log=log)
    asyncio.run(merge.run(context))
    assert calls == [merge.TICK_S, merge.TICK_S]
    assert log.getvalue() == "runner.model usage_merge_failed error=RuntimeError\n"


def test_only_deltas_of_the_same_model_and_access_merge(store_dsn: str) -> None:
    """KOR-31 bills an item under its `model` and `access_origin`: a delta of another model or access writes the
    pending item out first; `at` is the newest merged request's time."""

    async def body(env: Env) -> None:
        written: list[dict[str, Any]] = []

        async def enqueue(conn: Any, item_kind: str, item: Any) -> int:
            assert item_kind == "usage"
            written.append(dict(item))
            return len(written)

        wall = Wall()
        fake = SimpleNamespace(wall=wall, outbox=SimpleNamespace(enqueue=enqueue))
        sid = session_id()

        def delta(model: str, origin: str, at: str, cost: int) -> dict[str, Any]:
            return {
                "session_id": sid,
                "delta": {"requests": 1, "cost_micro_usd": cost},
                "model": model,
                "access_origin": origin,
                "at": at,
            }

        async def add(item: dict[str, Any]) -> None:
            async with env.pool.connection() as conn, conn.transaction():
                await merge.add(conn, fake, item)

        await add(delta(OPUS, "platform", "2026-11-24T10:00:00Z", 1))
        wall.value += 2
        await add(delta(OPUS, "platform", "2026-11-24T10:00:02Z", 2))
        wall.value += 2
        await add(delta(FLASH, "platform", "2026-11-24T10:00:04Z", 4))  # another model
        assert written == [
            {**delta(OPUS, "platform", "2026-11-24T10:00:02Z", 3), "delta": {"requests": 2, "cost_micro_usd": 3}}
        ]
        ((_, item, first),) = await pending(env)
        assert (item["model"], item["delta"]["cost_micro_usd"], first) == (FLASH, 4, START + 4)  # a new window
        wall.value += 1
        await add(delta(FLASH, "operator", "2026-11-24T10:00:05Z", 8))  # another access
        assert [one["delta"]["cost_micro_usd"] for one in written] == [3, 4]
        assert written[1]["access_origin"] == "platform"
        ((_, item, first),) = await pending(env)
        assert (item["access_origin"], first) == ("operator", START + 5)

    run(store_dsn, body)


def test_mergeable_needs_the_same_billing_key_and_utc_day() -> None:
    row = {"model": OPUS, "access_origin": "platform", "at": "2026-11-24T23:59:58.000Z"}
    assert merge.mergeable(row, {**row, "at": "2026-11-24T23:59:59.999Z"})
    assert not merge.mergeable(row, {**row, "at": "2026-11-25T00:00:00.000Z"})
    assert not merge.mergeable(row, {**row, "model": FLASH})
    # KOR-46: consumption is booked to the person whose instruction caused it
    assert not merge.mergeable({**row, "triggered_by": "a"}, {**row, "triggered_by": "b"})
    # a delta (or a pending row) without a valid time has no day: it merges and keeps the known time
    assert merge.mergeable(row, {"model": OPUS, "access_origin": "platform"})
    assert merge.mergeable({"model": OPUS, "access_origin": "platform"}, row)


def test_deltas_of_another_utc_day_do_not_merge(store_dsn: str) -> None:
    """Review (KOR-35): the hub prices and books an item at its `at`; a merged item never spans midnight UTC."""

    async def body(env: Env) -> None:
        written: list[dict[str, Any]] = []

        async def enqueue(conn: Any, item_kind: str, item: Any) -> int:
            written.append(dict(item))
            return len(written)

        wall = Wall()
        fake = SimpleNamespace(wall=wall, outbox=SimpleNamespace(enqueue=enqueue))
        sid = session_id()

        def delta(at: str | None, cost: int) -> dict[str, Any]:
            item: dict[str, Any] = {
                "session_id": sid,
                "delta": {"requests": 1, "cost_micro_usd": cost},
                "model": OPUS,
                "access_origin": "platform",
            }
            return item if at is None else {**item, "at": at}

        async def add(item: dict[str, Any]) -> None:
            async with env.pool.connection() as conn, conn.transaction():
                await merge.add(conn, fake, item)

        await add(delta("2026-11-24T23:59:57.000Z", 1))
        wall.value += 1
        await add(delta(None, 2))  # no time: merges, the known time stays
        wall.value += 2
        await add(delta("2026-11-25T00:00:00.500Z", 4))  # the next UTC day, 3.5 s later
        assert written == [{**delta("2026-11-24T23:59:57.000Z", 3), "delta": {"requests": 2, "cost_micro_usd": 3}}]
        ((_, item, first),) = await pending(env)
        assert (item["at"], item["delta"]["cost_micro_usd"], first) == ("2026-11-25T00:00:00.500Z", 4, START + 3)

    run(store_dsn, body)
