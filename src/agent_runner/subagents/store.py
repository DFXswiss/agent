"""Store queries of the subagent area over `runner.session` and `runner.operation` (02-data-model.md section 4).

Children are ordinary session rows (`depth` > 0, `parent_id` set) and their requests are `subagent.request` rows of the
operation pipeline (`child_session_id` from the hub's decision); the only table of the area is the one of the
children's instructions (`instructions.py`, ADR-088 section 1).
"""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from agent_runner.sessions.records import COLUMNS, FINAL, LocalSession

OP_TYPE = "subagent.request"
IN_FLIGHT = ("accepted", "executing")
"""Operation states in which the hub created a child that this device may not have started yet."""
RECENT_S = 3600
"""Children that ended within this time are still watched (finished ping, settling of the final text)."""


async def children(conn: AsyncConnection[Any]) -> list[LocalSession]:
    """Subagent sessions that have not ended or ended within `RECENT_S`, oldest first."""
    cursor = conn.cursor(row_factory=dict_row)
    await cursor.execute(
        f"SELECT {COLUMNS} FROM runner.session WHERE depth > 0 AND (state <> ALL(%s)"
        " OR ended_at > now() - make_interval(secs => %s)) ORDER BY created_at, id",
        (sorted(FINAL), RECENT_S),
    )
    return [LocalSession(**row) for row in await cursor.fetchall()]


async def in_flight(conn: AsyncConnection[Any], task_id: str) -> int:
    """Accepted `subagent.request` operations of the task whose child is not a local session yet."""
    cur = await conn.execute(
        "SELECT count(*) FROM runner.operation o WHERE o.task_id = %s AND o.type = %s AND o.state = ANY(%s)"
        " AND NOT EXISTS (SELECT 1 FROM runner.session s WHERE s.id = o.child_session_id)",
        (task_id, OP_TYPE, list(IN_FLIGHT)),
    )
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def counts(conn: AsyncConnection[Any], task_id: str) -> tuple[int, int]:
    """`(running, total)` children of the task known on this device, the accepted ones not started yet included;
    running = not ended and not `suspended` (ADR-088 section 2)."""
    cur = await conn.execute(
        "SELECT count(*) FILTER (WHERE state <> ALL(%s)), count(*) FROM runner.session"
        " WHERE task_id = %s AND depth > 0",
        (sorted(FINAL | {"suspended"}), task_id),
    )
    row = await cur.fetchone()
    assert row is not None
    pending = await in_flight(conn, task_id)
    return int(row[0]) + pending, int(row[1]) + pending


async def busy_tasks(conn: AsyncConnection[Any]) -> set[str]:
    """Tasks with a `subagent.request` that the pipeline is still working on (its child may appear any moment)."""
    cur = await conn.execute(
        "SELECT DISTINCT task_id::text FROM runner.operation WHERE type = %s AND state = ANY(%s)",
        (OP_TYPE, ["received", *IN_FLIGHT]),
    )
    return {str(row[0]) for row in await cur.fetchall()}


async def requested_child(conn: AsyncConnection[Any], parent_id: str, child_id: str) -> bool:
    """The hub named `child_id` as the child of an accepted request of `parent_id` (its start may still be due)."""
    cur = await conn.execute(
        "SELECT 1 FROM runner.operation WHERE type = %s AND session_id = %s AND child_session_id = %s"
        " AND state = ANY(%s)",
        (OP_TYPE, parent_id, child_id, [*IN_FLIGHT, "succeeded"]),
    )
    return await cur.fetchone() is not None


async def parents_ended(conn: AsyncConnection[Any], children: list[str]) -> set[str]:
    """Those of `children` whose parent session ended (`stopped`/`failed`, ADR-088 section 1: they end with it)."""
    cur = await conn.execute(
        "SELECT c.id::text FROM runner.session c JOIN runner.session p ON p.id = c.parent_id"
        " WHERE c.id = ANY(%s::uuid[]) AND p.state = ANY(%s)",
        (children, sorted(FINAL)),
    )
    return {str(row[0]) for row in await cur.fetchall()}
