"""Journal endpoints (03-api.md section 7.1) and the ``journal.append`` op of ``api.sock``.

* ``GET /v1/journal?after=&limit=&wait=`` - records with ``seq > after`` (default 0), at most ``limit`` (default 200,
  max 1000), waiting up to ``wait`` seconds (default 0, max 25) for the first one; ``head`` is the last ``seq``
* ``POST /v1/journal/ack {through}`` - acknowledges records up to ``through`` (``409 state_conflict`` beyond the head)
* op ``journal.append {kind, session_id, body} -> {seq}`` - records of the API process. It may append only the kinds
  of ``API_KINDS``; kinds that need control-side checks (prompt origin, pending calls, transcript, files) get their
  own ops. A ``session_id`` must name an active session; only VM-level kinds may omit it.
"""

from __future__ import annotations

from typing import Any

from agent_guest.control import ControlContext
from agent_guest.journal import JournalError
from agent_guest.routing import HttpError, Request, Response, Route, json_response
from agent_guest.state import INACTIVE_STATES, StateError

DEFAULT_LIMIT = 200
API_KINDS = frozenset({"session.hook", "usage", "ping.delivered", "guest.error", "platform.refused"})


def read(context: ControlContext, request: Request) -> Response:
    query = request.query
    records, head = context.journal.read(
        after=int(query.get("after", "0")),
        limit=int(query.get("limit", str(DEFAULT_LIMIT))),
        wait=int(query.get("wait", "0")),
    )
    return json_response({"records": records, "head": head})


def ack(context: ControlContext, request: Request) -> Response:
    try:
        context.journal.ack(request.data["through"])
    except JournalError as error:
        raise HttpError(error.code) from None
    return json_response({})


def append(context: ControlContext, args: dict[str, Any]) -> dict[str, Any]:
    kind, session_id, body = args.get("kind"), args.get("session_id"), args.get("body")
    if set(args) != {"kind", "session_id", "body"} or kind not in API_KINDS or not isinstance(body, dict):
        raise StateError("request_invalid")
    if session_id is not None:
        session = context.state.session(session_id) if isinstance(session_id, str) else None
        if session is None or session.state in INACTIVE_STATES:
            raise StateError("not_found")
    try:
        seq = context.journal.append(kind, body, session_id)
    except JournalError as error:
        if error.code == "unavailable":
            raise
        raise JournalError("request_invalid", error.detail) from None
    return {"seq": seq}


ROUTES = [
    Route("GET", "/v1/journal", read, request_schema="guest/journal.request.json", body="none"),
    Route("POST", "/v1/journal/ack", ack, request_schema="guest/journal-ack.request.json"),
]
API_OPS = {"journal.append": append}
