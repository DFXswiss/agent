"""Outbox handler extension of the sessions area (ADR-027 section 4 `<area>/outbox.py:HANDLERS`, ADR-030; KOR-46,
ADR-083 section 3).

It runs after the base handler of the item kind `audit` (which checked the action, its empty detail and the scope and
wrote the audit row) in the same savepoint. For `session.prompt_withdrawn` - the device withdrew a prompt its author
took back before it reached the guest; the target is the prompt event - it confirms the requested withdrawal of that
event (`hub.prompt_withdrawal.withdrawn_at` = the receive time) when the reporting device is the device of a main
session of the withdrawal's task (only main sessions hold prompts), and records the `session` frame of the device's
newest main session of that task, so the browsers read the queue again. The confirmed prompt leaves the routing
(`agent_hub.sessions.prompts`). A report for a prompt without a requested withdrawal, a confirmed one or one of another
device's task changes nothing beyond the audit row. The target must be an event (`target_kind` `event`, a UUID; `400
request_invalid` otherwise, the item is `rejected`). Other audit actions pass unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from agent_hub.errors import HubError
from agent_hub.outbox.base import Handler, ItemContext
from agent_hub.outbox.bodies import AuditBody
from agent_hub.sessions import rows

ACTION = "session.prompt_withdrawn"


def _event(body: AuditBody) -> UUID:
    if body.target_kind != "event":
        raise HubError("request_invalid")
    try:
        return UUID(body.target_id)
    except ValueError:
        raise HubError("request_invalid") from None


def prompt_withdrawn(item: ItemContext, body: AuditBody) -> None:
    """Confirm a requested withdrawal the device reported (module docstring)."""
    if body.action != ACTION:
        return
    event = _event(body)
    found = item.connection.execute(
        "UPDATE hub.prompt_withdrawal w SET withdrawn_at = %s WHERE w.event_id = %s AND w.withdrawn_at IS NULL"
        " AND EXISTS (SELECT 1 FROM hub.session s WHERE s.task_id = w.task_id AND s.device_id = %s AND s.depth = 0)"
        " RETURNING (SELECT s.id FROM hub.session s WHERE s.task_id = w.task_id AND s.device_id = %s AND s.depth = 0"
        " ORDER BY s.created_at DESC, s.id DESC LIMIT 1), (SELECT org_id FROM hub.task WHERE id = w.task_id)",
        (item.now, event, item.device, item.device),
    ).fetchone()
    if found is None:
        return
    item.effects.org_frame(found[1], "session", rows.session_frame(item.connection, found[0]))


HANDLERS: Mapping[str, Handler] = {"audit": prompt_withdrawn}
