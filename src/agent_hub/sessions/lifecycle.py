"""Creating main sessions and placing them on a device (03-api.md sections 4.6.1-4.6.3, ADR-002, ADR-021).

`create_main_session` inserts a session of depth 0 in state `requested` (agent `claude-code`, the only agent software
of v1) with its first transition and the audit row `session.requested`, then `place` applies the scheduler's
decision: with a device it sets `device_id` and `estimate_ms` and records the hint `session.start {session_id}` for
that device; without one the session waits (`device_id` NULL) and the task stays `scheduling` with the scheduler's
reason. Both end with the `session` frame and the task derivation. Callers hold the task row (`lock_task`) or have
just created it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg

from agent_hub.scheduler.core import Decision, Placement
from agent_hub.sessions import rows
from agent_hub.sessions.effects import Effects
from agent_hub.sessions.taskstate import Actor, TaskRow, audit, refresh_task

Connection = psycopg.Connection[Any]
AGENT_KIND = "claude-code"
START_HINT = "session.start"


def place(
    connection: Connection,
    task: TaskRow,
    session_id: UUID,
    decision: Decision,
    now: datetime,
    *,
    actor: Actor,
    effects: Effects,
    reopen: bool = False,
) -> TaskRow:
    """Apply the scheduler's decision to a waiting session (module docstring); the task after the derivation
    (`reopen`: a follow-up prompt reopens a closed task)."""
    reason = None
    if isinstance(decision, Placement):
        connection.execute(
            "UPDATE hub.session SET device_id = %s, estimate_ms = %s WHERE id = %s",
            (decision.device_id, decision.estimate_ms, session_id),
        )
        effects.hint(decision.device_id, START_HINT, {"session_id": str(session_id)})
    else:
        reason = decision.reason
    effects.org_frame(task.org_id, "session", rows.session_frame(connection, session_id))
    return refresh_task(connection, task, now, actor, effects, scheduling_reason=reason, reopen=reopen)


def create_main_session(
    connection: Connection,
    task: TaskRow,
    session_id: UUID,
    decision: Decision,
    now: datetime,
    *,
    actor: Actor,
    effects: Effects,
    reopen: bool = False,
    reason: str | None = None,
) -> TaskRow:
    """A new main session of the task, placed by `decision` (module docstring); `reason` is the reason of its first
    transition (`capacity_exhausted` for the hub's attempt after a refusal, `agent_hub.sessions.retry`)."""
    connection.execute(
        "INSERT INTO hub.session (id, task_id, org_id, wg_id, owner_id, depth, state, agent_kind, created_at)"
        " VALUES (%s, %s, %s, %s, %s, 0, 'requested', %s, %s)",
        (session_id, task.id, task.org_id, task.wg_id, task.owner_id, AGENT_KIND, now),
    )
    connection.execute(
        "INSERT INTO hub.session_transition (session_id, seq, state, reason, at, received_at, reported_by)"
        " VALUES (%s, 1, 'requested', %s, %s, %s, NULL)",
        (session_id, reason, now, now),
    )
    audit(
        connection,
        actor,
        "session.requested",
        task,
        now,
        detail={"reason": reason, "model_access": None, "model_id": None},
        session_id=session_id,
        target=("session", session_id),
    )
    return place(connection, task, session_id, decision, now, actor=actor, effects=effects, reopen=reopen)
