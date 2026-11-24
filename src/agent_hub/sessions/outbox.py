"""Outbox handler extension of the sessions area (ADR-027 section 4 `<area>/outbox.py:HANDLERS`, ADR-030; KOR-46,
ADR-083 section 3).

It runs after the base handler of the item kind `audit` (which checked the action, its empty detail and the scope and
wrote the audit row) in the same savepoint. For `session.prompt_withdrawn` - the device withdrew a prompt its author
took back before it reached the guest; the target is the prompt event - it confirms the requested withdrawal of that
event (`hub.prompt_withdrawal.withdrawn_at` = the receive time) only while the reporting device still holds the prompt
undelivered (`agent_hub.sessions.prompts.device_holder`: a main session of that device that is neither `stopping` nor
ended has it pending - exactly the condition that lists it in that device's `prompt_queue.withdrawn`), and records the
`session` frame of that session, so the browsers read the queue again. The confirmed prompt leaves the routing
(`agent_hub.sessions.prompts`). A report for a prompt without a requested withdrawal, a confirmed one, one delivered
meanwhile or one the device does not hold changes nothing beyond the audit row (a late report can never make a
delivered prompt leave the routing, which would count the next one as delivered). The target must be an event
(`target_kind` `event`, a UUID; `400 request_invalid` otherwise, the item is `rejected`). Other audit actions pass
unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from agent_hub.errors import HubError
from agent_hub.outbox.base import Handler, ItemContext
from agent_hub.outbox.bodies import AuditBody
from agent_hub.sessions import prompts, rows

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
    requested = item.connection.execute(
        "SELECT w.task_id, t.org_id FROM hub.prompt_withdrawal w JOIN hub.task t ON t.id = w.task_id"
        " WHERE w.event_id = %s AND w.withdrawn_at IS NULL FOR UPDATE OF w",
        (event,),
    ).fetchone()
    if requested is None:
        return
    holder = prompts.device_holder(item.connection, item.device, requested[0], str(event))
    if holder is None:
        return
    item.connection.execute("UPDATE hub.prompt_withdrawal SET withdrawn_at = %s WHERE event_id = %s", (item.now, event))
    item.effects.org_frame(requested[1], "session", rows.session_frame(item.connection, holder))


HANDLERS: Mapping[str, Handler] = {"audit": prompt_withdrawn}
