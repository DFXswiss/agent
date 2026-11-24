"""Security headers on every HTTP response (03-api.md section 1, 06-web.md section 10.2, ADR-019).

The outermost hub middleware: it sets the headers on every response (API, errors, static files, the SPA page),
replacing any value a route set, adds `Cache-Control: no-store` to API responses that set no caching, and answers
`503 unavailable` (never exception text) when anything below fails before a response started.
"""

from __future__ import annotations

import logging

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agent_hub.errors import INTERNAL_ERROR, error_bytes

LOG = logging.getLogger("agent_hub.http")
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
    "font-src 'self'; base-uri 'none'; form-action 'self' http://127.0.0.1:*; frame-ancestors 'none'"
)
SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"content-security-policy", CSP.encode("ascii")),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"permissions-policy", b"publickey-credentials-get=(self), publickey-credentials-create=(self)"),
)
_NAMES = frozenset(name for name, _ in SECURITY_HEADERS)
API_PREFIX = "/v1/"


def json_response_messages(status: int, body: bytes, extra: list[tuple[bytes, bytes]]) -> list[Message]:
    """The two ASGI messages of a complete JSON response."""
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
        *extra,
    ]
    return [
        {"type": "http.response.start", "status": status, "headers": headers},
        {"type": "http.response.body", "body": body},
    ]


class SecurityHeaders:
    """ASGI middleware that adds the security headers to every HTTP response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        api = scope["path"].startswith(API_PREFIX)
        started = False

        async def send_with_headers(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = [(name, value) for name, value in message.get("headers", []) if name.lower() not in _NAMES]
                if api and not any(name.lower() == b"cache-control" for name, _ in headers):
                    headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": [*headers, *SECURITY_HEADERS]}
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        except Exception:
            LOG.error("request %s %s failed before a response", scope.get("method"), scope["path"], exc_info=True)
            if started:
                return
            body = b"" if scope.get("method") == "HEAD" else error_bytes(INTERNAL_ERROR)
            for message in json_response_messages(503, body, []):
                await send_with_headers(message)
