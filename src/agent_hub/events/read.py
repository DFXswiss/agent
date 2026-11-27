"""Reading the events of one scope (03-api.md sections 1 and 4.4, 02-data-model.md section 3).

`scope_of` tells whether an ID is an org scope (`hub.org`) or a workgroup scope (`hub.wg`); the routes check the right
on it (browsers, `agent_hub.events.routes`; devices, HUB-08). `read_events` returns the stored events of one scope
in ascending `scope_seq` after a cursor, with the optional filters `kinds`, `task`, `session` and `ref`:

- a watched kind (`agent_hub.events.watched`: `session.terminal`, `browser.action`) is returned only when `kinds`
  lists it (ADR-016, ADR-085: terminal output and the browser record are read on demand);
- `limit` events at most, and at most `max_bytes` (default `PAGE_BYTES`) of stored event bytes (header text, `ct`
  and `sig`) - but always the first matching event -, so an answer stays well below the 8 MiB the signed clients
  accept however large the events are (HOT-29: a page of 1000 events of up to 256 KiB each would not); `next_after` is
  the `scope_seq` of the last returned event when further matching events exist, else None; `head` is the scope's
  current `scope_seq` (0 while it has no event);
- newest first (`before`, KOR-44; instead of `after`): the `limit` matching events below `before` in descending
  `scope_seq`, and `next_before` the smallest returned `scope_seq` when older matching events exist, else None (a
  long record such as a browser's is read page by page from its end).

Both directions are served by an index that yields the order (no sort; ADR-086 section 2): the primary key
`(scope_id, scope_seq)`, `event_task (task_id, scope_seq)`, `event_session_seq (session_id, scope_seq)` (migration
0030) and, for one kind of a session, `event_session_kind (session_id, kind, scope_seq)` (one kind is an equality).
The byte bound is a running sum in the same query (from the stored `size` of `ct` and the length of the header text,
so no `ct` is loaded for the sum): only the events that begin within the bound leave the database, and always the
first two (the first is served even when it is larger than the bound, the second tells whether more exist).

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
PAGE_BYTES = 4 * 1024 * 1024
"""Stored event bytes of one page at most (beyond its first event): as JSON (`ct` and `sig` in b64u, about 4/3) an
answer stays below 6 MiB, under the 8 MiB limit of the signed clients (`agent_runner.sync.http.MAX_RESPONSE_BYTES`)."""
_SIZE = "size + octet_length(header_text) + 64"
"""The stored bytes of one event: `size` is the length of `ct` (kept at insert), `sig` has 64 bytes."""


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
    next_before: int | None = None
    backwards: bool = False
    size: int = 0
    """The stored bytes of the page's events (counted against `max_bytes`; not part of the answer)."""

    def to_json(self) -> dict[str, Any]:
        body: dict[str, Any] = {"events": self.events, "next_after": self.next_after, "head": self.head}
        if self.backwards:
            body["next_before"] = self.next_before
        return body


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
    before: int | None = None,
    max_bytes: int | None = None,
) -> EventPage:
    """The events of the scope after `after`, or newest first below `before`, at most `max_bytes` (default
    `PAGE_BYTES`) of them beyond the first (module docstring)."""
    max_bytes = PAGE_BYTES if max_bytes is None else max_bytes
    rows = _rows(
        connection,
        scope_id,
        after=after,
        limit=limit,
        kinds=kinds,
        task=task,
        session=session,
        ref=ref,
        before=before,
        max_bytes=max_bytes,
    )
    taken: list[tuple[Any, ...]] = []
    used = 0
    for row in rows[:limit]:
        if taken and used + row[5] > max_bytes:
            break
        taken.append(row[:5])
        used += row[5]
    page = [stored_json(row) for row in taken]
    more = len(rows) > len(taken)
    last = page[-1]["scope_seq"] if more else None
    if before is not None:
        return EventPage(page, None, head(connection, scope_id), next_before=last, backwards=True, size=used)
    return EventPage(page, last, head(connection, scope_id), size=used)


def _rows(
    connection: Connection,
    scope_id: UUID,
    *,
    after: int,
    limit: int,
    kinds: Collection[str] | None,
    task: UUID | None,
    session: UUID | None,
    ref: str | None,
    before: int | None,
    max_bytes: int,
) -> list[tuple[Any, ...]]:
    """The candidate rows `(scope_seq, received_at, header_text, ct, sig, bytes)` in page order: of the first
    `limit + 1` matching events those that begin within `max_bytes`, and always the first two (module docstring)."""
    backwards = before is not None
    clauses = ["scope_id = %s", "scope_seq < %s" if backwards else "scope_seq > %s"]
    params: list[Any] = [scope_id, before if backwards else after]
    if kinds is None:
        clauses.append("kind <> ALL(%s)")
        params.append(sorted(watched.KINDS))
    elif len(kinds) == 1:  # an equality lets `event_session_kind` yield the order
        clauses.append("kind = %s")
        params.extend(kinds)
    else:
        clauses.append("kind = ANY(%s)")
        params.append(sorted(kinds))
    for column, value in (("task_id", task), ("session_id", session), ("ref", ref)):
        if value is not None:
            clauses.append(f"{column} = %s")
            params.append(str(value) if column == "ref" else value)
    order = "scope_seq DESC" if backwards else "scope_seq"
    params += [limit + 1, max_bytes]
    return connection.execute(
        "SELECT scope_seq, received_at, header_text, ct, sig, bytes FROM ("
        f"SELECT scope_seq, received_at, header_text, ct, sig, {_SIZE} AS bytes,"
        f" sum({_SIZE}) OVER w AS running, row_number() OVER w AS n FROM hub.event WHERE "
        + " AND ".join(clauses)
        + f" WINDOW w AS (ORDER BY {order} ROWS UNBOUNDED PRECEDING) ORDER BY {order} LIMIT %s"
        f") AS candidate WHERE running - bytes <= %s OR n <= 2 ORDER BY {order}",
        params,
    ).fetchall()
