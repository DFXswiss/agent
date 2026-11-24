"""The projection `whiteboard_entry` of the whiteboard events (02-data-model.md sections 1.3, 2.6; KONZEPT section 12).

A whiteboard entry exists on one level: `org` (event scope `org`), `workgroup` (scope `wg`, no task) or `task` (scope
`wg` with the header's `task`); its ID is the events' `ref`. The hub accepts whiteboard events by the kind rules of the
events area (04-crypto.md section 4.3: org admin on the org level, workgroup write below; HUB-06) and only projects
them here, in the storing transaction (`agent_hub.whiteboards.event_hooks`), so the row never disagrees with the log:

- `whiteboard.entry` creates the row (on the task level only for a task of the event's workgroup: the event rules
  check the `task` header only for its form, so a writer of another workgroup cannot attach an entry - and its pings -
  to a foreign task) (`created_event_id` = `latest_event_id` = the event, `created_at` = `updated_at`
  = the hub receive time, `notify` = the request flag). An entry whose ID already exists - on any level - creates
  nothing: the first entry wins and the later event stays a plain, unprojected event.
- `whiteboard.edit` / `whiteboard.delete` move `latest_event_id` and `updated_at` (a delete also sets `deleted`), but
  only for an existing, not deleted entry of the same org, level, workgroup and task. An edit of an unknown entry, of
  an entry of another scope or task, or after the delete changes nothing (readers ignore it in the same way; the
  history endpoint lists exactly the projected events, `agent_hub.whiteboards.read`).

The events are applied in the order the hub stored them: within one call in call order, across calls in commit order,
which is the order of their `event.append` audit rows (`seq` is assigned under the global audit lock that every storing
transaction holds until its commit). `rebuild` takes that lock, replays all whiteboard events in that order and gives
the same rows as the incremental projection; the `notify` flag, which is not part of the stored event, comes back from
the `whiteboard.notify` audit row that every notified entry gets (`agent_hub.notifications.rules`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg

from agent_hub.audit.chain import AUDIT_LOCK

Connection = psycopg.Connection[Any]
ENTRY, EDIT, DELETE = "whiteboard.entry", "whiteboard.edit", "whiteboard.delete"
KINDS = (ENTRY, EDIT, DELETE)
ORG, WORKGROUP, TASK = "org", "workgroup", "task"
NOTIFY_ACTION = "whiteboard.notify"
_EVENT_COLUMNS = "e.id, e.org_id, e.scope, e.scope_id, e.wg_id, e.task_id, e.author_id, e.kind, e.ref, e.received_at"


def level_of(scope: str, task_id: UUID | None) -> str:
    """The level of a whiteboard event: `org` scope, else `task` with a task and `workgroup` without."""
    if scope == "org":
        return ORG
    return WORKGROUP if task_id is None else TASK


@dataclass(frozen=True)
class BoardEvent:
    """The stored metadata of one whiteboard event (never its content)."""

    id: UUID
    org_id: UUID
    scope: str
    scope_id: UUID
    wg_id: UUID | None
    task_id: UUID | None
    author_id: UUID
    kind: str
    ref: UUID
    received_at: datetime

    @property
    def level(self) -> str:
        return level_of(self.scope, self.task_id)


def _event(row: tuple[Any, ...]) -> BoardEvent:
    event_id, org, scope, scope_id, wg, task, author, kind, ref, received_at = row
    return BoardEvent(event_id, org, scope, scope_id, wg, task, author, kind, UUID(ref), received_at)


def board_events(connection: Connection, ids: Sequence[str]) -> list[BoardEvent]:
    """The stored whiteboard events among `ids`, in the order of `ids`."""
    rows = connection.execute(
        f"SELECT {_EVENT_COLUMNS} FROM hub.event e WHERE e.id = ANY(%s::uuid[]) AND e.kind = ANY(%s)",
        (list(ids), list(KINDS)),
    ).fetchall()
    found = {str(row[0]): _event(row) for row in rows}
    return [found[event_id] for event_id in ids if event_id in found]


def apply(connection: Connection, event: BoardEvent, *, notify: bool) -> bool:
    """Project one whiteboard event (module docstring); True when it created the entry."""
    if event.kind == ENTRY:
        created = connection.execute(
            "INSERT INTO hub.whiteboard_entry (id, org_id, level, wg_id, task_id, author_id, created_event_id,"
            " latest_event_id, created_at, updated_at, deleted, notify)"
            " SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, false, %s"
            " WHERE %s::uuid IS NULL OR EXISTS (SELECT 1 FROM hub.task WHERE id = %s AND wg_id = %s)"
            " ON CONFLICT (id) DO NOTHING RETURNING id",
            (
                event.ref,
                event.org_id,
                event.level,
                event.wg_id,
                event.task_id,
                event.author_id,
                event.id,
                event.id,
                event.received_at,
                event.received_at,
                notify,
                event.task_id,
                event.task_id,
                event.wg_id,
            ),
        ).fetchone()
        return created is not None
    connection.execute(
        "UPDATE hub.whiteboard_entry SET latest_event_id = %s, updated_at = %s, deleted = %s"
        " WHERE id = %s AND org_id = %s AND level = %s AND wg_id IS NOT DISTINCT FROM %s"
        " AND task_id IS NOT DISTINCT FROM %s AND NOT deleted",
        (
            event.id,
            event.received_at,
            event.kind == DELETE,
            event.ref,
            event.org_id,
            event.level,
            event.wg_id,
            event.task_id,
        ),
    )
    return False


def rebuild(connection: Connection) -> int:
    """Recompute every `whiteboard_entry` row from the stored events (module docstring); the number of entries."""
    connection.execute("SELECT pg_advisory_xact_lock(%s)", (AUDIT_LOCK,))  # no append interleaves with the replay
    connection.execute("DELETE FROM hub.whiteboard_entry")
    rows = connection.execute(
        f"SELECT {_EVENT_COLUMNS}, EXISTS (SELECT 1 FROM hub.audit n WHERE n.action = %s AND n.event_id = e.id)"
        " FROM hub.event e JOIN hub.audit a ON a.event_id = e.id AND a.action = 'event.append'"
        " WHERE e.kind = ANY(%s) ORDER BY a.seq",
        (NOTIFY_ACTION, list(KINDS)),
    ).fetchall()
    return sum(apply(connection, _event(row[:-1]), notify=bool(row[-1])) for row in rows)
