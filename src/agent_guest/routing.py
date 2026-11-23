"""HTTP/1.1 plumbing shared by the control socket and the API port (03-api.md sections 1 and 7, 05-runner.md §5.1).

Routes are data (``Route``) contributed by the discovered modules ``control_routes/*`` and ``api_routes/*``
(ADR-027 section 4); ``Router`` matches them and ``Handler`` serves them with ``http.server``:

* the request target is split into path and query; a repeated query parameter is refused
* an optional ``authorize(headers) -> bool`` runs before routing (control socket: bearer control token), so an
  unauthenticated caller learns nothing about the routes (``401 unauthorized``)
* path parameters are checked against their schema (``common.json#/$defs/uuid``); a mismatch is ``404 not_found``
* bodies need ``Content-Length`` (no chunked requests), at most ``Route.max_body`` bytes (``413 size_exceeded``);
  JSON bodies are parsed strictly (UTF-8, no duplicate keys, no ``NaN``) and, like query parameters, validated
  against ``Route.request_schema`` (``400 request_invalid`` with ``detail.pointers``)
* ``Route.body == "raw"`` hands the handler a bounded reader instead (tar streams, model requests)
* handlers return a ``Response``; raising ``HttpError`` produces the error body ``{"error": code, "detail"?}``; any
  other exception is logged to stderr and answered ``503 unavailable`` without exception text
* ``handler_timeout`` (API process: 10 s) bounds a handler; after it the client gets ``503 unavailable``
"""

from __future__ import annotations

import json
import re
import sys
import threading
import traceback
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from email.message import Message
from http.server import BaseHTTPRequestHandler
from typing import Any, BinaryIO, Literal, cast
from urllib.parse import parse_qsl, urlsplit

from agent_guest.schema import SchemaSet, pointers

MAX_JSON_BODY = 1024 * 1024
_LENGTH_RE = re.compile(r"[0-9]{1,16}")
CHUNK = 64 * 1024
STATUS_BY_CODE = {
    "request_invalid": 400,
    "path_invalid": 400,
    "unauthorized": 401,
    "not_found": 404,
    "already_assigned": 409,
    "not_assigned": 409,
    "id_conflict": 409,
    "state_conflict": 409,
    "size_exceeded": 413,
    "rate_limited": 429,
    "sandbox_failed": 500,
    "unavailable": 503,
}


class HttpError(Exception):
    def __init__(self, code: str, status: int | None = None, detail: Sequence[str] = ()) -> None:
        super().__init__(code)
        self.code = code
        self.status = status if status is not None else STATUS_BY_CODE[code]
        self.detail = list(detail)


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, str]
    headers: Message
    params: dict[str, str] = field(default_factory=dict)
    data: Any = None
    stream: BodyReader | None = None
    peer: tuple[str, int] | None = None
    """Client address of a TCP connection (``None`` on the unix sockets)."""
    server: tuple[str, int] | None = None
    """The server's own TCP address (``None`` on the unix sockets)."""


@dataclass
class Response:
    status: int = 200
    body: bytes | Iterable[bytes] = b""
    content_type: str = "application/json"
    headers: dict[str, str] = field(default_factory=dict)


def json_response(value: object, status: int = 200) -> Response:
    text = json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return Response(status=status, body=text.encode("utf-8"))


def error_response(error: HttpError) -> Response:
    body: dict[str, Any] = {"error": error.code}
    if error.detail:
        body["detail"] = {"pointers": error.detail[:32]}
    return json_response(body, error.status)


def rate_limited(retry_after: int) -> Response:
    """``429 {"error":"rate_limited"}`` with ``Retry-After`` (too many failed token checks, ``authguard.py``)."""
    response = error_response(HttpError("rate_limited"))
    response.headers["Retry-After"] = str(retry_after)
    return response


RouteHandler = Callable[[Any, Request], Response]


@dataclass(frozen=True)
class Route:
    """One endpoint. ``path`` may contain ``{name}`` segments and end with ``/*`` (prefix route, rest in ``*``)."""

    method: str
    path: str
    handler: RouteHandler
    request_schema: str | None = None
    body: Literal["json", "none", "raw"] = "json"
    max_body: int = MAX_JSON_BODY
    path_params: Mapping[str, str] = field(default_factory=dict)
    timeout: float | None = None


