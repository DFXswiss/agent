"""``POST /v1/assign`` (03-api.md section 7.1): binds the VM to one workgroup, once.

Repeating the same ``{vm_id, org_id, wg_id}`` answers ``{}`` again (the runner may retry); any other assignment is
``409 already_assigned``; a broken VM is never assigned (``409 state_conflict``).
"""

from __future__ import annotations

from agent_guest.control import ControlContext
from agent_guest.routing import HttpError, Request, Response, Route, json_response
from agent_guest.state import StateError


def assign(context: ControlContext, request: Request) -> Response:
    data = request.data
    try:
        context.state.assign(data["vm_id"], data["org_id"], data["wg_id"])
    except StateError as error:
        raise HttpError(error.code) from None
    return json_response({})


ROUTES = [Route("POST", "/v1/assign", assign, request_schema="guest/assign.request.json")]
