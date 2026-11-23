"""TCP relays: every connection to the hub and to a double passes one, so it is logged and can be cut.

A relay listens on one address and forwards each accepted connection to its target. `cut()` closes every open
connection and refuses (accepts and closes at once) new ones until `restore()`; the fault switches of 07-testing.md
section 9 ("cut/restore hub ↔ runner, hub ↔ browser, runner ↔ GitHub double") are cuts of the matching relay.
Each connection is reported to `log` as one record when it opens and one when it ends. The end record names the
direction whose copy stopped first (`first`: `up` = client to target, `down` = target to client) and why (`reason`:
`eof` when that side closed, else `recv:<error class>`, `send:<error class>` or `tap:<error class>`), so a
connection that ended before its answer shows which side ended it (KOR-16). The sockets of a connection are closed
once, by the connection's thread after both copies ended (`_wake`).

Tap (SCN-02, plaintext scan of 07-testing.md section 2.1 E2E-09/M1): with `tap=<directory>` every byte the target
sends back on a connection (the hub's answers and SSE frames to the browser) is also appended to
`<directory>/<n>.down`, one file per connection in accept order, and (SCN-09, the full check of E2E-09) every byte the
client sends (requests, WebSocket frames of a device) to `<directory>/<n>.up`; `checks/plaintext_scan.py` decodes them.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

Record = dict[str, Any]
BUFFER = 65536
CONNECT_TIMEOUT_S = 5.0
ACCEPT_POLL_S = 0.1
BROWSER_TAP = "browser-hub-tap"
RUNNER_TAP = "runner-hub-tap"
DIRECTIONS = ("up", "down")


class Relay:
    def __init__(
        self,
        name: str,
        listen: tuple[str, int],
        target: tuple[str, int],
        log: Callable[[Record], None],
        tap: str | None = None,
    ) -> None:
        self.name = name
        self.tap = Path(tap) if tap else None
        if self.tap is not None:
            self.tap.mkdir(parents=True, exist_ok=True)
        self.listen = listen
        self.target = target
        self.log = log
        self.cut_active = False
        self.connections = 0
        self._lock = threading.Lock()
        self._open: set[socket.socket] = set()
        self._stopped = threading.Event()
        self._server = socket.create_server(listen)
        self._server.settimeout(ACCEPT_POLL_S)
        self.port = int(self._server.getsockname()[1])
        self._thread = threading.Thread(target=self._accept, name=f"relay-{name}", daemon=True)
        self._thread.start()

    def _record(self, event: str, client: str, **extra: Any) -> None:
        target = f"{self.target[0]}:{self.target[1]}"
        self.log({"ts": time.time(), "link": self.name, "event": event, "client": client, "target": target, **extra})

    def _accept(self) -> None:
        while not self._stopped.is_set():
            try:
                conn, peer = self._server.accept()
            except TimeoutError:
                continue
            conn.settimeout(None)
            client = f"{peer[0]}:{peer[1]}"
            if self.cut_active:
                self._record("refused", client)
                conn.close()
                continue
            threading.Thread(target=self._serve, args=(conn, client), daemon=True).start()

    def _serve(self, conn: socket.socket, client: str) -> None:
        try:
            upstream = socket.create_connection(self.target, timeout=CONNECT_TIMEOUT_S)
        except OSError as exc:
            self._record("failed", client, error=type(exc).__name__)
            conn.close()
            return
        upstream.settimeout(None)
        with self._lock:
            self._open.update((conn, upstream))
            self.connections += 1
            number = self.connections
        self._record("open", client)
        counts = [0, 0]
        ended: list[tuple[str, str]] = []
        up = self.tap / f"{number:05d}.up" if self.tap is not None else None
        down = self.tap / f"{number:05d}.down" if self.tap is not None else None
        pumps = [
            threading.Thread(
                target=self._pump, args=(conn, upstream, counts, 0, ended), kwargs={"tap": up}, daemon=True
            ),
            threading.Thread(
                target=self._pump, args=(upstream, conn, counts, 1, ended), kwargs={"tap": down}, daemon=True
            ),
        ]
        for pump in pumps:
            pump.start()
        for pump in pumps:
            pump.join()
        with self._lock:
            self._open.difference_update((conn, upstream))
        # The only close of the two sockets: no copy and no cut can still act on them (see `_wake`).
        conn.close()
        upstream.close()
        first, reason = ended[0]
        self._record("close", client, bytes_up=counts[0], bytes_down=counts[1], first=first, reason=reason)

    def _pump(
        self,
        source: socket.socket,
        sink: socket.socket,
        counts: list[int],
        index: int,
        ended: list[tuple[str, str]],
        *,
        tap: Path | None = None,
    ) -> None:
        reason = "eof"
        copy = None
        try:
            copy = tap.open("ab") if tap is not None else None
            while True:
                try:
                    data = source.recv(BUFFER)
                except OSError as exc:
                    reason = f"recv:{type(exc).__name__}"
                    break
                if not data:
                    break
                if copy is not None:
                    copy.write(data)
                    copy.flush()
                try:
                    sink.sendall(data)
                except OSError as exc:
                    reason = f"send:{type(exc).__name__}"
                    break
                counts[index] += len(data)
        except OSError as exc:  # the tap file
            reason = f"tap:{type(exc).__name__}"
        finally:
            if copy is not None:
                copy.close()
        ended.append((DIRECTIONS[index], reason))
        for sock in (source, sink):
            _wake(sock)

    def cut(self) -> None:
        """End every open connection (its thread closes the sockets) and refuse new ones until `restore`."""
        self.cut_active = True
        with self._lock:
            for sock in self._open:
                _wake(sock)

    def restore(self) -> None:
        self.cut_active = False

    def stop(self) -> None:
        self._stopped.set()
        self._thread.join()
        self._server.close()
        self.cut()

    def state(self) -> Record:
        return {
            "listen": f"{self.listen[0]}:{self.port}",
            "target": f"{self.target[0]}:{self.target[1]}",
            "cut": self.cut_active,
            "connections": self.connections,
        }


def _wake(sock: socket.socket) -> None:
    """Shut both directions down, which ends the copies blocked on the socket; never close it here (KOR-16).

    A socket is closed exactly once, by its connection's thread after both copies ended. CPython reads a socket's
    descriptor number for `shutdown`/`recv`/`send` after it released the interpreter lock; had a copy (or a cut) closed
    the socket while the other copy was about to shut it down, that number could already belong to a connection
    accepted meanwhile by any relay of the process, which would end without its answer. While the connection's thread
    holds the socket open, its number cannot be reused, and `cut` shuts down only sockets still in `_open` (under the
    lock the connection's thread takes before closing them)."""
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)
