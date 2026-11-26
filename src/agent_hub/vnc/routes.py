"""Routes of the VNC tunnels (ADR-101 section 3; 03-api.md sections 4.10a and 6.3).

| Route | Right |
|---|---|
| `GET /v1/vnc/{request}` (WebSocket) | `owner`: the user of an active `vnc.open` root of the user's own identity |
| `GET /v1/device/vnc/{request}` (WebSocket) | `device_addressed`: the device of an active `vnc.open` root |

The upgrade passes the signature gate like `GET /v1/device/ws`; a request that is unknown, of another user or device,
of another kind, a follow-up, closed, or (for the client) sent by the user's agent instead of the user is closed with
a policy violation before anything is relayed: only the person takes over a screen, never an agent (E-39). The work
happens in `agent_hub.vnc.relay`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from starlette.websockets import WebSocket

from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.credentials.eligibility import is_eligible
from agent_hub.db.pool import transaction
from agent_hub.device.keys import ActiveDevice, device_id
from agent_hub.errors import HubError
from agent_hub.http.context import CurrentPrincipal, HubContext
from agent_hub.http.routing import HubRouter
from agent_hub.orgs.routes import parse_id, user_id
from agent_hub.requests import store
from agent_hub.vnc import relay

router = HubRouter()
KIND = "vnc.open"


def _context(websocket: WebSocket) -> HubContext:
    return cast(HubContext, websocket.app.state.hub)


def _open_root(connection: Any, request_id: UUID | None, now: datetime) -> store.RequestRow:
    row = None if request_id is None else store.load(connection, request_id)
    if row is None or row.kind != KIND or row.follows or not row.active(now):
        raise HubError("not_found")
    return row


def client_row(connection: Any, user: UUID, request_id: UUID | None, now: datetime) -> store.RequestRow:
    """The request of the client end (module docstring)."""
    row = _open_root(connection, request_id, now)
    if row.user_id != user:
        raise HubError("not_found")
    if json.loads(row.envelope)["header"]["user"] != str(row.user_id):
        raise HubError("forbidden")
    return row


def device_row(connection: Any, device: UUID, request_id: UUID | None, now: datetime) -> store.RequestRow:
    """The request of the device end (module docstring)."""
    row = _open_root(connection, request_id, now)
    if row.device_id != device or not is_eligible(connection, device, row.user_id):
        raise HubError("not_found")
    return row


def _auditor(context: HubContext, row: store.RequestRow) -> relay.Audit:
    async def audit(action: str, detail: Mapping[str, int | str]) -> None:
        entry = AuditRow(
            at=context.clock(),
            actor_kind="user",
            actor_id=row.user_id,
            action=action,
            result="ok",
            device_id=row.device_id,
            target_kind="device_request",
            target_id=str(row.id),
            detail=dict(detail),
        )

        def write() -> None:
            with transaction(context.pool) as connection:
                append_audit(connection, entry)

        await asyncio.to_thread(write)

    return audit


async def _tunnel(
    websocket: WebSocket, side: str, request: str, find: Callable[[Any, UUID | None, datetime], store.RequestRow]
) -> None:
    context = _context(websocket)
    now = context.clock()

    def load() -> store.RequestRow:
        with transaction(context.pool) as connection:
            return find(connection, parse_id(request), now)

    try:
        row = await asyncio.to_thread(load)
    except HubError:
        await websocket.close(relay.POLICY_VIOLATION)
        return
    seconds = (row.expires_at - now).total_seconds()
    await relay.TUNNELS.run(side, websocket, row.id, seconds=seconds, audit=_auditor(context, row))


@router.hub_websocket("/v1/vnc/{request}", auth="browser", right="owner")
async def client_tunnel(websocket: WebSocket, principal: CurrentPrincipal, request: str) -> None:
    """The owner's end of a VNC tunnel."""
    user = user_id(principal)
    await _tunnel(websocket, relay.CLIENT, request, lambda c, r, now: client_row(c, user, r, now))


@router.hub_websocket("/v1/device/vnc/{request}", auth="device", right="device_addressed")
async def device_tunnel(websocket: WebSocket, principal: ActiveDevice, request: str) -> None:
    """The device's end of a VNC tunnel."""
    device = device_id(principal)
    await _tunnel(websocket, relay.DEVICE, request, lambda c, r, now: device_row(c, device, r, now))
