"""Pings on the control socket and the ping ops of ``api.sock`` (05-runner.md section 9, 03-api.md section 7.1).

* ``POST /v1/sessions/{id}/pings {ping_id, kind, level?, ref}`` -> ``{}``: keeps the content-free ping
  (``pings.PingStore.store``). Unknown session ``404 not_found``; stopped or failed session ``409 state_conflict``; the
  same ``ping_id`` with other fields ``409 id_conflict``. Nothing reaches the TUI: no tmux, no prompt delivery; the
  session sees the notice with its next hook answer.
* op ``ping.notice {session_id} -> {notice: string | null}`` - the hook addon (``hook_addons/pings.py``)
* op ``ping.take {session_id} -> {notifications: [...]}`` - the tool ``get_notifications``
"""

from __future__ import annotations

from typing import Any

from agent_guest.control import ControlContext
from agent_guest.pings import PingStore
from agent_guest.routing import HttpError, Request, Response, Route, json_response
from agent_guest.state import StateError


def _store(context: ControlContext) -> PingStore:
    return PingStore(context.journal, context.state)


def receive(context: ControlContext, request: Request) -> Response:
    try:
        _store(context).store(request.params["id"], request.data)
    except StateError as error:
        raise HttpError(error.code) from None
    return json_response({})


def _session_id(args: dict[str, Any]) -> str:
    session_id = args.get("session_id")
    if set(args) != {"session_id"} or not isinstance(session_id, str):
        raise StateError("request_invalid")
    return session_id


def notice(context: ControlContext, args: dict[str, Any]) -> dict[str, Any]:
    return {"notice": _store(context).notice(_session_id(args))}


def take(context: ControlContext, args: dict[str, Any]) -> dict[str, Any]:
    return {"notifications": _store(context).take(_session_id(args))}


ROUTES = [
    Route(
        "POST",
        "/v1/sessions/{id}/pings",
        receive,
        request_schema="guest/session-pings.request.json",
        path_params={"id": "common.json#/$defs/uuid"},
    )
]
API_OPS = {"ping.notice": notice, "ping.take": take}
