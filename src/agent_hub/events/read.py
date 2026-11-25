"""Reading the events of one scope (03-api.md sections 1 and 4.4, 02-data-model.md section 3).

`scope_of` tells whether an ID is an org scope (`hub.org`) or a workgroup scope (`hub.wg`); the routes check the right
on it (browsers, `agent_hub.events.routes`; devices, HUB-08). `read_events` returns the stored events of one scope
in ascending `scope_seq` after a cursor, with the optional filters `kinds`, `task`, `session` and `ref`:

- a watched kind (`agent_hub.events.watched`: `session.terminal`, `browser.action`) is returned only when `kinds`
  lists it (ADR-016, ADR-085: terminal output and the browser record are read on demand);
- `limit` events at most; `next_after` is the `scope_seq` of the last returned event when further matching events
  exist, else None; `head` is the scope's current `scope_seq` (0 while it has no event).

Every event is returned exactly as stored: the canonical header text, `ct` and `sig` (b64u), with the hub's
`scope_seq` and `received_at`. The hub never decrypts; readers verify (04-crypto.md section 5.12).
"""

from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg

from agent_hub.crypto.encoding import b64u_encode, format_time
from agent_hub.events import watched

Connection = psycopg.Connection[Any]
PAGE_DEFAULT = 200
PAGE_MAX = 1000


@dataclass(frozen=True)
class Scope:
    """An event scope: `org` (the org itself) or `wg` (a workgroup of `org_id`)."""

    scope: str
    scope_id: UUID
    org_id: UUID


@dataclass(frozen=True)
class EventPage:
    """One page of `read_events`."""

    events: list[dict[str, Any]]
    next_after: int | None
    head: int

    def to_json(self) -> dict[str, Any]:
        return {"events": self.events, "next_after": self.next_after, "head": self.head}


def scope_of(connection: Connection, scope_id: UUID) -> Scope | None:
    """The scope with this ID, None if it is neither an org nor a workgroup."""
    if connection.execute("SELECT 1 FROM hub.org WHERE id = %s", (scope_id,)).fetchone() is not None:
        return Scope("org", scope_id, scope_id)
    row = connection.execute("SELECT org_id FROM hub.wg WHERE id = %s", (scope_id,)).fetchone()
    return None if row is None else Scope("wg", scope_id, row[0])


def head(connection: Connection, scope_id: UUID) -> int:
    """The current `scope_seq` of a scope (0 without events)."""
    row = connection.execute("SELECT seq FROM hub.scope_head WHERE scope_id = %s", (scope_id,)).fetchone()
    return 0 if row is None else int(row[0])


def stored_json(row: tuple[Any, ...]) -> dict[str, Any]:
    """`{scope_seq, received_at, event}` of a row `(scope_seq, received_at, header_text, ct, sig)`."""
    scope_seq, received_at, header_text, ct, sig = row
    event = {"header": json.loads(header_text), "ct": b64u_encode(bytes(ct)), "sig": b64u_encode(bytes(sig))}
    return {"scope_seq": int(scope_seq), "received_at": format_time(received_at), "event": event}


def read_events(
    connection: Connection,
    scope_id: UUID,
    *,
    after: int = 0,
    limit: int = PAGE_DEFAULT,
    kinds: Collection[str] | None = None,
    task: UUID | None = None,
    session: UUID | None = None,
    ref: str | None = None,
) -> EventPage:
    """The events of the scope after `after` (module docstring)."""
    clauses = ["scope_id = %s", "scope_seq > %s"]
    params: list[Any] = [scope_id, after]
    if kinds is None:
        clauses.append("kind <> ALL(%s)")
        params.append(sorted(watched.KINDS))
    else:
        clauses.append("kind = ANY(%s)")
        params.append(sorted(kinds))
    for column, value in (("task_id", task), ("session_id", session), ("ref", ref)):
        if value is not None:
            clauses.append(f"{column} = %s")
            params.append(str(value) if column == "ref" else value)
    params.append(limit + 1)
    rows = connection.execute(
        "SELECT scope_seq, received_at, header_text, ct, sig FROM hub.event WHERE "
        + " AND ".join(clauses)
        + " ORDER BY scope_seq LIMIT %s",
        params,
    ).fetchall()
    page = [stored_json(row) for row in rows[:limit]]
    more = len(rows) > limit
    return EventPage(page, page[-1]["scope_seq"] if more else None, head(connection, scope_id))
