"""``GET /v1/health`` of the control socket (03-api.md section 7.1, ``guest/health.response.json``).

``state`` is ``broken`` once the VM was marked broken (sandbox self-test), else ``assigned`` with ``org_id``/``wg_id``
or ``unassigned``. ``sessions`` lists the cached sessions (at most 64, sorted by ID); ``landlock_abi`` comes from
``build.json`` (0 without it), ``claude_version`` from the Claude install record, ``boot_id`` from the kernel.
"""

from __future__ import annotations

from typing import Any

from agent_guest.control import ControlContext
from agent_guest.routing import Request, Response, Route, json_response

MAX_SESSIONS = 64


def health(context: ControlContext, request: Request) -> Response:
    info, state = context.info, context.state
    assignment = state.assignment
    body: dict[str, Any] = {
        "guest_version": info.guest_version,
        "claude_version": info.claude_version,
        "state": "broken" if state.broken else ("unassigned" if assignment is None else "assigned"),
    }
    if assignment is not None:
        body["org_id"] = assignment["org_id"]
        body["wg_id"] = assignment["wg_id"]
    body["sessions"] = [
        {"session_id": session.id, "state": session.state} for session in state.sessions()[:MAX_SESSIONS]
    ]
    body["landlock_abi"] = info.landlock_abi
    body["boot_id"] = info.boot_id
    return json_response(body)


ROUTES = [Route("GET", "/v1/health", health, request_schema="guest/health.request.json", body="none")]
