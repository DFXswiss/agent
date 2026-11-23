"""Host-side dispatcher of the attack suite: one model double per session behind the golden's test model port.

A TEST_MODE golden lets the guest API process reach exactly one port of the Lima host gateway (05-runner.md
section 4.3). Several sessions run in parallel, each against its own scripted adversary, so the dispatcher listens
on that port and forwards ``/d/<key>/<rest>`` to ``<double of key>/<rest>``; the session's credential names
``…/d/<key>`` as its upstream base URL. Requests for an unknown key are answered ``404`` and counted in
``unknown``, so a request that reached the host without a registered double is never silent.

Bodies are forwarded unchanged (the model double answers complete SSE streams); hop-by-hop headers are dropped and
the response gets a ``Content-Length``.
"""

from __future__ import annotations

import http.client
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Protocol
from urllib.parse import urlsplit

KEY_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
PATH_RE = re.compile(r"^/d/([A-Za-z0-9-]{1,64})(/.*)?$")
HOP_HEADERS = frozenset({"host", "connection", "content-length", "transfer-encoding", "keep-alive"})
FORWARD_TIMEOUT_S = 120.0


class Upstream(Protocol):
    @property
    def url(self) -> str: ...


class Dispatcher:
    """`register(key, double)` / `unregister(key)`; `start()` serves on ``host:port`` in a background thread."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._lock = threading.Lock()
        self._doubles: dict[str, Upstream] = {}
        self.unknown: list[str] = []
        self._server = ThreadingHTTPServer((host, port), _handler_for(self))
        self._server.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def register(self, key: str, double: Upstream) -> str:
        """Route ``/d/<key>`` to `double`; returns the path prefix."""
        if not KEY_RE.fullmatch(key):
            raise ValueError("invalid dispatcher key")
        with self._lock:
            self._doubles[key] = double
        return f"/d/{key}"

    def unregister(self, key: str) -> None:
        with self._lock:
            self._doubles.pop(key, None)

    def lookup(self, path: str) -> tuple[Upstream, str] | None:
        """The double and the forwarded path for a request path; None (and recorded) when unknown."""
        match = PATH_RE.fullmatch(path.split("?", 1)[0])
        with self._lock:
            double = self._doubles.get(match.group(1)) if match else None
            if double is None or match is None:
                self.unknown.append(path)
                return None
        query = path[len(path.split("?", 1)[0]) :]
        return double, (match.group(2) or "/") + query

    def start(self) -> Dispatcher:
        self._thread = threading.Thread(target=self._server.serve_forever, name="atk-dispatch", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _handler_for(dispatcher: Dispatcher) -> type[BaseHTTPRequestHandler]:
    class Handler(_Handler):
        owner = dispatcher

    return Handler


class _Handler(BaseHTTPRequestHandler):
    owner: Dispatcher
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        return None

    def do_GET(self) -> None:
        self._forward()

    def do_POST(self) -> None:
        self._forward()

    def _forward(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        found = self.owner.lookup(self.path)
        if found is None:
            self._answer(404, [("Content-Type", "text/plain")], b"no double for this path\n")
            return
        double, path = found
        target = urlsplit(double.url)
        headers = {name: value for name, value in self.headers.items() if name.lower() not in HOP_HEADERS}
        connection = http.client.HTTPConnection(target.hostname or "", target.port, timeout=FORWARD_TIMEOUT_S)
        try:
            connection.request(self.command, path, body=body, headers=headers)
            response = connection.getresponse()
            data = response.read()
            kept = [(name, value) for name, value in response.getheaders() if name.lower() not in HOP_HEADERS]
            self._answer(response.status, kept, data)
        except OSError:
            self._answer(502, [("Content-Type", "text/plain")], b"model double unreachable\n")
        finally:
            connection.close()

    def _answer(self, status: int, headers: list[tuple[str, str]], data: bytes) -> None:
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
