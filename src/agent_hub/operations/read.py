"""Reading operations for browsers (03-api.md section 4.7; metadata only, ADR-003 and ADR-014).

- `list_operations(task)`: the operations of a task newest first (`created_at DESC, id DESC`, 02-data-model.md section 1
  rule 5), at most `limit` (default 50, max 200), with the opaque cursor `next_before` of the task lists
  (`agent_hub.device.tasks.encode_cursor`).
- `get_operation(id)`: the operation row with its newest 100 transitions (ascending). The row names the
  `operation.params` and `operation.result` events (`params_event_id`, `result_event_id`); parameters, results and
  pull-request data exist only inside those encrypted events.

The caller checked `task:read` on the operation's workgroup.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

import psycopg

from agent_hub.device.tasks import decode_cursor, encode_cursor
from agent_hub.outbox.handlers_operation import ROW_COLUMNS, ROW_FIELDS
from agent_hub.sessions.rows import as_json

Connection = psycopg.Connection[Any]
LIST_DEFAULT = 50
LIST_MAX = 200
TRANSITIONS_MAX = 100
_CREATED = ROW_FIELDS.index("created_at")
TRANSITION_FIELDS = ("seq", "state", "error_code", "at", "by_id")


def _row(row: tuple[Any, ...]) -> dict[str, Any]:
    return {name: as_json(value) for name, value in zip(ROW_FIELDS, row, strict=True)}


def list_operations(connection: Connection, task: UUID, *, before: str | None, limit: int) -> dict[str, Any]:
    """A page of the task's operations, newest first (module docstring)."""
    clauses = ["task_id = %s"]
    params: list[Any] = [task]
    if before is not None:
        at, operation = decode_cursor(before)
        clauses.append("(created_at, id) < (%s, %s)")
        params.extend([at, operation])
    params.append(limit + 1)
    found = connection.execute(
        f"SELECT {ROW_COLUMNS} FROM hub.operation WHERE {' AND '.join(clauses)}"
        " ORDER BY created_at DESC, id DESC LIMIT %s",
        params,
    ).fetchall()
    page = found[:limit]
    return {
        "operations": [_row(row) for row in page],
        "next_before": encode_cursor(page[-1][_CREATED], page[-1][0]) if len(found) > limit else None,
    }


def get_operation(connection: Connection, operation: UUID) -> dict[str, Any]:
    """The operation with its transitions (module docstring); the caller found the operation."""
    row = cast(
        tuple[Any, ...],
        connection.execute(f"SELECT {ROW_COLUMNS} FROM hub.operation WHERE id = %s", (operation,)).fetchone(),
    )
    transitions = connection.execute(
        "SELECT seq, state, error_code, at, by_id FROM (SELECT * FROM hub.operation_transition"
        " WHERE operation_id = %s ORDER BY seq DESC LIMIT %s) newest ORDER BY seq",
        (operation, TRANSITIONS_MAX),
    ).fetchall()
    return {
        "operation": _row(row),
        "transitions": [
            {name: as_json(value) for name, value in zip(TRANSITION_FIELDS, item, strict=True)} for item in transitions
        ],
    }
