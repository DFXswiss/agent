"""Outbox item `audit` (03-api.md section 6.1, 02-data-model.md section 2.9): a runner-side audit row.

Only the actions of `contracts/enums/outbox-audit-actions.json` are accepted, each with the detail schema of its
action (`agent_hub.outbox.bodies`). A row that names a session is the agent's (ADR-095 section 4): actor = the session
as `main_agent`/`subagent` (`agent_hub.audit.agents`), `on_behalf_of` = the session's `triggered_by` while it is an
active member of the org (else null), `device_id` = the device; a row without a session is attributed to the device
(`actor_kind` `device`). Its scope comes from the hub's own rows, never from the runner: with `session_id` the
session must run on the device (`device:session`; a `task_id` given as well must be the session's task, else
`400 request_invalid`), with only `task_id` the task's workgroup must be one the device is assigned to
(`device:wg`); org, workgroup and task are copied from those rows.
`ping.delivered` needs `session_id` (05-runner.md section 9). A hub ping (`whiteboard.entry`) additionally marks the
durable `session_ping` row of the session delivered (an unknown ping or one of another session or kind is
`404 not_found`; a ping delivered before keeps its first time). The runner's own pings (`subagent.finished`,
`operation.finished`, created by the runner, never by the hub) have no `session_ping` row: their delivery is audited
without marking anything, unless the ID is one of the hub's pings of that session (`404 not_found`, a hub ping is never
a runner ping; other sessions' pings are not looked at, so a device learns nothing about them). The device is trusted
for its own sessions here as for every runner-side audit row: nothing ties a runner ping ID to a ping the hub knows.
"""

from __future__ import annotations

from uuid import UUID

from agent_hub.audit.agents import session_actor
from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.errors import HubError
from agent_hub.outbox.base import ItemContext, session_on_device
from agent_hub.outbox.bodies import AuditBody

PING_DELIVERED = "ping.delivered"
RUNNER_PING_KINDS = frozenset({"subagent.finished", "operation.finished"})


def _scope(item: ItemContext, body: AuditBody) -> tuple[UUID | None, UUID | None, UUID | None, UUID | None]:
    """`(org_id, wg_id, task_id, session_id)` of the row (module docstring)."""
    if body.session_id is not None:
        org, wg, task, session = session_on_device(item, body.session_id)
        if body.task_id is not None and UUID(body.task_id) != task:
            raise HubError("request_invalid")
        return org, wg, task, session
    if body.task_id is None:
        return None, None, None, None
    row = item.connection.execute("SELECT org_id, wg_id, id FROM hub.task WHERE id = %s", (body.task_id,)).fetchone()
    if row is None:
        raise HubError("not_found")
    if not item.view().reads_wg(row[1]):
        raise HubError("forbidden")
    return row[0], row[1], row[2], None


def _mark_ping(item: ItemContext, body: AuditBody, session: UUID | None) -> None:
    if session is None:
        raise HubError("request_invalid")
    if body.detail["kind"] in RUNNER_PING_KINDS:
        hub_ping = item.connection.execute(
            "SELECT 1 FROM hub.session_ping WHERE id = %s AND session_id = %s", (body.detail["ping_id"], session)
        ).fetchone()
        if hub_ping is not None:
            raise HubError("not_found")
        return
    marked = item.connection.execute(
        "UPDATE hub.session_ping SET delivered_at = COALESCE(delivered_at, %s) WHERE id = %s AND session_id = %s"
        " AND kind = %s RETURNING id",
        (item.now, body.detail["ping_id"], session, body.detail["kind"]),
    ).fetchone()
    if marked is None:
        raise HubError("not_found")


def handle(item: ItemContext, body: AuditBody) -> None:
    """Write the audit row (and mark a delivered ping)."""
    org, wg, task, session = _scope(item, body)
    if body.action == PING_DELIVERED:
        _mark_ping(item, body, session)
    actor = None if session is None else session_actor(item.connection, session)
    append_audit(
        item.connection,
        AuditRow(
            at=item.now,
            actor_kind="device" if actor is None else actor.kind,
            actor_id=item.device if actor is None else actor.id,
            action=body.action,
            result=body.result,
            org_id=org,
            wg_id=wg,
            task_id=task,
            session_id=session,
            device_id=item.device,
            target_kind=body.target_kind,
            target_id=body.target_id,
            detail=body.detail,
            on_behalf_of=None if actor is None else actor.on_behalf_of,
        ),
    )
