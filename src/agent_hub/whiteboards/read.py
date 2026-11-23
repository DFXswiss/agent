"""Reading whiteboards (03-api.md section 4.9): the entry list and the history of one entry. Metadata and signed,
encrypted events only - the text exists only inside the events (KONZEPT section 12, ADR-003).

**Visibility.** A user sees an entry of the org level while an active member of its org (`org:member`) and an entry of
the workgroup or task level while reading its workgroup (`wg:read`, which needs active membership as well).

**List** (`list_entries`): the visible entries, optionally filtered by `org`, `wg`, `task` and `level`, newest first
(`created_at DESC, id DESC`), at most `limit` (default 50, max 200) with the opaque cursor `next_before` of the other
lists. Deleted entries are listed with `deleted: true`. `latest_event` is the scope position of the entry's newest
projected event. The routes check the named filters first (`404 not_found` for an org the caller is not a member of, a
workgroup it does not read or a task of such a workgroup).

**History** (`history`): exactly the events the projection applied, ascending: the creating `whiteboard.entry` and the
`whiteboard.edit`/`whiteboard.delete` events of the entry in its scope and task up to the latest projected event
(`agent_hub.whiteboards.projection`: unprojected events with the same `ref` are not part of the entry's history).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg

from agent_hub.device.tasks import decode_cursor, encode_cursor
from agent_hub.events.read import stored_json
from agent_hub.sessions.rows import as_json
from agent_hub.whiteboards.projection import DELETE, EDIT, ORG

Connection = psycopg.Connection[Any]
LIST_DEFAULT = 50
LIST_MAX = 200
HISTORY_MAX = 10000
_FIELDS = ("entry_id", "org_id", "level", "wg_id", "task_id", "author_id", "created_at", "updated_at", "deleted")
_VISIBLE = (
    "w.org_id IN (SELECT org_id FROM hub.org_member WHERE principal_id = %(user)s AND kind = 'user'"
    " AND removed_seq IS NULL) AND (w.level = 'org' OR w.wg_id IN (SELECT a.wg_id FROM hub.wg_access a"
    " JOIN hub.wg g ON g.id = a.wg_id JOIN hub.org_member m ON m.org_id = g.org_id AND m.principal_id = a.user_id"
    " AND m.removed_seq IS NULL WHERE a.user_id = %(user)s AND a.revoked_seq IS NULL))"
)


@dataclass(frozen=True)
class Filters:
    """The filters of `GET /v1/whiteboards` (None: not filtered)."""

    org: UUID | None = None
    wg: UUID | None = None
    task: UUID | None = None
    level: str | None = None


@dataclass(frozen=True)
class Entry:
    """What the history needs of an entry."""

    id: UUID
    org_id: UUID
    level: str
    wg_id: UUID | None
    task_id: UUID | None
    created_event_id: UUID
    latest_event_id: UUID


def list_entries(
    connection: Connection, user: UUID, filters: Filters, *, before: str | None, limit: int
) -> dict[str, Any]:
    """A page of the visible entries (module docstring)."""
    clauses = [_VISIBLE]
    params: dict[str, Any] = {"user": user, "limit": limit + 1}
    for column, value in (("org_id", filters.org), ("wg_id", filters.wg), ("task_id", filters.task)):
        if value is not None:
            clauses.append(f"w.{column} = %({column})s")
            params[column] = value
    if filters.level is not None:
        clauses.append("w.level = %(level)s")
        params["level"] = filters.level
    if before is not None:
        params["at"], params["last"] = decode_cursor(before)
        clauses.append("(w.created_at, w.id) < (%(at)s, %(last)s)")
    found = connection.execute(
        "SELECT w.id, w.org_id, w.level, w.wg_id, w.task_id, w.author_id, w.created_at, w.updated_at, w.deleted,"
        " e.scope_id, e.scope_seq FROM hub.whiteboard_entry w JOIN hub.event e ON e.id = w.latest_event_id"
        f" WHERE {' AND '.join(clauses)} ORDER BY w.created_at DESC, w.id DESC LIMIT %(limit)s",
        params,
    ).fetchall()
    page = found[:limit]
    entries = []
    for item in page:
        entry = {name: as_json(value) for name, value in zip(_FIELDS, item[:9], strict=True)}
        entry["latest_event"] = {"scope_id": str(item[9]), "scope_seq": int(item[10])}
        entries.append(entry)
    last: tuple[Any, ...] | None = page[-1] if len(found) > limit else None
    return {"entries": entries, "next_before": None if last is None else encode_cursor(last[6], last[0])}


def entry(connection: Connection, entry_id: UUID) -> Entry | None:
    """The entry with this ID, None if unknown."""
    found = connection.execute(
        "SELECT id, org_id, level, wg_id, task_id, created_event_id, latest_event_id FROM hub.whiteboard_entry"
        " WHERE id = %s",
        (entry_id,),
    ).fetchone()
    return None if found is None else Entry(*found)


def history(connection: Connection, item: Entry) -> list[dict[str, Any]]:
    """The projected events of the entry, ascending (module docstring)."""
    rows = connection.execute(
        "SELECT e.scope_seq, e.received_at, e.header_text, e.ct, e.sig FROM hub.event e"
        " JOIN hub.event c ON c.id = %(created)s JOIN hub.event l ON l.id = %(latest)s"
        " WHERE e.scope_id = c.scope_id AND (e.id = c.id OR (e.kind = ANY(%(kinds)s) AND e.ref = c.ref"
        " AND e.task_id IS NOT DISTINCT FROM c.task_id AND e.scope_seq > c.scope_seq AND e.scope_seq <= l.scope_seq))"
        " ORDER BY e.scope_seq LIMIT %(limit)s",
        {
            "created": item.created_event_id,
            "latest": item.latest_event_id,
            "kinds": [EDIT, DELETE],
            "limit": HISTORY_MAX,
        },
    ).fetchall()
    return [stored_json(found) for found in rows]


def is_org_level(item: Entry) -> bool:
    """The entry belongs to the org level (right `org:member`), else to a workgroup (`wg:read`)."""
    return item.level == ORG
