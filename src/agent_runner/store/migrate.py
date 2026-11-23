"""Forward-only migrations of the runner store (docs/platform/spec/02-data-model.md section 6).

Files `migrations/NNNN_<name>.sql` are numbered from 0001 without gaps. `migrate` takes the session advisory lock
742180200, creates schema `runner` and `runner.schema_version(version int PK, name, checksum text, applied_at)`,
checks the SHA-256 checksum of every applied file (`migration_checksum_mismatch`), refuses a database that knows a
version this code does not (`migration_unknown`) and applies each pending file in its own transaction. The result
is one line: `applied <n>`, `unchanged <n>` or `failed <step>`; database error text is never part of it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import psycopg
from psycopg import sql

from agent_runner.errors import RunnerError

ADVISORY_LOCK = 742180200
LOCK_TIMEOUT_MS = 60_000
MIGRATIONS_DIR = Path(__file__).with_name("migrations")
_FILE_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str


@dataclass(frozen=True)
class MigrationResult:
    status: Literal["applied", "unchanged", "failed"]
    count: int = 0
    step: str | None = None

    @property
    def line(self) -> str:
        if self.status == "failed":
            return f"failed {self.step}"
        return f"{self.status} {self.count}"


def load_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """The migration files in version order; a bad name, a duplicate or a gap raises `migration_invalid`."""
    found: list[Migration] = []
    for path in sorted(directory.iterdir()):
        if path.suffix != ".sql":
            continue
        match = _FILE_RE.fullmatch(path.name)
        if match is None:
            raise RunnerError("migration_invalid", path.name)
        data = path.read_bytes()
        found.append(
            Migration(
                version=int(match.group(1)),
                name=match.group(2),
                sql=data.decode("utf-8"),
                checksum=hashlib.sha256(data).hexdigest(),
            )
        )
    for expected, migration in enumerate(found, start=1):
        if migration.version != expected:
            raise RunnerError("migration_invalid", f"{migration.version:04d}_{migration.name}")
    return found


def _step(migration: Migration) -> str:
    return f"{migration.version:04d}_{migration.name}"


def migrate(dsn: str, directory: Path = MIGRATIONS_DIR, *, lock_timeout_ms: int = LOCK_TIMEOUT_MS) -> MigrationResult:
    """Bring schema `runner` of the database at `dsn` up to date.

    A checksum mismatch or an unknown applied version raises `RunnerError`, so does an unreachable store
    (`store_unavailable`) or a lock held longer than `lock_timeout_ms` (`migration_locked`). A failing migration
    returns `failed <step>` and leaves the earlier migrations applied (each runs in its own transaction).
    """
    migrations = load_migrations(directory)
    try:
        conn = psycopg.connect(dsn, autocommit=True)
    except psycopg.Error:
        raise RunnerError("store_unavailable") from None
    with conn:
        try:
            conn.execute(sql.SQL("SET lock_timeout = {}").format(sql.Literal(f"{int(lock_timeout_ms)}ms")))
            try:
                conn.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK,))
            except psycopg.errors.LockNotAvailable:
                raise RunnerError("migration_locked") from None
            try:
                return _migrate_locked(conn, migrations)
            finally:
                conn.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK,))
        except psycopg.Error:
            # Never database error text (02-data-model.md section 6): only the code leaves this module.
            raise RunnerError("store_unavailable") from None


def _migrate_locked(conn: psycopg.Connection[tuple[object, ...]], migrations: list[Migration]) -> MigrationResult:
    with conn.transaction():
        conn.execute("CREATE SCHEMA IF NOT EXISTS runner")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS runner.schema_version ("
            " version integer PRIMARY KEY,"
            " name text NOT NULL,"
            " checksum text NOT NULL,"
            " applied_at timestamptz NOT NULL DEFAULT now())"
        )
    applied = {
        int(str(row[0])): str(row[1])
        for row in conn.execute("SELECT version, checksum FROM runner.schema_version ORDER BY version")
    }
    known = {migration.version: migration for migration in migrations}
    for version, checksum in applied.items():
        migration = known.get(version)
        if migration is None:
            raise RunnerError("migration_unknown", f"{version:04d}")
        if migration.checksum != checksum:
            raise RunnerError("migration_checksum_mismatch", _step(migration))
    pending = [migration for migration in migrations if migration.version not in applied]
    for migration in pending:
        try:
            with conn.transaction():
                conn.execute(migration.sql.encode("utf-8"))
                conn.execute(
                    "INSERT INTO runner.schema_version (version, name, checksum) VALUES (%s, %s, %s)",
                    (migration.version, migration.name, migration.checksum),
                )
        except psycopg.Error:
            return MigrationResult("failed", step=_step(migration))
    if pending:
        return MigrationResult("applied", count=len(pending))
    return MigrationResult("unchanged", count=len(applied))
