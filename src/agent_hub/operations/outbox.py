"""Outbox handler extensions of the operations area (ADR-027 section 4 `<area>/outbox.py:HANDLERS`, ADR-030).

They run after the base handlers of the outbox area (HUB-10), in the same savepoint, and only record signals that are
sent after the commit (inputs of the session pings of HUB-15):

- `operation_transition`: when the item ended the operation (`succeeded`, `failed`, `expired`; the base handler
  stored it with `finished_at` = this push's time - a repeated report of the current state changes nothing and
  signals nothing), the signal `operation.finished` (`operation_id`, `session_id`, `task_id`, `org_id`, `state`).
  The base handler already writes the row, the transition, the audit row, the `operation` frame and releases the
  dedupe slot on `failed`/`expired`.
- `session_transition`: when a child session (one with a parent) ended now (`stopped`/`failed`, `ended_at` = this
  push's time), the signal `subagent.finished` (`session_id` = the child, `parent_session_id`, `task_id`, `org_id`).
"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from agent_hub.operations.steps import FINISHED_SIGNAL, FINISHED_STATES
from agent_hub.outbox.base import Handler, ItemContext
from agent_hub.outbox.bodies import OperationTransitionBody, SessionTransitionBody
from agent_hub.sessions.derive import FINAL

SUBAGENT_FINISHED_SIGNAL = "subagent.finished"


def operation_finished(item: ItemContext, body: OperationTransitionBody) -> None:
    """Signal an operation that this item ended (module docstring)."""
    if body.state not in FINISHED_STATES:
        return
    row = item.connection.execute(
        "SELECT session_id, task_id, org_id FROM hub.operation WHERE id = %s AND state = %s AND finished_at = %s",
        (body.operation_id, body.state, item.now),
    ).fetchone()
    if row is not None:
        item.effects.signal(
            FINISHED_SIGNAL,
            operation_id=UUID(body.operation_id),
            session_id=row[0],
            task_id=row[1],
            org_id=row[2],
            state=body.state,
        )


def subagent_finished(item: ItemContext, body: SessionTransitionBody) -> None:
    """Signal a child session that ended with this item (module docstring)."""
    if body.state not in FINAL:
        return
    row = item.connection.execute(
        "SELECT parent_session_id, task_id, org_id FROM hub.session WHERE id = %s AND parent_session_id IS NOT NULL"
        " AND state = %s AND ended_at = %s",
        (body.session_id, body.state, item.now),
    ).fetchone()
    if row is not None:
        item.effects.signal(
            SUBAGENT_FINISHED_SIGNAL,
            session_id=UUID(body.session_id),
            parent_session_id=row[0],
            task_id=row[1],
            org_id=row[2],
        )


HANDLERS: Mapping[str, Handler] = {
    "operation_transition": operation_finished,
    "session_transition": subagent_finished,
}
