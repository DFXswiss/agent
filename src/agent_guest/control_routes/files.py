"""File import and export of a session workspace on the control socket (03-api.md section 7.1; ``files.py``).

* ``POST /v1/sessions/{id}/files/import?dir=<relative dir>&replace=true|false`` - body ``application/x-tar`` with a
  ``Content-Length`` of at most 512 MiB (``413 size_exceeded`` before anything is read) -> ``{files, bytes}``
* ``POST /v1/sessions/{id}/files/export {dir, paths?}`` -> ``application/x-tar`` with ``X-Manifest-Sha256``

The import is the workspace scanner's baseline, never ``session.file`` records (``Capture.import_guard``, HOT-06).

Unknown session ``404 not_found``; stopped or failed session ``409 state_conflict``; the refusals of ``files.py``
with their codes (``path_invalid``, ``request_invalid``, ``size_exceeded``, ``state_conflict``, ``not_found``).
"""

from __future__ import annotations

from agent_guest import files
from agent_guest.control import ControlContext
from agent_guest.control_routes import capture
from agent_guest.routing import HttpError, Request, Response, Route, json_response
from agent_guest.state import INACTIVE_STATES, Session


def _session(context: ControlContext, session_id: str) -> Session:
    found = context.state.session(session_id)
    if found is None:
        raise HttpError("not_found")
    if found.state in INACTIVE_STATES:
        raise HttpError("state_conflict")
    return found


def import_files(context: ControlContext, request: Request) -> Response:
    session = _session(context, request.params["id"])
    stream = request.stream
    assert stream is not None
    try:
        done = files.import_tar(
            session,
            request.query["dir"],
            stream,
            replace=request.query["replace"] == "true",
            guard=capture.capture(context).import_guard(session),
        )
    except files.FilesError as error:
        raise HttpError(error.code) from None
    return json_response(done)


def export_files(context: ControlContext, request: Request) -> Response:
    session = _session(context, request.params["id"])
    try:
        exported = files.export_tar(session, request.data["dir"], request.data.get("paths"))
    except files.FilesError as error:
        raise HttpError(error.code) from None
    return Response(
        body=exported.stream, content_type="application/x-tar", headers={"X-Manifest-Sha256": exported.digest}
    )


ROUTES = [
    Route(
        "POST",
        "/v1/sessions/{id}/files/import",
        import_files,
        request_schema="guest/session-files-import.request.json",
        body="raw",
        max_body=files.MAX_IMPORT_BYTES,
        path_params={"id": "common.json#/$defs/uuid"},
    ),
    Route(
        "POST",
        "/v1/sessions/{id}/files/export",
        export_files,
        request_schema="guest/session-files-export.request.json",
        path_params={"id": "common.json#/$defs/uuid"},
    ),
]
