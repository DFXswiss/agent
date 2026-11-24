"""The main command `agent-hub` / `python -m agent_hub` (00-overview.md sections 5.1 and 7).

Without a subcommand, and with `serve`, it runs the hub with the configuration of 03-api.md section 2 (uvicorn on
`AGENT_HUB_HOST:AGENT_HUB_PORT`, lifespan required, no proxy headers trusted). Further subcommands are discovered:
every `agent_hub/<area>/cli.py` defines `register(subparsers)`, adds its parser and sets `handler(args) -> exit code`
(e.g. `agent-hub migrate` of the database area). Exit codes: 0 stopped, 2 configuration error (the message names the
variable, never its value), 3 the start failed (uvicorn's code, e.g. no database within the pool timeout). On SIGTERM
or SIGINT the hub shuts down in order and exits with 0: it first closes its live channels (every browser stream gets
`resync {hub_restart}` and ends, every device socket is closed with 1012; otherwise uvicorn would wait for the open
streams forever), then uvicorn finishes the open requests and runs the lifespan shutdown (jobs stopped, read log
written, pool closed). Unlike plain uvicorn, `HubServer` does not re-raise the signal after the orderly shutdown, so
supervisors see a clean exit instead of -15.

Idle connections (KOR-18): the server keeps an idle HTTP connection for `KEEP_ALIVE_S` (uvicorn's default is 5 s).
The side that closes an idle connection first must be the client, otherwise a request sent at that moment is lost.
The runner reuses a connection for at most 2 s, browsers repeat such a request themselves, and a reverse proxy in front
must reuse its upstream connections for less than `KEEP_ALIVE_S` (cloudflared's default is 90 s, nginx's 60 s; see
the hub operation runbook).
"""

from __future__ import annotations

import argparse
import contextlib
import os
import socket
import sys
from collections.abc import Callable, Generator, Sequence

import uvicorn
from fastapi import FastAPI

from agent_hub import __version__
from agent_hub.app import create_app
from agent_hub.config import ConfigError, HubConfig, load_config
from agent_hub.registry import CLI, callables
from agent_hub.stream.service import close_streams

EXIT_CONFIG = 2
EXIT_START_FAILED = 3
KEEP_ALIVE_S = 120
"""Seconds an idle connection is kept: well above every client's idle time (runner 2 s, proxies 60-90 s)."""
# Called with the server before it runs (embedding, tests that stop it through `should_exit`).
SERVER_OBSERVERS: list[Callable[[uvicorn.Server], object]] = []


class HubServer(uvicorn.Server):
    """uvicorn's server with the hub's shutdown order and a clean exit after a signal (module docstring)."""

    def __init__(self, config: uvicorn.Config, app: FastAPI) -> None:
        super().__init__(config)
        self.hub_app = app

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        await close_streams(self.hub_app)
        await super().shutdown(sockets)

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        with super().capture_signals():
            try:
                yield
            finally:
                self._captured_signals.clear()  # handled by the orderly shutdown: no re-raise


def build_server(config: HubConfig) -> uvicorn.Server:
    """The uvicorn server of the hub app."""
    app = create_app(config)
    settings = uvicorn.Config(
        app,
        host=config.bind_host,
        port=config.bind_port,
        lifespan="on",
        proxy_headers=False,
        server_header=False,
        timeout_keep_alive=KEEP_ALIVE_S,
        log_level="info",
    )
    return HubServer(settings, app)


def serve(config: HubConfig) -> int:
    """Run the hub until it is stopped; a failed start exits with `EXIT_START_FAILED` (raised by uvicorn)."""
    server = build_server(config)
    for observer in SERVER_OBSERVERS:
        observer(server)
    server.run()
    return 0


def serve_command(_args: argparse.Namespace) -> int:
    try:
        config = load_config(os.environ)
    except ConfigError as error:
        print(f"agent-hub: {error}", file=sys.stderr)
        return EXIT_CONFIG
    return serve(config)


def build_parser() -> argparse.ArgumentParser:
    """The parser with `serve` and every discovered subcommand."""
    parser = argparse.ArgumentParser(prog="agent-hub", description="agent.dfx.swiss platform hub")
    parser.add_argument("--version", action="version", version=f"agent-hub {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="command")
    serve_parser = subparsers.add_parser("serve", help="run the hub (the default without a command)")
    serve_parser.set_defaults(handler=serve_command)
    for _area, register in callables(CLI):
        register(subparsers)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the arguments and run the chosen command."""
    args = build_parser().parse_args(argv)
    handler: Callable[[argparse.Namespace], int] = getattr(args, "handler", serve_command)
    return int(handler(args))
