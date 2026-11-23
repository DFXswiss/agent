"""The runner's own PostgreSQL cluster under `$AGENT_RUNNER_HOME/pg` (05-runner.md section 1.4, ADR-022).

Cluster lifecycle is the legacy `agent_cli.pg` imported read-only (00-overview.md section 5): `initdb
--auth=trust`, listening on 127.0.0.1 only, own port file, `LC_ALL=C`. Importing it has no side effects. The
runner keeps its data in the database `agent_runner` of that cluster.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import psycopg

from agent_cli import pg
from agent_runner.errors import RunnerError

DATABASE = "agent_runner"


@dataclass(frozen=True)
class ClusterTools:
    """The cluster operations, injectable for tests (defaults: `agent_cli.pg`)."""

    ensure: Callable[[Path], str] = pg.ensure_cluster
    stop: Callable[[Path], None] = pg.stop_cluster
    create_database: Callable[[str, str], str] = pg.create_database
    exists: Callable[[Path], bool] = pg.cluster_exists


def start_store(pg_dir: Path, tools: ClusterTools | None = None) -> str:
    """Start (and on first use create) the cluster; return the DSN of database `agent_runner`.

    Failures of the legacy helpers (`PgError`, whose text may contain server output) become
    `RunnerError("store_unavailable")`, so do database errors (their text may name server details).
    """
    used = tools or ClusterTools()
    try:
        admin = used.ensure(pg_dir)
        return used.create_database(admin, DATABASE)
    except (pg.PgError, OSError, psycopg.Error):
        raise RunnerError("store_unavailable") from None


def store_exists(pg_dir: Path, tools: ClusterTools | None = None) -> bool:
    """True when a cluster exists under `pg_dir`; probe failures are `store_unavailable`."""
    used = tools or ClusterTools()
    try:
        return used.exists(pg_dir)
    except (pg.PgError, OSError):
        raise RunnerError("store_unavailable") from None


def stop_store(pg_dir: Path, tools: ClusterTools | None = None) -> None:
    """Stop the cluster when it runs; a missing cluster is not an error."""
    used = tools or ClusterTools()
    try:
        if used.exists(pg_dir):
            used.stop(pg_dir)
    except (pg.PgError, OSError):
        raise RunnerError("store_unavailable") from None
