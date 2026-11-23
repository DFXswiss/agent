"""A line-oriented TCP server for the mail protocols, bound to exactly one address, with optional implicit TLS.

`LineServer` accepts connections in a background thread and runs one `Connection` per client in its own thread. With
a TLS context the socket is wrapped at accept time and the handshake runs in the connection thread, so a client that
never completes it cannot block the accept loop. `stop` closes the listening socket and every open connection.
"""

from __future__ import annotations

import contextlib
import socket
import socketserver
import ssl
import threading
from collections.abc import Callable
from typing import Any, Self

from agent_doubles.common.server import POLL_INTERVAL, validate_host

MAX_LINE = 64 * 1024
IDLE_TIMEOUT = 300.0


class LineTooLong(Exception):
    """A client line above `MAX_LINE` bytes."""


class Connection:
    """One client connection: buffered line and byte reads, writes; subclasses implement `serve`."""

    def __init__(self, sock: socket.socket, server: LineServer) -> None:
        self.sock = sock
        self.server = server
        self.reader = sock.makefile("rb")

    def readline(self) -> bytes:
        """One line including its end (empty at end of stream); raises LineTooLong."""
        line = self.reader.readline(MAX_LINE + 1)
        if len(line) > MAX_LINE:
            raise LineTooLong()
        return line

    def read_exact(self, size: int) -> bytes:
        """Exactly `size` bytes, or fewer at end of stream."""
        return self.reader.read(size)

    def write(self, data: bytes) -> None:
        self.sock.sendall(data)

    def serve(self) -> None:
        raise NotImplementedError


class _TCPServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    block_on_close = False


class _TCPServer6(_TCPServer):
    address_family = socket.AF_INET6


class LineServer:
    """A server for one protocol at `host`:`port` (0 picks a free port); `factory` builds the connection object."""

    def __init__(
        self,
        factory: Callable[[socket.socket, LineServer], Connection],
        host: str,
        port: int,
        *,
        tls: ssl.SSLContext | None = None,
    ) -> None:
        if not 0 <= port <= 65535:
            raise ValueError(f"invalid port {port}")
        self.factory = factory
        self.tls = tls
        self._open: set[socket.socket] = set()
        self._lock = threading.Lock()
        owner = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                owner._handle(self.request)

        server_class = _TCPServer6 if ":" in validate_host(host) else _TCPServer
        self._server = server_class((host, port), Handler)
        self._thread: threading.Thread | None = None

    @property
    def host(self) -> str:
        return str(self._server.server_address[0])

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def start(self) -> Self:
        self._thread = threading.Thread(target=self._server.serve_forever, args=(POLL_INTERVAL,), daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """Stop accepting, close every open connection and the listening socket."""
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join()
            self._thread = None
        with self._lock:
            sockets = list(self._open)
        for sock in sockets:
            _shutdown(sock)
        self._server.server_close()

    def _handle(self, raw: socket.socket) -> None:
        raw.settimeout(IDLE_TIMEOUT)
        sock: Any = raw
        with self._lock:
            self._open.add(raw)
        try:
            if self.tls is not None:
                sock = self.tls.wrap_socket(raw, server_side=True)
            self.factory(sock, self).serve()
        except (OSError, LineTooLong):
            # A broken connection, a failed handshake, an idle timeout or an overlong line ends this client only.
            pass
        finally:
            with self._lock:
                self._open.discard(raw)
            _shutdown(sock)


def _shutdown(sock: socket.socket) -> None:
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)
    sock.close()
