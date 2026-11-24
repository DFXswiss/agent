"""The task row as a function of its sessions (02-data-model.md sections 2.4, 2.4.1).

Every change of a session ends with `refresh_task`: it derives the task state (`agent_hub.sessions.derive`), writes
the new state with a `task_transition` row when it changed, keeps `last_activity_at` current and records the `task`
frame and the signal `task.state` (`task_id`, `state`) in the caller's `Effects` (sent after the commit; HUB-15
subscribes). A closed task is never changed here; `close_task` closes it and `refresh_task(..., reopen=True)` takes it
back to the derivation (a follow-up prompt after closing).

Lock order of the tasks area (every writer keeps it, so no two writers deadlock): a follow-up prompt first takes the
per-task prompt lock (`agent_hub.tasks.service.add_prompt`, an advisory lock no other writer takes), then events
(`append_events`: org rows, scope heads), then the task row (`lock_task`), then its session rows, then audit rows.
Both `refresh_task` and `close_task` end with the refresh of the task's attention record (`agent_hub.attention.store`,
ADR-076), which takes the audit lock before the attention row.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg

from agent_hub.attention import store as attention
from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.sessions import rows
from agent_hub.sessions.derive import CLOSED, SCHEDULING, WORKING, MainSession, task_state
from agent_hub.sessions.effects import Effects

Connection = psycopg.Connection[Any]
SIGNAL = "task.state"


@dataclass(frozen=True)
class Actor:
    """Who causes a change: a user, a device or the hub itself (audit `actor_kind`, transition `by_id`)."""

    kind: str
    id: UUID | None = None


HUB = Actor("hub")


@dataclass(frozen=True)
class TaskRow:
    """The fields of a task the writers need."""

    id: UUID
    org_id: UUID
    wg_id: UUID
    owner_id: UUID
    state: str
    state_reason: str | None


_COLUMNS = "id, org_id, wg_id, owner_id, state, state_reason"


def _task(row: tuple[Any, ...]) -> TaskRow:
    return TaskRow(*row)


def lock_task(connection: Connection, task_id: UUID | str) -> TaskRow | None:
    """The task held `FOR UPDATE`, None for an unknown task."""
    row = connection.execute(f"SELECT {_COLUMNS} FROM hub.task WHERE id = %s FOR UPDATE", (task_id,)).fetchone()
    return None if row is None else _task(row)


def audit(
    connection: Connection,
    actor: Actor,
    action: str,
    task: TaskRow,
    now: datetime,
    *,
    detail: dict[str, Any],
    session_id: UUID | None = None,
    device_id: UUID | None = None,
    target: tuple[str, UUID] | None = None,
) -> None:
    """One audit row of the tasks area (metadata only)."""
    kind, target_id = target or ("task", task.id)
    append_audit(
        connection,
        AuditRow(
            at=now,
            actor_kind=actor.kind,
            actor_id=actor.id,
            action=action,
            result="ok",
            org_id=task.org_id,
            wg_id=task.wg_id,
            task_id=task.id,
            session_id=session_id,
            device_id=device_id,
            target_kind=kind,
            target_id=str(target_id),
            detail=detail,
        ),
    )


def _write_state(
    connection: Connection,
    task: TaskRow,
    state: str,
    reason: str | None,
    now: datetime,
    *,
    actor: Actor,
    effects: Effects,
) -> TaskRow:
    connection.execute(
        "UPDATE hub.task SET state = %s, state_reason = %s, state_at = %s, closed_at = %s WHERE id = %s",
        (state, reason, now, now if state == CLOSED else None, task.id),
    )
    connection.execute(
        "INSERT INTO hub.task_transition (task_id, seq, state, reason, at, by_id) VALUES (%s,"
        " (SELECT COALESCE(MAX(seq), 0) + 1 FROM hub.task_transition WHERE task_id = %s), %s, %s, %s, %s)",
        (task.id, task.id, state, reason, now, actor.id),
    )
    effects.signal(SIGNAL, task_id=task.id, state=state)
    return TaskRow(task.id, task.org_id, task.wg_id, task.owner_id, state, reason)


def _main_session(connection: Connection, task_id: UUID) -> MainSession | None:
    row = connection.execute(
        "SELECT state, state_reason, device_id IS NOT NULL FROM hub.session WHERE task_id = %s AND depth = 0"
        " ORDER BY created_at DESC, id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return None if row is None else MainSession(state=row[0], state_reason=row[1], placed=bool(row[2]))


def refresh_task(
    connection: Connection,
    task: TaskRow,
    now: datetime,
    actor: Actor,
    effects: Effects,
    *,
    scheduling_reason: str | None = None,
    reopen: bool = False,
) -> TaskRow:
    """Derive and store the task state, touch `last_activity_at`, record the `task` frame (module docstring).

    `scheduling_reason` is the scheduler's reason while the main session waits for a device; without it a task that
    stays `scheduling` keeps its reason. A closed task is left alone unless `reopen` (a follow-up prompt).
    """
    if task.state == CLOSED and not reopen:
        attention.refresh(connection, task.id, now, effects)
        return task
    working = connection.execute(
        "SELECT EXISTS (SELECT 1 FROM hub.session WHERE task_id = %s AND state = %s)", (task.id, WORKING)
    ).fetchone()
    reason = scheduling_reason
    if reason is None and task.state == SCHEDULING:
        reason = task.state_reason
    state, state_reason = task_state(
        closed=False,
        any_working=bool(working and working[0]),
        main=_main_session(connection, task.id),
        scheduling_reason=reason,
    )
    if (state, state_reason) != (task.state, task.state_reason):
        task = _write_state(connection, task, state, state_reason, now, actor=actor, effects=effects)
    connection.execute(
        "UPDATE hub.task SET last_activity_at = GREATEST(last_activity_at, %s) WHERE id = %s", (now, task.id)
    )
    effects.org_frame(task.org_id, "task", rows.task_json(connection, task.id) or {})
    attention.refresh(connection, task.id, now, effects)
    return task


def close_task(connection: Connection, task: TaskRow, now: datetime, actor: Actor, effects: Effects) -> TaskRow:
    """Close the task (user action; the caller stops its sessions)."""
    closed = _write_state(connection, task, CLOSED, None, now, actor=actor, effects=effects)
    connection.execute("UPDATE hub.task SET last_activity_at = %s WHERE id = %s", (now, task.id))
    attention.refresh(connection, task.id, now, effects)
    return closed
