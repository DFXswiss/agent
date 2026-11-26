"""Reading tasks and sessions for browsers (03-api.md section 4.6).

- `list_tasks(wg)`: the tasks of the workgroup newest first (`created_at DESC, id DESC`, 02-data-model.md section 1
  rule 5), optional `state`, at most `limit` (default 50, max 200), cursor `next_before` in the form of the device
  task list (`agent_hub.device.tasks.encode_cursor`: b64u of canonical `{"at": <microseconds>, "id"}`). With
  `order="activity"` (KOR-32) the order is the latest activity (`last_activity_at DESC, id DESC`) and the cursor holds
  the last activity of the page's last row instead of its creation time (the task overview of the web client merges
  the lists of several workgroups by activity and pages each with its own cursor). The ticket filters of ADR-100
  (`status`, `urgency`, `assignee`, `topic`; `none` for tasks without a person or topic) combine with the state.
  `order="urgency"` (ADR-101) puts the most urgent first, then the latest activity; its cursor adds the rank.
  `overview` (`working`, `waiting`, `no_agent`) keeps the open tasks of one overview of the start page: their
  attention record (`agent_hub.attention.derive.OVERVIEW`, the one rule of the three numbers) belongs to it.
- `count_tasks(wgs)`: the counters of the task dashboard per status, per urgency and per overview (ADR-101 section 1).
- `get_task(task)`: the task row, its sessions as a tree order (depth, creation), each with its usage and its newest
  100 transitions, and the newest 100 task transitions.
- `get_session(session)`: the session row, its newest 1000 transitions, usage, VM and the IDs of its first 1000
  children (oldest first).

Rows hold metadata only; the title and every content stay in events.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg

from agent_hub.attention.derive import CLOSED, DONE, OVERVIEW, overview_of
from agent_hub.crypto.encoding import b64u_decode, b64u_encode, canonical
from agent_hub.crypto.errors import CryptoError
from agent_hub.device.tasks import EPOCH, decode_cursor, encode_cursor
from agent_hub.errors import HubError
from agent_hub.sessions import rows

Connection = psycopg.Connection[Any]
LIST_DEFAULT = 50
LIST_MAX = 200
TASK_TRANSITIONS = 100
SESSIONS_MAX = 100
SESSION_TRANSITIONS = 1000
CHILDREN_MAX = 1000
"""The IDs of a session's children in one answer; the number of children is a hub setting (ADR-088 section 2)."""


ORDERS = {"created": ("t.created_at", 6), "activity": ("t.last_activity_at", 8)}
"""List orders: the time column and its index in `rows.TASK_COLUMNS` (the cursor's time)."""
NONE = "none"


OVERVIEW_SQL = (
    "EXISTS (SELECT 1 FROM hub.task_attention a WHERE a.task_id = t.id AND a.category = ANY(%s)"
    " AND NOT (a.category = %s AND a.reason = %s))"
)
"""A task of one overview of the open tasks (`agent_hub.attention.derive.OVERVIEW`, the start page's three numbers):
its attention record has a category of the overview and is not `done/closed`."""


@dataclass(frozen=True)
class TicketFilter:
    """The ticket filters of a list (ADR-100 section 5): a status, an urgency, a person and a topic (an ID, or `none`
    for tasks without one), and the overview of the open tasks (`working`, `waiting`, `no_agent`; ADR-101); None
    filters nothing."""

    status: str | None = None
    urgency: str | None = None
    assignee: UUID | str | None = None
    topic: UUID | str | None = None
    overview: str | None = None

    def clauses(self) -> tuple[list[str], list[Any]]:
        """The SQL conditions over `hub.task t` and their parameters."""
        clauses: list[str] = []
        params: list[Any] = []
        if self.overview is not None:
            clauses.append(OVERVIEW_SQL)
            params.extend([list(OVERVIEW[self.overview]), DONE, CLOSED])
        given: tuple[tuple[str, UUID | str | None], ...] = (
            ("status", self.status),
            ("urgency", self.urgency),
            ("assignee_id", self.assignee),
            ("topic_id", self.topic),
        )
        for column, value in given:
            if value == NONE:
                clauses.append(f"t.{column} IS NULL")
            elif value is not None:
                clauses.append(f"t.{column} = %s")
                params.append(value)
        return clauses, params


