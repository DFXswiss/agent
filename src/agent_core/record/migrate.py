"""The schema of the record and the `migrate` command (RECORD-PROTOCOL.md sections 11.2 to 11.4).

`migrate` alone uses the owner role. It prints one line, `migrated <version>`, `unchanged
<version>` or `failed <step>`, and never a value of the configuration or a message of a database
error (section 13).
"""

from __future__ import annotations

from typing import Any, Mapping

import psycopg
from psycopg import sql

from .connection import ConnectionStringError, identifier, libpq_supported, migrate_config

SCHEMA_VERSION = 1
KNOWN_VERSIONS = frozenset({SCHEMA_VERSION})
MIGRATE_LOCK = 742180002
# How long step `migrate` waits for the migration lock or a catalog lock before it fails.
LOCK_TIMEOUT_MS = 60000

STEP_CONFIG = "config"
STEP_OWNER = "owner"
STEP_MIGRATE = "migrate"

# Section 11.3, statement by statement. `{database}`, `{owner}` and `{writer}` are identifiers;
# `{{` and `}}` are literal braces.
SCHEMA_STATEMENTS = (
    "REVOKE ALL ON DATABASE {database} FROM PUBLIC",
    "GRANT CONNECT ON DATABASE {database} TO {writer}",
    "CREATE SCHEMA record AUTHORIZATION {owner}",
    "REVOKE ALL ON SCHEMA record FROM PUBLIC",
    "GRANT USAGE ON SCHEMA record TO {writer}",
    """CREATE TABLE record.schema_version (
  version    integer     PRIMARY KEY,
  applied_at timestamptz NOT NULL
)""",
    """CREATE TABLE record.event (
  position     bigint      PRIMARY KEY CHECK (position >= 1),
  event_id     uuid        NOT NULL UNIQUE,
  actor_key_id text        NOT NULL,
  prev_hash    text        NOT NULL CHECK (prev_hash = '-' OR prev_hash ~ '^[0-9a-f]{{64}}$'),
  event_hash   text        NOT NULL UNIQUE CHECK (event_hash ~ '^[0-9a-f]{{64}}$'),
  received_at  timestamptz NOT NULL
               CHECK (date_trunc('second', received_at AT TIME ZONE 'UTC') = received_at AT TIME ZONE 'UTC'),
  event_text   bytea       NOT NULL CHECK (octet_length(event_text) BETWEEN 1 AND 65536),
  signature    bytea       NOT NULL CHECK (octet_length(signature) = 64),
  record_hash  text        NOT NULL UNIQUE CHECK (record_hash ~ '^[0-9a-f]{{64}}$'),
  UNIQUE (actor_key_id, prev_hash)
)""",
    """CREATE TABLE record.checkpoint (
  hub_key_id   text        NOT NULL,
  position     bigint      NOT NULL CHECK (position >= 1),
  record_hash  text        NOT NULL CHECK (record_hash ~ '^[0-9a-f]{{64}}$'),
  published_at timestamptz NOT NULL
               CHECK (date_trunc('second', published_at AT TIME ZONE 'UTC') = published_at AT TIME ZONE 'UTC'),
  signature    bytea       NOT NULL CHECK (octet_length(signature) = 64),
  PRIMARY KEY (hub_key_id, position)
)""",
    """CREATE FUNCTION record.refuse_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'record rows are never changed or removed' USING ERRCODE = 'P0001';
END
$$""",
    "REVOKE ALL ON FUNCTION record.refuse_change() FROM PUBLIC",
    """CREATE TRIGGER event_no_change BEFORE UPDATE OR DELETE ON record.event
  FOR EACH ROW EXECUTE FUNCTION record.refuse_change()""",
    """CREATE TRIGGER event_no_truncate BEFORE TRUNCATE ON record.event
  FOR EACH STATEMENT EXECUTE FUNCTION record.refuse_change()""",
    """CREATE TRIGGER checkpoint_no_change BEFORE UPDATE OR DELETE ON record.checkpoint
  FOR EACH ROW EXECUTE FUNCTION record.refuse_change()""",
    """CREATE TRIGGER checkpoint_no_truncate BEFORE TRUNCATE ON record.checkpoint
  FOR EACH STATEMENT EXECUTE FUNCTION record.refuse_change()""",
    "GRANT SELECT, INSERT ON record.event, record.checkpoint TO {writer}",
    "GRANT SELECT ON record.schema_version TO {writer}",
    "INSERT INTO record.schema_version (version, applied_at) VALUES ({version}, now())",
)

