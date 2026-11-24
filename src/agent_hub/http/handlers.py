"""Exception handlers of the hub app: every error leaves as `{"error": "<code>"}` (03-api.md sections 1 and 11).

`HubError` keeps its code, status, detail and headers. Request validation failures become `400 request_invalid` with
the JSON pointers of the offending fields (`detail.pointers`, never values). Unknown routes and methods become `404
not_found`, other HTTP exceptions `400 request_invalid` or `503 unavailable`. Unexpected exceptions are answered by
the request pipeline (`503 unavailable`).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException

from agent_hub.errors import INTERNAL_ERROR, HubError, error_body

MAX_POINTERS = 64
MAX_POINTER_LENGTH = 256


def _escape(part: object) -> str:
    return str(part).replace("~", "~0").replace("/", "~1")


def pointers(errors: Sequence[Any]) -> list[str]:
    """JSON pointers (RFC 6901) of validation errors below the request part (`body`, `query`, `path`)."""
    found: list[str] = []
    for error in errors:
        if not isinstance(error, dict) or error.get("type") == "json_invalid":
            continue
        location = tuple(error.get("loc", ()))[1:]
        if error.get("type") == "extra_forbidden":
            # The name of an unknown field is request text: point at the object that has it.
            location = location[:-1]
        pointer = "".join("/" + _escape(part) for part in location)
        if len(pointer) <= MAX_POINTER_LENGTH and pointer not in found:
            found.append(pointer)
    return found[:MAX_POINTERS]


async def hub_error(_request: Request, exception: Exception) -> Response:
    error = cast(HubError, exception)
    return JSONResponse(error_body(error.code, error.detail), status_code=error.status, headers=error.headers)


async def validation_error(_request: Request, exception: Exception) -> Response:
    found = pointers(cast(RequestValidationError, exception).errors())
    return JSONResponse(error_body("request_invalid", {"pointers": found} if found else None), status_code=400)


async def http_error(_request: Request, exception: Exception) -> Response:
    status = cast(HTTPException, exception).status_code
    if status in (404, 405):
        return JSONResponse(error_body("not_found"), status_code=404)
    if status < 500:
        return JSONResponse(error_body("request_invalid"), status_code=400)
    return JSONResponse(error_body(INTERNAL_ERROR), status_code=503)


def install(app: FastAPI) -> None:
    """Register the handlers on the app."""
    app.add_exception_handler(HubError, hub_error)
    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(HTTPException, http_error)
