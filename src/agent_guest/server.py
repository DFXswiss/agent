"""``agent-guest-api`` (user ``platform``): HTTP on ``127.0.0.1:7700`` for the sessions (05-runner.md section 5.1).

Started by ``agent-guest-api.service`` as ``python3 -m agent_guest.server``. Serves the discovered
``api_routes/*:ROUTES`` (``/mcp``, ``/hooks/<Event>``, ``/model/...`` are added by later packages; this package has
``GET /v1/health``); unknown paths answer ``404 {"error":"not_found"}``. Every handler is bounded by
``HANDLER_TIMEOUT`` (10 s, below the 15 s hook timeout of Claude Code) unless the route sets its own ``timeout``
(MCP waits of 03-api.md section 12: 20 s / 30 s) and answers ``503 unavailable`` after it. At most
``MAX_CONNECTIONS`` connections and ``MAX_HANDLERS`` running handlers (including abandoned ones) exist at a time, so
one session cannot exhaust the process shared by all sessions of the VM.

The process has no write path: it never opens the journal or any file for writing. Records, pending calls and
session lookups go to the control process over ``api.sock`` (``ApiContext.control``, an ``apisock.ApiClient``).
This module must not import ``journal``, ``state`` or ``sqlite3`` (checked by a test).
"""

from __future__ import annotations

import signal
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from types import FrameType
from typing import Any

from agent_guest import registry
from agent_guest.apisock import ApiClient
from agent_guest.config import ConfigError, GuestConfig, load_config
from agent_guest.routing import Handler, Router
from agent_guest.schema import SchemaSet

HANDLER_TIMEOUT = 10.0
MAX_CONNECTIONS = 256
MAX_HANDLERS = 64


@dataclass
class ApiContext:
    """Handler context of every API route."""

    config: GuestConfig
    schemas: SchemaSet
    control: ApiClient


class ApiHandler(Handler):
    server_version = "agent-guest-api"
    timeout = 15


class ApiHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128
    handler_timeout = HANDLER_TIMEOUT

    def __init__(self, config: GuestConfig, router: Router, context: ApiContext) -> None:
        self.router = router
        self.context = context
        self.schemas = context.schemas
        self.handler_slots = threading.BoundedSemaphore(MAX_HANDLERS)
        self.connection_slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__((config.api_host, config.api_port), ApiHandler)

    def process_request(self, request: Any, client_address: Any) -> None:
        """At most ``MAX_CONNECTIONS`` open connections; more are closed at once (one session cannot starve others)."""
        if not self.connection_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connection_slots.release()


def create_server(config: GuestConfig) -> ApiHttpServer:
    schemas = SchemaSet(config.contracts)
    context = ApiContext(config=config, schemas=schemas, control=ApiClient(config.api_socket))
    return ApiHttpServer(config, Router(registry.api_routes()), context)


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    if argv:
        sys.stderr.write("usage: python3 -m agent_guest.server (configuration through AGENT_GUEST_*)\n")
        return 2
    try:
        server = create_server(load_config(environ))
    except (ConfigError, registry.RegistryError) as error:
        sys.stderr.write(f"agent-guest-api: {error}\n")
        return 1
    except OSError as error:
        sys.stderr.write(f"agent-guest-api: os_error {error.errno}\n")
        return 1

    def on_signal(signum: int, frame: FrameType | None) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    previous = {sig: signal.signal(sig, on_signal) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        server.serve_forever(0.05)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        server.server_close()
        server.context.control.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