# Step `owner`: the owner role is no superuser and owns the database.
_OWNER_CHECK = """
SELECT r.rolsuper, d.datdba = r.oid
FROM pg_roles r JOIN pg_database d ON d.datname = current_database()
WHERE r.rolname = current_user
"""

# Step `migrate`: the writer role exists as a role of its own, can log in and is no superuser.
# The pseudo-role PUBLIC has no row in `pg_roles`.
_WRITER_CHECK = "SELECT rolcanlogin, rolsuper FROM pg_roles WHERE rolname = %s"


class _UnknownVersion(Exception):
    """`record.schema_version` holds a version the code does not know, or lacks one it knows."""


class _BadWriter(Exception):
    """The writer role does not exist, cannot log in, or is a superuser."""


def schema_statements(database: str, owner: str, writer: str) -> list[sql.Composed]:
    """The statements of section 11.3 for these names; a name of a wrong form or reserved raises."""
    if owner == writer:
        raise ConnectionStringError("the owner and the writer are the same role")
    names = {
        "database": identifier(database),
        "owner": identifier(owner),
        "writer": identifier(writer),
        "version": sql.Literal(SCHEMA_VERSION),
    }
    return [sql.SQL(statement).format(**names) for statement in SCHEMA_STATEMENTS]


def _check_writer(connection: psycopg.Connection[Any], writer: str) -> None:
    row = connection.execute(_WRITER_CHECK, (writer,)).fetchone()
    if row is None:
        raise _BadWriter()
    can_login, superuser = row
    if not can_login or superuser:
        raise _BadWriter()


def run_migrate(environ: Mapping[str, str]) -> tuple[str, int]:
    """Steps `config`, `owner` and `migrate` of section 11.4: the printed line and the exit code."""
    try:
        owner, writer = migrate_config(environ)
    except ConnectionStringError:
        return _failed(STEP_CONFIG)
    if not libpq_supported():
        return _failed(STEP_CONFIG)
    step = STEP_OWNER
    try:
        with owner.connect(autocommit=True) as connection:
            superuser, owns_database = connection.execute(_OWNER_CHECK).fetchone()
            if superuser or not owns_database:
                return _failed(STEP_OWNER)
            step = STEP_MIGRATE
            with connection.transaction():
                connection.execute(
                    "SELECT set_config('lock_timeout', %s, true)", (f"{LOCK_TIMEOUT_MS}ms",)
                )
                connection.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATE_LOCK,))
                _check_writer(connection, writer.role)
                exists = connection.execute(
                    "SELECT to_regclass('record.schema_version') IS NOT NULL"
                ).fetchone()
                if not exists[0]:
                    for statement in schema_statements(owner.database, owner.role, writer.role):
                        connection.execute(statement)
                    return f"migrated {SCHEMA_VERSION}", 0
                rows = connection.execute("SELECT version FROM record.schema_version").fetchall()
                if {row[0] for row in rows} != KNOWN_VERSIONS:
                    raise _UnknownVersion()
                return f"unchanged {SCHEMA_VERSION}", 0
    except Exception:
        # Never the exception itself: a database message can carry a value (section 13).
        return _failed(step)


def _failed(step: str) -> tuple[str, int]:
    return f"failed {step}", 1
