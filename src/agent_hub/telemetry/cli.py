"""CLI of the telemetry area (ADR-027 section 4): `agent-hub ui-health [--days N]`.

The operator's view of the anonymous UI health counters (ADR-086 section 5, 08-monitoring.md section 6.1) without a
browser session: the rows of the last N days (1-90, default 7) as `GET /v1/platform/ui-health` returns them, printed
as one JSON line on stdout. It reads the hub configuration from the environment like the hub process. Exit codes: 0
done, 2 configuration error (the message names the variable, never its value), 3 the database cannot be reached
within `CONNECT_TIMEOUT_SECONDS`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from datetime import datetime
from typing import Any

from psycopg_pool import PoolTimeout

from agent_hub.clock import system_clock
from agent_hub.config import ConfigError, load_config
from agent_hub.db.pool import open_pool
from agent_hub.telemetry.routes import DAYS_DEFAULT
from agent_hub.telemetry.store import rows

EXIT_CONFIG = 2
EXIT_UNAVAILABLE = 3
CONNECT_TIMEOUT_SECONDS = 30.0
MAX_DAYS = 90
CLOCK: list[Callable[[], datetime]] = [system_clock]
"""The clock of "today" (replaced by tests)."""


def days_argument(text: str) -> int:
    """A number of days within 1..`MAX_DAYS`."""
    if not (text.isascii() and text.isdigit()) or not 1 <= int(text) <= MAX_DAYS:
        raise argparse.ArgumentTypeError(f"must be a whole number from 1 to {MAX_DAYS}")
    return int(text)


def ui_health_command(args: argparse.Namespace) -> int:
    """Print the counters of the last `--days` days (module docstring)."""
    try:
        config = load_config(os.environ)
    except ConfigError as error:
        print(f"agent-hub: {error}", file=sys.stderr)
        return EXIT_CONFIG
    try:
        pool = open_pool(
            config.database, min_size=1, max_size=1, timeout=CONNECT_TIMEOUT_SECONDS, name="agent-hub-ui-health"
        )
    except PoolTimeout:
        print("agent-hub: the hub database is not reachable", file=sys.stderr)
        return EXIT_UNAVAILABLE
    try:
        with pool.connection() as connection:
            result = rows(connection, CLOCK[0]().date(), args.days)
    finally:
        pool.close()
    print(json.dumps(result, separators=(",", ":")))
    return 0


def register(subparsers: argparse._SubParsersAction[Any]) -> None:
    """Add `ui-health`."""
    parser = subparsers.add_parser("ui-health", help="print the anonymous UI health counters of the web clients")
    parser.add_argument("--days", type=days_argument, default=DAYS_DEFAULT, help="days back, today included (1-90)")
    parser.set_defaults(handler=ui_health_command)
