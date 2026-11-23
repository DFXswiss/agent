"""U-RUN-prompts retention (KOR-03, AUD-01 B-19; RUN-05 decision 25): a prompt event whose delivery has been final
for a day goes; its delivery row stays (never delivered again), open or unrouted prompts and the earliest delivered
prompt of each task stay (restart detection)."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import psycopg

from agent_runner.prompts import queue, retention
from agent_runner.store.pool import open_pool

ORG = "01930001-0000-7000-8000-000000000001"
WG = "01930002-0000-7000-8000-00000000000a"
TASK_A = "01930003-0000-7000-8000-00000000000a"
TASK_B = "01930003-0000-7000-8000-00000000000b"
TASK_C = "01930003-0000-7000-8000-00000000000c"
SESSION = "01930004-0000-7000-8000-000000000001"
DAY = 24 * 3600


def _prompt(conn: psycopg.Connection[Any], task: str, seq: int, state: str | None, age: int) -> str:
    event_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO runner.prompt_event (event_id, org_id, wg_id, task_id, scope_seq, author_sig, sealed, received_at)"
        " VALUES (%s, %s, %s, %s, %s, 's', %s, now() - make_interval(secs => %s))",
        (event_id, ORG, WG, task, seq, json.dumps({"header": {}}), age),
    )
    if state is not None:
        conn.execute(
            "INSERT INTO runner.prompt_delivery (event_id, session_id, state, updated_at, task_id, scope_seq)"
            " VALUES (%s, %s, %s, now() - make_interval(secs => %s), %s, %s)",
            (event_id, SESSION, state, age, task, seq),
        )
    return event_id


def test_delivered_prompts_go_after_a_day_and_what_is_still_needed_stays(store_dsn: str) -> None:
    with psycopg.connect(store_dsn, autocommit=True) as conn:
        refused_first = _prompt(conn, TASK_A, 1, "refused", 2 * DAY)
        first = _prompt(conn, TASK_A, 2, "accepted", 2 * DAY)  # earliest prompt that counts: stays
        old = [_prompt(conn, TASK_A, seq, state, 2 * DAY) for seq, state in ((3, "accepted"), (4, "failed"))]
        recent = _prompt(conn, TASK_A, 5, "accepted", 3600)
        typed = _prompt(conn, TASK_A, 6, "typed", 2 * DAY)
        unrouted = _prompt(conn, TASK_A, 7, None, 2 * DAY)
        other_first = _prompt(conn, TASK_B, 8, "accepted", 2 * DAY)
        other_old = _prompt(conn, TASK_B, 9, "accepted", 2 * DAY)
        # task C: the earliest prompt is open now and refused later; the accepted one after it stays (restart hint)
        open_c = _prompt(conn, TASK_C, 10, "typed", 2 * DAY)
        accepted_c = _prompt(conn, TASK_C, 11, "accepted", 2 * DAY)

    async def prune() -> tuple[int, int, bool]:
        pool = await open_pool(store_dsn)
        try:
            async with pool.connection() as conn, conn.transaction():
                partial = await retention.prune(conn, batch=1)
                rest = await retention.prune(conn)
                restarted = await queue.earlier_of_task(conn, TASK_A, 10)
            return partial, rest, restarted
        finally:
            await pool.close()

    assert asyncio.run(prune()) == (1, 3, True)
    with psycopg.connect(store_dsn, autocommit=True) as conn:
        conn.execute("UPDATE runner.prompt_delivery SET state = 'refused' WHERE event_id = %s", (open_c,))
    assert asyncio.run(prune())[:2] == (1, 0)
    with psycopg.connect(store_dsn, autocommit=True) as conn:
        kept = {str(row[0]) for row in conn.execute("SELECT event_id FROM runner.prompt_event").fetchall()}
        deliveries = conn.execute("SELECT count(*) FROM runner.prompt_delivery").fetchone()
    assert kept == {first, recent, typed, unrouted, other_first, accepted_c}
    assert {refused_first, *old, other_old, open_c}.isdisjoint(kept)
    # every delivery row stays: a final prompt is never delivered again when the hub routes it once more
    assert deliveries == (10,)
