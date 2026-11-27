"""The instructions of a child session (`runner.subagent_instruction`, migration 0030; ADR-088 section 1).

A child gets its instructions from its parent session: the `subagent.request` that started it (the first instruction,
handed over with the start's initial prompt) and every `subagent.instruct`. The instruction ID is the operation ID and
the guest's `prompt_id`. The rows hold no text (the prompt is the operation's parameter) and move forward only:

- `add`: the instruction is known (`created_at` = the receipt of its operation, the order of creation);
- `delivered`: the guest registered its prompt (the start's initial prompt counts as registered with the start);
- `accepted` (the state observer, guest `prompt_accepted` with that `prompt_id`): the child's turn on it began;
- `turn_ended` (the state observer, guest `idle`): every accepted instruction of the child whose turn had not ended.

**Live** instructions are the delivered ones and those whose operation is still `executing` (its executor waits for
its turn). A row that was never delivered and whose operation ended (failed, or failed `timeout` after a restart of
the daemon) is no instruction any more: it neither blocks the next one nor answers `get_subagent_result`.

- `ahead(conn, child, operation_id)`: a live instruction of the child created before this one whose turn has not
  ended - the one-at-a-time rule: an instruction is handed over only after every earlier one was accepted and its turn
  ended.
- `newest(conn, children)`: the newest live instruction of each child.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row

COLUMNS = (
    "operation_id::text AS operation_id, child_session_id::text AS child_session_id,"
    " parent_session_id::text AS parent_session_id, actor_id::text AS actor_id, created_at, delivered_at,"
    " accepted_at, ended_at"
)
LIVE = (
    "(i.delivered_at IS NOT NULL OR EXISTS (SELECT 1 FROM runner.operation o WHERE o.id = i.operation_id"
    " AND o.state = 'executing'))"
)


@dataclass(frozen=True)
class Instruction:
    operation_id: str
    child_session_id: str
    parent_session_id: str
    actor_id: str | None
    created_at: datetime
    delivered_at: datetime | None
    accepted_at: datetime | None
    ended_at: datetime | None


async def add(
    conn: AsyncConnection[Any],
    *,
    operation_id: str,
    child_id: str,
    parent_id: str,
    actor_id: str | None,
    created_at: datetime,
    delivered_at: datetime | None = None,
) -> None:
    """Record an instruction once (a repeated call keeps the first row)."""
    await conn.execute(
        "INSERT INTO runner.subagent_instruction (operation_id, child_session_id, parent_session_id, actor_id,"
        " created_at, delivered_at) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (operation_id) DO NOTHING",
        (operation_id, child_id, parent_id, actor_id, created_at, delivered_at),
    )


async def requested_model(conn: AsyncConnection[Any], child_id: str) -> str | None:
    """The `model_id` the `subagent.request` that started the child asked for, None without one (or when the
    operation row is gone)."""
    cursor = await conn.execute(
        "SELECT o.params->>'model_id' FROM runner.subagent_instruction i"
        " JOIN runner.operation o ON o.id = i.operation_id"
        " WHERE i.child_session_id = %s AND o.type = 'subagent.request' ORDER BY i.created_at LIMIT 1",
        (child_id,),
    )
    row = await cursor.fetchone()
    return None if row is None else row[0]


async def get(conn: AsyncConnection[Any], operation_id: str) -> Instruction | None:
    cursor = conn.cursor(row_factory=dict_row)
    await cursor.execute(f"SELECT {COLUMNS} FROM runner.subagent_instruction WHERE operation_id = %s", (operation_id,))
    row = await cursor.fetchone()
    return None if row is None else Instruction(**row)


async def ahead(conn: AsyncConnection[Any], child_id: str, operation_id: str) -> bool:
    """A live instruction of the child before `operation_id` whose turn has not ended (module docstring)."""
    cur = await conn.execute(
        "SELECT EXISTS (SELECT 1 FROM runner.subagent_instruction i, runner.subagent_instruction mine"
        f" WHERE mine.operation_id = %s AND i.child_session_id = %s AND i.ended_at IS NULL AND {LIVE}"
        " AND (i.created_at, i.operation_id) < (mine.created_at, mine.operation_id))",
        (operation_id, child_id),
    )
    row = await cur.fetchone()
    assert row is not None
    return bool(row[0])


async def newest(conn: AsyncConnection[Any], children: list[str]) -> dict[str, Instruction]:
    """The newest live instruction of each of `children` that has one."""
    cursor = conn.cursor(row_factory=dict_row)
    await cursor.execute(
        f"SELECT DISTINCT ON (i.child_session_id) {COLUMNS} FROM runner.subagent_instruction i"
        f" WHERE i.child_session_id = ANY(%s::uuid[]) AND {LIVE}"
        " ORDER BY i.child_session_id, i.created_at DESC, i.operation_id DESC",
        (children,),
    )
    return {row["child_session_id"]: Instruction(**row) for row in await cursor.fetchall()}


async def ended_before_newest(conn: AsyncConnection[Any], children: list[str], since: datetime) -> list[Instruction]:
    """The instructions of `children` whose turn ended at or after `since` and that are not their child's newest one
    (a later instruction followed quickly), oldest first."""
    cursor = conn.cursor(row_factory=dict_row)
    await cursor.execute(
        f"SELECT {COLUMNS} FROM runner.subagent_instruction i WHERE i.child_session_id = ANY(%s::uuid[])"
        " AND i.ended_at IS NOT NULL AND i.ended_at >= %s AND EXISTS (SELECT 1 FROM runner.subagent_instruction j"
        " WHERE j.child_session_id = i.child_session_id AND (j.created_at, j.operation_id) > (i.created_at,"
        " i.operation_id)) ORDER BY i.ended_at, i.operation_id",
        (children, since),
    )
    return [Instruction(**row) for row in await cursor.fetchall()]


async def delivered(conn: AsyncConnection[Any], operation_id: str, at: datetime) -> None:
    await conn.execute(
        "UPDATE runner.subagent_instruction SET delivered_at = %s WHERE operation_id = %s AND delivered_at IS NULL",
        (at, operation_id),
    )


async def undelivered(conn: AsyncConnection[Any], operation_id: str) -> None:
    """A prompt marked as handed over that the guest refused or never got: pending again (it stays live only while
    its operation executes, `LIVE`, so a failed one blocks no later instruction)."""
    await conn.execute(
        "UPDATE runner.subagent_instruction SET delivered_at = NULL WHERE operation_id = %s AND accepted_at IS NULL",
        (operation_id,),
    )


async def accepted(conn: AsyncConnection[Any], child_id: str, prompt_id: str, at: datetime) -> None:
    """The child accepted the prompt `prompt_id` (a UUID): the turn on that instruction began."""
    await conn.execute(
        "UPDATE runner.subagent_instruction SET accepted_at = %s WHERE operation_id = %s AND child_session_id = %s"
        " AND delivered_at IS NOT NULL AND accepted_at IS NULL",
        (at, prompt_id, child_id),
    )


async def turn_ended(conn: AsyncConnection[Any], child_id: str, at: datetime) -> None:
    await conn.execute(
        "UPDATE runner.subagent_instruction SET ended_at = %s WHERE child_session_id = %s AND accepted_at IS NOT NULL"
        " AND ended_at IS NULL",
        (at, child_id),
    )
