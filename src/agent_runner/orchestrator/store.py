"""Rows of `runner.orchestrator` (`store/migrations/0031_orchestrator.sql`; 05-runner.md section 17).

Every function runs in the caller's transaction. States: `registered`, `handed_over` (both may relay) and `ended`
(revoked at the hub while the session goes on); `forget` deletes the row once the session ended.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg import AsyncConnection

RELAYING = ("registered", "handed_over")


@dataclass(frozen=True)
class Pairing:
    session_id: str
    pairing_id: str
    state: str


async def get(conn: AsyncConnection[Any], session_id: str) -> Pairing | None:
    cur = await conn.execute(
        "SELECT session_id::text, pairing_id::text, state FROM runner.orchestrator WHERE session_id = %s",
        (session_id,),
    )
    row = await cur.fetchone()
    return None if row is None else Pairing(session_id=row[0], pairing_id=row[1], state=row[2])


async def register(conn: AsyncConnection[Any], session_id: str, pairing_id: str, at: datetime) -> None:
    """The session's keys are registered at the hub as `pairing_id` (a new registration replaces the pairing)."""
    await conn.execute(
        "INSERT INTO runner.orchestrator (session_id, pairing_id, state, registered_at)"
        " VALUES (%s, %s, 'registered', %s)"
        " ON CONFLICT (session_id) DO UPDATE SET pairing_id = EXCLUDED.pairing_id, state = 'registered',"
        " registered_at = EXCLUDED.registered_at, handed_over_at = NULL",
        (session_id, pairing_id, at),
    )


async def handed_over(conn: AsyncConnection[Any], session_id: str, at: datetime) -> None:
    await conn.execute(
        "UPDATE runner.orchestrator SET state = 'handed_over', handed_over_at = %s WHERE session_id = %s",
        (at, session_id),
    )


async def ended(conn: AsyncConnection[Any], session_id: str, pairing_id: str) -> bool:
    """The hub listed `pairing_id` as revoked: the row ends (no relay; still no checkpoint and no idle suspend)."""
    cur = await conn.execute(
        "UPDATE runner.orchestrator SET state = 'ended' WHERE session_id = %s AND pairing_id = %s AND state <> 'ended'",
        (session_id, pairing_id),
    )
    return cur.rowcount == 1


async def of_ended_sessions(conn: AsyncConnection[Any]) -> list[Pairing]:
    """Rows whose session ended (`stopped`, `failed`) or is unknown on this device."""
    cur = await conn.execute(
        "SELECT o.session_id::text, o.pairing_id::text, o.state FROM runner.orchestrator o"
        " LEFT JOIN runner.session s ON s.id = o.session_id"
        " WHERE s.id IS NULL OR s.state IN ('stopped', 'failed') ORDER BY o.session_id"
    )
    return [Pairing(session_id=row[0], pairing_id=row[1], state=row[2]) for row in await cur.fetchall()]


async def forget(conn: AsyncConnection[Any], session_id: str) -> None:
    """Delete the row of an ended session together with a checkpoint still due for it (it must never be taken)."""
    await conn.execute(
        "UPDATE runner.context_session SET due_at = NULL WHERE session_id = %s AND due_at IS NOT NULL", (session_id,)
    )
    await conn.execute("DELETE FROM runner.orchestrator WHERE session_id = %s", (session_id,))


async def session_ids(conn: AsyncConnection[Any]) -> set[str]:
    """Sessions with a row of any state (never suspended when idle: the orchestrator would end, 05 section 17.6)."""
    cur = await conn.execute("SELECT session_id::text FROM runner.orchestrator")
    return {row[0] for row in await cur.fetchall()}