URGENCY = "urgency"
URGENCY_RANKS = {"low": 0, "normal": 1, "high": 2, "urgent": 3}
RANK_SQL = "(CASE t.urgency WHEN 'urgent' THEN 3 WHEN 'high' THEN 2 WHEN 'normal' THEN 1 ELSE 0 END)"
"""The rank of a task's urgency for `order=urgency` (ADR-101 section 2): the most urgent first."""
_URGENCY_INDEX = rows.TASK_FIELDS.index("urgency")
_ACTIVITY_INDEX = ORDERS["activity"][1]


def encode_rank_cursor(rank: int, at: datetime, task: UUID) -> str:
    """The cursor of `order=urgency`: the rank, the last activity in microseconds and the ID of a page's last row."""
    micros = (at - EPOCH) // timedelta(microseconds=1)
    return b64u_encode(canonical({"at": micros, "id": str(task), "rank": rank}))


def decode_rank_cursor(text: str) -> tuple[int, datetime, UUID]:
    """`(rank, last_activity_at, id)` of an urgency cursor; anything else is `400 request_invalid`."""
    try:
        data = json.loads(b64u_decode(text))
        rank, at, task = data["rank"], EPOCH + timedelta(microseconds=data["at"]), UUID(data["id"])
        if rank not in URGENCY_RANKS.values() or encode_rank_cursor(rank, at, task) != text:
            raise ValueError("not a cursor")
    except (CryptoError, ValueError, TypeError, KeyError, OverflowError):
        raise HubError("request_invalid", detail={"pointers": ["/before"]}) from None
    return rank, at, task


def list_tasks(
    connection: Connection,
    wg: UUID,
    *,
    state: str | None,
    before: str | None,
    limit: int,
    order: str = "created",
    ticket: TicketFilter | None = None,
) -> dict[str, Any]:
    """The tasks of a workgroup, newest first (module docstring); the caller checked `wg:read`."""
    clauses = ["t.wg_id = %s"]
    params: list[Any] = [wg]
    if state is not None:
        clauses.append("t.state = %s")
        params.append(state)
    more, values = (ticket or TicketFilter()).clauses()
    clauses += more
    params += values
    if order == URGENCY:
        return _by_urgency(connection, clauses, params, before=before, limit=limit)
    column, at_index = ORDERS[order]
    if before is not None:
        at, task = decode_cursor(before)
        clauses.append(f"({column}, t.id) < (%s, %s)")
        params.extend([at, task])
    params.append(limit + 1)
    found = rows.task_rows(connection, " AND ".join(clauses), params, column)
    page = found[:limit]
    return {
        "tasks": [rows.task_row_json(row) for row in page],
        "next_before": encode_cursor(page[-1][at_index], page[-1][0]) if len(found) > limit else None,
    }


def _by_urgency(
    connection: Connection, clauses: list[str], params: list[Any], *, before: str | None, limit: int
) -> dict[str, Any]:
    """`order=urgency`: the most urgent first, then the latest activity, then the ID (all descending)."""
    if before is not None:
        rank, at, task = decode_rank_cursor(before)
        clauses.append(f"({RANK_SQL}, t.last_activity_at, t.id) < (%s, %s, %s)")
        params.extend([rank, at, task])
    params.append(limit + 1)
    found = rows.task_rows(connection, " AND ".join(clauses), params, f"{RANK_SQL} DESC, t.last_activity_at")
    page = found[:limit]
    last = page[-1] if len(found) > limit else None
    return {
        "tasks": [rows.task_row_json(row) for row in page],
        "next_before": None
        if last is None
        else encode_rank_cursor(URGENCY_RANKS[last[_URGENCY_INDEX]], last[_ACTIVITY_INDEX], last[0]),
    }


