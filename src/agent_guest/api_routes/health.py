"""``GET /v1/health`` of the API process: reachability of port 7700 and of the control process behind it.

Answers ``{"state": "ok", "guest_version": ...}`` when the control process answers ``hello`` on ``api.sock``, else
``503 {"error": "unavailable"}``. Needs no session token and returns nothing session-specific.
"""

from __future__ import annotations

from agent_guest.apisock import ApiSockError
from agent_guest.routing import HttpError, Request, Response, Route, json_response
from agent_guest.server import ApiContext


def health(context: ApiContext, request: Request) -> Response:
    try:
        hello = context.control.hello()
    except ApiSockError:
        raise HttpError("unavailable") from None
    return json_response({"state": "ok", "guest_version": hello["guest_version"]})


ROUTES = [Route("GET", "/v1/health", health, body="none")]
