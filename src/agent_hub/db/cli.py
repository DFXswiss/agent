"""CLI of the database area: `agent-hub migrate` (02-data-model.md section 6, ADR-027 section 4).

The main CLI (HUB-02) discovers `<area>/cli.py:register(subparsers)`; until then `python -m agent_hub.db migrate` is
the entry point. A registered subcommand sets `handler(args) -> exit code`.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from typing import Any

from agent_hub.db.migrate import run_migrate


def migrate_command(_args: argparse.Namespace) -> int:
    """Print the one line of the migration and return its exit code."""
    line, code = run_migrate(os.environ)
    print(line, file=sys.stdout if code == 0 else sys.stderr)
    return code


def register(subparsers: argparse._SubParsersAction[Any]) -> None:
    """Add the subcommand `migrate`."""
    parser = subparsers.add_parser(
        "migrate",
        help="apply the hub schema migrations (owner role, AGENT_HUB_OWNER_DATABASE and AGENT_HUB_DATABASE)",
    )
    parser.set_defaults(handler=migrate_command)


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m agent_hub.db <command>`."""
    parser = argparse.ArgumentParser(prog="python -m agent_hub.db", description="hub database commands")
    register(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args(argv)
    handler = args.handler
    return int(handler(args))
