"""The record of working modes (`hub.task_mode`, ADR-098 section 3) and its only writers.

Every write goes through `switch_on` or `switch_off` in the caller's transaction (the task locked by the caller), and
the only callers are the three ways of ADR-098 section 3: the person routes and task creation (`modes.routes`,
`tasks.service`: ways `web`, `tool`, `inherited`), the device route of an executing `task.mode` (way `sentence`) and the
revocation when a chooser loses `wg:write` (`modes.signals`). A test holds this list closed (no default, E-19).

- `switch_on`: a mode that is on already is answered unchanged (no audit); otherwise the row becomes `on` with the
  chooser, the way, the operation and the originating task, and `task.mode_on` is audited with the acting principal.
  A mode whose `needs` are not all on (or chosen in the same request, `also`) is `409 mode_needs` with the missing
  names in `detail.modes` - nothing is switched on implicitly (ADR-098 section 2).
- `switch_off`: a mode that is not on is answered unchanged; otherwise the row becomes `off` with the reason and
  `task.mode_off` is audited. The driver state stays with the row (a later `on` starts the driver afresh: it is
  cleared then).
- `active(connection, task_id)`: the modes that are on, in the API form (`common.json#/$defs/task_mode`), by name.
- `devices_of(connection, task_id)`: the devices of the task's live sessions, which get `reconcile {}` after a change.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from agent_hub.audit.recorder import unchanged
from agent_hub.crypto.encoding import format_time
from agent_hub.errors import HubError
from agent_hub.modes.catalog import Mode
from agent_hub.sessions.taskstate import Actor, TaskRow, audit

Connection = psycopg.Connection[Any]
ON, OFF = "on", "off"
WEB, TOOL, SENTENCE, INHERITED = "web", "tool", "sentence", "inherited"
PERSON, SESSION, CHOOSER_LEFT = "person", "session", "chooser_left"
_COLUMNS = "mode, version, chosen_by, way, origin_task, changed_at"


@dataclass(frozen=True)
class Choice:
    """Who switches a mode on and how (ADR-098 section 3)."""

    chosen_by: UUID
    way: str
    operation_id: UUID | None = None
    origin_task: UUID | None = None


def _json(row: tuple[Any, ...]) -> dict[str, Any]:
    mode, version, chosen_by, way, origin, changed_at = row
    return {
        "mode": mode,
        "version": version,
        "chosen_by": str(chosen_by),
        "way": way,
        "origin_task": None if origin is None else str(origin),
        "changed_at": format_time(changed_at),
    }


def active(connection: Connection, task_id: UUID | str) -> list[dict[str, Any]]:
    """The modes that are on for the task, by name (module docstring)."""
    rows = connection.execute(
        f"SELECT {_COLUMNS} FROM hub.task_mode WHERE task_id = %s AND state = 'on' ORDER BY mode", (task_id,)
    ).fetchall()
    return [_json(row) for row in rows]


def is_on(connection: Connection, task_id: UUID | str, name: str) -> bool:
    found = connection.execute(
        "SELECT 1 FROM hub.task_mode WHERE task_id = %s AND mode = %s AND state = 'on'", (task_id, name)
    ).fetchone()
    return found is not None


def missing_needs(connection: Connection, task_id: UUID, mode: Mode, also: Collection[str] = ()) -> list[str]:
    """The modes `mode` needs that are neither on for the task nor in `also`."""
    return [name for name in mode.needs if name not in also and not is_on(connection, task_id, name)]


def require_needs(connection: Connection, task_id: UUID, mode: Mode, also: Collection[str] = ()) -> None:
    missing = missing_needs(connection, task_id, mode, also)
    if missing:
        raise HubError("mode_needs", detail={"modes": missing})


def _current(connection: Connection, task_id: UUID, name: str) -> dict[str, Any] | None:
    row = connection.execute(
        f"SELECT {_COLUMNS}, state FROM hub.task_mode WHERE task_id = %s AND mode = %s FOR UPDATE", (task_id, name)
    ).fetchone()
    if row is None or row[-1] != ON:
        return None
    return _json(row[:-1])


def switch_on(
    connection: Connection,
    task: TaskRow,
    mode: Mode,
    choice: Choice,
    *,
    actor: Actor,
    now: datetime,
    also: Collection[str] = (),
    on_behalf_of: UUID | None = None,
) -> tuple[dict[str, Any], bool]:
    """Switch the mode on (module docstring); the record and whether it changed."""
    current = _current(connection, task.id, mode.name)
    if current is not None:
        unchanged()
        return current, False
    require_needs(connection, task.id, mode, also)
    connection.execute(
        "INSERT INTO hub.task_mode (task_id, mode, version, state, chosen_by, way, op_id, origin_task, changed_at,"
        " changed_by, reason, driver_state) VALUES (%s, %s, %s, 'on', %s, %s, %s, %s, %s, %s, NULL, NULL)"
        " ON CONFLICT (task_id, mode) DO UPDATE SET version = EXCLUDED.version, state = 'on',"
        " chosen_by = EXCLUDED.chosen_by, way = EXCLUDED.way, op_id = EXCLUDED.op_id,"
        " origin_task = EXCLUDED.origin_task, changed_at = EXCLUDED.changed_at, changed_by = EXCLUDED.changed_by,"
        " reason = NULL, driver_state = NULL",
        (
            task.id,
            mode.name,
            mode.version,
            choice.chosen_by,
            choice.way,
            choice.operation_id,
            choice.origin_task,
            now,
            actor.id,
        ),
    )
    audit(
        connection,
        actor,
        "task.mode_on",
        task,
        now,
        detail={
            "mode": mode.name,
            "version": mode.version,
            "way": choice.way,
            "chosen_by": str(choice.chosen_by),
            "operation_id": None if choice.operation_id is None else str(choice.operation_id),
            "origin_task": None if choice.origin_task is None else str(choice.origin_task),
        },
        on_behalf_of=on_behalf_of,
    )
    return cast(dict[str, Any], _current(connection, task.id, mode.name)), True


def switch_off(
    connection: Connection,
    task: TaskRow,
    name: str,
    *,
    actor: Actor,
    now: datetime,
    reason: str,
    operation_id: UUID | None = None,
    on_behalf_of: UUID | None = None,
) -> bool:
    """Switch the mode off (module docstring); whether it changed."""
    current = _current(connection, task.id, name)
    if current is None:
        unchanged()
        return False
    connection.execute(
        "UPDATE hub.task_mode SET state = 'off', reason = %s, changed_at = %s, changed_by = %s,"
        " op_id = COALESCE(%s, op_id) WHERE task_id = %s AND mode = %s",
        (reason, now, actor.id, operation_id, task.id, name),
    )
    audit(
        connection,
        actor,
        "task.mode_off",
        task,
        now,
        detail={
            "mode": name,
            "reason": reason,
            "chosen_by": current["chosen_by"],
            "operation_id": None if operation_id is None else str(operation_id),
        },
        on_behalf_of=on_behalf_of,
    )
    return True


def store_driver(connection: Connection, task_id: UUID, name: str, state: dict[str, Any]) -> bool:
    """Keep the driver state a device reported while the mode is on (a report older than the stored one is ignored);
    whether the mode is on."""
    row = connection.execute(
        "SELECT driver_state FROM hub.task_mode WHERE task_id = %s AND mode = %s AND state = 'on' FOR UPDATE",
        (task_id, name),
    ).fetchone()
    if row is None:
        return False
    stored = row[0]
    if stored is None or str(stored.get("at", "")) <= str(state["at"]):
        connection.execute(
            "UPDATE hub.task_mode SET driver_state = %s WHERE task_id = %s AND mode = %s",
            (Jsonb(state), task_id, name),
        )
    return True


def devices_of(connection: Connection, task_id: UUID) -> list[UUID]:
    """The devices of the task's live sessions (module docstring)."""
    rows = connection.execute(
        "SELECT DISTINCT device_id FROM hub.session WHERE task_id = %s AND device_id IS NOT NULL"
        " AND state NOT IN ('stopped', 'failed') ORDER BY device_id",
        (task_id,),
    ).fetchall()
    return [cast(UUID, row[0]) for row in rows]
