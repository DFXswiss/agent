"""WebSocket hint `request {request_id}` (03-api.md section 6.2, 05-runner.md section 11.1): offer the request to the
service, which fetches and verifies it by signed HTTP. A request ID that is not a canonical UUID is logged and
dropped."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from agent_runner.reconcile import wire
from agent_runner.requests.service import service_for


async def on_request(context: Any, hint: Mapping[str, object]) -> None:
    service = service_for(context)
    try:
        request_id = wire.uuid_text(hint.get("request_id"), "request")
    except wire.ResponseInvalid:
        context.log.write("runner.requests hint_invalid\n")
        context.log.flush()
        return
    if service is not None:
        service.offer(request_id)


HANDLERS: dict[str, Callable[..., Any]] = {"request": on_request}
