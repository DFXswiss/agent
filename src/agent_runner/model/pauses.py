"""The model pauses of main sessions (`runner.model_pause`; KOR-50, ADR-081 sections 3 and 4).

A row is written before the guest is asked anything, so a crash in between is carried on by the next run of the
reconcile step `model_fallback` (`model.fallback`):

- `wait`: no allowed access is left (`model.access` answered a wait): the session waits until `until` (the earliest
  release) with `dfx_choice`; a new main session that started on the access with the earliest release has such a row
  from its start (`hold`, before its first prompt);
- `switch`: the session's access is used up and another kind of access is allowed: paused and resumed at once.

`interrupted` = a turn was running (the session gets the continuation sentence), `suspended` = the guest suspended
it, `resumed_at` = the pause ended (an ended pause is never continued again; a new pause of the same session replaces
it). `holding(context)` is the set of sessions whose prompts wait (`billing.pause.deferred`): kept in memory by the
step and by `hold`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from psycopg import AsyncConnection

ATTRIBUTE = "model_pauses"
WAIT = "wait"
SWITCH = "switch"


@dataclass
class Holding:
    sessions: set[str] = field(default_factory=set)


def holding(context: Any) -> set[str]:
    """The sessions whose prompts wait for model quota (module docstring)."""
    if context is None:
        return set()
    found = getattr(context, ATTRIBUTE, None)
    if isinstance(found, Holding):
        return found.sessions
    created = Holding()
    setattr(context, ATTRIBUTE, created)
    return created.sessions


@dataclass(frozen=True)
class Pause:
    session_id: str
    kind: str
    interrupted: bool
    paused_at: datetime
    until: datetime | None
    dfx_choice: bool
    from_access: str
    from_model: str | None
    suspended: bool


def stamp(value: float | None) -> datetime | None:
    return None if value is None else datetime.fromtimestamp(value, UTC)


async def record(
    conn: AsyncConnection[Any],
    session_id: str,
    *,
    kind: str,
    interrupted: bool,
    at: float,
    until: float | None,
    choice: bool,
    from_access: str,
    from_model: str | None,
) -> None:
    """Open (or carry on) the pause of a session; an open pause keeps its start, its `interrupted` and `suspended`."""
    await conn.execute(
        "INSERT INTO runner.model_pause (session_id, kind, interrupted, paused_at, until, dfx_choice, from_access,"
        " from_model) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (session_id) DO UPDATE SET"
        " kind = EXCLUDED.kind, until = EXCLUDED.until, dfx_choice = EXCLUDED.dfx_choice,"
        " interrupted = CASE WHEN runner.model_pause.resumed_at IS NULL"
        " THEN runner.model_pause.interrupted OR EXCLUDED.interrupted ELSE EXCLUDED.interrupted END,"
        " paused_at = CASE WHEN runner.model_pause.resumed_at IS NULL"
        " THEN runner.model_pause.paused_at ELSE EXCLUDED.paused_at END,"
        " suspended = runner.model_pause.resumed_at IS NULL AND runner.model_pause.suspended,"
        " from_access = CASE WHEN runner.model_pause.resumed_at IS NULL"
        " THEN runner.model_pause.from_access ELSE EXCLUDED.from_access END,"
        " from_model = CASE WHEN runner.model_pause.resumed_at IS NULL"
        " THEN runner.model_pause.from_model ELSE EXCLUDED.from_model END, resumed_at = NULL",
        (session_id, kind, interrupted, stamp(at), stamp(until), choice, from_access, from_model),
    )


async def open_pauses(conn: AsyncConnection[Any]) -> list[Pause]:
    cursor = await conn.execute(
        "SELECT session_id::text, kind, interrupted, paused_at, until, dfx_choice, from_access, from_model, suspended"
        " FROM runner.model_pause WHERE resumed_at IS NULL ORDER BY paused_at, session_id"
    )
    return [Pause(*row) for row in await cursor.fetchall()]


async def get(conn: AsyncConnection[Any], session_id: str) -> Pause | None:
    cursor = await conn.execute(
        "SELECT session_id::text, kind, interrupted, paused_at, until, dfx_choice, from_access, from_model, suspended"
        " FROM runner.model_pause WHERE session_id = %s AND resumed_at IS NULL",
        (session_id,),
    )
    row = await cursor.fetchone()
    return None if row is None else Pause(*row)


async def ever(conn: AsyncConnection[Any], session_id: str) -> bool:
    """The session was paused for model quota at some time (a later move back is `released`)."""
    cursor = await conn.execute("SELECT 1 FROM runner.model_pause WHERE session_id = %s", (session_id,))
    return await cursor.fetchone() is not None


async def update(conn: AsyncConnection[Any], session_id: str, **fields: object) -> None:
    """Set columns of the open pause (names are keyword names chosen by the code, never input)."""
    names = sorted(fields)
    assignments = ", ".join(f"{name} = %s" for name in names)
    await conn.execute(
        f"UPDATE runner.model_pause SET {assignments} WHERE session_id = %s AND resumed_at IS NULL",
        [*(fields[name] for name in names), session_id],
    )


async def end(conn: AsyncConnection[Any], session_id: str, at: float) -> None:
    await update(conn, session_id, resumed_at=stamp(at))
