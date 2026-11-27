"""The model pauses of main sessions (`runner.model_pause`; KOR-50, ADR-081 sections 3 and 4).

A row is written before the guest is asked anything, so a crash in between is carried on by the next run of the
reconcile step `model_fallback` (`model.fallback`):

- `wait`: no allowed access is left (`model.access` answered a wait): the session waits until `until` (the earliest
  release) with `dfx_choice`; a new main session that started on the access with the earliest release has such a row
  from its start (`hold`, before its first prompt);
- `switch`: the session's access is used up and another kind of access is allowed: paused and resumed at once.

`interrupted` = a turn was running (the session gets the continuation sentence), `actor_id` = the acting user of that
turn (HOT-28: the continuation acts for that person, `credentials.acting`), `suspended` = the guest suspended it,
`resumed_at` = the pause ended (an ended pause is never continued again; a new pause of the same session replaces it).
`held(context, session_id)`: the session's prompts wait (`billing.pause.deferred`). The set `holding(context)` is
computed by each (serialized) run of the step and replaced as a whole; a wait recorded by a start or resume meanwhile
(`pend`, `model.access.hold`) stays held until a run has seen its row, so a concurrent run cannot undo it.

`report(conn, env, session_id, until, choice)`: towards a hub with `model_fallback.api` the outbox item `session_wait`
`{session_id, wait_until, dfx_choice, at}` whenever the wait of a session starts or changes, and with `wait_until:
null` when it ends (`end`); an unchanged wait is never sent again (the last report of each session is kept in memory).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from psycopg import AsyncConnection

from agent_runner.credentials import acting
from agent_runner.model import policy
from agent_runner.sessions.records import time_text

ATTRIBUTE = "model_pauses"
WAIT = "wait"
SWITCH = "switch"


@dataclass
class Holding:
    sessions: set[str] = field(default_factory=set)
    pending: set[str] = field(default_factory=set)
    reported: dict[str, tuple[float | None, bool]] = field(default_factory=dict)


def state_of(context: Any) -> Holding:
    found = getattr(context, ATTRIBUTE, None)
    if isinstance(found, Holding):
        return found
    created = Holding()
    setattr(context, ATTRIBUTE, created)
    return created


def holding(context: Any) -> set[str]:
    """The sessions the last run of the step holds (module docstring)."""
    return set() if context is None else state_of(context).sessions


def held(context: Any, session_id: str) -> bool:
    """The session's prompts wait for model quota (module docstring)."""
    if context is None:
        return False
    found = state_of(context)
    return session_id in found.sessions or session_id in found.pending


def pend(context: Any, session_id: str) -> None:
    """A wait recorded outside the step: held until a run has seen its row."""
    state_of(context).pending.add(session_id)


async def report(conn: AsyncConnection[Any], env: Any, session_id: str, until: float | None, choice: bool) -> None:
    """The outbox item `session_wait` when the session's wait changed (module docstring)."""
    if env.context is None or not policy.api(env.desired):
        return
    sent = state_of(env.context).reported
    if sent.get(session_id) == (until, choice):
        return
    body = {
        "session_id": session_id,
        "wait_until": None if until is None else time_text(until),
        "dfx_choice": choice,
        "at": env.at(),
    }
    await env.outbox.enqueue(conn, "session_wait", body)
    sent[session_id] = (until, choice)


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
    actor_id: str | None = None
    """The acting user of the interrupted turn (HOT-28, `acting.TURN_ACTOR`; None: that turn had none)."""


COLUMNS = (
    "session_id::text, kind, interrupted, paused_at, until, dfx_choice, from_access, from_model, suspended,"
    " actor_id::text"
)


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
    """Open (or carry on) the pause of a session with the acting user of its turn (`acting.TURN_ACTOR`); an open pause
    keeps its start, its `interrupted` and `suspended`, and an open pause of an interrupted turn its person."""
    await conn.execute(
        "INSERT INTO runner.model_pause (session_id, kind, interrupted, paused_at, until, dfx_choice, from_access,"
        f" from_model, actor_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, {acting.TURN_ACTOR})"
        " ON CONFLICT (session_id) DO UPDATE SET"
        " actor_id = CASE WHEN runner.model_pause.resumed_at IS NULL AND runner.model_pause.interrupted"
        " THEN runner.model_pause.actor_id ELSE EXCLUDED.actor_id END,"
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
        (session_id, kind, interrupted, stamp(at), stamp(until), choice, from_access, from_model, session_id),
    )


async def open_pauses(conn: AsyncConnection[Any]) -> list[Pause]:
    cursor = await conn.execute(
        f"SELECT {COLUMNS} FROM runner.model_pause WHERE resumed_at IS NULL ORDER BY paused_at, session_id"
    )
    return [Pause(*row) for row in await cursor.fetchall()]


async def get(conn: AsyncConnection[Any], session_id: str) -> Pause | None:
    cursor = await conn.execute(
        f"SELECT {COLUMNS} FROM runner.model_pause WHERE session_id = %s AND resumed_at IS NULL", (session_id,)
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


async def end(conn: AsyncConnection[Any], env: Any, session_id: str) -> None:
    """End the open pause of the session; the end of a wait is reported (`wait_until: null`)."""
    found = await get(conn, session_id)
    await update(conn, session_id, resumed_at=stamp(env.wall()))
    if found is not None and found.kind == WAIT:
        await report(conn, env, session_id, None, False)
