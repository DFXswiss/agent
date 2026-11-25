"""Pending MCP calls (02-data-model.md section 5 ``pending``, 03-api.md section 7.1): answer route and api.sock ops.

Library for GST-04/GST-07: the API process creates a pending call (``pending.create``), which journals an
``operation.request``, ``platform.query`` or ``workvm.call`` record carrying the new ``pending_id``; the runner
answers with ``POST /v1/pending/{id}/answer``; the API process waits for the answer with a time limit
(``pending.wait``).

* ``POST /v1/pending/{id}/answer {status, result | error}`` - ``{}``; unknown ID ``404 not_found``; a second, different
  answer ``409 state_conflict`` (an identical repeat is accepted)
* op ``pending.create {session_id, kind: "operation" | "query" | "workvm", body} -> {pending_id}`` - ``body`` is the
  journal body without ``pending_id``; it is validated with the ID added (``request_invalid`` with pointers); the
  kind ``relay`` is the orchestrator tool's alone (``orch.sock``, ``control_routes/orchestrator.py``) and refused here;
  an operation the session's rights profile does not allow is recorded ``local_check: "rejected"`` with
  ``rights_denied`` (``profiles.refusal``, ADR-090 section 3), so the runner never executes it
* op ``pending.wait {pending_id, session_id, timeout <= 33} -> {state: "answered", answer} | {state: "waiting"}``;
  33 s is the longest MCP wait (``get_operation``/``get_subagent_result``: ``wait_seconds`` ≤ 30 plus the read margin,
  GST-07; the work VM tools: ``wait_seconds`` ≤ 30 plus 5 s, KOR-29), below the 35 s limit of the ``/mcp`` route
"""

from __future__ import annotations

from typing import Any

from agent_guest import profiles, sessions
from agent_guest.control import ControlContext
from agent_guest.journal import JournalError
from agent_guest.routing import HttpError, Request, Response, Route, json_response
from agent_guest.state import PENDING_KINDS, StateError

MAX_WAIT_SECONDS = 33


def answer(context: ControlContext, request: Request) -> Response:
    try:
        context.pending.answer(request.params["id"], request.data)
    except StateError as error:
        raise HttpError(error.code) from None
    return json_response({})


def create(context: ControlContext, args: dict[str, Any]) -> dict[str, Any]:
    session_id, kind, body = args.get("session_id"), args.get("kind"), args.get("body")
    if (
        set(args) != {"session_id", "kind", "body"}
        or not isinstance(session_id, str)
        or kind not in PENDING_KINDS
        or kind == "relay"
        or not isinstance(body, dict)
        or "pending_id" in body
    ):
        raise StateError("request_invalid")
    if kind == "operation" and body.get("local_check") == "passed":
        refused = profiles.refusal(sessions.manager(context).profile(session_id), body.get("type"))
        if refused is not None:
            body = {**body, "local_check": "rejected", "local_error": refused}
    try:
        pending_id = context.pending.create(session_id, kind, body)
    except JournalError as error:
        if error.code == "unavailable":
            raise
        raise JournalError("request_invalid", error.detail) from None
    return {"pending_id": pending_id}


def wait(context: ControlContext, args: dict[str, Any]) -> dict[str, Any]:
    pending_id, session_id, timeout = args.get("pending_id"), args.get("session_id"), args.get("timeout")
    if (
        set(args) != {"pending_id", "session_id", "timeout"}
        or not isinstance(pending_id, str)
        or not isinstance(session_id, str)
        or not isinstance(timeout, int | float)
        or isinstance(timeout, bool)
        or not 0 <= timeout <= MAX_WAIT_SECONDS
    ):
        raise StateError("request_invalid")
    result = context.pending.wait(pending_id, timeout, session_id)
    if result is None:
        return {"state": "waiting"}
    return {"state": "answered", "answer": result}


ROUTES = [
    Route(
        "POST",
        "/v1/pending/{id}/answer",
        answer,
        request_schema="guest/pending-answer.request.json",
        path_params={"id": "common.json#/$defs/uuid"},
    )
]
API_OPS = {"pending.create": create, "pending.wait": wait}
