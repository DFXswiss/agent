"""Outbox items of model access with fallback (ADR-080, KOR-50; ADR-027 section 4 `<area>/outbox.py:HANDLERS`, base
handlers claimed with `BASE_KINDS`, ADR-037).

- `operator_quota` (platform devices only, else `403 forbidden`): the complete snapshot of the device's operator
  subscriptions replaces the stored one unless the stored one was taken later (`hub.operator_quota`, one row per
  device; `GET /v1/orgs/{org}/quotas` reads it).
- `dfx_key_state` (platform devices only): `issuer: true` enters the device in `hub.dfx_issuer`, `false` removes it
  (`agent_hub.models.dfx.issuer` elects among them).
- `dfx_key_issued`, `dfx_key_usage` (platform devices only): `agent_hub.models.dfx.issued` and `usage_booked`.
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
from agent_hub.outbox.base import Handler, ItemContext, moment
from agent_hub.outbox.bodies import DfxKeyStateBody, OperatorQuotaBody, UsageBody

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


HANDLERS: Mapping[str, Handler] = {
    "operator_quota": operator_quota,
    "dfx_key_state": dfx_key_state,
    "dfx_key_issued": dfx.issued,
    "dfx_key_usage": dfx.usage_booked,
    "usage": key_cost,
}
BASE_KINDS = frozenset({"operator_quota", "dfx_key_state", "dfx_key_issued", "dfx_key_usage"})
"""This area's handlers are the base handlers of these kinds (`agent_hub.outbox.chains`, ADR-037)."""