STATUS_KEYS = ("todo", "inProgress", "completed")
URGENCY_KEYS = tuple(URGENCY_RANKS)


def count_tasks(connection: Connection, wgs: list[UUID], *, state: str | None, ticket: TicketFilter) -> dict[str, Any]:
    """The counters of the task dashboard (ADR-101 section 1) over the workgroups `wgs` (the caller checked that
    it reads each): per status the tasks that match every filter but the status, per urgency those that match every
    filter but the urgency, per overview of the open tasks (`overview_of` of the attention record) those that match
    every filter but the overview, and `total` the tasks that match all of them - so a click on a counter gives that
    many."""
    clauses = ["t.wg_id = ANY(%s)"]
    params: list[Any] = [wgs]
    if state is not None:
        clauses.append("t.state = %s")
        params.append(state)
    more, values = TicketFilter(assignee=ticket.assignee, topic=ticket.topic).clauses()
    found = connection.execute(
        "SELECT t.status, t.urgency, a.category, a.reason, count(*) FROM hub.task t"
        f" LEFT JOIN hub.task_attention a ON a.task_id = t.id WHERE {' AND '.join(clauses + more)}"
        " GROUP BY t.status, t.urgency, a.category, a.reason",
        params + values,
    ).fetchall()
    by_status = dict.fromkeys(STATUS_KEYS, 0)
    by_urgency = dict.fromkeys(URGENCY_KEYS, 0)
    by_overview = dict.fromkeys(OVERVIEW, 0)
    total = 0
    for status, urgency, category, reason, count in found:
        # a task without a record yet (the backfill derives it) is in no overview
        overview = None if category is None else overview_of(category, reason)
        status_ok = ticket.status is None or status == ticket.status
        urgency_ok = ticket.urgency is None or urgency == ticket.urgency
        overview_ok = ticket.overview is None or overview == ticket.overview
        by_status[status] += count if urgency_ok and overview_ok else 0
        by_urgency[urgency] += count if status_ok and overview_ok else 0
        if overview is not None and status_ok and urgency_ok:
            by_overview[overview] += count
        total += count if status_ok and urgency_ok and overview_ok else 0
    return {"by_status": by_status, "by_urgency": by_urgency, "by_overview": by_overview, "total": total}


def get_task(connection: Connection, task: UUID) -> dict[str, Any]:
    """A task with its sessions and transitions (module docstring); the caller checked `task:read`."""
    sessions = [
        {
            "session": session,
            "usage": rows.usage_json(connection, session["session_id"]),
            "transitions": rows.session_transitions(connection, session["session_id"], TASK_TRANSITIONS),
        }
        for session in rows.task_sessions(connection, task)[:SESSIONS_MAX]
    ]
    return {
        "task": rows.task_json(connection, task),
        "sessions": sessions,
        "transitions": rows.task_transitions(connection, task, TASK_TRANSITIONS),
    }


def get_session(connection: Connection, session_id: UUID) -> dict[str, Any]:
    """A session with transitions, usage, VM and children (module docstring); the caller checked `task:read`."""
    session = rows.session_json(connection, session_id) or {}
    children = connection.execute(
        "SELECT id FROM hub.session WHERE parent_session_id = %s ORDER BY created_at, id LIMIT %s",
        (session_id, CHILDREN_MAX),
    ).fetchall()
    return {
        "session": session,
        "transitions": rows.session_transitions(connection, session_id, SESSION_TRANSITIONS),
        "usage": rows.usage_json(connection, session_id),
        "vm": rows.vm_json(connection, session["vm_id"]),
        "children": [str(row[0]) for row in children],
    }
