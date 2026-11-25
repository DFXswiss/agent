"""Reading task and session rows in their API form (contracts `api/objects.json`: `task_row`, `session_row`,
`session_transition`, `task_transition`, `usage`, `vm_row`).

Rows carry metadata only (02-data-model.md section 2.4): the title of a task lives in its `task.meta` events, prompts
and output in events. In a task row `main_session_id` is the **current** main session (the newest session of depth
0: a follow-up prompt after `stopped`/`failed` creates a new one, 03-api.md section 4.6.3) and `title_event_id` the
newest `task.meta` event of the task. A session row also names how a new main session continued the conversation
(`context_restore`: `restored` from the task's checkpoint, `fallback` with `context_reason` when it had to start a new
conversation, null while nothing was reported; ADR-072 section 7). `github_identity` is the GitHub identity chosen
for the session (ADR-078 section 3, null without a choice); `triggered_by` who triggered it (the author of the prompt
a main session was created for, the triggering person of a child session's `subagent.request`, null when unknown;
ADR-083 section 2). `interrupted_at` is set while the session's device has failed (ADR-086 section 2, null otherwise;
an ended session keeps it), `failover_of` names, for a session the hub started on another device after a device
failure, the lost session it continues (ADR-086 section 3).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg

from agent_hub.crypto.encoding import format_time

Connection = psycopg.Connection[Any]
TASK_FIELDS = (
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
    "github_identity",
)
TASK_COLUMNS = (
    "t.id, t.org_id, t.wg_id, t.owner_id, t.state, t.state_reason, t.created_at, t.state_at, t.last_activity_at,"
    " t.closed_at,"
    " (SELECT s.id FROM hub.session s WHERE s.task_id = t.id AND s.depth = 0 ORDER BY s.created_at DESC, s.id DESC"
    " LIMIT 1),"
    " (SELECT e.id FROM hub.event e WHERE e.task_id = t.id AND e.kind = 'task.meta' ORDER BY e.scope_seq DESC LIMIT 1),"
    " t.github_identity, t.placement_device_id, t.placement_vm_id"
)
"""The task columns: `TASK_FIELDS`, then the chosen device and VM (ADR-075; `placement` only when one is chosen)."""
SESSION_FIELDS = (
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
    "context_restore",
    "context_reason",
    "github_identity",
    "triggered_by",
    "interrupted_at",
    "failover_of",
)
SESSION_COLUMNS = (
    "id, task_id, org_id, wg_id, owner_id, device_id, vm_id, parent_session_id, origin_operation_id, depth, state,"
    " state_reason, model_access, model_id, agent_kind, agent_version, estimate_ms, created_at, ready_at,"
    " first_working_at, ended_at, context_restore, context_reason, github_identity, triggered_by,"
    " CASE WHEN interruption_ended_at IS NULL OR interruption_ended_at < interrupted_at THEN interrupted_at END,"
    " failover_of"
)
USAGE_FIELDS = (
    "requests",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "thinking_tokens",
    "cost_micro_usd",
    "cost_basis",
    "context_used_tokens",
    "context_window_tokens",
    "last_provider",
    "updated_at",
)
VM_FIELDS = (
    "vm_id",
    "device_id",
    "wg_id",
    "state",
    "golden_version",
    "cpus",
    "memory_mib",
    "disk_gib",
    "created_at",
    "state_at",
)


def as_json(value: Any) -> Any:
    """A column value as JSON: UUIDs as text, times in the API form, the rest unchanged."""
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return format_time(value)
    return value


def _zip(names: tuple[str, ...], row: tuple[Any, ...]) -> dict[str, Any]:
    return {name: as_json(value) for name, value in zip(names, row, strict=True)}


def _task(row: tuple[Any, ...]) -> dict[str, Any]:
    found = _zip(TASK_FIELDS, row[: len(TASK_FIELDS)])
    device, vm = row[len(TASK_FIELDS) :]
    if device is not None:
        found["placement"] = {"device_id": str(device), "vm_id": vm}
    return found


def task_json(connection: Connection, task_id: UUID | str) -> dict[str, Any] | None:
    """The task row in the API form, None for an unknown task."""
    row = connection.execute(f"SELECT {TASK_COLUMNS} FROM hub.task t WHERE t.id = %s", (task_id,)).fetchone()
    return None if row is None else _task(row)


def task_rows(connection: Connection, where: str, params: list[Any], column: str) -> list[tuple[Any, ...]]:
    """Raw task rows (`TASK_COLUMNS`) of a filtered list, newest first by `column` (a fixed column name), then ID."""
    return connection.execute(
        f"SELECT {TASK_COLUMNS} FROM hub.task t WHERE {where} ORDER BY {column} DESC, t.id DESC LIMIT %s", params
    ).fetchall()


def task_row_json(row: tuple[Any, ...]) -> dict[str, Any]:
    """A raw task row (`TASK_COLUMNS`) in the API form."""
    return _task(row)


def session_json(connection: Connection, session_id: UUID | str) -> dict[str, Any] | None:
    """The session row in the API form, None for an unknown session."""
    row = connection.execute(f"SELECT {SESSION_COLUMNS} FROM hub.session WHERE id = %s", (session_id,)).fetchone()
    return None if row is None else _zip(SESSION_FIELDS, row)


def task_sessions(connection: Connection, task_id: UUID | str) -> list[dict[str, Any]]:
    """The sessions of a task as a tree order: by depth, then creation."""
    rows = connection.execute(
        f"SELECT {SESSION_COLUMNS} FROM hub.session WHERE task_id = %s ORDER BY depth, created_at, id", (task_id,)
    ).fetchall()
    return [_zip(SESSION_FIELDS, row) for row in rows]


def session_transitions(connection: Connection, session_id: UUID | str, limit: int) -> list[dict[str, Any]]:
    """The newest `limit` transitions of a session, ascending."""
    rows = connection.execute(
        "SELECT seq, state, reason, at, received_at FROM (SELECT * FROM hub.session_transition WHERE session_id = %s"
        " ORDER BY seq DESC LIMIT %s) newest ORDER BY seq",
        (session_id, limit),
    ).fetchall()
    return [_zip(("seq", "state", "reason", "at", "received_at"), row) for row in rows]


def task_transitions(connection: Connection, task_id: UUID | str, limit: int) -> list[dict[str, Any]]:
    """The newest `limit` transitions of a task, ascending."""
    rows = connection.execute(
        "SELECT seq, state, reason, at FROM (SELECT * FROM hub.task_transition WHERE task_id = %s"
        " ORDER BY seq DESC LIMIT %s) newest ORDER BY seq",
        (task_id, limit),
    ).fetchall()
    return [_zip(("seq", "state", "reason", "at"), row) for row in rows]


def usage_json(connection: Connection, session_id: UUID | str) -> dict[str, Any] | None:
    """The usage counter of a session, None before the first usage report."""
    row = connection.execute(
        f"SELECT {', '.join(USAGE_FIELDS)} FROM hub.usage_counter WHERE session_id = %s", (session_id,)
    ).fetchone()
    return None if row is None else _zip(USAGE_FIELDS, row)


def vm_json(connection: Connection, vm_id: str | None) -> dict[str, Any] | None:
    """The VM row of a session, None without a VM."""
    if vm_id is None:
        return None
    row = connection.execute(
        "SELECT id, device_id, wg_id, state, golden_version, cpus, memory_mib, disk_gib, created_at, state_at"
        " FROM hub.vm WHERE id = %s",
        (vm_id,),
    ).fetchone()
    return None if row is None else _zip(VM_FIELDS, row)


def session_frame(connection: Connection, session_id: UUID | str) -> dict[str, Any]:
    """The data of the stream frame `session`: the row and its newest transition."""
    newest = session_transitions(connection, session_id, 1)
    return {"session": session_json(connection, session_id), "transition": newest[0] if newest else None}
