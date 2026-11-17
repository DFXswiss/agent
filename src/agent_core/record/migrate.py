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

# The first statements of every run, before any other statement. A setting of the owner role or
# the database is applied at login and can change what the run checks and creates:
# - `role` makes the session act as another role; `SET ROLE NONE` drops it, and step `owner`
#   checks the login role (`session_user`).
# - `search_path` can put a schema with a shadow of `pg_roles`, of a function or of an operator
#   before `pg_catalog`, which would let a superuser owner pass step `owner` or bind a check of
#   section 11.3 to another function. The run searches only `pg_catalog`, and `pg_temp` last, so
#   that a temporary object never shadows one of `pg_catalog`; the schema statements name
#   `record.` in full.
# The run also passes `options` empty (connection.py), so PGOPTIONS sets no parameter, and sets
# READ COMMITTED itself (step `migrate`). Section 11.4 does not describe these four behaviours.
SESSION_STATEMENTS = (
    "SET ROLE NONE",
    "SET search_path = pg_catalog, pg_temp",
)

# Step `owner`, checked for the role named in the owner's connection string: the session logged
# in as that role, and that role is no superuser and owns the database. Every catalog object is
# named with its schema as well, in addition to the `search_path` above.
_OWNER_CHECK = """
SELECT session_user OPERATOR(pg_catalog.=) r.rolname, r.rolsuper, d.datdba OPERATOR(pg_catalog.=) r.oid
FROM pg_catalog.pg_roles r
JOIN pg_catalog.pg_database d ON d.datname OPERATOR(pg_catalog.=) pg_catalog.current_database()
WHERE r.rolname OPERATOR(pg_catalog.=) %s
"""

# Step `migrate`, before the schema is applied: the writer role exists as a role of its own. The
# pseudo-role PUBLIC has no row in `pg_roles`, so nothing is ever granted to PUBLIC. A GRANT to a
# missing role would fail as well; this check keeps the refusal explicit.
_WRITER_EXISTS = (
    "SELECT pg_catalog.count(*) FROM pg_catalog.pg_roles WHERE rolname OPERATOR(pg_catalog.=) %s"
)


class _UnknownVersion(Exception):
    """`record.schema_version` holds a version the code does not know, or lacks one it knows."""


class _MissingWriter(Exception):
    """The writer role does not exist."""


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


def _check_writer_exists(connection: psycopg.Connection[Any], writer: str) -> None:
    (count,) = connection.execute(_WRITER_EXISTS, (writer,)).fetchone()
    if count != 1:
        raise _MissingWriter()


def run_migrate(environ: Mapping[str, str]) -> tuple[str, int]:
    """Steps `config`, `owner` and `migrate` of section 11.4: the printed line and the exit code.

    Step `migrate` succeeds exactly in the two cases of section 11.4: it applies the schema when
    `record.schema_version` does not exist (and fails when a statement of section 11.3 fails), and
    it does nothing when that table holds exactly the known versions. It fails when the table
    holds an unknown version or lacks a known one, and when it waits longer than LOCK_TIMEOUT_MS
    for the migration lock or a catalog lock.
    """
    try:
        owner, writer = migrate_config(environ)
    except ConnectionStringError:
        return _failed(STEP_CONFIG)
    if not libpq_supported():
        return _failed(STEP_CONFIG)
    step = STEP_OWNER
    try:
        with owner.connect(autocommit=True) as connection:
            # The run acts as the owner role it connected with and reads the real catalog, never
            # a role or a search path that a setting of the role or the database gave at login.
            for statement in SESSION_STATEMENTS:
                connection.execute(statement)
            is_login_role, superuser, owns_database = connection.execute(
                _OWNER_CHECK, (owner.role,)
            ).fetchone()
            if not is_login_role or superuser or not owns_database:
                return _failed(STEP_OWNER)
            step = STEP_MIGRATE
            with connection.transaction():
                # The first statement, so that a `default_transaction_isolation` of the role or
                # the database cannot make a run that waited for the lock miss the schema a
                # concurrent run committed in the meantime.
                connection.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
                connection.execute(
                    "SELECT pg_catalog.set_config('lock_timeout', %s, true)", (f"{LOCK_TIMEOUT_MS}ms",)
                )
                connection.execute("SELECT pg_catalog.pg_advisory_xact_lock(%s)", (MIGRATE_LOCK,))
                exists = connection.execute(
                    "SELECT pg_catalog.to_regclass('record.schema_version') IS NOT NULL"
                ).fetchone()
                if not exists[0]:
                    _check_writer_exists(connection, writer.role)
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
