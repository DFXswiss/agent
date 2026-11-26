"""The runner's own PostgreSQL cluster under `$AGENT_RUNNER_HOME/pg` (05-runner.md section 1.4, ADR-022).

Cluster lifecycle is the legacy `agent_cli.pg` imported read-only (00-overview.md section 5): `initdb
--auth=trust`, listening on 127.0.0.1 only, own port file, `LC_ALL=C`. Importing it has no side effects. The
runner keeps its data in the database `agent_runner` of that cluster.

Durability (HOT-25): a commit the runner made may already be on its way to the hub (outbox), so the store must not lose
it in a power cut. On macOS `fsync()` hands the data to the drive but does not empty the drive's write cache; only
`fcntl(F_FULLFSYNC)` does (fsync(2) of macOS: "For applications that require tighter guarantees about the integrity
of their data, Mac OS X provides the F_FULLFSYNC fcntl"). PostgreSQL's default `wal_sync_method` there is
`open_datasync`, which therefore loses the last commits when the power fails (PostgreSQL documentation, "WAL
Reliability": "On macOS, write caching can be prevented by setting wal_sync_method to fsync_writethrough"; the value
calls `F_FULLFSYNC` and, as `pg_fsync` follows it, flushes data files at checkpoints the same way). `start_store`
therefore checks the setting at **every** start: a cluster whose value differs from `durable_wal_sync_method()` gets
it through `ALTER SYSTEM` (kept in `postgresql.auto.conf`) and is restarted once, so new installations and existing
ones (after an update, which restarts the daemon) both run with it; a value that does not hold after the restart is
`store_not_durable`. Other systems keep PostgreSQL's default (`fdatasync` on Linux reaches the medium).
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg import sql

from agent_cli import pg
from agent_runner.errors import RunnerError

DATABASE = "agent_runner"
WAL_SYNC_METHOD = "wal_sync_method"
DARWIN_WAL_SYNC_METHOD = "fsync_writethrough"


def durable_wal_sync_method(platform: str = sys.platform) -> str | None:
    """The `wal_sync_method` the store needs on `platform`; None where PostgreSQL's default is durable."""
    return DARWIN_WAL_SYNC_METHOD if platform == "darwin" else None


@dataclass(frozen=True)
class ClusterTools:
    """The cluster operations, injectable for tests (defaults: `agent_cli.pg`)."""

    ensure: Callable[[Path], str] = pg.ensure_cluster
    stop: Callable[[Path], None] = pg.stop_cluster
    create_database: Callable[[str, str], str] = pg.create_database
    exists: Callable[[Path], bool] = pg.cluster_exists
    wal_sync_method: str | None = None
    """The required `wal_sync_method` (module docstring); None leaves the server's value (tools of a test that hand
    out a shared server). The daemon's tools are `device_tools()`."""


def device_tools() -> ClusterTools:
    """The tools of the device's own cluster: the legacy helpers with the durable `wal_sync_method`."""
    return ClusterTools(wal_sync_method=durable_wal_sync_method())


def _setting(admin: str, name: str) -> str:
    with psycopg.connect(admin, autocommit=True) as conn:
        row = conn.execute("SELECT current_setting(%s)", (name,)).fetchone()
    assert row is not None
    return str(row[0])


def ensure_durable(pg_dir: Path, admin: str, tools: ClusterTools) -> tuple[str, str]:
    """Make the running cluster use `tools.wal_sync_method`; returns the admin DSN after it and `ok` (nothing to do),
    `set` (changed and restarted) or `default` (no requirement on this system). `store_not_durable` when the value
    does not hold after the restart."""
    wanted = tools.wal_sync_method
    if wanted is None:
        return admin, "default"
    if _setting(admin, WAL_SYNC_METHOD) == wanted:
        return admin, "ok"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("ALTER SYSTEM SET wal_sync_method = {}").format(sql.Literal(wanted)))
    tools.stop(pg_dir)
    admin = tools.ensure(pg_dir)
    if _setting(admin, WAL_SYNC_METHOD) != wanted:
        raise RunnerError("store_not_durable")
    return admin, "set"


def start_store(pg_dir: Path, tools: ClusterTools | None = None) -> str:
    """Start (and on first use create) the cluster with a durable `wal_sync_method` (module docstring); return the DSN
    of database `agent_runner`.

    Failures of the legacy helpers (`PgError`, whose text may contain server output) become
    `RunnerError("store_unavailable")`, so do database errors (their text may name server details).
    """
    used = tools or device_tools()
    try:
        admin, _state = ensure_durable(pg_dir, used.ensure(pg_dir), used)
        return used.create_database(admin, DATABASE)
    except (pg.PgError, OSError, psycopg.Error):
        raise RunnerError("store_unavailable") from None


def store_exists(pg_dir: Path, tools: ClusterTools | None = None) -> bool:
    """True when a cluster exists under `pg_dir`; probe failures are `store_unavailable`."""
    used = tools or device_tools()
    try:
        return used.exists(pg_dir)
    except (pg.PgError, OSError):
        raise RunnerError("store_unavailable") from None


def stop_store(pg_dir: Path, tools: ClusterTools | None = None) -> None:
    """Stop the cluster when it runs; a missing cluster is not an error."""
    used = tools or device_tools()
    try:
        if used.exists(pg_dir):
            used.stop(pg_dir)
    except (pg.PgError, OSError):
        raise RunnerError("store_unavailable") from None
