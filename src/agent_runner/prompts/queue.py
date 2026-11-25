"""Prompt store: verified prompt events and their delivery rows (02-data-model.md section 4 `prompt_delivery`, migration
0003; 05-runner.md sections 2.2 step 6, 2.3 and 6.2; 03-api.md section 6.1 `pending_prompts`,
`delivered_prompt_event_ids`).

- `runner.prompt_event`: every prompt event the inbox verified (E1-E5, author `wg:write` at `lh`), kept exactly as
  received with the author's verified signing key and user ID (`author_id`: the acting user of the turn the prompt
  starts, `credentials.acting`). The text is opened only into memory (`verify.open_text`).
- `runner.prompt_delivery`: one row per prompt event the hub routed to a session of this device (`event_id`,
  `session_id`, `task_id`, `scope_seq`, `channel`, `attempts`), with the delivery state:

  * `pending`: routed, not yet handed to the guest;
  * `registered`: being handed over, written **before** the guest call (write-ahead), so a crash or a network error
    between the call and its answer leaves a row that is sent again with the same `prompt_id` (the guest keys its
    registry by `prompt_id`, so a repeat never delivers twice);
  * `typed`: the guest holds it - `POST .../prompts` answered `registered` (channel `prompt`), or the guest started
    the agent with it as initial prompt (channel `start`, seen as `agent_starting`);
  * `accepted`: the UserPromptSubmit hook accepted it (`prompt_accepted`), or the hub lists it as delivered;
  * `refused`: never deliverable (misrouted, stale, unreadable, refused by the guest's registration);
  * `failed`: the guest could not deliver it into the TUI (`prompt_refused` `delivery_failed`);
  * `withdrawn`: its author took it back while it was still `pending` (ADR-083 section 3, `withdraw`).

  `accepted`, `refused`, `failed` and `withdrawn` are final: such a prompt is never delivered again, also not when the
  hub routes it once more. `via_agent` keeps the hub's report that the prompt came through its author's agent (the
  qualifier of the sender line, `speaker.py`); once set it stays. A row of an open state whose session ended
  (`stopped`, `failed`, or unknown locally) is moved to the session the hub routes it to next (a new main session of
  the task, 03-api.md section 4.6.3); the guest registry of the ended session can no longer accept it.
  One exception to finality (ADR-087 sections 3.4 and 3.7): the interrupted instruction of a session lost with a
  failed device (or with this device's restart) is repeated to its successor. When the hub routes a prompt whose row
  is `accepted` for session L to a session S that the device state lists in `failovers` with `from_session` = L, the
  row moves to S as `pending` (`route(..., failover_from=L)`) and is delivered once more; once it belongs to S it is
  final again (a further routing to S changes nothing).
- `adopt_delivered`: the hub's `delivered_prompt_event_ids` (after a store wipe the only memory of what was delivered)
  make those rows `accepted` - inserted when unknown - so they are never delivered again.
- `withdraw` (ADR-083 section 3, `prompt_queue.withdrawn` of the device state): a row still `pending` becomes
  `withdrawn`; a prompt without a row yet is remembered in `runner.prompt_withdrawal`, and its routing records the row
  `withdrawn` at once; a row `registered` or later may already be at the guest and is delivered. Each withdrawal is
  recorded once (`prompt_withdrawal`, also for the ones with a row) and reported once by the caller. A withdrawn prompt
  is final everywhere: it never holds back a later prompt (`earlier_unrouted`), never makes one stale
  (`handed_after`), and a session that starts after it is not a restarted one (`earlier_of_task`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

OPEN = ("pending", "registered", "typed")
FINAL = frozenset({"accepted", "refused", "failed", "withdrawn"})
STATES = frozenset({*OPEN, *FINAL})
ENDED_SESSION = ("stopped", "failed")


@dataclass(frozen=True)
class StoredPrompt:
    event_id: str
    org_id: str
    wg_id: str
    task_id: str
    scope_seq: int
    author_sig: str
    sealed: Mapping[str, Any]


@dataclass(frozen=True)
class Delivery:
    event_id: str
    session_id: str
    task_id: str | None
    scope_seq: int | None
    state: str
    channel: str
    attempts: int
    age: float
    """Seconds since the last change of the row (store clock)."""
    via_agent: bool = False


_DELIVERY_COLUMNS = (
    "event_id::text AS event_id, session_id::text AS session_id, task_id::text AS task_id, scope_seq, state,"
    " channel, attempts, EXTRACT(EPOCH FROM (now() - updated_at))::float8 AS age, via_agent"
)
_WITHDRAWN = "EXISTS (SELECT 1 FROM runner.prompt_withdrawal w WHERE w.event_id = e.event_id)"


async def keep(
    conn: AsyncConnection[Any],
    *,
    header: Mapping[str, object],
    scope_seq: int,
    author_sig: str,
    sealed: Mapping[str, object],
    author_id: str,
) -> bool:
    """Store a verified prompt event; False when it is known (by ID or by its place in the scope)."""
    cur = await conn.execute(
        "INSERT INTO runner.prompt_event (event_id, org_id, wg_id, task_id, scope_seq, author_sig, sealed, author_id)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
        (
            header["id"],
            header["org"],
            header["scope_id"],
            header["task"],
            scope_seq,
            author_sig,
            Jsonb(dict(sealed)),
            author_id,
        ),
    )
    if cur.rowcount != 1:
        return False
    await _place(conn)
    return True


async def _place(conn: AsyncConnection[Any]) -> None:
    """Rows adopted from the hub's delivered list before their event was stored get the event's task and place, so
    the stale check sees them (an older prompt left out of the list is never delivered again after a wipe)."""
    await conn.execute(
        "UPDATE runner.prompt_delivery d SET task_id = e.task_id, scope_seq = e.scope_seq FROM runner.prompt_event e"
        " WHERE d.event_id = e.event_id AND d.task_id IS NULL"
    )


def _stored(row: Mapping[str, Any]) -> StoredPrompt:
    return StoredPrompt(
        event_id=row["event_id"],
        org_id=row["org_id"],
        wg_id=row["wg_id"],
        task_id=row["task_id"],
        scope_seq=int(row["scope_seq"]),
        author_sig=row["author_sig"],
        sealed=row["sealed"],
    )


_EVENT_COLUMNS = (
    "event_id::text AS event_id, org_id::text AS org_id, wg_id::text AS wg_id, task_id::text AS task_id, scope_seq,"
    " author_sig, sealed"
)


async def event(conn: AsyncConnection[Any], event_id: str) -> StoredPrompt | None:
    cursor = conn.cursor(row_factory=dict_row)
    await cursor.execute(f"SELECT {_EVENT_COLUMNS} FROM runner.prompt_event WHERE event_id = %s", (event_id,))
    row = await cursor.fetchone()
    return None if row is None else _stored(row)


async def event_at(conn: AsyncConnection[Any], wg_id: str, scope_seq: int) -> StoredPrompt | None:
    cursor = conn.cursor(row_factory=dict_row)
    await cursor.execute(
        f"SELECT {_EVENT_COLUMNS} FROM runner.prompt_event WHERE wg_id = %s AND scope_seq = %s", (wg_id, scope_seq)
    )
    row = await cursor.fetchone()
    return None if row is None else _stored(row)


async def earlier_of_task(conn: AsyncConnection[Any], task_id: str, scope_seq: int) -> bool:
    """Whether the task has a stored prompt before `scope_seq` that was not refused, failed or withdrawn (a session that
    does not start with the task's first prompt is a restarted session)."""
    cur = await conn.execute(
        "SELECT 1 FROM runner.prompt_event e WHERE e.task_id = %s AND e.scope_seq < %s AND NOT EXISTS"
        " (SELECT 1 FROM runner.prompt_delivery d WHERE d.event_id = e.event_id"
        " AND d.state IN ('refused', 'failed', 'withdrawn')) AND NOT " + _WITHDRAWN + " LIMIT 1",
        (task_id, scope_seq),
    )
    return await cur.fetchone() is not None


async def earlier_unrouted(conn: AsyncConnection[Any], task_id: str, scope_seq: int) -> bool:
    """Whether the task has a stored prompt before `scope_seq` that has no delivery row yet, was not withdrawn and comes
    after every prompt of the task that was handed over (it may still be routed: its hint was lost)."""
    cur = await conn.execute(
        "SELECT 1 FROM runner.prompt_event e WHERE e.task_id = %s AND e.scope_seq < %s"
        " AND NOT EXISTS (SELECT 1 FROM runner.prompt_delivery d WHERE d.event_id = e.event_id)"
        " AND NOT " + _WITHDRAWN + " AND NOT EXISTS (SELECT 1 FROM runner.prompt_delivery h WHERE h.task_id = e.task_id"
        " AND h.scope_seq > e.scope_seq AND h.state IN ('registered', 'typed', 'accepted')) LIMIT 1",
        (task_id, scope_seq),
    )
    return await cur.fetchone() is not None


async def delivery(conn: AsyncConnection[Any], event_id: str, *, lock: bool = False) -> Delivery | None:
    cursor = conn.cursor(row_factory=dict_row)
    await cursor.execute(
        f"SELECT {_DELIVERY_COLUMNS} FROM runner.prompt_delivery WHERE event_id = %s" + (" FOR UPDATE" if lock else ""),
        (event_id,),
    )
    row = await cursor.fetchone()
    return None if row is None else Delivery(**row)


async def open_rows(conn: AsyncConnection[Any], session_id: str) -> list[Delivery]:
    """The open rows of a session in delivery order (`scope_seq`)."""
    cursor = conn.cursor(row_factory=dict_row)
    await cursor.execute(
        f"SELECT {_DELIVERY_COLUMNS} FROM runner.prompt_delivery WHERE session_id = %s AND state = ANY(%s)"
        " ORDER BY scope_seq NULLS FIRST, event_id",
        (session_id, list(OPEN)),
    )
    return [Delivery(**row) for row in await cursor.fetchall()]


async def sessions_with_open_rows(conn: AsyncConnection[Any]) -> list[str]:
    cur = await conn.execute(
        "SELECT DISTINCT session_id::text FROM runner.prompt_delivery WHERE state = ANY(%s) ORDER BY 1", (list(OPEN),)
    )
    return [str(row[0]) for row in await cur.fetchall()]


async def handed_after(conn: AsyncConnection[Any], task_id: str, scope_seq: int) -> bool:
    """Whether a later prompt of the task was already handed to a guest or accepted (an earlier one is then stale)."""
    cur = await conn.execute(
        "SELECT 1 FROM runner.prompt_delivery WHERE task_id = %s AND scope_seq > %s"
        " AND state IN ('registered', 'typed', 'accepted') LIMIT 1",
        (task_id, scope_seq),
    )
    return await cur.fetchone() is not None


async def route(
    conn: AsyncConnection[Any], prompt: StoredPrompt, session_id: str, *, failover_from: str | None = None
) -> str:
    """Record that the hub routes `prompt` to `session_id`: the row's state afterwards (`pending` for a new row, one
    moved from an ended session, or an `accepted` row of `failover_from` - the lost session `session_id` continues,
    module docstring -, `withdrawn` for a new row of a prompt withdrawn before, else the existing state)."""
    await conn.execute(
        "INSERT INTO runner.prompt_delivery (event_id, session_id, task_id, scope_seq, state)"
        " VALUES (%s, %s, %s, %s, CASE WHEN EXISTS (SELECT 1 FROM runner.prompt_withdrawal w WHERE w.event_id = %s)"
        " THEN 'withdrawn' ELSE 'pending' END) ON CONFLICT (event_id) DO UPDATE SET"
        " session_id = EXCLUDED.session_id, task_id = EXCLUDED.task_id, scope_seq = EXCLUDED.scope_seq,"
        " state = 'pending', channel = 'prompt', attempts = 0, updated_at = now()"
        " WHERE (runner.prompt_delivery.state = ANY(%s) AND runner.prompt_delivery.session_id <> EXCLUDED.session_id"
        " AND NOT EXISTS (SELECT 1 FROM runner.session s WHERE s.id = runner.prompt_delivery.session_id"
        " AND s.state <> ALL(%s)))"
        " OR (runner.prompt_delivery.state = 'accepted' AND runner.prompt_delivery.session_id::text = %s)",
        (
            prompt.event_id,
            session_id,
            prompt.task_id,
            prompt.scope_seq,
            prompt.event_id,
            list(OPEN),
            list(ENDED_SESSION),
            failover_from,
        ),
    )
    found = await delivery(conn, prompt.event_id)
    assert found is not None
    return found.state if found.session_id == session_id else "elsewhere"


async def set_state(
    conn: AsyncConnection[Any],
    event_id: str,
    state: str,
    *,
    expected: Iterable[str] | None = None,
    channel: str | None = None,
    attempt: bool = False,
) -> bool:
    """Move a row to `state` (only from `expected` when given); True when a row changed."""
    if state not in STATES:
        raise ValueError(f"unknown prompt delivery state: {state!r}")
    sql = "UPDATE runner.prompt_delivery SET state = %s, updated_at = now()"
    params: list[object] = [state]
    if channel is not None:
        sql += ", channel = %s"
        params.append(channel)
    if attempt:
        sql += ", attempts = attempts + 1"
    sql += " WHERE event_id = %s"
    params.append(event_id)
    if expected is not None:
        sql += " AND state = ANY(%s)"
        params.append(list(expected))
    cur = await conn.execute(sql, params)
    return cur.rowcount == 1


async def adopt_delivered(conn: AsyncConnection[Any], delivered: Mapping[str, Iterable[str]]) -> int:
    """Rows of the hub's delivered list become `accepted` (inserted when unknown); returns the number changed."""
    changed = 0
    for session_id, event_ids in sorted(delivered.items()):
        for event_id in event_ids:
            cur = await conn.execute(
                "INSERT INTO runner.prompt_delivery (event_id, session_id, state) VALUES (%s, %s, 'accepted')"
                " ON CONFLICT (event_id) DO UPDATE SET state = 'accepted', updated_at = now()"
                " WHERE runner.prompt_delivery.state <> 'accepted'",
                (event_id, session_id),
            )
            changed += cur.rowcount
    await _place(conn)
    return changed


async def accepted_prompt(conn: AsyncConnection[Any], session_id: str, event_id: str) -> bool:
    """The guest's `prompt_accepted` for a row of this session: `accepted` (from any state but `accepted`)."""
    cur = await conn.execute(
        "UPDATE runner.prompt_delivery SET state = 'accepted', updated_at = now()"
        " WHERE event_id = %s AND session_id = %s AND state <> 'accepted'",
        (event_id, session_id),
    )
    return cur.rowcount == 1


async def typed_start(conn: AsyncConnection[Any], session_id: str) -> int:
    """`agent_starting`: the initial prompt handed over with the start is held by the guest."""
    cur = await conn.execute(
        "UPDATE runner.prompt_delivery SET state = 'typed', updated_at = now()"
        " WHERE session_id = %s AND channel = 'start' AND state = 'registered'",
        (session_id,),
    )
    return cur.rowcount


async def mark_via_agent(conn: AsyncConnection[Any], event_ids: Iterable[str]) -> int:
    """The hub reports these prompts came through their author's agent: `via_agent` (kept once set)."""
    cur = await conn.execute(
        "UPDATE runner.prompt_delivery SET via_agent = true WHERE event_id = ANY(%s::uuid[]) AND NOT via_agent",
        (sorted(event_ids),),
    )
    return cur.rowcount


async def withdraw(conn: AsyncConnection[Any], event_id: str) -> Delivery | bool:
    """Carry out the withdrawal of one prompt (module docstring): the row that became `withdrawn`, True for a prompt
    without a row (remembered for its routing), False when nothing changed (recorded before, or the prompt was handed
    to the guest or ended)."""
    known = await conn.execute("SELECT 1 FROM runner.prompt_withdrawal WHERE event_id = %s", (event_id,))
    if await known.fetchone() is not None:
        return False
    row = await delivery(conn, event_id, lock=True)
    if row is not None and not await set_state(conn, event_id, "withdrawn", expected=("pending",)):
        return False
    await conn.execute("INSERT INTO runner.prompt_withdrawal (event_id) VALUES (%s)", (event_id,))
    return True if row is None else row
