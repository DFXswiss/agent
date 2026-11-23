"""A small HTTP client for the doubles: their own routes and the control routes under ``/_double/``.

Used by tests and by the E2E orchestrator to switch scenarios, read the state and the journal and reset a double.
Plain ``http.client``, one connection per request, no proxy (a double is always reached directly).
"""

from __future__ import annotations

import http.client
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Response:
    """A complete response: status, headers (lower-case names) and the decoded body bytes."""

    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        """The body decoded as JSON."""
        return json.loads(self.body)


class DoubleClient:
    """Requests against one double at `base_url` (``http://host:port``)."""

    def __init__(self, base_url: str, *, timeout: float = 30.0) -> None:
        parts = urlsplit(base_url)
        if parts.scheme != "http" or parts.hostname is None or parts.port is None:
            raise ValueError(f"invalid double URL {base_url!r}")
        self.host = parts.hostname
        self.port = parts.port
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        body: bytes | Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        """Send one request; a non-bytes `body` other than None is sent as JSON."""
        data: bytes | None
        sent = dict(headers or {})
        if body is None or isinstance(body, bytes):
            data = body
        else:
            data = json.dumps(body).encode()
            sent.setdefault("content-type", "application/json")
        connection = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            connection.request(method, path, body=data, headers=sent)
            response = connection.getresponse()
            raw = response.read()
            return Response(response.status, {k.lower(): v for k, v in response.getheaders()}, raw)
        finally:
            connection.close()

    def state(self) -> dict[str, Any]:
        """``GET /_double/state``."""
        result: dict[str, Any] = self.request("GET", "/_double/state").json()
        return result

    def journal(self) -> list[dict[str, Any]]:
        """``GET /_double/journal``."""
        result: list[dict[str, Any]] = self.request("GET", "/_double/journal").json()
        return result

    def reset(self) -> Response:
        """``POST /_double/reset``."""
        return self.request("POST", "/_double/reset", {})

    def scenario(self, payload: Mapping[str, Any]) -> Response:
        """``POST /_double/scenario`` with a double-specific payload."""
        return self.request("POST", "/_double/scenario", dict(payload))