class BodyReader:
    """Reads at most ``length`` bytes of a request body; unread rest is drained or the connection closed."""

    def __init__(self, stream: BinaryIO, length: int) -> None:
        self._stream = stream
        self.remaining = length

    def read(self, size: int = -1) -> bytes:
        if size < 0 or size > self.remaining:
            size = self.remaining
        data = self._stream.read(size) if size else b""
        self.remaining -= len(data)
        if size and not data:
            self.remaining = 0
            raise HttpError("request_invalid")
        return data


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _no_constant(name: str) -> object:
    raise ValueError(name)


def parse_json(data: bytes) -> Any:
    """Strict JSON: UTF-8, no duplicate keys, no ``NaN``/``Infinity``, bounded nesting; raises ``ValueError``."""
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
    except RecursionError:
        raise ValueError("nesting too deep") from None


class Router:
    def __init__(self, routes: Iterable[Route]) -> None:
        self._routes: list[tuple[Route, re.Pattern[str]]] = []
        seen: set[tuple[str, str]] = set()
        for route in routes:
            key = (route.method, route.path)
            if key in seen:
                raise ValueError(f"duplicate route {route.method} {route.path}")
            seen.add(key)
            self._routes.append((route, compile_path(route.path)))

    @property
    def routes(self) -> list[Route]:
        return [route for route, _ in self._routes]

    def match(self, method: str, path: str) -> tuple[Route, dict[str, str]] | None:
        for route, pattern in self._routes:
            found = pattern.fullmatch(path)
            if found and route.method == method:
                return route, found.groupdict()
        return None


def _tcp(address: object) -> tuple[str, int] | None:
    """A TCP address ``(host, port)`` as a plain pair; anything else (a unix socket address) is ``None``."""
    if isinstance(address, tuple) and len(address) >= 2 and isinstance(address[0], str) and type(address[1]) is int:
        return address[0], address[1]
    return None


def compile_path(path: str) -> re.Pattern[str]:
    prefix = path.endswith("/*")
    parts = (path[:-2] if prefix else path).split("/")
    regex = "/".join(
        f"(?P<{part[1:-1]}>[^/]+)" if part.startswith("{") and part.endswith("}") else re.escape(part) for part in parts
    )
    if prefix:
        regex += "/(?P<rest>.*)"
    return re.compile(regex)


