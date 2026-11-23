"""A local HTTP double of the two usage endpoints the runner polls (05-runner.md section 10.6):

- `GET /api/oauth/usage` (Anthropic OAuth usage): answers only a request with exactly `Authorization: Bearer
  <token>` and `oauth-2025-04-20` among the values of `anthropic-beta` (else `401`), like the real endpoint for a
  missing or wrong credential;
- `GET /api/v1/key` (OpenRouter key spend): answers only `Authorization: Bearer <token>`.

Scripted answers (`queue(path, status, body, headers)`) are served first, then the default document. Every request
is recorded with its headers (`requests`). The double never logs. It listens on loopback, on a port of the package
range when `WP_PORT_BASE` is set (`port(offset)`: base + 10 .. + 17), else on a free port.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

USAGE_PATH = "/api/oauth/usage"
KEY_PATH = "/api/v1/key"
OAUTH_BETA = "oauth-2025-04-20"

USAGE_DOCUMENT: dict[str, Any] = {
    "five_hour": {"utilization": 12.0, "resets_at": "2026-11-21T16:59:31.123456+00:00", "used_dollars": None},
    "seven_day": {"utilization": 3.5, "resets_at": "2026-11-26T09:00:00Z", "severity": "normal"},
    "seven_day_opus": None,
    "limits": [
        {"kind": "session", "percent": 12, "severity": "normal", "resets_at": None, "scope": None},
        {"kind": "weekly_all", "percent": 3.5, "severity": "normal", "resets_at": "2026-11-26T09:00:00Z"},
        {
            "kind": "weekly_scoped",
            "percent": 21,
            "severity": "warning",
            "resets_at": "2026-11-26T09:00:00Z",
            "scope": {"model": {"display_name": "Opus", "id": "claude-opus-5-5"}},
        },
    ],
    "extra_usage": {"is_enabled": False},
}
KEY_DOCUMENT: dict[str, Any] = {
    "data": {
        "label": "test key",
        "usage": 12.5,
        "usage_daily": 0.75,
        "usage_weekly": 3.25,
        "usage_monthly": 9.0,
        "limit": 50,
        "limit_remaining": 37.5,
        "limit_reset": "monthly",
        "is_free_tier": False,
    }
}


def port(offset: int) -> int:
    base = os.environ.get("WP_PORT_BASE")
    return int(base) + 10 + offset % 8 if base else 0


@dataclass
class Answer:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


class UsageDouble:
    def __init__(self, token: str, *, offset: int = 0) -> None:
        self.token = token
        self.scripted: dict[str, list[Answer]] = {USAGE_PATH: [], KEY_PATH: []}
        self.requests: list[tuple[str, dict[str, str]]] = []
        double = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                headers = {name.lower(): value for name, value in self.headers.items()}
                double.requests.append((self.path, headers))
                answer = double.answer(self.path, headers)
                self.send_response(answer.status)
                for name, value in {"Content-Type": "application/json", **answer.headers}.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(answer.body)))
                self.end_headers()
                self.wfile.write(answer.body)

            def log_message(self, format: str, *args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", port(offset)), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    def __enter__(self) -> UsageDouble:
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(10)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def queue(self, path: str, status: int, body: object = None, headers: Mapping[str, str] | None = None) -> None:
        raw = body if isinstance(body, bytes) else json.dumps(body if body is not None else {}).encode()
        self.scripted[path].append(Answer(status, raw, dict(headers or {})))

    def answer(self, path: str, headers: Mapping[str, str]) -> Answer:
        if path not in self.scripted:
            return Answer(404, b'{"error":"not_found"}')
        if headers.get("authorization") != f"Bearer {self.token}":
            return Answer(401, b'{"error":{"type":"authentication_error"}}')
        betas = [part.strip() for part in headers.get("anthropic-beta", "").split(",")]
        if path == USAGE_PATH and OAUTH_BETA not in betas:
            return Answer(401, b'{"error":{"type":"authentication_error"}}')
        if self.scripted[path]:
            return self.scripted[path].pop(0)
        document = USAGE_DOCUMENT if path == USAGE_PATH else KEY_DOCUMENT
        return Answer(200, json.dumps(document).encode())
