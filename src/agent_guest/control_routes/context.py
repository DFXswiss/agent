"""Context checkpoints of a session on the control socket (ADR-072 sections 2 and 7, 03-api.md section 7.1;
``contexts.py``).

* ``POST /v1/sessions/{id}/context/export {workspace_max_bytes, transcript_max_bytes}`` -> ``application/x-tar``:
  the context archive (``context.json``, ``transcript/``, ``work/``) in any state of a session whose sandbox exists
  (also ``suspended``, ``stopped`` and ``failed``: the directories are kept after the end). Unknown session ``404
  not_found``; a session without conversation (or without a recorded task) ``409 state_conflict``; a transcript over
  ``transcript_max_bytes`` ``413 size_exceeded``; a workspace over ``workspace_max_bytes`` is left out
  (``context.json`` ``workspace: "omitted_too_large"``). The export first waits, bounded, for a quiet transcript -
  for an ``idle`` session until the turn's final answer is written - and a conversation without a complete turn
  (no ``user`` and ``assistant`` record) is ``409 state_conflict`` as well (``contexts.settle``, KOR-33 E2E).
* ``POST /v1/contexts/{id}/stage`` - body ``application/x-tar`` (a context archive, ``Content-Length`` at most
  ``contexts.MAX_STAGE_BYTES``, ``413 size_exceeded`` before anything is read) -> ``{files, bytes}``: staged for the
  session ``{id}`` that a following ``POST /v1/sessions`` with ``restore`` creates (or resumes, when a ``suspended``
  session of this ID exists here). The stage also rewrites the transcript for the session's directory and records
  its baseline, so the create only moves files (KOR-33 review no. 9). A guest session with this ID that is not
  ``suspended`` is ``409 state_conflict``; a refused archive ``400 request_invalid`` (anything but regular files,
  directories and links inside ``work/``; transcript files of another conversation; a bad ``context.json``) or
  ``path_invalid``, ``413 size_exceeded``.
"""

from __future__ import annotations

import time

from agent_guest import contexts, sessions
from agent_guest.clock import iso_ms
from agent_guest.control import ControlContext
from agent_guest.files import FilesError
from agent_guest.routing import HttpError, Request, Response, Route, json_response


def export_context(context: ControlContext, request: Request) -> Response:
    session = context.state.session(request.params["id"])
    if session is None:
        raise HttpError("not_found")
    task_id = sessions.manager(context).task_id(session.id)
    if task_id is None:
        raise HttpError("state_conflict")
    try:
        stream = contexts.export_context(
            session,
            task_id=task_id,
            agent_version=context.info.claude_version,
            created_at=iso_ms(context.journal.clock.now()),
            workspace_max=request.data["workspace_max_bytes"],
            transcript_max=request.data["transcript_max_bytes"],
            clock=contexts.Settle(state=lambda: _state(context, session.id)),
        )
    except FilesError as error:
        raise HttpError(error.code) from None
    return Response(body=stream, content_type="application/x-tar")


def _state(context: ControlContext, session_id: str) -> str | None:
    """The session's current state (the export waits for an ``idle`` session's last turn)."""
    current = context.state.session(session_id)
    return None if current is None else current.state


def stage_context(context: ControlContext, request: Request) -> Response:
    session_id = request.params["id"]
    existing = context.state.session(session_id)
    if existing is not None and existing.state != "suspended":
        raise HttpError("state_conflict")
    stream = request.stream
    assert stream is not None
    root = sessions.manager(context).paths.sessions_root
    try:
        done = contexts.stage(root, session_id, stream, now=time.time())
    except FilesError as error:
        raise HttpError(error.code) from None
    return json_response(done)


ROUTES = [
    Route(
        "POST",
        "/v1/sessions/{id}/context/export",
        export_context,
        request_schema="guest/session-context-export.request.json",
        path_params={"id": "common.json#/$defs/uuid"},
    ),
    Route(
        "POST",
        "/v1/contexts/{id}/stage",
        stage_context,
        request_schema="guest/context-stage.request.json",
        body="raw",
        max_body=contexts.MAX_STAGE_BYTES,
        path_params={"id": "common.json#/$defs/uuid"},
    ),
]
