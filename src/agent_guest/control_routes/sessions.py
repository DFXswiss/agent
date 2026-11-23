"""``POST /v1/sessions`` and ``POST /v1/sessions/{id}/control`` (03-api.md section 7.1, 05-runner.md §6.1, §6.3).

* ``POST /v1/sessions`` - body ``guest/sessions-create.request.json``; creates the sandbox (``agent_guest.sessions``)
  and answers ``{"state": "sandbox_ready"}``; the agent starts when the runner provisioned the credential. Errors:
  ``400 request_invalid`` (schema, ``depth``/``parent_session_id`` disagree, model environment for another access
  than ``platform``, NUL characters), ``409 id_conflict`` (same ID, different request, session active),
  ``409 state_conflict`` (session stopped or failed), ``500 sandbox_failed`` (user, directories or harness could not
  be created; the session is recorded ``failed`` with reason ``sandbox_failed``).
* ``POST /v1/sessions/{id}/control {action}`` - ``stop`` is handled here (``{}`` also for a session that already
  ended); every other action is looked up in ``CONTROL_ACTIONS`` of the ``control_routes`` modules (GST-06 adds
  ``interrupt`` and ``suspend`` in ``control.py``); an action nobody registered is ``400 request_invalid``. Unknown
  session: ``404 not_found``.
* ``STARTUP`` (start of a restarted control process): ``SessionManager.reattach`` - watch the running agents again,
  end sessions whose pending start was lost, finish interrupted stops (KOR-02).
"""

from __future__ import annotations

from agent_guest import sessions
from agent_guest.control import ControlContext
from agent_guest.routing import HttpError, Request, Response, Route, json_response
from agent_guest.state import StateError

SESSION_ID = {"id": "common.json#/$defs/uuid"}


def create(context: ControlContext, request: Request) -> Response:
    try:
        sessions.manager(context).create(request.data)
    except sessions.SessionError as error:
        raise HttpError(error.code) from None
    return json_response({"state": "sandbox_ready"})


def control(context: ControlContext, request: Request) -> Response:
    session_id, action = request.params["id"], request.data["action"]
    if context.state.session(session_id) is None:
        raise HttpError("not_found")
    try:
        if action == "stop":
            sessions.manager(context).stop(session_id)
        else:
            handler = sessions.control_actions().get(action)
            if handler is None:
                raise HttpError("request_invalid")
            handler(context, session_id)
    except (sessions.SessionError, StateError) as error:
        raise HttpError(error.code) from None
    return json_response({})


REATTACHED = sessions.RUNNING_STATES | {"sandbox_ready", "stopping"}


def startup(context: ControlContext) -> None:
    if any(session.state in REATTACHED for session in context.state.sessions()):
        sessions.manager(context).reattach()


STARTUP = startup
ROUTES = [
    # The initial prompt may have 256 Ki characters, up to 6 bytes each as JSON escapes.
    Route(
        "POST", "/v1/sessions", create, request_schema="guest/sessions-create.request.json", max_body=2 * 1024 * 1024
    ),
    Route(
        "POST",
        "/v1/sessions/{id}/control",
        control,
        request_schema="guest/session-control.request.json",
        path_params=SESSION_ID,
    ),
]
