"""CLI of the backup area (ADR-027 section 4): `export`, `restore`, `rebuild-projections` (02-data-model.md 1.3, 3).

- `agent-hub export --org <id> [--out <file>]` writes the org's export (`agent_hub.backup.export`) to the file (written
  with mode 0600 to a partial file next to it and linked to its name when complete; an existing file is never
  overwritten) or to
  stdout, and prints `exported <rows>` (on stderr when the file goes to stdout). It reads as the writer role
  (`AGENT_HUB_DATABASE`, the hub's configuration) and signs with the hub key.
- `agent-hub restore --in <file> [--in <file> ...]` (`agent_hub.backup.restore`) needs the hub configuration and
  `AGENT_HUB_OWNER_DATABASE`; it prints `restored <rows>` or `failed <step>`.
- `agent-hub rebuild-projections [--check]` (`agent_hub.backup.rebuild`) prints one JSON line
  `{"ok","check","rebuilt","differences"}`.

Exit codes: 0 success, 1 refused (`failed <step>`, or differences found by `--check`), 2 configuration error (the
message names the variable, never its value), 3 the database cannot be reached within `CONNECT_TIMEOUT_SECONDS`.
No output ever holds a value of the configuration or a database message.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO
from uuid import UUID

import psycopg
from psycopg_pool import PoolTimeout

from agent_hub.backup import rebuild
from agent_hub.backup.export import OrgUnknown, write_export
from agent_hub.backup.restore import SESSION_STATEMENTS, RestoreFailed, owner_string, restore
from agent_hub.backup.tables import ValueUnwritable
from agent_hub.config import ConfigError, HubConfig, load_config
from agent_hub.db.migrate import OWNER_VARIABLE
from agent_hub.db.pool import open_pool

EXIT_REFUSED = 1
EXIT_CONFIG = 2
EXIT_UNAVAILABLE = 3
CONNECT_TIMEOUT_SECONDS = 30.0
FILE_MODE = 0o600
Clock = Callable[[], datetime]


def _now() -> datetime:
    return datetime.now(UTC)


CLOCK: list[Clock] = [_now]
"""The clock of `exported_at` (replaced by tests)."""


def _config(environ: Mapping[str, str]) -> HubConfig | None:
    try:
        return load_config({name: value for name, value in environ.items() if name != OWNER_VARIABLE})
    except ConfigError as error:
        print(f"agent-hub: {error}", file=sys.stderr)
        return None


def _refused(line: str) -> int:
    print(line, file=sys.stderr)
    return EXIT_REFUSED


def _export_to(config: HubConfig, org: UUID, out: BinaryIO) -> int:
    pool = open_pool(config.database, min_size=1, max_size=1, timeout=CONNECT_TIMEOUT_SECONDS, name="agent-hub-export")
    try:
        with pool.connection() as connection:
            connection.autocommit = True
            return write_export(connection, org, config.signer(), out, CLOCK[0]())
    finally:
        pool.close()


class _Refused(Exception):
    """An export step refused; `line` is printed."""

    def __init__(self, line: str) -> None:
        super().__init__(line)
        self.line = line


def _write_file(config: HubConfig, org: UUID, target: Path) -> int:
    """Write the export to a private partial file next to `target` and link it to `target` when complete: an existing
    target is refused, also one created meanwhile (the link fails); the partial file is always removed."""
    if target.exists():
        raise _Refused("failed out_exists")
    partial = target.with_name(f".{target.name}.{secrets.token_hex(8)}.partial")
    descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    try:
        with os.fdopen(descriptor, "wb") as out:
            count = _export_to(config, org, out)
        try:
            os.link(partial, target)
        except FileExistsError:
            raise _Refused("failed out_exists") from None
    finally:
        partial.unlink()
    return count


def _export(config: HubConfig, args: argparse.Namespace) -> int:
    try:
        org = UUID(args.org)
    except ValueError:
        raise _Refused("failed org") from None
    try:
        if args.out is None:
            count = _export_to(config, org, sys.stdout.buffer)
            sys.stdout.buffer.flush()
            print(f"exported {count}", file=sys.stderr)
        else:
            count = _write_file(config, org, Path(args.out))
            print(f"exported {count}")
    except OrgUnknown:
        raise _Refused("failed org") from None
    except ValueUnwritable:
        raise _Refused("failed value_unwritable") from None
    except OSError:
        raise _Refused("failed write") from None
    return 0


def export_command(args: argparse.Namespace) -> int:
    """`agent-hub export` (module docstring)."""
    config = _config(os.environ)
    if config is None:
        return EXIT_CONFIG
    try:
        return _export(config, args)
    except PoolTimeout:
        print("agent-hub: the hub database is not reachable", file=sys.stderr)
        return EXIT_UNAVAILABLE
    except _Refused as refused:
        return _refused(refused.line)


def restore_command(args: argparse.Namespace) -> int:
    """`agent-hub restore` (module docstring)."""
    config = _config(os.environ)
    if config is None:
        return EXIT_CONFIG
    try:
        owner = owner_string(os.environ)
    except RestoreFailed as failure:
        print(f"failed {failure.step}", file=sys.stderr)
        return EXIT_CONFIG
    try:
        contents = [Path(name).read_bytes() for name in args.inputs]
    except OSError:
        return _refused("failed file:read")
    try:
        connection = owner.connect(autocommit=True)
    except psycopg.OperationalError:
        print("agent-hub: the hub database is not reachable", file=sys.stderr)
        return EXIT_UNAVAILABLE
    try:
        with connection:
            for statement in SESSION_STATEMENTS:
                connection.execute(statement)
            count = restore(connection, owner.role, contents, config)
    except RestoreFailed as failure:
        return _refused(f"failed {failure.step}")
    print(f"restored {count}")
    return 0


def rebuild_command(args: argparse.Namespace) -> int:
    """`agent-hub rebuild-projections` (module docstring)."""
    config = _config(os.environ)
    if config is None:
        return EXIT_CONFIG
    try:
        pool = open_pool(
            config.database, min_size=1, max_size=1, timeout=CONNECT_TIMEOUT_SECONDS, name="agent-hub-rebuild"
        )
    except PoolTimeout:
        print("agent-hub: the hub database is not reachable", file=sys.stderr)
        return EXIT_UNAVAILABLE
    try:
        with pool.connection() as connection:
            connection.autocommit = True
            result = rebuild.rebuild(connection, config, check=args.check)
    except Exception:  # a database or a log error: never its message (module docstring)
        return _refused("failed rebuild")
    finally:
        pool.close()
    report = result.report(check=args.check)
    print(json.dumps(report, separators=(",", ":")))
    return 0 if report["ok"] else EXIT_REFUSED


def register(subparsers: argparse._SubParsersAction[Any]) -> None:
    """Add `export`, `restore` and `rebuild-projections`."""
    export_parser = subparsers.add_parser("export", help="write the export file agent-hub-export-v1 of one org")
    export_parser.add_argument("--org", required=True, help="the org ID")
    export_parser.add_argument("--out", help="the file to write (new; default: stdout)")
    export_parser.set_defaults(handler=export_command)
    restore_parser = subparsers.add_parser(
        "restore", help="restore org exports into an empty migrated database (owner role, AGENT_HUB_OWNER_DATABASE)"
    )
    restore_parser.add_argument("--in", dest="inputs", action="append", required=True, help="an export file")
    restore_parser.set_defaults(handler=restore_command)
    rebuild_parser = subparsers.add_parser("rebuild-projections", help="rebuild every projection from its sources")
    rebuild_parser.add_argument(
        "--check", action="store_true", help="compare the rebuilt state with the stored one and roll back"
    )
    rebuild_parser.set_defaults(handler=rebuild_command)
