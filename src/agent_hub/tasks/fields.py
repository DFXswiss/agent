"""The ticket fields of a task (E-41, ADR-100 sections 1, 2 and 5): status, urgency, person and topic.

The hub holds IDs and codes only; title, description, GitHub organisation, repository and topic names stay sealed
(`task.meta`, `topic.profile`).

- `status` is `todo`, `inProgress` or `completed`; closed ⇔ completed (the trigger of migration 0040 keeps it on
  every write). `urgency` is `low`, `normal` (the default), `high` or `urgent`.
- `assignee_id` names an active user member of the task's organization (a person or an agent account; ADR-071),
  `topic_id` a topic of the organization: the `ref` of a `topic.profile` event in its org scope. Anything else is
  `400 request_invalid` with the pointer.
- `update` (`PUT /v1/tasks/{task}/fields`, `task:write`): at least one field; the changed values are stored and audited
  (`task.fields`, the new values); a change to `completed` closes the task like `POST /v1/tasks/{task}/close` (live
  sessions stop, audit `task.close`), `todo` or `inProgress` on a closed task reopens it without an agent (state
  `idle`, a transition by the user). A status never starts an agent; the first prompt does
  (`agent_hub.tasks.service.add_prompt`, which moves `todo` to `inProgress`). Repeating the stored values changes
  nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, cast
from uuid import UUID

import psycopg

from agent_hub.audit.recorder import unchanged
from agent_hub.errors import HubError
from agent_hub.http.context import HubContext
from agent_hub.orgs import rights
from agent_hub.sessions import rows
from agent_hub.sessions.derive import CLOSED, IDLE
from agent_hub.sessions.effects import Effects
from agent_hub.sessions.taskstate import Actor, TaskRow, audit, close_task, write_state

Connection = psycopg.Connection[Any]
STATUSES = ("todo", "inProgress", "completed")
URGENCIES = ("low", "normal", "high", "urgent")
TODO, IN_PROGRESS, COMPLETED = STATUSES
NONE = "none"
"""The filter value of a list for a task without a person or without a topic."""
NAMES = ("status", "urgency", "assignee_id", "topic_id")


def _invalid(pointer: str) -> HubError:
    return HubError("request_invalid", detail={"pointers": [pointer]})


def check_code(value: object, allowed: tuple[str, ...], pointer: str) -> str:
    """A code of `allowed` (`400 request_invalid` with the pointer otherwise)."""
    if not isinstance(value, str) or value not in allowed:
        raise _invalid(pointer)
    return value


def check_assignee(connection: Connection, org_id: UUID, user: UUID | None, pointer: str) -> None:
    """None, or an active user member of the organization (an admin or a member, never a device)."""
    if user is not None and rights.org_role(connection, org_id, user) not in ("admin", "member"):
        raise _invalid(pointer)


def topic_known(connection: Connection, org_id: UUID, topic: UUID) -> bool:
    """The organization has the topic: a `topic.profile` event with that `ref` in its org scope."""
    found = connection.execute(
        "SELECT 1 FROM hub.event WHERE kind = 'topic.profile' AND ref = %s AND scope_id = %s LIMIT 1",
        (str(topic), org_id),
    ).fetchone()
    return found is not None


def check_topic(connection: Connection, org_id: UUID, topic: UUID | None, pointer: str) -> None:
    """None, or a topic of the organization."""
    if topic is not None and not topic_known(connection, org_id, topic):
        raise _invalid(pointer)


def stored(connection: Connection, task_id: UUID) -> dict[str, Any]:
    """The stored fields of a task (`NAMES`)."""
    row = cast(
        tuple[Any, ...],
        connection.execute(
            "SELECT status, urgency, assignee_id, topic_id FROM hub.task WHERE id = %s", (task_id,)
        ).fetchone(),
    )
    return dict(zip(NAMES, row, strict=True))


def _detail(changes: Mapping[str, Any]) -> dict[str, Any]:
    return {name: None if value is None else str(value) for name, value in changes.items()}


def update(
    connection: Connection,
    task: TaskRow,
    changes: Mapping[str, Any],
    *,
    now: datetime,
    actor: Actor,
    effects: Effects,
) -> TaskRow:
    """`PUT /v1/tasks/{task}/fields` on a locked, writable task (module docstring); the caller checked the values'
    form, this checks person and topic against the organization and stops the sessions of a completed task through
    `stop`."""
    check_assignee(connection, task.org_id, changes.get("assignee_id"), "/assignee_id")
    check_topic(connection, task.org_id, changes.get("topic_id"), "/topic_id")
    current = stored(connection, task.id)
    changed = {name: value for name, value in changes.items() if current[name] != value}
    if not changed:
        unchanged()
        return task
    plain = {name: value for name, value in changed.items() if name != "status"}
    if plain:
        assignments = ", ".join(f"{name} = %s" for name in plain)
        connection.execute(f"UPDATE hub.task SET {assignments} WHERE id = %s", (*plain.values(), task.id))
    status = changed.get("status")
    if status == COMPLETED:
        task = close_task(connection, task, now, actor, effects)
        from agent_hub.tasks.service import stop_sessions  # noqa: PLC0415 - the service imports this module

        stop_sessions(connection, task, now, actor, effects)
        audit(connection, actor, "task.close", task, now, detail={"by": "user"})
    elif status is not None:
        if task.state == CLOSED:
            task = write_state(connection, task, IDLE, None, now, actor=actor, effects=effects)
        connection.execute("UPDATE hub.task SET status = %s WHERE id = %s", (status, task.id))
    connection.execute(
        "UPDATE hub.task SET last_activity_at = GREATEST(last_activity_at, %s) WHERE id = %s", (now, task.id)
    )
    audit(connection, actor, "task.fields", task, now, detail=_detail(changed))
    effects.org_frame(task.org_id, "task", rows.task_json(connection, task.id) or {})
    return task


def put_fields(
    context: HubContext, user: UUID, task_id: UUID, changes: Mapping[str, Any]
) -> tuple[dict[str, Any], Effects]:
    """The route's transaction: the answer `{task}` and the effects to send after the commit."""
    from agent_hub.tasks.service import task_for_write  # noqa: PLC0415 - the service imports this module

    now = context.clock()
    effects = Effects()
    with context.pool.connection() as connection, connection.transaction():
        task = task_for_write(connection, user, task_id)
        update(connection, task, changes, now=now, actor=Actor("user", user), effects=effects)
        body = {"task": rows.task_json(connection, task_id)}
    return body, effects


def start_progress(connection: Connection, task_id: UUID) -> bool:
    """A captured task got its agent (its first main session): `todo` becomes `inProgress`; whether it changed."""
    found = connection.execute(
        "UPDATE hub.task SET status = 'inProgress' WHERE id = %s AND status = 'todo' RETURNING id", (task_id,)
    ).fetchone()
    return found is not None
