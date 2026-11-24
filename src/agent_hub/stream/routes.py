"""Routes of the live channels: `GET /v1/stream` (browsers, 03-api.md section 5) and `GET /v1/device/ws` (devices,
section 6.2). Both are streaming routes: the request pipeline verifies the signature and the key kind, the responses
are never response-signed (03-api.md section 1). The browser channel is a read (ADR-063 section 3): the route records
`read.stream` of the organization and `read.stream.terminal` for each session whose terminal output it watches before
the stream starts, and drops the opened stream when that fails; a refused stream is recorded by the pipeline. The
device channel carries hints only and is not logged. The work happens in the process's `StreamService`
(`agent_hub.stream.service`)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime
from typing import Annotated, cast
from uuid import UUID

from fastapi import Query, Request
from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send
from starlette.websockets import WebSocket

from agent_hub.audit.chain import AuditRow
from agent_hub.auth.keys import Principal
from agent_hub.device.features import HEADER as FEATURES_HEADER
from agent_hub.device.features import parse_features
from agent_hub.device.keys import ActiveDevice
from agent_hub.errors import HubError
from agent_hub.http.context import CurrentPrincipal
from agent_hub.http.readlog import read_row
from agent_hub.http.routing import HubRouter
from agent_hub.stream.service import StreamService, service_of
from agent_hub.stream.sse import BrowserStream, StreamQuery, parse_query

router = HubRouter()
STREAM_HEADERS = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}


class OpenedStream(StreamingResponse):
    """The response of an opened browser stream. The stream is registered before the response starts (no event is
    missed between its opening frames and the first byte) and forgotten when the response ends, however it ends -
    also when its body never starts (a `HEAD`, answered with the response start; a client gone before the first
    byte), where the body's own cleanup never runs."""

    def __init__(self, service: StreamService, stream: BrowserStream) -> None:
        super().__init__(service.browser_body(stream), media_type="text/event-stream", headers=STREAM_HEADERS)
        self.service = service
        self.stream = stream

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.service.abandon(self.stream)


def _service(service: StreamService | None) -> StreamService:
    if service is None:
        raise HubError("unavailable")
    return service


def stream_rows(principal: Principal, query: StreamQuery, at: datetime) -> list[AuditRow]:
    """`read.stream` of the organization and `read.stream.terminal` of each watched session (workgroup and task placed
    by the log)."""
    opened = read_row(principal, resource="stream", status=200, path_params={}, query={}, at=at)
    rows = [replace(opened, org_id=query.org)]
    for session in sorted(query.terminals, key=str):
        row = read_row(principal, resource="stream.terminal", status=200, path_params={}, query={}, at=at)
        rows.append(replace(row, org_id=query.org, session_id=session))
    return rows


@router.get("/v1/stream", auth="browser", right="org:member", read_resource="stream", streaming=True)
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
    context = service.context
    try:
        for row in stream_rows(principal, query, context.clock()):
            await asyncio.to_thread(context.read_log.admit, row)
    except Exception:
        service.abandon(opened)  # nothing leaves the hub without its read rows (review M3)
        raise
    return OpenedStream(service, opened)


@router.hub_websocket("/v1/device/ws", auth="device", right="device")
async def device_ws(websocket: WebSocket, principal: ActiveDevice) -> None:
    """The device hint channel (right `device`: `agent_hub.device.keys.active_device`)."""
    service = service_of(websocket.app)
    if service is None:
        await websocket.close(1012)
        return
    features = parse_features(websocket.headers.get(FEATURES_HEADER))
    await service.run_device(websocket, cast(UUID, principal.principal_id), features)
