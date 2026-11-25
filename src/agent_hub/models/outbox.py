"""Outbox items of model access with fallback (ADR-081, KOR-50; ADR-027 section 4 `<area>/outbox.py:HANDLERS`, base
handlers claimed with `BASE_KINDS`, ADR-037).

- `operator_quota` (platform devices only, else `403 forbidden`): the complete snapshot of the device's operator
  subscriptions replaces the stored one unless the stored one was taken later (`hub.operator_quota`, one row per
  device; `GET /v1/orgs/{org}/quotas` reads it).
- `dfx_key_state` (platform devices only): `issuer: true` enters the device in `hub.dfx_issuer`, `false` removes it
  (`agent_hub.models.dfx.issuer` elects among them).
- `dfx_key_issued`, `dfx_key_usage` (platform devices only): `agent_hub.models.dfx.issued` and `usage_booked`.
- `session_wait` (`device:session`: the session must run on the pushing device, else `404 not_found` /
  `403 forbidden`): the model-quota wait of a session in any live state (ADR-081 sections 3 and 4) - `wait_until`
  (null clears the wait) and `dfx_choice`; a report older than the stored change (`session.wait_at`) changes
  nothing. A change derives the task again (its attention record shows `quota_wait` / `quota_choice` while its main
  session waits).
- `audit` (an extension after the outbox area's handler): a stored `session.model_switched` notifies the task's
  owner (`agent_hub.notifications.rules.switch_rule`, published after the commit).
- `usage` (an extension after the usage and billing handlers): the provider cost of a request an **org device** made
  on DFX access (`access_origin: platform`) is the weight of its session in the next booking of the device's key
  (`agent_hub.models.dfx.record_cost`; only while the device holds a live key of the session's org). Platform devices
  are charged per request by the billing area instead.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast
from uuid import UUID

from psycopg.types.json import Jsonb

from agent_hub.models import dfx
from agent_hub.notifications import rules
from agent_hub.outbox.base import Handler, ItemContext, moment, session_on_device
from agent_hub.outbox.bodies import AuditBody, DfxKeyStateBody, OperatorQuotaBody, SessionWaitBody, UsageBody
from agent_hub.sessions.taskstate import Actor, TaskRow, lock_task, refresh_task

UPSERT_SNAPSHOT = (
    "INSERT INTO hub.operator_quota (device_id, at, snapshot, received_at) VALUES (%s, %s, %s, %s)"
    " ON CONFLICT (device_id) DO UPDATE SET at = EXCLUDED.at, snapshot = EXCLUDED.snapshot,"
    " received_at = EXCLUDED.received_at WHERE hub.operator_quota.at <= EXCLUDED.at"
)


def operator_quota(item: ItemContext, body: OperatorQuotaBody) -> None:
    """Keep the newest snapshot of the device (module docstring)."""
    dfx.require_platform(item)
    snapshot = [entry.model_dump() for entry in body.subscriptions]
    item.connection.execute(UPSERT_SNAPSHOT, (item.device, moment(body.at), Jsonb(snapshot), item.now))


def dfx_key_state(item: ItemContext, body: DfxKeyStateBody) -> None:
    """Enter or remove the device as a possible issuer (module docstring)."""
    dfx.require_platform(item)
    if body.issuer:
        item.connection.execute(
            "INSERT INTO hub.dfx_issuer (device_id, announced_at) VALUES (%s, %s)"
            " ON CONFLICT (device_id) DO UPDATE SET announced_at = EXCLUDED.announced_at",
            (item.device, item.now),
        )
    else:
        item.connection.execute("DELETE FROM hub.dfx_issuer WHERE device_id = %s", (item.device,))


def key_cost(item: ItemContext, body: UsageBody) -> None:
    """The weight of an org device's session on its key (module docstring)."""
    if body.access_origin != dfx.PLATFORM or body.delta.cost_micro_usd == 0:
        return
    if dfx.platform_device(item.connection, item.device):
        return
    row = item.connection.execute("SELECT org_id FROM hub.session WHERE id = %s", (body.session_id,)).fetchone()
    org = cast(tuple[UUID], row)[0]  # the base handler checked that the session runs on this device
    dfx.record_cost(item.connection, org, item.device, UUID(body.session_id), body.delta.cost_micro_usd)


def switch_notice(item: ItemContext, body: AuditBody) -> None:
    """Notify the owner of the task of a model switch (module docstring)."""
    if body.action != "session.model_switched" or body.session_id is None:
        return
    row = item.connection.execute("SELECT task_id FROM hub.session WHERE id = %s", (body.session_id,)).fetchone()
    task = cast(tuple[UUID], row)[0]  # the base handler checked that the session runs on this device
    rows = rules.switch_rule(item.connection, task, body.detail, item.now)
    item.after.append(lambda: rules.publish_rows(rows))


def session_wait(item: ItemContext, body: SessionWaitBody) -> None:
    """Store the wait of a session (module docstring)."""
    _org, _wg, task_id, session = session_on_device(item, body.session_id)
    task = cast(TaskRow, lock_task(item.connection, task_id))
    until = None if body.wait_until is None else moment(body.wait_until)
    choice = body.dfx_choice and until is not None
    changed = item.connection.execute(
        "UPDATE hub.session SET wait_until = %s, dfx_choice = %s, wait_at = %s WHERE id = %s"
        " AND (wait_at IS NULL OR wait_at <= %s) AND (wait_until, dfx_choice) IS DISTINCT FROM (%s::timestamptz, %s)"
        " RETURNING id",
        (until, choice, moment(body.at), session, moment(body.at), until, choice),
    ).fetchone()
    if changed is not None:
        refresh_task(item.connection, task, item.now, Actor("device", item.device), item.effects)


HANDLERS: Mapping[str, Handler] = {
    "operator_quota": operator_quota,
    "dfx_key_state": dfx_key_state,
    "dfx_key_issued": dfx.issued,
    "dfx_key_usage": dfx.usage_booked,
    "usage": key_cost,
    "audit": switch_notice,
    "session_wait": session_wait,
}
BASE_KINDS = frozenset({"operator_quota", "dfx_key_state", "dfx_key_issued", "dfx_key_usage", "session_wait"})
"""This area's handlers are the base handlers of these kinds (`agent_hub.outbox.chains`, ADR-037)."""
