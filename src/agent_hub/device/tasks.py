"""Task metadata for devices (03-api.md section 6.1): `GET /v1/device/workgroups/{wg}/tasks` and
`GET /v1/device/tasks/{task}`, read-only from the tables of the tasks area (02-data-model.md sections 2.4, 2.5).

Both need `device:wg`: the device is assigned to the workgroup (of the task); an unknown or foreign workgroup or task
is `404 not_found`. Rows carry metadata only - titles, prompts and output live in events.

- A task row is `{task_id, org_id, wg_id, owner_id, state, state_reason, created_at, state_at, last_activity_at,
  closed_at, main_session_id, title_event_id}`; `main_session_id` is the task's current session of depth 0 (the newest
  one, as in the browser API; KOR-06 A-15), `title_event_id` the newest `task.meta` event of the task.
- The list is newest first (`created_at DESC, id DESC`, 02-data-model.md section 1 rule 5) with the optional filter
  `state`, at most `limit` rows (default 50, max 200) and the opaque cursor `next_before` (b64u of the canonical
  `{"at": <microseconds>, "id"}` of the last row); `before` continues after that row.
- The task answer adds its sessions (the tree: by depth, then creation) and its operations
  (`{id, type, state, error_code, result_event_id}`, oldest first).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg

from agent_hub.crypto.encoding import b64u_decode, b64u_encode, canonical, format_time
from agent_hub.crypto.errors import CryptoError
from agent_hub.device.access import DeviceView
from agent_hub.errors import HubError

Connection = psycopg.Connection[Any]
LIST_DEFAULT = 50
LIST_MAX = 200
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
TASK_STATES = frozenset({"scheduling", "starting", "working", "idle", "failed", "closed"})
_TASK_COLUMNS = (
    "t.id, t.org_id, t.wg_id, t.owner_id, t.state, t.state_reason, t.created_at, t.state_at, t.last_activity_at,"
    " t.closed_at,"
    " (SELECT s.id FROM hub.session s WHERE s.task_id = t.id AND s.depth = 0"
    " ORDER BY s.created_at DESC, s.id DESC LIMIT 1),"
    " (SELECT e.id FROM hub.event e WHERE e.task_id = t.id AND e.kind = 'task.meta'"
    " ORDER BY e.scope_seq DESC LIMIT 1)"
)
_SESSION_COLUMNS = (
    "id, task_id, org_id, wg_id, owner_id, device_id, vm_id, parent_session_id, origin_operation_id, depth, state,"
    " state_reason, model_access, model_id, agent_kind, agent_version, estimate_ms, created_at, ready_at,"
    " first_working_at, ended_at"
)
_SESSION_FIELDS = (
    "session_id",
    "task_id",
    "org_id",
    "wg_id",
    "owner_id",
    "device_id",
    "vm_id",
    "parent_session_id",
    "origin_operation_id",
    "depth",
    "state",
    "state_reason",
    "model_access",
    "model_id",
    "agent_kind",
    "agent_version",
    "estimate_ms",
    "created_at",
    "ready_at",
    "first_working_at",
    "ended_at",
)


def _json(value: Any) -> Any:
    """A column value as JSON: UUIDs as text, times in the API form, the rest unchanged."""
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return format_time(value)
    return value


def task_row(row: tuple[Any, ...]) -> dict[str, Any]:
    """A task row in the API form."""
    names = (
        "task_id",
        "org_id",
        "wg_id",
        "owner_id",
        "state",
        "state_reason",
        "created_at",
        "state_at",
        "last_activity_at",
        "closed_at",
        "main_session_id",
        "title_event_id",
    )
    return {name: _json(value) for name, value in zip(names, row, strict=True)}


def encode_cursor(at: datetime, task: UUID) -> str:
    """The opaque list cursor of a row (its creation time in microseconds since the epoch and its ID)."""
    micros = (at - EPOCH) // timedelta(microseconds=1)
    return b64u_encode(canonical({"at": micros, "id": str(task)}))


def decode_cursor(text: str) -> tuple[datetime, UUID]:
    """`(created_at, id)` of a cursor; anything else is `400 request_invalid`."""
    try:
        data = json.loads(b64u_decode(text))
        at = EPOCH + timedelta(microseconds=data["at"])
        task = UUID(data["id"])
        if encode_cursor(at, task) != text:
            raise ValueError("not a cursor")
    except (CryptoError, ValueError, TypeError, KeyError, OverflowError):
        raise HubError("request_invalid", detail={"pointers": ["/before"]}) from None
    return at, task


def list_tasks(
    connection: Connection,
    view: DeviceView,
    wg: UUID,
    *,
    state: str | None = None,
    before: str | None = None,
    limit: int = LIST_DEFAULT,
) -> dict[str, Any]:
    """The tasks of an assigned workgroup, newest first (module docstring)."""
    if not view.reads_wg(wg):
        raise HubError("not_found")
    clauses = ["t.wg_id = %s"]
    params: list[Any] = [wg]
    if state is not None:
        clauses.append("t.state = %s")
        params.append(state)
    if before is not None:
        at, task = decode_cursor(before)
        clauses.append("(t.created_at, t.id) < (%s, %s)")
        params.extend([at, task])
    params.append(limit + 1)
    rows = connection.execute(
        f"SELECT {_TASK_COLUMNS} FROM hub.task t WHERE "
        + " AND ".join(clauses)
        + " ORDER BY t.created_at DESC, t.id DESC LIMIT %s",
        params,
    ).fetchall()
    page = rows[:limit]
    more = len(rows) > limit
    return {
        "tasks": [task_row(row) for row in page],
        "next_before": encode_cursor(page[-1][6], page[-1][0]) if more else None,
    }


def get_task(connection: Connection, view: DeviceView, task: UUID) -> dict[str, Any]:
    """A task of an assigned workgroup with its sessions and operations."""
    row = connection.execute(f"SELECT {_TASK_COLUMNS} FROM hub.task t WHERE t.id = %s", (task,)).fetchone()
    if row is None or not view.reads_wg(row[2]):
        raise HubError("not_found")
    sessions = [
        {name: _json(value) for name, value in zip(_SESSION_FIELDS, found, strict=True)}
        for found in connection.execute(
            f"SELECT {_SESSION_COLUMNS} FROM hub.session WHERE task_id = %s ORDER BY depth, created_at, id", (task,)
        )
    ]
    operations = [
        {
            "id": str(found[0]),
            "type": found[1],
            "state": found[2],
            "error_code": found[3],
            "result_event_id": _json(found[4]),
        }
        for found in connection.execute(
            "SELECT id, type, state, error_code, result_event_id FROM hub.operation WHERE task_id = %s"
            " ORDER BY created_at, id",
            (task,),
        )
    ]
    return {"task": task_row(row), "sessions": sessions, "operations": operations}
