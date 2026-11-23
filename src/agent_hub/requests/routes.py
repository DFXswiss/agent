"""Routes of the requests area (03-api.md sections 4.1, 4.10 and 6.1).

| Route | Right |
|---|---|
| `GET /v1/me/devices` | `user`: the devices eligible for the user's device requests and `CK` |
| `POST /v1/device-requests` | `user`, the device in `GET /v1/me/devices` |
| `GET /v1/device-requests/{id}` | `owner`: the user of the request |
| `GET /v1/device/requests/{id}` | `device_addressed` |
| `POST /v1/device/requests/{id}/responses` | `device_addressed` |

`GET /v1/me/devices` lists one item per device and org with the workgroups that make the device eligible and its
presence. The rules live in `agent_hub.requests.store`. After the commit a new request sends the hint
`request {request_id}` to its device (after `reconcile {}` when it replaced an active request, so the runner drops
the replaced flow), a new response the browser frame `device_response {request_id, seq}` to the
user's browsers. Reads are read-logged by the pipeline.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from agent_hub.credentials.eligibility import eligible_devices
from agent_hub.crypto.encoding import format_time
from agent_hub.device.keys import ActiveDevice, device_id
from agent_hub.errors import HubError
from agent_hub.http.context import Context, CurrentPrincipal, HubContext
from agent_hub.http.routing import HubRouter
from agent_hub.orgs.routes import parse_id, user_id
from agent_hub.requests import store
from agent_hub.stream.publish import Audience, hint, publish

router = HubRouter()


class EnvelopeBody(BaseModel):
    """Body of `POST /v1/device-requests` and `POST /v1/device/requests/{id}/responses` (form checked by the store)."""

    model_config = ConfigDict(extra="forbid")
    envelope: dict[str, Any]


async def _in_transaction[T](context: HubContext, work: Callable[[Any], T]) -> T:
    def run() -> T:
        with context.pool.connection() as connection, connection.transaction():
            return work(connection)

    return await asyncio.to_thread(run)


def _path_id(text: str) -> UUID:
    found = parse_id(text)
    if found is None:
        raise HubError("not_found")
    return found


def my_devices(connection: Any, user: UUID, now: Any, online_seconds: int) -> list[dict[str, Any]]:
    """The body items of `GET /v1/me/devices`."""
    items = eligible_devices(connection, user)
    seen = dict(
        connection.execute(
            "SELECT device_id, last_heartbeat_at FROM hub.device_presence WHERE device_id = ANY(%s::uuid[])",
            (sorted({str(item.device_id) for item in items}),),
        ).fetchall()
    )
    online_after = now - timedelta(seconds=online_seconds)
    return [
        {
            "device_id": str(item.device_id),
            "kind": item.kind,
            "org_id": str(item.org_id),
            "wg_ids": [str(wg) for wg in item.wg_ids],
            "online": item.device_id in seen and seen[item.device_id] > online_after,
            "last_seen_at": format_time(seen[item.device_id]) if item.device_id in seen else None,
        }
        for item in items
    ]


@router.get("/v1/me/devices", auth="browser", right="user", read_resource="me.devices")
async def get_my_devices(context: Context, principal: CurrentPrincipal) -> dict[str, Any]:
    user, now, window = user_id(principal), context.clock(), context.config.device_online_seconds
    return {"devices": await _in_transaction(context, lambda connection: my_devices(connection, user, now, window))}


@router.post("/v1/device-requests", auth="browser", right="user")
async def post_request(context: Context, principal: CurrentPrincipal, body: EnvelopeBody) -> dict[str, Any]:
    user, now = user_id(principal), context.clock()
    created = await _in_transaction(context, lambda c: store.create_request(c, user, body.envelope, now))
    if created.replaced:
        hint(created.row.device_id, "reconcile", {})
    if not created.replayed:
        hint(created.row.device_id, "request", {"request_id": str(created.row.id)})
    answer: dict[str, Any] = {"request_id": str(created.row.id), "state": created.row.effective_state(now)}
    if created.replayed:
        answer["replayed"] = True
    return answer


@router.get("/v1/device-requests/{id}", auth="browser", right="owner", read_resource="device_request")
async def get_request(context: Context, principal: CurrentPrincipal, id: str) -> dict[str, Any]:
    user, request_id, now = user_id(principal), _path_id(id), context.clock()
    return await _in_transaction(context, lambda c: store.user_view(c, user, request_id, now))


@router.get("/v1/device/requests/{id}", auth="device", right="device_addressed", read_resource="device.request")
async def get_device_request(context: Context, principal: ActiveDevice, id: str) -> dict[str, Any]:
    device, request_id, now = device_id(principal), _path_id(id), context.clock()
    return await _in_transaction(context, lambda c: store.device_fetch(c, device, request_id, now))


@router.post("/v1/device/requests/{id}/responses", auth="device", right="device_addressed")
async def post_response(context: Context, principal: ActiveDevice, id: str, body: EnvelopeBody) -> dict[str, Any]:
    device, request_id, now = device_id(principal), _path_id(id), context.clock()
    responded = await _in_transaction(context, lambda c: store.add_response(c, device, request_id, body.envelope, now))
    if responded.stored:
        data = {"request_id": str(request_id), "seq": responded.seq}
        publish(Audience("user", responded.user_id), "device_response", data)
    return {}
