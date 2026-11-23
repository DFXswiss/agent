"""Command-line conventions shared by the doubles.

Every double accepts ``--host`` (default ``127.0.0.1``; the server binds only to this address), ``--port``
(default 0: a free port), ``--port-file`` (the chosen port is written there atomically, for orchestrators) and
``--journal`` (the JSON-lines journal file). It prints its base URL on the first line of standard output, serves
until SIGTERM or SIGINT, then prints its state as JSON on standard error and exits 0 when the state is ``ok``.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import Any, Protocol

STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)
EXIT_OK = 0
EXIT_NOT_OK = 1
EXIT_USAGE = 2


class Servable(Protocol):
    """What `serve_until_signal` needs from a double."""

    @property
    def url(self) -> str: ...

    def start(self) -> Any: ...

    def stop(self) -> None: ...

    def state(self) -> dict[str, Any]: ...


def add_server_arguments(parser: argparse.ArgumentParser) -> None:
    """Add ``--host``, ``--port``, ``--port-file`` and ``--journal``."""
    parser.add_argument("--host", default="127.0.0.1", help="address to bind to (only this one)")
    parser.add_argument("--port", type=int, default=0, help="port, 0 for a free one")
    parser.add_argument("--port-file", type=Path, help="write the chosen port to this file")
    parser.add_argument("--journal", type=Path, help="append the request journal (JSON lines) to this file")


def write_port_file(path: Path, port: int) -> None:
    """Write `port` to `path` atomically, so a reader never sees a partial number."""
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(f"{port}\n", encoding="utf-8")
    os.replace(temporary, path)


def serve_until_signal(double: Servable, port_file: Path | None, wait: Callable[[threading.Event], None]) -> int:
    """Start `double`, announce it, serve until a stop signal, print the state; the exit code of the state.

    `wait` blocks until the event is set (the CLI passes a polling wait, so signal handlers run promptly).
    """
    stop = threading.Event()

    def on_signal(signum: int, frame: FrameType | None) -> None:
        stop.set()

    previous = {sig: signal.signal(sig, on_signal) for sig in STOP_SIGNALS}
    try:
        double.start()
        if port_file is not None:
            write_port_file(port_file, int(double.url.rsplit(":", 1)[1]))
        print(double.url, flush=True)
        wait(stop)
    finally:
        double.stop()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    state = double.state()
    print(json.dumps(state, indent=2, sort_keys=True), file=sys.stderr)
    return EXIT_OK if state["ok"] else EXIT_NOT_OK


def poll_wait(stop: threading.Event) -> None:
    """Wait for `stop` in short steps, so a signal handler runs promptly on every platform."""
    while not stop.wait(0.1):
        pass
