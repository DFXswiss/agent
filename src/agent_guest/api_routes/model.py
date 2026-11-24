"""The model gateway routes on port 7700 (05-runner.md §10.2); the logic is in ``gateway.py``.

* ``POST /model/v1/messages`` (Claude Code adds ``?beta=true``) - forwarded, ``usage`` journaled
* ``POST /model/v1/messages/count_tokens`` - forwarded, no ``usage``
* ``HEAD|GET /model/api/hello`` - ``200 {}`` locally, no token (connectivity probe of Claude Code)
* every other method and path under ``/model`` - ``404 not_found_error`` and ``guest.error gateway_route``
* control process unreachable - ``503 api_error``

Request bodies are read whole (the model must be checked before anything is forwarded), at most ``MAX_BODY``. A
handler may run ``TIMEOUT`` seconds until the upstream's response headers arrive (a non-streaming request waits for
the whole answer); the streamed body itself is not bounded by it.
"""

from __future__ import annotations

from collections.abc import Callable

from agent_guest.apisock import ApiSockError
from agent_guest.fault import guard_suppressed
from agent_guest.gateway import Gateway, anthropic_error, gateway_for
from agent_guest.routing import Request, Response, Route, json_response
from agent_guest.server import ApiContext

MAX_BODY = 32 * 1024 * 1024
TIMEOUT = 610.0
METHODS = ("GET", "POST", "PUT", "DELETE", "HEAD")
BODY_METHODS = frozenset({"POST", "PUT"})


def _guarded(work: Callable[[], Response]) -> Response:
    """The control process unreachable: ``503`` in the Anthropic shape (Claude Code retries it)."""
    try:
        return work()
    except ApiSockError:
        return anthropic_error(503, "api_error", "The gateway is not available right now.")


def _gateway(context: ApiContext) -> Gateway:
    return gateway_for(context.control, context.config.peer_table, fail_open=guard_suppressed(context.config))


def messages(context: ApiContext, request: Request) -> Response:
    gateway = _gateway(context)
    return _guarded(lambda: gateway.forward(request, "/v1/messages", record_usage=True))


def count_tokens(context: ApiContext, request: Request) -> Response:
    gateway = _gateway(context)
    return _guarded(lambda: gateway.forward(request, "/v1/messages/count_tokens", record_usage=False))


def hello(context: ApiContext, request: Request) -> Response:
    return json_response({})


def unknown(context: ApiContext, request: Request) -> Response:
    gateway = _gateway(context)
    return _guarded(lambda: gateway.unknown(request))


ROUTES = [
    Route("POST", "/model/v1/messages", messages, body="raw", max_body=MAX_BODY, timeout=TIMEOUT),
    Route("POST", "/model/v1/messages/count_tokens", count_tokens, body="raw", max_body=MAX_BODY, timeout=TIMEOUT),
    Route("GET", "/model/api/hello", hello, body="none"),
    Route("HEAD", "/model/api/hello", hello, body="none"),
    *(
        Route(method, path, unknown, body="raw" if method in BODY_METHODS else "none", max_body=MAX_BODY)
        for method in METHODS
        for path in ("/model", "/model/*")
    ),
]
