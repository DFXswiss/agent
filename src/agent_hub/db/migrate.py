"""Schema migrations of the hub and the command `agent-hub migrate` (02-data-model.md section 6).

Pattern of `agent_core.record.migrate`: only the owner role migrates; the run drops any role and search path a
setting gave at login, refuses a superuser, a role that does not own the database and the writer role, waits at most
LOCK_TIMEOUT_MS for the advisory lock MIGRATE_LOCK and applies every pending file of `migrations/` in one
transaction. It prints exactly one line - `applied <version>`, `unchanged <version>` or `failed <step>` - and never a
value of the configuration or the text of a database error (the connection strings are secrets).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from typing import Any, LiteralString, cast

import psycopg
from psycopg import sql

from agent_core.record.connection import (
    ConnectionString,
    ConnectionStringError,
    identifier,
    libpq_supported,
    read_connection_string,
)

OWNER_VARIABLE = "AGENT_HUB_OWNER_DATABASE"
WRITER_VARIABLE = "AGENT_HUB_DATABASE"
MIGRATE_LOCK = 742180100
LOCK_TIMEOUT_MS = 60000

STEP_CONFIG = "config"
"""The connection strings are missing or malformed, name the same role or different databases, or libpq < 16."""
STEP_FILES = "files"
"""The migration files of the package are not a gapless series 0001, 0002, ... of well-named files."""
STEP_OWNER = "owner"
"""The login role is a superuser, is not the role of the connection string or does not own the database."""
STEP_LOCK = "lock"
"""The advisory lock or a catalog lock was not granted within the lock timeout."""
STEP_CHECKSUM = "checksum"
"""`migration_checksum_mismatch`: an applied version is unknown to the code or its name or checksum differ."""
STEP_MIGRATE = "migrate"
"""A statement of a pending migration failed, the writer role is missing, or the connection broke."""

ERROR_CHECKSUM_MISMATCH = "migration_checksum_mismatch"

FILE_RE = re.compile(r"(?P<version>[0-9]{4})_(?P<name>[a-z0-9_]{1,64})\.sql")

# The first statements of every run (see agent_core.record.migrate.SESSION_STATEMENTS): act as the login role
# and resolve names only in pg_catalog, with pg_temp last.
SESSION_STATEMENTS = (
    "SET ROLE NONE",
    "SET search_path = pg_catalog, pg_temp",
)

_OWNER_CHECK = """
SELECT session_user OPERATOR(pg_catalog.=) r.rolname, r.rolsuper, d.datdba OPERATOR(pg_catalog.=) r.oid
FROM pg_catalog.pg_roles r
JOIN pg_catalog.pg_database d ON d.datname OPERATOR(pg_catalog.=) pg_catalog.current_database()
WHERE r.rolname OPERATOR(pg_catalog.=) %s
"""

_WRITER_EXISTS = "SELECT pg_catalog.count(*) FROM pg_catalog.pg_roles WHERE rolname OPERATOR(pg_catalog.=) %s"


class MigrationFileError(Exception):
    """The migration directory breaks the naming or numbering rule."""


class MigrationChecksumMismatch(Exception):
    """`migration_checksum_mismatch`: the database holds a version whose file is missing or differs."""

    code = ERROR_CHECKSUM_MISMATCH


class _MissingWriter(Exception):
    """The writer role does not exist."""


@dataclass(frozen=True)
class Migration:
    """One migration file: `NNNN_<name>.sql`, its text and the SHA-256 of its bytes."""

    version: int
    name: str
    text: str
    checksum: str


def default_directory() -> Traversable:
    """The `migrations` directory of this package."""
    return resources.files("agent_hub.db").joinpath("migrations")


def load_migrations(directory: Traversable | None = None) -> list[Migration]:
    """Every `*.sql` file of the directory, ordered; they must be numbered 0001, 0002, ... without a gap."""
    source = default_directory() if directory is None else directory
    migrations: list[Migration] = []
    for entry in sorted(source.iterdir(), key=lambda item: item.name):
        if not entry.name.endswith(".sql"):
            continue
        match = FILE_RE.fullmatch(entry.name)
        if match is None:
            raise MigrationFileError("a migration file breaks the name rule NNNN_<name>.sql")
        raw = entry.read_bytes()
        migrations.append(
            Migration(
                version=int(match["version"]),
                name=match["name"],
                text=raw.decode("utf-8"),
                checksum=hashlib.sha256(raw).hexdigest(),
            )
        )
    if [migration.version for migration in migrations] != list(range(1, len(migrations) + 1)):
        raise MigrationFileError("the migration files are not numbered 0001, 0002, ... without a gap")
    return migrations


def compose(migration: Migration, database: str, writer: str) -> sql.Composed:
    """The text of a migration with `{database}` and `{writer}` bound as identifiers (`{{`/`}}` are braces)."""
    return sql.SQL(migration.text).format(database=identifier(database), writer=identifier(writer))


def pending(migrations: list[Migration], applied: Iterable[tuple[int, str, str]]) -> list[Migration]:
    """The migrations not yet applied; MigrationChecksumMismatch when an applied row has no identical file.

    `applied` holds (version, name, checksum) rows of `hub.schema_version`. Forward-only: the applied versions
    must be exactly 1..k for some k and match the first k files byte for byte.
    """
    rows = sorted(applied)
    if len(rows) > len(migrations):
        raise MigrationChecksumMismatch()
    for row, migration in zip(rows, migrations, strict=False):
        if row != (migration.version, migration.name, migration.checksum):
            raise MigrationChecksumMismatch()
    return migrations[len(rows) :]


def _failed(step: str) -> tuple[str, int]:
    return f"failed {step}", 1


class _StepFailed(Exception):
    """A step of the run failed; only its name is printed."""

    def __init__(self, step: str) -> None:
        super().__init__(step)
        self.step = step


def _config(environ: Mapping[str, str]) -> tuple[ConnectionString, ConnectionString]:
    """The owner's and the writer's connection strings: two roles of one database, and libpq 16 or later."""
    try:
        owner = read_connection_string(environ, OWNER_VARIABLE)
        writer = read_connection_string(environ, WRITER_VARIABLE)
    except ConnectionStringError:
        raise _StepFailed(STEP_CONFIG) from None
    same_database = (owner.host, owner.port, owner.database) == (writer.host, writer.port, writer.database)
    if owner.role == writer.role or not same_database or not libpq_supported():
        raise _StepFailed(STEP_CONFIG)
    return owner, writer


def _files(directory: Traversable | None) -> list[Migration]:
    try:
        return load_migrations(directory)
    except (MigrationFileError, UnicodeDecodeError):
        raise _StepFailed(STEP_FILES) from None


def run_migrate(
    environ: Mapping[str, str],
    *,
    directory: Traversable | None = None,
    lock_timeout_ms: int = LOCK_TIMEOUT_MS,
) -> tuple[str, int]:
    """Run the migration; the printed line and the exit code (0 or 1)."""
    try:
        owner, writer = _config(environ)
        migrations = _files(directory)
    except _StepFailed as failure:
        return _failed(failure.step)
    return _run(owner, writer.role, migrations, lock_timeout_ms)


def _run(owner: ConnectionString, writer: str, migrations: list[Migration], lock_timeout_ms: int) -> tuple[str, int]:
    step = STEP_OWNER
    try:
        with owner.connect(autocommit=True) as connection:
            for statement in SESSION_STATEMENTS:
                connection.execute(statement)
            check = _one(connection, _OWNER_CHECK, (owner.role,))
            if not check[0] or check[1] or not check[2]:
                return _failed(STEP_OWNER)
            step = STEP_LOCK
            with connection.transaction():
                # First statement of the transaction: a default isolation level of the role or database must not
                # hide a schema that a concurrent run committed while this run waited for the lock.
                connection.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
                connection.execute("SELECT pg_catalog.set_config('lock_timeout', %s, true)", (f"{lock_timeout_ms}ms",))
                connection.execute("SELECT pg_catalog.pg_advisory_xact_lock(%s)", (MIGRATE_LOCK,))
                step = STEP_CHECKSUM
                todo = pending(migrations, _applied(connection))
                if not todo:
                    return f"unchanged {len(migrations)}", 0
                step = STEP_MIGRATE
                _check_writer_exists(connection, writer)
                for migration in todo:
                    connection.execute(compose(migration, owner.database, writer))
                    connection.execute(
                        "INSERT INTO hub.schema_version (version, name, checksum, applied_at)"
                        " VALUES (%s, %s, %s, pg_catalog.now())",
                        (migration.version, migration.name, migration.checksum),
                    )
                return f"applied {len(migrations)}", 0
    except Exception as error:
        # Never the exception itself: a database message can carry a value.
        if isinstance(error, psycopg.errors.LockNotAvailable):
            return _failed(STEP_LOCK)
        return _failed(step)


def _applied(connection: psycopg.Connection[Any]) -> list[tuple[int, str, str]]:
    if not _one(connection, "SELECT pg_catalog.to_regclass('hub.schema_version') IS NOT NULL")[0]:
        return []
    rows = connection.execute("SELECT version, name, checksum FROM hub.schema_version").fetchall()
    return [(int(row[0]), str(row[1]), str(row[2])) for row in rows]


def _check_writer_exists(connection: psycopg.Connection[Any], writer: str) -> None:
    if _one(connection, _WRITER_EXISTS, (writer,))[0] != 1:
        raise _MissingWriter()


def _one(connection: psycopg.Connection[Any], query: LiteralString, params: tuple[Any, ...] = ()) -> tuple[Any, ...]:
    """The single row of a query that always returns exactly one row."""
    return cast(tuple[Any, ...], connection.execute(query, params).fetchone())
