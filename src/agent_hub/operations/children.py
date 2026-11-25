"""Child sessions of accepted `subagent.request` operations (03-api.md section 9, 05-runner.md section 13, ADR-017).

The hub creates the child in the registration's transaction, right after the decision `accepted`: a new session of the
parent's task on the parent's device and VM, `parent_session_id` = the requesting session, `origin_operation_id` = the
operation, `depth` = parent depth + 1, owner = the task owner, agent `claude-code`, state `requested` with its first
transition. It writes the audit row `session.requested` (actor: the requesting device) and records the `session` frame
for the org's browsers, the hint `session.start {session_id}` for the device (whose `GET /v1/device/state` lists the
child in `sessions`) and the task derivation (`refresh_task`: the task's activity time and `task` frame). The child's
`github_identity` is the one the registration names, else the parent session's (ADR-078 section 3); its `triggered_by`
is the operation's (ADR-083 section 2); its `agent_profile` the resolved snapshot of the profile the request named
(ADR-090 section 1; NULL: the child works with its parent's rights). The model of the child is chosen by the runner
(the parent's access kind, or the `model_id` the runner allowed). The caller holds the task row (lock order of
`agent_hub.sessions.taskstate`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg

from agent_hub.crypto.ids import uuid7
from agent_hub.profiles.model import Profile
from agent_hub.profiles.store import snapshot
from agent_hub.sessions import rows
from agent_hub.sessions.effects import Effects
from agent_hub.sessions.lifecycle import AGENT_KIND, START_HINT
from agent_hub.sessions.taskstate import Actor, TaskRow, audit, refresh_task

Connection = psycopg.Connection[Any]


@dataclass(frozen=True)
class Parent:
    """The requesting session as far as its child needs it."""

    id: UUID
    device_id: UUID
    vm_id: str | None
    depth: int


def child_of(connection: Connection, operation_id: UUID) -> UUID | None:
    """The child session created for an operation, None for an operation without one."""
    row = connection.execute("SELECT id FROM hub.session WHERE origin_operation_id = %s", (operation_id,)).fetchone()
    return None if row is None else UUID(str(row[0]))


def create_child(
    connection: Connection,
    task: TaskRow,
    parent: Parent,
    operation_id: UUID,
    now: datetime,
    *,
    effects: Effects,
    github_identity: str | None = None,
    triggered_by: UUID | None = None,
    agent_profile: Profile | None = None,
) -> UUID:
    """Create the child session of an accepted `subagent.request` (module docstring); its ID."""
    child = UUID(uuid7(int(now.timestamp() * 1000)))
    connection.execute(
        "INSERT INTO hub.session (id, task_id, org_id, wg_id, owner_id, device_id, vm_id, parent_session_id,"
        " origin_operation_id, depth, state, agent_kind, created_at, triggered_by, github_identity, agent_profile)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'requested', %s, %s, %s,"
        " COALESCE(%s::uuid, (SELECT github_identity FROM hub.session WHERE id = %s)), %s)",
        (
            child,
            task.id,
            task.org_id,
            task.wg_id,
            task.owner_id,
            parent.device_id,
            parent.vm_id,
            parent.id,
            operation_id,
            parent.depth + 1,
            AGENT_KIND,
            now,
            triggered_by,
            github_identity,
            parent.id,
            snapshot(agent_profile),
        ),
    )
    connection.execute(
        "INSERT INTO hub.session_transition (session_id, seq, state, reason, at, received_at, reported_by)"
        " VALUES (%s, 1, 'requested', NULL, %s, %s, NULL)",
        (child, now, now),
    )
    actor = Actor("device", parent.device_id)
    audit(
        connection,
        actor,
        "session.requested",
        task,
        now,
        detail={"reason": None, "model_access": None, "model_id": None},
        session_id=child,
        device_id=parent.device_id,
        target=("session", child),
    )
    effects.hint(parent.device_id, START_HINT, {"session_id": str(child)})
    effects.org_frame(task.org_id, "session", rows.session_frame(connection, child))
    refresh_task(connection, task, now, actor, effects)
    return child
