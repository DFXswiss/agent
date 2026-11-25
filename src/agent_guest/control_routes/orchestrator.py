"""The main orchestrator's routes, ops and socket (05-runner.md section 17; the logic is ``orchestrator.py``).

* ``POST /v1/sessions/{id}/orchestrator/keys`` - ``{pairing_id, pub, pop, fp}``; ``not_found`` for an unknown or ended
  session, ``state_conflict`` (not a running main session, another orchestrator, the handover already given),
  ``unavailable`` (the tool failed)
* ``POST /v1/sessions/{id}/orchestrator/handover`` - ``{}``; the claim runs in the background (``state_conflict`` for
  another pairing, no keys, or another handover)
* ``POST /v1/sessions/{id}/orchestrator/end`` - ``{}``; ends the orchestrator of the session when it is still that
  pairing (revoked at the hub, or the session ended while suspended); otherwise nothing changes
* ``POST /v1/relay/{id}/answer`` - ``{}``; answers a ``relay`` pending call only (``not_found`` for any other ID or
  kind, ``state_conflict`` for a second, different answer); bodies up to ``RELAY_ANSWER_MAX_BODY`` (the hub's answer
  of at most 8 MiB as b64u)
* op ``orchestrator.mcp {session_id} -> {socket}`` on ``api.sock`` - for the API process's ``/mcp-orchestrator``
* ``orch.sock`` (``root:<ORCH_GROUP> 0660``, bound at the start of the control process): JSON lines like ``api.sock``
  with the single op ``relay {method, target, headers, body} -> {status, headers, body}``
* ``SESSION_EVENTS["agent_ended"]``: the orchestrator ends with its session's agent run when the session is stopped or
  failed (``Orchestrator.ended``)
"""

from __future__ import annotations

import contextlib
import os
import threading
from typing import Any

from agent_guest import orchestrator
from agent_guest.apisock import ApiSockServer
from agent_guest.control import POLL_SECONDS, ControlContext, bind_unix
from agent_guest.orchestrator import OrchestratorError
from agent_guest.routing import HttpError, Request, Response, Route, json_response
from agent_guest.state import StateError

UUID = {"id": "common.json#/$defs/uuid"}
RELAY_ANSWER_MAX_BODY = 12 * 1024 * 1024


def keys(context: ControlContext, request: Request) -> Response:
    try:
        result = orchestrator.orchestrator(context).keys(request.params["id"], request.data)
    except OrchestratorError as error:
        raise HttpError(error.code) from None
    return json_response(result)


def handover(context: ControlContext, request: Request) -> Response:
    try:
        orchestrator.orchestrator(context).handover(request.params["id"], request.data)
    except OrchestratorError as error:
        raise HttpError(error.code) from None
    return json_response({})


def end(context: ControlContext, request: Request) -> Response:
    orchestrator.orchestrator(context).end_pairing(request.params["id"], request.data["pairing_id"])
    return json_response({})


def relay_answer(context: ControlContext, request: Request) -> Response:
    pending_id = request.params["id"]
    with context.journal.transaction() as db:
        row = db.execute("SELECT kind FROM pending WHERE id = ?", (pending_id,)).fetchone()
    if row is None or row[0] != "relay":
        raise HttpError("not_found")
    try:
        context.pending.answer(pending_id, request.data)
    except StateError as error:
        raise HttpError(error.code) from None
    return json_response({})


def mcp(context: ControlContext, args: dict[str, Any]) -> dict[str, Any]:
    session_id = args.get("session_id")
    if set(args) != {"session_id"} or not isinstance(session_id, str):
        raise StateError("request_invalid")
    try:
        return {"socket": orchestrator.orchestrator(context).mcp_socket(session_id)}
    except OrchestratorError:
        raise StateError("not_found") from None


def relay(context: ControlContext, args: dict[str, Any]) -> dict[str, Any]:
    return orchestrator.orchestrator(context).relay(args)


def ended(context: ControlContext, session_id: str) -> None:
    orchestrator.orchestrator(context).ended(session_id)


def startup(context: ControlContext) -> None:
    """Serve ``orch.sock`` and end an orchestrator whose session no longer runs."""
    config = context.config
    server = bind_unix(config.orch_socket, config.orch_group, lambda path: ApiSockServer(path, OPS, context))
    thread = threading.Thread(target=lambda: server.serve_forever(POLL_SECONDS), name="agent-guest-orch", daemon=True)
    thread.start()

    def close() -> None:
        server.shutdown()
        server.server_close()
        thread.join(5)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(config.orch_socket)  # bind_unix made it a socket; nobody else writes /run/agent-guest

    context.on_close(close)
    orchestrator.orchestrator(context).check()


OPS = {"relay": relay}
ROUTES = [
    Route(
        "POST",
        "/v1/sessions/{id}/orchestrator/keys",
        keys,
        request_schema="guest/orchestrator-keys.request.json",
        path_params=UUID,
    ),
    Route(
        "POST",
        "/v1/sessions/{id}/orchestrator/handover",
        handover,
        request_schema="guest/orchestrator-handover.request.json",
        path_params=UUID,
    ),
    Route(
        "POST",
        "/v1/sessions/{id}/orchestrator/end",
        end,
        request_schema="guest/orchestrator-end.request.json",
        path_params=UUID,
    ),
    Route(
        "POST",
        "/v1/relay/{id}/answer",
        relay_answer,
        request_schema="guest/relay-answer.request.json",
        path_params=UUID,
        max_body=RELAY_ANSWER_MAX_BODY,
    ),
]
API_OPS = {"orchestrator.mcp": mcp}
STARTUP = startup
SESSION_EVENTS = {"agent_ended": ended}
