"""Control routes (``/_double/…``) of the mail and auth-page doubles, in the style of the other doubles.

A route table maps ``(method, path)`` to an action that receives the decoded JSON body (``{}`` for none) and the query
parameters and returns ``(status, payload)``; a `Raw` payload is sent as it is, anything else as JSON. Unknown paths
are ``404``, a known path with another method ``405`` (with ``Allow``), an unreadable or non-JSON body ``400`` and a
`ControlError` raised by the action its status with ``{"error": text}``. Control requests are neither journaled nor
counted.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from agent_doubles.common.server import BodyError, DoubleHandler

Action = Callable[[Any, dict[str, str]], tuple[int, Any]]


class ControlError(Exception):
    """An invalid control request: status and message."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Raw:
    """A non-JSON control answer."""

    content_type: str
    data: bytes


def body_object(body: Any) -> dict[str, Any]:
    """`body` when it is a JSON object, else ControlError."""
    if not isinstance(body, dict):
        raise ControlError("body must be an object")
    return body


def handle_control(
    handler: DoubleHandler,
    routes: Mapping[tuple[str, str], Action],
    path: str,
    *,
    query: dict[str, str],
    raw: bytes,
    problem: BodyError | None,
) -> None:
    """Answer one control request through `routes`."""
    allowed = sorted(method for method, route in routes if route == path)
    if not allowed:
        handler.send_json(404, {"error": "unknown control route"})
        return
    if handler.command not in allowed:
        handler.send_json(405, {"error": "method not allowed"}, (("allow", ", ".join(allowed)),))
        return
    if problem is not None:
        handler.send_json(problem.status, {"error": problem.message})
        return
    try:
        body = json.loads(raw) if raw else {}
    except ValueError:
        handler.send_json(400, {"error": "body is not JSON"})
        return
    try:
        status, payload = routes[(handler.command, path)](body, query)
    except ControlError as exc:
        handler.send_json(exc.status, {"error": exc.message})
        return
    if isinstance(payload, Raw):
        handler.send_body(status, payload.data, payload.content_type)
    else:
        handler.send_json(status, payload)
