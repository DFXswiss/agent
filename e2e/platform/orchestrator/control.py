"""Control API of a run for the specs (03-api.md / 07-testing.md sections 5.1 and 9), on `127.0.0.1:<control>`.

Routes are discovered (ADR-027 section 4): every module `orchestrator/routes/<name>.py` exports `ROUTES`, a list of
`Route(method, pattern, handler)`; `{name}` in a pattern matches one path segment. A handler gets the run context,
the path parameters and the JSON body and returns `(status, json)`. Errors are JSON `{"error": code, "detail"}`:
`not_found` (no route), `not_available` (501: the feature belongs to a later package), `invalid` (400), `failed`,
`unauthorized` (401: no or a wrong bearer token).
Two modules must not declare the same method and pattern.
"""

from __future__ import annotations

import hmac
import importlib
import json
import pkgutil
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

Handler = Callable[[Any, dict[str, str], Any], tuple[int, Any]]
MAX_BODY = 1024 * 1024
ROUTES_PACKAGE = "orchestrator.routes"


@dataclass(frozen=True)
class Route:
    method: str
    pattern: str
    handler: Handler

    def match(self, method: str, path: str) -> dict[str, str] | None:
        if method != self.method:
            return None
        regex = "^" + re.sub(r"\\\{([a-z_]+)\\\}", r"(?P<\1>[A-Za-z0-9._-]+)", re.escape(self.pattern)) + "$"
        found = re.match(regex, path)
        return found.groupdict() if found else None


class RouteError(Exception):
    """A handler refuses a request: `status`, `code` and a detail text."""

    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code


def not_available(feature: str, package: str) -> RouteError:
    return RouteError(501, "not_available", f"{feature} is not available yet (arrives with {package})")


def discover(package: str = ROUTES_PACKAGE) -> list[Route]:
    """All routes of `<package>/*.py`, sorted by module name; duplicates are refused."""
    module = importlib.import_module(package)
    routes: list[Route] = []
    seen: dict[tuple[str, str], str] = {}
    for info in sorted(pkgutil.iter_modules(module.__path__), key=lambda i: i.name):
        sub = importlib.import_module(f"{package}.{info.name}")
        declared = getattr(sub, "ROUTES", None)
        if not isinstance(declared, list) or not all(isinstance(r, Route) for r in declared):
            raise TypeError(f"{package}.{info.name}.ROUTES must be a list of Route")
        for route in declared:
            key = (route.method, route.pattern)
            if key in seen:
                raise ValueError(f"{route.method} {route.pattern} declared by {seen[key]} and {info.name}")
            seen[key] = info.name
            routes.append(route)
    return routes


def dispatch(routes: list[Route], context: Any, method: str, path: str, body: Any) -> tuple[int, Any]:
    for route in routes:
        params = route.match(method, path)
        if params is not None:
            try:
                return route.handler(context, params, body)
            except RouteError as exc:
                return exc.status, {"error": exc.code, "detail": str(exc)}
            except Exception as exc:
                return 500, {"error": "failed", "detail": f"{type(exc).__name__}: {exc}"}
    return 404, {"error": "not_found", "detail": f"no route {method} {path}"}


class ControlServer:
    """Every request needs `Authorization: Bearer <token>` (a run secret handed to Playwright): processes of the
    device or a page in the browser cannot drive the run or read the hub database without it."""

    def __init__(self, context: Any, port: int, token: str, routes: list[Route] | None = None) -> None:
        table = routes if routes is not None else discover()
        expected = f"Bearer {token}".encode()

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                given = (self.headers.get("Authorization") or "").encode()
                if not hmac.compare_digest(given, expected):
                    status, payload = 401, {"error": "unauthorized", "detail": "control token missing or wrong"}
                elif length > MAX_BODY:
                    status, payload = 413, {"error": "invalid", "detail": "body too large"}
                else:
                    raw = self.rfile.read(length) if length else b""
                    try:
                        body = json.loads(raw) if raw else None
                    except ValueError:
                        status, payload = 400, {"error": "invalid", "detail": "body is not JSON"}
                    else:
                        status, payload = dispatch(table, context, self.command, self.path.split("?", 1)[0], body)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _answer

        self.server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, name="control", daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        if self.thread.is_alive():
            self.server.shutdown()
        self.server.server_close()
