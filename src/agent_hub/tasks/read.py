"""Reading tasks and sessions for browsers (03-api.md section 4.6).

- `list_tasks(wg)`: the tasks of the workgroup newest first (`created_at DESC, id DESC`, 02-data-model.md section 1
  rule 5), optional `state`, at most `limit` (default 50, max 200), cursor `next_before` in the form of the device
  task list (`agent_hub.device.tasks.encode_cursor`: b64u of canonical `{"at": <microseconds>, "id"}`). With
  `order="activity"` (KOR-26B) the order is the latest activity (`last_activity_at DESC, id DESC`) and the cursor holds
  the last activity of the page's last row instead of its creation time (the task overview of the web client merges
  the lists of several workgroups by activity and pages each with its own cursor).
- `get_task(task)`: the task row, its sessions as a tree order (depth, creation), each with its usage and its newest
  100 transitions, and the newest 100 task transitions.
- `get_session(session)`: the session row, its newest 1000 transitions, usage, VM and the IDs of its children.

Rows hold metadata only; the title and every content stay in events.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from agent_hub.device.tasks import decode_cursor, encode_cursor
from agent_hub.sessions import rows

Connection = psycopg.Connection[Any]
LIST_DEFAULT = 50
LIST_MAX = 200
TASK_TRANSITIONS = 100
SESSIONS_MAX = 100
SESSION_TRANSITIONS = 1000
CHILDREN_MAX = 10


ORDERS = {"created": ("t.created_at", 6), "activity": ("t.last_activity_at", 8)}
"""List orders: the time column and its index in `rows.TASK_COLUMNS` (the cursor's time)."""


def list_tasks(
    connection: Connection,
    wg: UUID,
    *,
    state: str | None,
    before: str | None,
    limit: int,
    order: str = "created",
) -> dict[str, Any]:
    """The tasks of a workgroup, newest first (module docstring); the caller checked `wg:read`."""
    column, at_index = ORDERS[order]
    clauses = ["t.wg_id = %s"]
    params: list[Any] = [wg]
    if state is not None:
        clauses.append("t.state = %s")
        params.append(state)
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