class Handler(BaseHTTPRequestHandler):
    """Serves ``server.router`` with ``server.context``; subclasses set ``timeout`` (socket read timeout)."""

    protocol_version = "HTTP/1.1"
    server_version = "agent-guest"
    sys_version = ""
    error_content_type = "application/json"
    error_message_format = '{"error":"%(explain)s"}'
    timeout = 60

    def log_message(self, format: str, *args: Any) -> None:
        """Request lines are not logged (they could name session paths); errors go through ``log_error``."""

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """Errors of ``http.server`` itself (bad request line, unknown method) as ``{"error": code}`` bodies."""
        name = "not_found" if code in (404, 405, 501) else "request_invalid"
        if code in (405, 501):
            code = 404
        super().send_error(code, None, name)

    def version_string(self) -> str:
        return self.server_version

    def log_error(self, format: str, *args: Any) -> None:
        sys.stderr.write(f"{self.server_version}: {format % args}\n")

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()

    def do_DELETE(self) -> None:
        self._dispatch()

    def do_HEAD(self) -> None:
        """Routes declare ``HEAD`` themselves (model gateway ``/model/api/hello``); the body is never sent."""
        self._dispatch()

    _reader: BodyReader | None = None

    def _dispatch(self) -> None:
        self._reset()
        try:
            request, route = self._prepare()
            response = self._run(route, request)
        except HttpError as error:
            response = error_response(error)
            if self._reader is None:
                # The body (if any) was not read: never parse its bytes as the next request.
                self.close_connection = True
        if self._reader is not None and self._reader.remaining:
            self.close_connection = True
        self._send(response)

    def _reset(self) -> None:
        self._reader = None

    def _prepare(self) -> tuple[Request, Route]:
        server: Any = self.server
        authorize: Callable[[Message], bool] | None = getattr(server, "authorize", None)
        if authorize is not None and not authorize(self.headers):
            raise HttpError("unauthorized")
        target = urlsplit(self.path)
        if not self.path.startswith("/"):
            raise HttpError("not_found")
        try:
            pairs = parse_qsl(target.query, keep_blank_values=True, strict_parsing=bool(target.query))
            query = _no_duplicates(pairs)
        except ValueError:
            raise HttpError("request_invalid") from None
        found = server.router.match(self.command, target.path)
        if found is None:
            raise HttpError("not_found")
        route, params = found
        schemas: SchemaSet = server.schemas
        for name, reference in route.path_params.items():
            if schemas.errors(reference, params[name]):
                raise HttpError("not_found")
        request = Request(method=self.command, path=target.path, query=query, headers=self.headers, params=params)
        request.peer, request.server = _tcp(self.client_address), _tcp(server.server_address)
        self._body(route, request)
        if route.request_schema is not None:
            instance = request.data if route.body == "json" else query
            violations = schemas.errors(route.request_schema, instance)
            if violations:
                raise HttpError("request_invalid", detail=pointers(violations))
        return request, route

    def _body(self, route: Route, request: Request) -> None:
        if "Transfer-Encoding" in self.headers:
            self.close_connection = True
            raise HttpError("request_invalid")
        values = self.headers.get_all("Content-Length") or (["0"] if route.body == "none" else [])
        text = values[0] if len(values) == 1 else ""
        if not _LENGTH_RE.fullmatch(text):
            self.close_connection = True
            raise HttpError("request_invalid")
        length = int(text)
        if route.body == "none":
            if length:
                self.close_connection = True
                raise HttpError("request_invalid")
            return
        if length > route.max_body:
            self.close_connection = True
            raise HttpError("size_exceeded")
        self._reader = BodyReader(cast(BinaryIO, self.rfile), length)
        if route.body == "raw":
            request.stream = self._reader
            return
        data = self._reader.read()
        try:
            request.data = parse_json(data)
        except ValueError:
            raise HttpError("request_invalid") from None

    def _run(self, route: Route, request: Request) -> Response:
        server: Any = self.server
        timeout: float | None = route.timeout if route.timeout is not None else getattr(server, "handler_timeout", None)
        if timeout is None:
            return self._call(route, request)
        outcome: list[Response] = []

        def work() -> None:
            outcome.append(self._call(route, request))

        # A server with a handler limit also bounds the handler threads (``handler_slots``).
        slots: threading.BoundedSemaphore = server.handler_slots
        if not slots.acquire(blocking=False):
            raise HttpError("unavailable")

        def guarded() -> None:
            try:
                work()
            finally:
                slots.release()

        worker = threading.Thread(target=guarded, name="agent-guest-handler", daemon=True)
        worker.start()
        worker.join(timeout)
        if not outcome:
            # The handler keeps running in the background; its result is discarded.
            self.close_connection = True
            raise HttpError("unavailable")
        return outcome[0]

    def _call(self, route: Route, request: Request) -> Response:
        server: Any = self.server
        try:
            return route.handler(server.context, request)
        except HttpError as error:
            return error_response(error)
        except Exception:
            sys.stderr.write(f"{self.server_version}: handler {route.method} {route.path} failed\n")
            traceback.print_exc(file=sys.stderr)
            return error_response(HttpError("unavailable"))

    def _send(self, response: Response) -> None:
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Cache-Control", "no-store")
        for name, value in response.headers.items():
            self.send_header(name, value)
        if isinstance(response.body, bytes):
            self.send_header("Content-Length", str(len(response.body)))
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(response.body)
            return
        self.send_header("Transfer-Encoding", "chunked")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        for chunk in response.body:
            if chunk:
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
        self.wfile.write(b"0\r\n\r\n")
