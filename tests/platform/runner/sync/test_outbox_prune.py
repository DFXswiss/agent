"""U-RUN-outbox retention (KOR-03, AUD-01 B-19; ADR-038): acknowledged items older than an hour are deleted in
batches, never an unacknowledged one, never the newest one (the numbering continues behind it), nothing while the
hub's position is unknown, only a prefix (KOR-10: a hole would be asked for again by a restored hub). `realign`
renumbers what a restored hub never saw (KOR-10, AUD-02 NA-02)."""

from __future__ import annotations

from typing import Any

from agent_runner.sync import outbox

from .conftest import Harness, run


async def _seqs(conn: Any) -> list[int]:
    cur = await conn.execute("SELECT origin_seq FROM runner.outbox ORDER BY origin_seq")
    return [int(row[0]) for row in await cur.fetchall()]


async def _age(conn: Any, seconds: int, *seqs: int) -> None:
    await conn.execute(
        "UPDATE runner.outbox SET created_at = now() - make_interval(secs => %s) WHERE origin_seq = ANY(%s)",
        (seconds, list(seqs)),
    )


def test_acknowledged_items_older_than_an_hour_are_deleted(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            for _ in range(6):
                await env.enqueue("metrics", {"samples": []})
            async with env.pool.connection() as conn, conn.transaction():
                # the hub's position is unknown: nothing goes
                await _age(conn, 7200, 1, 2, 3, 4, 5, 6)
                assert await outbox.prune(conn) == 0
                await outbox.set_state(conn, outbox.PUSHED, "4")
                await _age(conn, 1800, 4)
                assert await outbox.prune(conn, batch=2) == 2
                assert await outbox.prune(conn) == 1
                # item 4 is younger than an hour, 5 and 6 are not acknowledged
                assert await _seqs(conn) == [4, 5, 6]
                assert await outbox.prune(conn) == 0
                await outbox.set_state(conn, outbox.PUSHED, "6")
                await _age(conn, 7200, 4)
                assert await outbox.prune(conn) == 2
                # the newest item stays, so the numbering continues behind it
                assert await _seqs(conn) == [6]
                assert await outbox.last_origin_seq(conn) == 6
                assert await outbox.backlog(conn) == 0
            assert await env.enqueue("metrics", {"samples": []}) == 7

    run(body)


def test_only_a_prefix_is_deleted(harness: Harness) -> None:
    """`created_at` is the writer's transaction start, so an item can be older than one with a lower number; the
    items above the lowest young one stay, so the stored range never has a hole."""

    async def body() -> None:
        async with harness.env() as env:
            for _ in range(5):
                await env.enqueue("metrics", {"samples": []})
            async with env.pool.connection() as conn, conn.transaction():
                await outbox.set_state(conn, outbox.PUSHED, "5")
                await _age(conn, 7200, 1, 3, 4, 5)
                assert await outbox.prune(conn) == 1
                assert await _seqs(conn) == [2, 3, 4, 5]
                await _age(conn, 7200, 2)
                assert await outbox.prune(conn) == 3
                assert await _seqs(conn) == [5]

    run(body)


def test_realign_leaves_a_stored_or_future_position_to_the_rewind(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            for _ in range(3):
                await env.enqueue("metrics", {"samples": []})
            async with env.pool.connection() as conn, conn.transaction():
                await outbox.set_state(conn, outbox.PUSHED, "3")
                assert await outbox.realign(conn, env.sync.storage_key, 2) is None
                assert await outbox.realign(conn, env.sync.storage_key, 4) is None
                assert await outbox.realign(conn, env.sync.storage_key, 0) is None
                await conn.execute("DELETE FROM runner.outbox WHERE origin_seq = 1")
                assert await outbox.realign(conn, env.sync.storage_key, 1) == 1
                assert await _seqs(conn) == [1, 2, 3]  # old 2 and 3, then the audit
                assert await outbox.pushed_origin_seq(conn) == 0

    run(body)
