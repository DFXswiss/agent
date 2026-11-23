"""`agent-hub rebuild-projections [--check]`: rebuild every projection from its sources (02-data-model.md 1.3).

The rebuilders are discovered (ADR-027 section 4: `<area>/projections.py:REBUILDERS`, a mapping from a name to a
function that rebuilds inside the caller's transaction and returns the number of objects it rebuilt). The registered
rebuilders take `(connection, config)`; a rebuilder that takes only `(connection)` (the platform log's, which needs no
configuration) is called with the connection alone (decision DR-01 4). They run in the order of their names, all in one
transaction.

`rebuild` compares the stored state before and after: a digest of every table that is not append-only (append-only
tables cannot change). The tables whose digest changed are the `differences` - with `check` the transaction is rolled
back and differences mean that the incremental state does not equal the rebuilt one; without `check` the rebuilt
state is committed and the differences name what the rebuild repaired.

Concurrent writers: before the digests the transaction takes `EXCLUSIVE` locks on every table that is not append-only
(sorted, waiting at most `lock_timeout`), so no hub request changes a projection between the digests and the
rebuild; reads continue. The hub keeps running.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

import psycopg

from agent_hub.backup.tables import RULE_BY_TABLE, schema_tables
from agent_hub.config import HubConfig
from agent_hub.registry import PROJECTION_REBUILDERS, merge_mappings

Connection = psycopg.Connection[Any]
Rebuilder = Callable[..., int]
LOCK_TIMEOUT_MS = 30000


@dataclass(frozen=True)
class RebuildResult:
    """What a run rebuilt (`name -> count`) and the tables whose content changed."""

    rebuilt: Mapping[str, int]
    differences: tuple[str, ...]

    def report(self, *, check: bool) -> dict[str, object]:
        """The JSON line of the CLI."""
        return {
            "ok": not (check and self.differences),
            "check": check,
            "rebuilt": dict(self.rebuilt),
            "differences": list(self.differences),
        }


def rebuilders() -> dict[str, Rebuilder]:
    """Every discovered rebuilder by name."""
    return cast(dict[str, Rebuilder], merge_mappings(PROJECTION_REBUILDERS))


def call(rebuilder: Rebuilder, connection: Connection, config: HubConfig) -> int:
    """Run one rebuilder with the arguments it takes (module docstring)."""
    if len(inspect.signature(rebuilder).parameters) == 1:
        return int(rebuilder(connection))
    return int(rebuilder(connection, config))


def mutable_tables(connection: Connection) -> list[str]:
    """The tables of the schema the hub can change, sorted: not append-only and not `schema_version` (migrations)."""
    return sorted(
        name
        for name in schema_tables(connection)
        if name != "schema_version" and not (name in RULE_BY_TABLE and RULE_BY_TABLE[name].append_only)
    )


def digests(connection: Connection, tables: list[str]) -> dict[str, str]:
    """A digest of the content of each table (independent of the physical row order)."""
    found: dict[str, str] = {}
    for name in tables:
        record = connection.execute(
            f"SELECT md5(coalesce(string_agg(t::text, E'\\n' ORDER BY t::text COLLATE \"C\"), '')) FROM hub.{name} t"
        ).fetchone()
        found[name] = str(cast(tuple[Any, ...], record)[0])
    return found


def run_all(connection: Connection, config: HubConfig) -> dict[str, int]:
    """Run every rebuilder in name order inside the caller's transaction; the counts."""
    return {name: call(rebuilder, connection, config) for name, rebuilder in sorted(rebuilders().items())}


def compare(connection: Connection, config: HubConfig, tables: list[str]) -> RebuildResult:
    """Digest, rebuild, digest again inside the caller's transaction."""
    before = digests(connection, tables)
    rebuilt = run_all(connection, config)
    after = digests(connection, tables)
    return RebuildResult(rebuilt, tuple(name for name in tables if before[name] != after[name]))


def rebuild(connection: Connection, config: HubConfig, *, check: bool) -> RebuildResult:
    """Lock, rebuild and compare in one transaction; rolled back with `check` (module docstring)."""
    with connection.transaction() as transaction:
        connection.execute("SELECT pg_catalog.set_config('lock_timeout', %s, true)", (f"{LOCK_TIMEOUT_MS}ms",))
        tables = mutable_tables(connection)
        connection.execute("LOCK TABLE " + ", ".join(f"hub.{name}" for name in tables) + " IN EXCLUSIVE MODE")
        result = compare(connection, config, tables)
        if check:
            raise psycopg.Rollback(transaction)
    return result
