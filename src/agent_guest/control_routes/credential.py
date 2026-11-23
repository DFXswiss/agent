"""``PUT /v1/sessions/{id}/credential`` (03-api.md §7.1) and its hand-off to the API process (op ``credential.take``).

The model credential of a session must end up in the memory of the API process only (05-runner.md §10.2,
``gateway_credentials.py``). The runner talks to the control process, and ``api.sock`` is served by the control
process, so the credential waits in a hand-off slot of this process (memory only, one per session, a newer
provisioning replaces an older one) until the API process takes it on the session's next model request:

* ``PUT /v1/sessions/{id}/credential`` - body ``guest/session-credential.request.json`` -> ``{}``; unknown or
  inactive session ``404 not_found``; rules the schema cannot state (reserved or repeated extra header names,
  user information, query or fragment in the upstream URL) ``400 request_invalid`` with the pointer. After the
  hand-off slot is filled, ``sessions.credential_provisioned`` starts the agent of a session that waits in
  ``sandbox_ready`` (05 section 6.1 step 5, GST-05); for a running session it does nothing (refresh).
* op ``credential.take {session_id} -> {credential: body | null, active: [session IDs]}`` - removes and returns the
  waiting provisioning of the session; ``active`` lets the API process drop credentials of ended sessions. Slots of
  sessions that are no longer active are discarded on every take.

The slot is never written to the journal or a file and never logged; a provisioning that the API process has not
taken yet is lost with a restart of the control process (the runner provisions again, 05 §6.1).
"""

from __future__ import annotations

import threading
from typing import Any

from agent_guest import sessions
from agent_guest.control import ControlContext
from agent_guest.gateway_credentials import CredentialInvalid, parse_credential
from agent_guest.routing import HttpError, Request, Response, Route, json_response
from agent_guest.state import INACTIVE_STATES, StateError


class Handoff:
    """Provisionings waiting for the API process, keyed by session ID; thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._waiting: dict[str, dict[str, Any]] = {}

    def offer(self, session_id: str, body: dict[str, Any]) -> None:
        with self._lock:
            self._waiting[session_id] = body

    def take(self, session_id: str, active: set[str]) -> dict[str, Any] | None:
        with self._lock:
            for name in [name for name in self._waiting if name not in active]:
                del self._waiting[name]
            return self._waiting.pop(session_id, None)

    def waiting(self) -> list[str]:
        with self._lock:
            return sorted(self._waiting)


_HANDOFF = "_credential_handoff"
_HANDOFF_LOCK = threading.Lock()


def handoff(context: ControlContext) -> Handoff:
    """The hand-off of this control process, attached to its context like the session manager (GST-05)."""
    with _HANDOFF_LOCK:
        found = vars(context).get(_HANDOFF)
        if not isinstance(found, Handoff):
            found = vars(context)[_HANDOFF] = Handoff()
        return found


def _active(context: ControlContext) -> set[str]:
    return {session.id for session in context.state.sessions() if session.state not in INACTIVE_STATES}


def provision(context: ControlContext, request: Request) -> Response:
    session_id = request.params["id"]
    if session_id not in _active(context):
        raise HttpError("not_found")
    try:
        parse_credential(request.data)
    except CredentialInvalid as error:
        raise HttpError("request_invalid", detail=[error.pointer]) from None
    handoff(context).offer(session_id, request.data)
    sessions.credential_provisioned(context, session_id)
    return json_response({})


def take(context: ControlContext, args: dict[str, Any]) -> dict[str, Any]:
    session_id = args.get("session_id")
    if set(args) != {"session_id"} or not isinstance(session_id, str):
        raise StateError("request_invalid")
    active = _active(context)
    return {"credential": handoff(context).take(session_id, active), "active": sorted(active)}


ROUTES = [
    Route(
        "PUT",
        "/v1/sessions/{id}/credential",
        provision,
        request_schema="guest/session-credential.request.json",
        path_params={"id": "common.json#/$defs/uuid"},
        max_body=64 * 1024,
    )
]
API_OPS = {"credential.take": take}
