"""HTTP plumbing shared by the doubles: a server bound to exactly one address and a base request handler.

`DoubleServer` binds only to the address it is given (no wildcard default; ``127.0.0.1`` is the usual choice and
the E2E orchestrator passes the Lima host address explicitly), serves in a background thread or in the calling
thread and stops cleanly. `DoubleHandler` reads bodies within a size limit and writes JSON, plain and chunked
responses with HTTP/1.1 keep-alive.
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Self

MAX_BODY = 64 * 1024 * 1024
# Seconds between shutdown checks of the serving loop (the standard library default of 0.5 s slows test teardown).
POLL_INTERVAL = 0.05


class BodyError(Exception):
    """A request body the double refuses to read: the HTTP status and the reason."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", "[::]", "*"})


def validate_host(host: str) -> str:
    """`host` when it names one address to bind to; empty, blank and wildcard hosts (all interfaces) are refused."""
    if not host or host != host.strip() or any(c.isspace() for c in host) or host in WILDCARD_HOSTS:
        raise ValueError(f"invalid bind address {host!r}")
    return host


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _Server6(_Server):
    address_family = socket.AF_INET6


class DoubleServer:
    """An HTTP server for one double, bound to `host`:`port` (port 0 picks a free port)."""

    def __init__(self, handler: type[BaseHTTPRequestHandler], host: str, port: int) -> None:
        if not 0 <= port <= 65535:
            raise ValueError(f"invalid port {port}")
        server = _Server6 if ":" in validate_host(host) else _Server
        self._httpd = server((host, port), handler)
        self._thread: threading.Thread | None = None
        self._serving = False

    @property
    def host(self) -> str:
        """The address the server is bound to."""
        return str(self._httpd.server_address[0])

    @property
    def port(self) -> int:
        """The bound port (the chosen one when 0 was given)."""
        return int(self._httpd.server_address[1])

    @property
    def url(self) -> str:
        """The base URL of the server (IPv6 addresses in brackets)."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    def start(self) -> Self:
        """Serve in a background thread."""
        self._serving = True
        serve = self._httpd.serve_forever
        self._thread = threading.Thread(target=serve, args=(POLL_INTERVAL,), name="double", daemon=True)
        self._thread.start()
        return self

    def serve_forever(self) -> None:
        """Serve in the calling thread until `stop` is called from another thread."""
        self._serving = True
        self._httpd.serve_forever(POLL_INTERVAL)

    def stop(self) -> None:
        """Stop serving (a shutdown requested before the loop runs ends it at once) and close the socket."""
        if self._serving:
            self._httpd.shutdown()
            self._serving = False
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        self._httpd.server_close()

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


class DoubleHandler(BaseHTTPRequestHandler):
    """Base request handler: quiet, HTTP/1.1, bounded bodies, JSON and chunked responses."""

    protocol_version = "HTTP/1.1"
    server_version = "agent-double/1"

    def log_message(self, format: str, *args: Any) -> None:
        """Requests are journaled by the double, not logged to standard error."""

    def read_body(self) -> bytes:
        """The request body; raises BodyError for a chunked body, an invalid length or a body above `MAX_BODY`."""
        if self.headers.get("transfer-encoding") is not None:
            self.close_connection = True
            raise BodyError(411, "request body without content-length")
        raw = self.headers.get("content-length")
        if raw is None:
            return b""
        if not raw.isdigit():
            self.close_connection = True
            raise BodyError(400, "invalid content-length")
        length = int(raw)
        if length > MAX_BODY:
            self.close_connection = True
            raise BodyError(413, f"request body above {MAX_BODY} bytes")
        return self.rfile.read(length)

    def send_body(self, status: int, data: bytes, content_type: str, headers: Iterable[tuple[str, str]] = ()) -> None:
        """A complete response; HEAD gets the headers only."""
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(data)))
        for name, value in headers:
            self.send_header(name, value)
        if self.close_connection:
            self.send_header("connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def send_json(self, status: int, payload: Any, headers: Iterable[tuple[str, str]] = ()) -> None:
        """A JSON response (compact separators, as the real services send)."""
        data = json.dumps(payload, separators=(",", ":")).encode()
        self.send_body(status, data, "application/json", headers)

    def start_chunked(self, status: int, content_type: str, headers: Iterable[tuple[str, str]] = ()) -> None:
        """The head of a chunked response without content length."""
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("transfer-encoding", "chunked")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()

    def write_chunk(self, data: bytes) -> None:
        """One chunk, flushed at once so the client sees it immediately."""
        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()

    def end_chunked(self) -> None:
        """The terminating chunk."""
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()
