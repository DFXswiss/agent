"""Routes of the live channels: `GET /v1/stream` (browsers, 03-api.md section 5) and `GET /v1/device/ws` (devices,
section 6.2). Both are streaming routes: the request pipeline verifies the signature and the key kind, the responses
are never response-signed (03-api.md section 1), no read-log row is written. The work happens in the process's
`StreamService` (`agent_hub.stream.service`)."""

from __future__ import annotations

from typing import Annotated, cast
from uuid import UUID

from fastapi import Query, Request
from fastapi.responses import StreamingResponse
from starlette.websockets import WebSocket

from agent_hub.device.features import HEADER as FEATURES_HEADER
from agent_hub.device.features import parse_features
from agent_hub.device.keys import ActiveDevice
from agent_hub.errors import HubError
from agent_hub.http.context import CurrentPrincipal
from agent_hub.http.routing import HubRouter
from agent_hub.stream.service import StreamService, service_of
from agent_hub.stream.sse import parse_query

router = HubRouter()
STREAM_HEADERS = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}


def _service(service: StreamService | None) -> StreamService:
    if service is None:
        raise HubError("unavailable")
    return service


@router.get("/v1/stream", auth="browser", right="org:member", streaming=True)
async def stream(
    request: Request,
    principal: CurrentPrincipal,
    *,
    org: Annotated[str, Query()],
    terminal: Annotated[list[str] | None, Query()] = None,
    cursors: Annotated[str | None, Query()] = None,
    monitoring: Annotated[str | None, Query()] = None,
    account: Annotated[str | None, Query()] = None,
) -> StreamingResponse:
    """The browser live channel of one org."""
    query = parse_query(org, terminal or [], cursors, monitoring, account)
    service = _service(service_of(request.app))
    opened = await service.open_browser(cast(UUID, principal.principal_id), query, principal.key_id)
    return StreamingResponse(service.browser_body(opened), media_type="text/event-stream", headers=STREAM_HEADERS)


@router.hub_websocket("/v1/device/ws", auth="device", right="device")
async def device_ws(websocket: WebSocket, principal: ActiveDevice) -> None:
    """The device hint channel (right `device`: `agent_hub.device.keys.active_device`)."""
    service = service_of(websocket.app)
    if service is None:
        await websocket.close(1012)
        return
    features = parse_features(websocket.headers.get(FEATURES_HEADER))
    await service.run_device(websocket, cast(UUID, principal.principal_id), features)
