"""CLI of the audit area (ADR-027 section 4): `agent-hub audit verify`.

The operator's (and the E2E orchestrator's, 07-testing.md section 2.1) check of the global audit chain on the hub
database: the same verification as `GET /v1/orgs/{org}/audit/verify` (`agent_hub.audit.query.verify`: every hash and
link from `seq` 1 to the head, plus the newest checkpoint of the hub key), without a browser session. It reads the hub
configuration from the environment like the hub process, prints the result as one JSON line on stdout and exits with
0 when the chain and the checkpoint verify, `EXIT_BROKEN` when they do not, 2 on a configuration error (the message
names the variable, never its value) and `EXIT_UNAVAILABLE` when the database cannot be reached within
`CONNECT_TIMEOUT_SECONDS` (so an outage is never mistaken for a broken chain).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from psycopg_pool import PoolTimeout

from agent_hub.audit.query import verify
from agent_hub.config import ConfigError, load_config
from agent_hub.db.pool import open_pool

EXIT_CONFIG = 2
EXIT_BROKEN = 1
EXIT_UNAVAILABLE = 3
CONNECT_TIMEOUT_SECONDS = 30.0


def verify_command(_args: argparse.Namespace) -> int:
    """Verify the chain and the newest checkpoint; print the result (module docstring)."""
    try:
        config = load_config(os.environ)
    except ConfigError as error:
        print(f"agent-hub: {error}", file=sys.stderr)
        return EXIT_CONFIG
    signer = config.signer()
    try:
        pool = open_pool(
            config.database, min_size=1, max_size=1, timeout=CONNECT_TIMEOUT_SECONDS, name="agent-hub-audit"
        )
    except PoolTimeout:
        print("agent-hub: the hub database is not reachable", file=sys.stderr)
        return EXIT_UNAVAILABLE
    try:
        with pool.connection() as connection:
            result = verify(connection, signer.key_id, signer.public_key)
    finally:
        pool.close()
    print(json.dumps(result, separators=(",", ":")))
    if not result["ok"]:
        print("agent-hub: the audit chain does not verify", file=sys.stderr)
        return EXIT_BROKEN
    return 0


def register(subparsers: argparse._SubParsersAction[Any]) -> None:
    """Add `audit verify`."""
    parser = subparsers.add_parser("audit", help="audit chain commands")
    commands = parser.add_subparsers(dest="audit_command", metavar="command", required=True)
    verify_parser = commands.add_parser("verify", help="verify the audit chain and the newest checkpoint")
    verify_parser.set_defaults(handler=verify_command)
