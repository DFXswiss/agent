"""The schema, rights, triggers and constraints of the record database and the `migrate` command.

RECORD-PROTOCOL.md sections 11.2 to 11.4 and section 14, "The database and the start", as far as
change 1 of section 16 builds them. Every test runs against a real PostgreSQL 16 test server
named by AGENT_CORE_TEST_POSTGRES, in a database and with roles of its own (tests/conftest.py).
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

import psycopg
import pytest
from psycopg import sql

from agent_core.record import connection, migrate
from agent_core.record.__main__ import USAGE
from agent_core.record.connection import ConnectionStringError, parse_connection_string
from agent_core.record.migrate import MIGRATE_LOCK, run_migrate, schema_statements
from tests.conftest import RecordDatabase, schema_exists
from tests.test_record_connection import MALFORMED

SRC = Path(__file__).resolve().parents[1] / "src"
TABLES = ("record.schema_version", "record.event", "record.checkpoint")
TRIGGERS = [
    ("checkpoint_no_change", "record.checkpoint", "A", "refuse_change"),
    ("checkpoint_no_truncate", "record.checkpoint", "A", "refuse_change"),
    ("event_no_change", "record.event", "A", "refuse_change"),
    ("event_no_truncate", "record.event", "A", "refuse_change"),
]
COLUMNS = [
    ("checkpoint", "hub_key_id", "text", "NO"),
    ("checkpoint", "position", "bigint", "NO"),
    ("checkpoint", "record_hash", "text", "NO"),
    ("checkpoint", "published_at", "timestamp with time zone", "NO"),
    ("checkpoint", "signature", "bytea", "NO"),
    ("event", "position", "bigint", "NO"),
    ("event", "event_id", "uuid", "NO"),
    ("event", "actor_key_id", "text", "NO"),
    ("event", "prev_hash", "text", "NO"),
    ("event", "event_hash", "text", "NO"),
    ("event", "received_at", "timestamp with time zone", "NO"),
    ("event", "event_text", "bytea", "NO"),
    ("event", "signature", "bytea", "NO"),
    ("event", "record_hash", "text", "NO"),
    ("schema_version", "version", "integer", "NO"),
    ("schema_version", "applied_at", "timestamp with time zone", "NO"),
]
KEYS = {
    ("record.checkpoint", "p", ("hub_key_id", "position")),
    ("record.event", "p", ("position",)),
    ("record.event", "u", ("event_id",)),
    ("record.event", "u", ("event_hash",)),
    ("record.event", "u", ("record_hash",)),
    ("record.event", "u", ("actor_key_id", "prev_hash")),
    ("record.schema_version", "p", ("version",)),
}

_ACL = """
SELECT coalesce(r.rolname, 'PUBLIC'), a.privilege_type, a.is_grantable
FROM {catalog} o CROSS JOIN LATERAL aclexplode(o.{column}) a
LEFT JOIN pg_roles r ON r.oid = a.grantee
WHERE {where}
"""
ACL_QUERIES = {
    "database": _ACL.format(catalog="pg_database", column="datacl", where="o.datname = current_database()"),
    "schema": _ACL.format(catalog="pg_namespace", column="nspacl", where="o.nspname = 'record'"),
    "function": _ACL.format(
        catalog="pg_proc", column="proacl", where="o.oid = 'record.refuse_change()'::regprocedure"
    ),
    **{
        table: _ACL.format(catalog="pg_class", column="relacl", where=f"o.oid = '{table}'::regclass")
        for table in TABLES
    },
}


# --- helpers ------------------------------------------------------------------------------------


def connect(url: str) -> psycopg.Connection[Any]:
    return parse_connection_string(url).connect(autocommit=True)


def migrated(db: RecordDatabase) -> None:
    assert run_migrate(db.migrate_env()) == ("migrated 1", 0)


def assert_no_secret(db: RecordDatabase, *outputs: str) -> None:
    for output in outputs:
        for secret in db.secret_values():
            assert secret not in output


def insert_event(
    conn: psycopg.Connection[Any],
    position: int = 1,
    *,
    actor: str = "example-staff-laptop-1",
    prev_hash: str = "-",
    event_id: str | None = None,
    event_hash: str | None = None,
    received_at: str = "2026-11-17T10:00:01Z",
    text: bytes = b"agent-event-v1",
    signature: bytes = bytes(64),
    record_hash: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO record.event (position, event_id, actor_key_id, prev_hash, event_hash,"
        " received_at, event_text, signature, record_hash)"
        " VALUES (%s, %s::uuid, %s, %s, %s, %s::timestamptz, %s, %s, %s)",
        (
            position,
            event_id or str(uuid.uuid4()),
            actor,
            prev_hash,
            event_hash or secrets.token_hex(32),
            received_at,
            text,
            signature,
            record_hash or secrets.token_hex(32),
        ),
    )


def insert_checkpoint(
    conn: psycopg.Connection[Any],
    position: int = 1,
    *,
    hub_key_id: str = "hub-1",
    record_hash: str | None = None,
    published_at: str = "2026-11-17T11:00:00Z",
    signature: bytes = bytes(64),
) -> None:
    conn.execute(
        "INSERT INTO record.checkpoint (hub_key_id, position, record_hash, published_at, signature)"
        " VALUES (%s, %s, %s, %s::timestamptz, %s)",
        (hub_key_id, position, record_hash or secrets.token_hex(32), published_at, signature),
    )


def grants(admin: psycopg.Connection[Any], name: str) -> set[tuple[str, str, bool]]:
    return {(row[0], row[1], row[2]) for row in admin.execute(ACL_QUERIES[name]).fetchall()}


def catalog(db: RecordDatabase) -> dict[str, Any]:
    """Everything of the record schema that the migration creates or grants, as the admin sees it."""
    with db.admin() as admin:
        out: dict[str, Any] = {
            name: sorted(grants(admin, name)) for name in ACL_QUERIES
        }
        out["schemas"] = admin.execute(
            "SELECT nspname, pg_get_userbyid(nspowner) FROM pg_namespace"
            " WHERE nspname NOT LIKE 'pg\\_%' AND nspname <> 'information_schema' ORDER BY 1"
        ).fetchall()
        out["relations"] = admin.execute(
            "SELECT c.relname, c.relkind, pg_get_userbyid(c.relowner) FROM pg_class c"
            " WHERE c.relnamespace = 'record'::regnamespace AND c.relkind <> 'i' ORDER BY 1"
        ).fetchall()
        out["public_relations"] = admin.execute(
            "SELECT count(*) FROM pg_class WHERE relnamespace = 'public'::regnamespace"
        ).fetchall()
        out["columns"] = admin.execute(
            "SELECT table_name, column_name, data_type, is_nullable FROM information_schema.columns"
            " WHERE table_schema = 'record' ORDER BY table_name, ordinal_position"
        ).fetchall()
        out["keys"] = admin.execute(
            "SELECT c.conrelid::regclass::text, c.contype,"
            " array(SELECT a.attname FROM unnest(c.conkey) WITH ORDINALITY k(attnum, n)"
            "       JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum ORDER BY k.n)"
            " FROM pg_constraint c"
            " WHERE c.connamespace = 'record'::regnamespace AND c.contype IN ('p', 'u') ORDER BY 1, 3"
        ).fetchall()
        out["checks"] = admin.execute(
            "SELECT c.conrelid::regclass::text, pg_get_constraintdef(c.oid) FROM pg_constraint c"
            " WHERE c.connamespace = 'record'::regnamespace AND c.contype = 'c' ORDER BY 1, 2"
        ).fetchall()
        out["triggers"] = admin.execute(
            "SELECT t.tgname, t.tgrelid::regclass::text, t.tgenabled, p.proname FROM pg_trigger t"
            " JOIN pg_proc p ON p.oid = t.tgfoid"
            " JOIN pg_class c ON c.oid = t.tgrelid"
            " WHERE NOT t.tgisinternal AND c.relnamespace = 'record'::regnamespace ORDER BY 1"
        ).fetchall()
        out["functions"] = admin.execute(
            "SELECT p.proname, pg_get_userbyid(p.proowner), l.lanname, p.prorettype::regtype::text"
            " FROM pg_proc p JOIN pg_language l ON l.oid = p.prolang"
            " WHERE p.pronamespace = 'record'::regnamespace ORDER BY 1"
        ).fetchall()
        out["versions"] = admin.execute(
            "SELECT version, applied_at FROM record.schema_version ORDER BY version"
        ).fetchall()
        out["column_grants"] = admin.execute(
            "SELECT count(*) FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid"
            " WHERE c.relnamespace = 'record'::regnamespace AND a.attacl IS NOT NULL"
        ).fetchall()
        out["writer_dependencies"] = admin.execute(
            "SELECT d.classid::regclass::text, d.objid::bigint, d.objsubid, d.deptype FROM pg_shdepend d"
            " WHERE d.refclassid = 'pg_authid'::regclass"
            " AND d.refobjid = (SELECT oid FROM pg_roles WHERE rolname = %s) ORDER BY 1, 2",
            (db.writer_role,),
        ).fetchall()
        out["writer_role"] = admin.execute(
            "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolreplication, rolcanlogin,"
            " (SELECT count(*) FROM pg_auth_members m WHERE m.member = r.oid)"
            " FROM pg_roles r WHERE rolname = %s",
            (db.writer_role,),
        ).fetchall()
        out["oids"] = admin.execute(
            "SELECT (SELECT oid::bigint FROM pg_database WHERE datname = current_database()),"
            " 'record'::regnamespace::oid::bigint, 'record.schema_version'::regclass::oid::bigint,"
            " 'record.event'::regclass::oid::bigint, 'record.checkpoint'::regclass::oid::bigint"
        ).fetchone()
    return out


# --- the migration ------------------------------------------------------------------------------


def test_migrate_creates_exactly_the_schema_and_rights_of_section_11_3(record_db: RecordDatabase) -> None:
    db = record_db
    assert not schema_exists(db)
    with db.admin() as admin:
        assert grants(admin, "database") == set()
    migrated(db)
    state = catalog(db)
    owner, writer = db.owner_role, db.writer_role

    assert state["schemas"] == [("public", "pg_database_owner"), ("record", owner)]
    assert state["relations"] == [
        ("checkpoint", "r", owner),
        ("event", "r", owner),
        ("schema_version", "r", owner),
    ]
    assert state["public_relations"] == [(0,)]
    assert state["columns"] == COLUMNS
    assert {(table, kind, tuple(columns)) for table, kind, columns in state["keys"]} == KEYS
    assert len(state["keys"]) == len(KEYS)
    assert [table for table, _ in state["checks"]] == ["record.checkpoint"] * 4 + ["record.event"] * 7
    assert state["triggers"] == TRIGGERS
    assert state["functions"] == [("refuse_change", owner, "plpgsql", "trigger")]
    assert [row[0] for row in state["versions"]] == [1]
    assert state["column_grants"] == [(0,)]

    # Rights: nothing through PUBLIC, and the writer holds exactly the rights of section 11.2.
    expected_writer = {
        "database": {(writer, "CONNECT", False)},
        "schema": {(writer, "USAGE", False)},
        "function": set(),
        "record.schema_version": {(writer, "SELECT", False)},
        "record.event": {(writer, "SELECT", False), (writer, "INSERT", False)},
        "record.checkpoint": {(writer, "SELECT", False), (writer, "INSERT", False)},
    }
    for name, expected in expected_writer.items():
        rows = {tuple(row) for row in state[name]}
        assert {row[0] for row in rows} <= {owner, writer}, name
        assert {row for row in rows if row[0] == writer} == expected, name
        assert all(row[0] != "PUBLIC" for row in rows), name

    database_oid, schema_oid, version_oid, event_oid, checkpoint_oid = state["oids"]
    assert sorted(state["writer_dependencies"]) == sorted(
        [
            ("pg_class", version_oid, 0, "a"),
            ("pg_class", event_oid, 0, "a"),
            ("pg_class", checkpoint_oid, 0, "a"),
            ("pg_database", database_oid, 0, "a"),
            ("pg_namespace", schema_oid, 0, "a"),
        ]
    )
    # The migration gives the writer no attribute and no membership.
    assert state["writer_role"] == [(False, False, False, False, False, True, 0)]

    with db.admin() as admin:
        row = admin.execute(
            "SELECT has_database_privilege(%(w)s, current_database(), 'CREATE'),"
            " has_database_privilege(%(w)s, current_database(), 'TEMPORARY'),"
            " has_schema_privilege(%(w)s, 'record', 'CREATE'),"
            " has_schema_privilege(%(w)s, 'public', 'CREATE'),"
            " has_function_privilege(%(w)s, 'record.refuse_change()', 'EXECUTE')",
            {"w": writer},
        ).fetchone()
    assert row == (False, False, False, False, False)


def test_a_second_run_changes_nothing(record_db: RecordDatabase) -> None:
    migrated(record_db)
    before = catalog(record_db)
    assert run_migrate(record_db.migrate_env()) == ("unchanged 1", 0)
    assert catalog(record_db) == before


@pytest.mark.parametrize("isolation", [None, "repeatable read", "serializable"])
def test_two_runs_at_once_create_the_schema_once(record_db: RecordDatabase, isolation: str | None) -> None:
    if isolation is not None:
        # A default isolation of the owner role must not make the run that waited for the lock
        # miss the schema the other run committed: `migrate` sets READ COMMITTED itself.
        with record_db.admin() as admin:
            admin.execute(
                sql.SQL("ALTER ROLE {} SET default_transaction_isolation = {}").format(
                    sql.Identifier(record_db.owner_role), sql.Literal(isolation)
                )
            )
    results: list[tuple[str, int]] = []
    env = record_db.migrate_env()
    runs = [threading.Thread(target=lambda: results.append(run_migrate(env))) for _ in range(2)]
    # Hold the migration lock until both runs wait for it, so that they really run at once.
    try:
        with record_db.admin(autocommit=False) as holder, record_db.admin() as watcher:
            holder.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATE_LOCK,))
            for run in runs:
                run.start()
            deadline = time.monotonic() + 60
            waiting = 0
            while waiting < 2 and time.monotonic() < deadline:
                row = watcher.execute(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid::bigint = %s"
                    " AND NOT granted"
                    " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())",
                    (MIGRATE_LOCK,),
                ).fetchone()
                assert row is not None
                waiting = row[0]
                time.sleep(0.05)
            assert waiting == 2
            holder.commit()
    finally:
        # Leaving the block closes the holder and so releases the lock even after a failed
        # assertion; join every started run before the fixture drops the database.
        for run in runs:
            if run.ident is not None:
                run.join(timeout=60)
    assert not any(run.is_alive() for run in runs)
    assert sorted(results) == [("migrated 1", 0), ("unchanged 1", 0)]
    with record_db.admin() as admin:
        assert admin.execute("SELECT version FROM record.schema_version").fetchall() == [(1,)]


# Role names PostgreSQL treats specially or reserves (RECORD-PROTOCOL.md section 15).
RESERVED_ROLES = ("public", "none", "user", "current_user", "current_role", "session_user", "pg_write_all_data")


def other_host(db: RecordDatabase) -> str:
    return "127.0.0.2" if db.host != "127.0.0.2" else "127.0.0.3"


def other_sslmode(db: RecordDatabase) -> str:
    return "require" if db.sslmode != "require" else "verify-full"


CONFIG_FAULTS = {
    "owner missing": lambda db: {"AGENT_CORE_RECORD_DATABASE": db.writer_url},
    "writer missing": lambda db: {"AGENT_CORE_RECORD_OWNER_DATABASE": db.owner_url},
    "owner empty": lambda db: {**db.migrate_env(), "AGENT_CORE_RECORD_OWNER_DATABASE": ""},
    "writer empty": lambda db: {**db.migrate_env(), "AGENT_CORE_RECORD_DATABASE": ""},
    "owner without sslmode": lambda db: {
        **db.migrate_env(),
        "AGENT_CORE_RECORD_OWNER_DATABASE": db.owner_url.split("?")[0],
    },
    "writer with sslrootcert": lambda db: {
        **db.migrate_env(),
        "AGENT_CORE_RECORD_DATABASE": db.writer_url + "&sslrootcert=system",
    },
    "owner with a short password": lambda db: {
        **db.migrate_env(),
        "AGENT_CORE_RECORD_OWNER_DATABASE": db.url(db.owner_role, db.owner_password[:63]),
    },
    "disable with a host name without a dot": lambda db: {
        "AGENT_CORE_RECORD_OWNER_DATABASE": db.url(db.owner_role, db.owner_password, host="postgres", sslmode="disable"),
        "AGENT_CORE_RECORD_DATABASE": db.url(db.writer_role, db.writer_password, host="postgres", sslmode="disable"),
    },
    "the same role in both": lambda db: {
        **db.migrate_env(),
        "AGENT_CORE_RECORD_DATABASE": db.url(db.owner_role, db.writer_password),
    },
    "different hosts": lambda db: {
        **db.migrate_env(),
        "AGENT_CORE_RECORD_DATABASE": db.url(db.writer_role, db.writer_password, host=other_host(db)),
    },
    "different ports": lambda db: {
        **db.migrate_env(),
        "AGENT_CORE_RECORD_DATABASE": db.url(db.writer_role, db.writer_password, port=db.port % 65535 + 1),
    },
    "different databases": lambda db: {
        **db.migrate_env(),
        "AGENT_CORE_RECORD_DATABASE": db.url(db.writer_role, db.writer_password, database=db.name + "_x"),
    },
    "different sslmode": lambda db: {
        **db.migrate_env(),
        "AGENT_CORE_RECORD_DATABASE": db.url(db.writer_role, db.writer_password, sslmode=other_sslmode(db)),
    },
    **{
        f"writer named {name}": (
            lambda db, name=name: {
                **db.migrate_env(),
                "AGENT_CORE_RECORD_DATABASE": db.url(name, db.writer_password),
            }
        )
        for name in RESERVED_ROLES
    },
    "owner named public": lambda db: {
        **db.migrate_env(),
        "AGENT_CORE_RECORD_OWNER_DATABASE": db.url("public", db.owner_password),
    },
    "owner with prefix pg_": lambda db: {
        **db.migrate_env(),
        "AGENT_CORE_RECORD_OWNER_DATABASE": db.url("pg_database_owner", db.owner_password),
    },
    "database named public": lambda db: {
        "AGENT_CORE_RECORD_OWNER_DATABASE": db.url(db.owner_role, db.owner_password, database="public"),
        "AGENT_CORE_RECORD_DATABASE": db.url(db.writer_role, db.writer_password, database="public"),
    },
}


@pytest.mark.parametrize("fault", sorted(CONFIG_FAULTS))
def test_migrate_fails_at_config(record_db: RecordDatabase, fault: str) -> None:
    line, code = run_migrate(CONFIG_FAULTS[fault](record_db))
    assert (line, code) == ("failed config", 1)
    assert not schema_exists(record_db)
    with record_db.admin() as admin:
        assert grants(admin, "database") == set()


@pytest.mark.parametrize("variable", ["AGENT_CORE_RECORD_OWNER_DATABASE", "AGENT_CORE_RECORD_DATABASE"])
def test_migrate_fails_at_config_for_each_malformed_connection_string_of_section_15(
    record_db: RecordDatabase, monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    # Section 14: each malformed connection string of section 15, through `migrate`, in either
    # variable while the other one is valid. No case may connect.
    attempts: list[str] = []

    def refuse(**kwargs: Any) -> None:
        attempts.append("connect")
        raise psycopg.OperationalError("no connection in this test")

    with monkeypatch.context() as patched:
        patched.setattr(psycopg, "connect", refuse)
        results = {
            name: run_migrate({**record_db.migrate_env(), variable: text}) for name, text in MALFORMED
        }
    assert results == {name: ("failed config", 1) for name, _ in MALFORMED}
    assert attempts == []
    assert not schema_exists(record_db)


@pytest.mark.parametrize("version", [150007, 159999])
def test_migrate_fails_at_config_for_a_libpq_below_16(
    record_db: RecordDatabase, monkeypatch: pytest.MonkeyPatch, version: int
) -> None:
    monkeypatch.setattr(connection, "libpq_version", lambda: version)
    assert run_migrate(record_db.migrate_env()) == ("failed config", 1)
    assert not schema_exists(record_db)


def test_migrate_fails_at_owner_for_a_superuser(record_db: RecordDatabase) -> None:
    with record_db.admin() as admin:
        admin.execute(sql.SQL("ALTER ROLE {} SUPERUSER").format(sql.Identifier(record_db.owner_role)))
    assert run_migrate(record_db.migrate_env()) == ("failed owner", 1)
    assert not schema_exists(record_db)


def test_migrate_fails_at_owner_for_an_owner_that_does_not_own_the_database(record_db: RecordDatabase) -> None:
    with record_db.admin() as admin:
        admin.execute(sql.SQL("ALTER DATABASE {} OWNER TO CURRENT_USER").format(sql.Identifier(record_db.name)))
    assert run_migrate(record_db.migrate_env()) == ("failed owner", 1)
    assert not schema_exists(record_db)


def test_migrate_fails_at_owner_when_the_owner_cannot_connect(record_db: RecordDatabase) -> None:
    # A role that does not exist fails to connect whatever authentication the test server uses.
    env = {
        **record_db.migrate_env(),
        "AGENT_CORE_RECORD_OWNER_DATABASE": record_db.url(record_db.owner_role + "_missing", "0" * 64),
    }
    assert run_migrate(env) == ("failed owner", 1)
    assert not schema_exists(record_db)


@contextmanager
def another_role(db: RecordDatabase) -> Iterator[str]:
    """A role without LOGIN for one test, dropped at its end with everything it owns."""
    name = f"t_other_{secrets.token_hex(6)}"
    with db.admin() as admin:
        admin.execute(sql.SQL("CREATE ROLE {} NOLOGIN NOSUPERUSER").format(sql.Identifier(name)))
    try:
        yield name
    finally:
        with db.admin() as admin:
            admin.execute(
                sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                    sql.Identifier(db.name), sql.Identifier(db.owner_role)
                )
            )
            # A `role = <name>` setting of the owner role names this role only as text, so it
            # would outlive the role; remove it here rather than rely on the fixture.
            admin.execute(sql.SQL("ALTER ROLE {} RESET role").format(sql.Identifier(db.owner_role)))
            admin.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(name)))
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))


def superuser_owner_acting_as(db: RecordDatabase, other: str) -> None:
    """The owner role becomes a superuser; `other`, no superuser, becomes the database owner.

    A check of `current_user` instead of the login role would pass step `owner` whenever the
    session acts as `other`.
    """
    with db.admin() as admin:
        admin.execute(sql.SQL("ALTER ROLE {} SUPERUSER").format(sql.Identifier(db.owner_role)))
        admin.execute(
            sql.SQL("ALTER DATABASE {} OWNER TO {}").format(sql.Identifier(db.name), sql.Identifier(other))
        )


def test_migrate_checks_the_login_role_not_a_role_set_for_it(record_db: RecordDatabase) -> None:
    with another_role(record_db) as other:
        superuser_owner_acting_as(record_db, other)
        with record_db.admin() as admin:
            admin.execute(
                sql.SQL("ALTER ROLE {} SET role = {}").format(
                    sql.Identifier(record_db.owner_role), sql.Literal(other)
                )
            )
        assert run_migrate(record_db.migrate_env()) == ("failed owner", 1)
        assert not schema_exists(record_db)


def test_migrate_checks_the_login_role_not_a_role_set_through_pgoptions(
    record_db: RecordDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two defences stop this case: the empty `options` keeps PGOPTIONS from the session, and
    # `SET ROLE NONE` with the check of `session_user` would refuse it even if it got through.
    # This test therefore passes without either one alone; the test below proves the empty
    # `options` on its own.
    with another_role(record_db) as other:
        superuser_owner_acting_as(record_db, other)
        # Only around the run, so that the administrator's connections never see the variable.
        with monkeypatch.context() as patched:
            patched.setenv("PGOPTIONS", f"-c role={other}")
            result = run_migrate(record_db.migrate_env())
        assert result == ("failed owner", 1)
        assert not schema_exists(record_db)


def test_pgoptions_does_not_reach_the_session(record_db: RecordDatabase, monkeypatch: pytest.MonkeyPatch) -> None:
    # The only test against the server that depends on the empty `options` of connection.py: a parameter the server
    # does not know ends the connection at login if it reaches the server.
    with monkeypatch.context() as patched:
        patched.setenv("PGOPTIONS", "-c t_no_such_parameter=1")
        result = run_migrate(record_db.migrate_env())
    assert result == ("migrated 1", 0)


def test_migrate_acts_as_the_owner_role_when_a_role_is_set_for_it(record_db: RecordDatabase) -> None:
    # Section 11.4 refuses only an owner role that is a superuser or does not own the database.
    # A `role` setting of an owner role that passes both checks does not fail the run; the run
    # drops it and creates every object as the owner role it connected with.
    with another_role(record_db) as other:
        with record_db.admin() as admin:
            admin.execute(
                sql.SQL("GRANT {} TO {}").format(sql.Identifier(other), sql.Identifier(record_db.owner_role))
            )
            admin.execute(
                sql.SQL("ALTER ROLE {} SET role = {}").format(
                    sql.Identifier(record_db.owner_role), sql.Literal(other)
                )
            )
        migrated(record_db)
        state = catalog(record_db)
        owner = record_db.owner_role
        assert ("record", owner) in state["schemas"]
        assert {row[2] for row in state["relations"]} == {owner}
        assert state["functions"] == [("refuse_change", owner, "plpgsql", "trigger")]


SHADOW = "t_shadow"


def shadow_catalog(db: RecordDatabase, where: str) -> None:
    """A schema with a `pg_roles` that shows no superuser and an `octet_length(bytea)` that is
    always 64, put before `pg_catalog` in the `search_path` of the owner role or of the database.

    A run that resolves a name through such a search path would let a superuser owner pass step
    `owner` and would bind the checks of section 11.3 to the shadow function.
    """
    with db.admin() as admin:
        admin.execute(f"CREATE SCHEMA {SHADOW}")
        admin.execute(f"GRANT USAGE ON SCHEMA {SHADOW} TO PUBLIC")
        admin.execute(
            f"CREATE VIEW {SHADOW}.pg_roles AS"
            " SELECT rolname, false AS rolsuper, oid FROM pg_catalog.pg_roles"
        )
        admin.execute(f"GRANT SELECT ON {SHADOW}.pg_roles TO PUBLIC")
        admin.execute(
            f"CREATE FUNCTION {SHADOW}.octet_length(bytea) RETURNS integer"
            " LANGUAGE sql IMMUTABLE AS 'SELECT 64'"
        )
        target = (
            sql.SQL("ROLE {}").format(sql.Identifier(db.owner_role))
            if where == "role"
            else sql.SQL("DATABASE {}").format(sql.Identifier(db.name))
        )
        admin.execute(
            sql.SQL("ALTER {} SET search_path = {}, pg_catalog").format(target, sql.Identifier(SHADOW))
        )


@pytest.mark.parametrize("where", ["role", "database"])
def test_migrate_checks_the_real_catalog_not_one_on_the_search_path(
    record_db: RecordDatabase, where: str
) -> None:
    shadow_catalog(record_db, where)
    with record_db.admin() as admin:
        admin.execute(sql.SQL("ALTER ROLE {} SUPERUSER").format(sql.Identifier(record_db.owner_role)))
    assert run_migrate(record_db.migrate_env()) == ("failed owner", 1)
    assert not schema_exists(record_db)


@pytest.mark.parametrize("where", ["role", "database"])
def test_the_checks_of_section_11_3_use_pg_catalog_whatever_the_search_path(
    record_db: RecordDatabase, where: str
) -> None:
    shadow_catalog(record_db, where)
    migrated(record_db)
    with connect(record_db.writer_url) as writer:
        with pytest.raises(psycopg.Error) as refused:
            insert_event(writer, signature=bytes(63))
        assert refused.value.sqlstate == "23514"
    with record_db.admin() as admin:
        # No object of the record depends on a function of the shadow schema.
        assert admin.execute(
            "SELECT count(*) FROM pg_catalog.pg_depend d JOIN pg_catalog.pg_proc p ON p.oid = d.refobjid"
            " WHERE d.refclassid = 'pg_catalog.pg_proc'::regclass"
            " AND p.pronamespace = %s::regnamespace",
            (SHADOW,),
        ).fetchone() == (0,)


def test_migrate_fails_for_an_unknown_schema_version(record_db: RecordDatabase) -> None:
    migrated(record_db)
    with record_db.admin() as admin:
        admin.execute("INSERT INTO record.schema_version (version, applied_at) VALUES (2, now())")
    assert run_migrate(record_db.migrate_env()) == ("failed migrate", 1)
    with record_db.admin() as admin:
        assert admin.execute("SELECT version FROM record.schema_version ORDER BY 1").fetchall() == [(1,), (2,)]


def test_migrate_fails_for_a_schema_without_its_version(record_db: RecordDatabase) -> None:
    migrated(record_db)
    with record_db.admin() as admin:
        admin.execute("DELETE FROM record.schema_version")
    assert run_migrate(record_db.migrate_env()) == ("failed migrate", 1)
    with record_db.admin() as admin:
        assert admin.execute("SELECT count(*) FROM record.schema_version").fetchall() == [(0,)]


def test_migrate_fails_for_a_partial_schema_and_changes_nothing(record_db: RecordDatabase) -> None:
    with record_db.admin() as admin:
        admin.execute(
            sql.SQL("CREATE SCHEMA record AUTHORIZATION {}").format(sql.Identifier(record_db.owner_role))
        )
    assert run_migrate(record_db.migrate_env()) == ("failed migrate", 1)
    with record_db.admin() as admin:
        assert admin.execute(
            "SELECT count(*) FROM pg_class WHERE relnamespace = 'record'::regnamespace"
        ).fetchall() == [(0,)]
        assert grants(admin, "database") == set()


def test_migrate_fails_without_the_writer_role_and_changes_nothing(record_db: RecordDatabase) -> None:
    with record_db.admin() as admin:
        admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(record_db.writer_role)))
    assert run_migrate(record_db.migrate_env()) == ("failed migrate", 1)
    assert not schema_exists(record_db)
    with record_db.admin() as admin:
        assert grants(admin, "database") == set()


def alter_writer(db: RecordDatabase, attribute: str) -> None:
    with db.admin() as admin:
        admin.execute(sql.SQL("ALTER ROLE {} {}").format(sql.Identifier(db.writer_role), sql.SQL(attribute)))


@pytest.mark.parametrize("attribute", ["NOLOGIN", "SUPERUSER"])
def test_a_first_run_applies_the_schema_whatever_the_attributes_of_the_writer(
    record_db: RecordDatabase, attribute: str
) -> None:
    # Section 11.4 step 3 applies the schema whenever `record.schema_version` does not exist; the
    # attributes of the writer are checked by the hub's start step `writer` (change 3), not here.
    alter_writer(record_db, attribute)
    migrated(record_db)
    with record_db.admin() as admin:
        assert grants(admin, "database") >= {(record_db.writer_role, "CONNECT", False)}


@pytest.mark.parametrize("attribute", ["NOLOGIN", "SUPERUSER"])
def test_a_second_run_is_unchanged_whatever_the_attributes_of_the_writer(
    record_db: RecordDatabase, attribute: str
) -> None:
    # Section 11.4 step 3 does nothing when the schema holds exactly the known versions.
    migrated(record_db)
    alter_writer(record_db, attribute)
    assert run_migrate(record_db.migrate_env()) == ("unchanged 1", 0)


def test_a_second_run_is_unchanged_without_the_writer_role(record_db: RecordDatabase) -> None:
    migrated(record_db)
    with record_db.admin() as admin:
        admin.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(record_db.writer_role)))
        admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(record_db.writer_role)))
    assert run_migrate(record_db.migrate_env()) == ("unchanged 1", 0)


def test_a_second_run_is_unchanged_even_when_a_table_is_missing(record_db: RecordDatabase) -> None:
    # Step 3 decides only on the versions in `record.schema_version`; the hub's start steps
    # `writer` and `verify` (change 3) find a damaged schema.
    migrated(record_db)
    with record_db.admin() as admin:
        admin.execute("DROP TABLE record.checkpoint")
    assert run_migrate(record_db.migrate_env()) == ("unchanged 1", 0)
    with record_db.admin() as admin:
        assert admin.execute("SELECT to_regclass('record.checkpoint') IS NULL").fetchone() == (True,)


@pytest.mark.parametrize("versions", [[2], [0], [2, 3]], ids=["2", "0", "2 and 3"])
def test_migrate_fails_for_only_unknown_schema_versions(record_db: RecordDatabase, versions: list[int]) -> None:
    # Both an unknown version and the lack of the known one.
    migrated(record_db)
    with record_db.admin() as admin:
        admin.execute("DELETE FROM record.schema_version")
        for version in versions:
            admin.execute(
                "INSERT INTO record.schema_version (version, applied_at) VALUES (%s, now())", (version,)
            )
    assert run_migrate(record_db.migrate_env()) == ("failed migrate", 1)
    with record_db.admin() as admin:
        assert [row[0] for row in admin.execute("SELECT version FROM record.schema_version ORDER BY 1")] == versions


def writer_named_public(db: RecordDatabase) -> dict[str, str]:
    return {**db.migrate_env(), "AGENT_CORE_RECORD_DATABASE": db.url("public", db.writer_password)}


def test_the_writer_named_public_is_refused_at_config_and_grants_nothing(record_db: RecordDatabase) -> None:
    assert run_migrate(writer_named_public(record_db)) == ("failed config", 1)
    assert not schema_exists(record_db)
    with record_db.admin() as admin:
        # The database keeps the ACL the deployment gave it: nothing was revoked or granted.
        assert admin.execute(
            "SELECT datacl IS NULL FROM pg_database WHERE datname = current_database()"
        ).fetchone() == (True,)


def test_the_migration_itself_refuses_the_writer_public(
    record_db: RecordDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Second line of defence: even with the name check of the configuration switched off, the
    # migration finds no role `public` in pg_roles and grants nothing to PUBLIC.
    monkeypatch.setattr(connection, "reserved_name", lambda name: False)
    assert run_migrate(writer_named_public(record_db)) == ("failed migrate", 1)
    assert not schema_exists(record_db)
    with record_db.admin() as admin:
        assert admin.execute(
            "SELECT datacl IS NULL FROM pg_database WHERE datname = current_database()"
        ).fetchone() == (True,)


@pytest.mark.parametrize(
    ("database", "owner", "writer"),
    [
        ("record", "record_owner", "public"),
        ("record", "record_owner", "PUBLIC"),
        ("record", "record_owner", "current_user"),
        ("record", "record_owner", "none"),
        ("record", "record_owner", "pg_write_all_data"),
        ("record", "public", "record_writer"),
        ("public", "record_owner", "record_writer"),
        ("record", "record_owner", "record-writer"),
        ("record", "record_owner", 'w" TO PUBLIC; --'),
        ("record", "record_role", "record_role"),
    ],
)
def test_schema_statements_refuse_a_name_that_is_not_accepted(database: str, owner: str, writer: str) -> None:
    with pytest.raises(ConnectionStringError):
        schema_statements(database, owner, writer)


def test_schema_statements_quote_the_names_as_identifiers(record_db: RecordDatabase) -> None:
    statements = schema_statements("record_db", "record_owner", "record_writer")
    with record_db.admin() as admin:
        texts = [statement.as_string(admin) for statement in statements]
    assert texts[:5] == [
        'REVOKE ALL ON DATABASE "record_db" FROM PUBLIC',
        'GRANT CONNECT ON DATABASE "record_db" TO "record_writer"',
        'CREATE SCHEMA record AUTHORIZATION "record_owner"',
        "REVOKE ALL ON SCHEMA record FROM PUBLIC",
        'GRANT USAGE ON SCHEMA record TO "record_writer"',
    ]
    assert texts[-3:-1] == [
        'GRANT SELECT, INSERT ON record.event, record.checkpoint TO "record_writer"',
        'GRANT SELECT ON record.schema_version TO "record_writer"',
    ]


def test_migrate_fails_when_the_lock_is_held_past_the_lock_timeout(
    record_db: RecordDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(migrate, "LOCK_TIMEOUT_MS", 200)
    with record_db.admin(autocommit=False) as holder:
        holder.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATE_LOCK,))
        assert run_migrate(record_db.migrate_env()) == ("failed migrate", 1)
        holder.rollback()
    assert not schema_exists(record_db)


def test_pghostaddr_does_not_redirect_the_connection(
    record_db: RecordDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 192.0.2.1 is a documentation address (RFC 5737) that no test server listens on.
    monkeypatch.setenv("PGHOSTADDR", "192.0.2.1")
    migrated(record_db)


# --- PUBLIC and other roles (section 11.2) ------------------------------------------------------

# Every privilege of PUBLIC, including the defaults of PostgreSQL that an ACL of NULL stands for.
_PUBLIC_ACL = """
SELECT a.privilege_type
FROM {catalog} o CROSS JOIN LATERAL aclexplode(coalesce(o.{column}, acldefault('{kind}', o.{owner}))) a
WHERE {where} AND a.grantee = 0
"""
PUBLIC_ACL_QUERIES = {
    "database": _PUBLIC_ACL.format(
        catalog="pg_database", column="datacl", kind="d", owner="datdba", where="o.datname = current_database()"
    ),
    "schema": _PUBLIC_ACL.format(
        catalog="pg_namespace", column="nspacl", kind="n", owner="nspowner", where="o.nspname = 'record'"
    ),
    "function": _PUBLIC_ACL.format(
        catalog="pg_proc",
        column="proacl",
        kind="f",
        owner="proowner",
        where="o.oid = 'record.refuse_change()'::regprocedure",
    ),
    **{
        table: _PUBLIC_ACL.format(
            catalog="pg_class", column="relacl", kind="r", owner="relowner", where=f"o.oid = '{table}'::regclass"
        )
        for table in TABLES
    },
}
DATABASE_PRIVILEGES = ("CREATE", "CONNECT", "TEMPORARY")
SCHEMA_PRIVILEGES = ("CREATE", "USAGE")
TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


def privileges_of(admin: psycopg.Connection[Any], role: str) -> list[tuple[str, str]]:
    """Every privilege `role` holds, directly or through PUBLIC, on the database and the record."""
    held: list[tuple[str, str]] = []
    checks: list[tuple[str, str, str, str]] = [
        ("database", "has_database_privilege(%s, current_database(), %s)", "", privilege)
        for privilege in DATABASE_PRIVILEGES
    ]
    checks += [("schema", "has_schema_privilege(%s, 'record', %s)", "", p) for p in SCHEMA_PRIVILEGES]
    checks += [("function", "has_function_privilege(%s, 'record.refuse_change()', %s)", "", "EXECUTE")]
    checks += [(t, "has_table_privilege(%s, %s, %s)", t, p) for t in TABLES for p in TABLE_PRIVILEGES]
    for name, call, table, privilege in checks:
        params = (role, table, privilege) if table else (role, privilege)
        row = admin.execute(f"SELECT {call}", params).fetchone()
        assert row is not None
        if row[0]:
            held.append((name, privilege))
    return held


def test_public_holds_no_privilege_on_the_record_after_the_migration(record_db: RecordDatabase) -> None:
    migrated(record_db)
    with record_db.admin() as admin:
        for name, query in PUBLIC_ACL_QUERIES.items():
            assert admin.execute(query).fetchall() == [], name
        assert privileges_of(admin, "public") == []


def test_a_third_role_can_neither_read_nor_write_the_record(record_db: RecordDatabase) -> None:
    migrated(record_db)
    third = f"t_third_{secrets.token_hex(6)}"
    password = secrets.token_hex(32)
    with record_db.admin() as admin:
        admin.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}"
            ).format(sql.Identifier(third), sql.Literal(password))
        )
    try:
        with record_db.admin() as admin:
            assert privileges_of(admin, third) == []
        # Without CONNECT, which PUBLIC no longer holds, the role cannot even connect.
        with pytest.raises(psycopg.OperationalError):
            connect(record_db.url(third, password)).close()
        # Even with CONNECT given by hand, it can neither read nor write a table of the record.
        with record_db.admin() as admin:
            admin.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(record_db.name), sql.Identifier(third)
                )
            )
        with connect(record_db.url(third, password)) as other:
            attempts: list[Callable[[], object]] = [
                lambda table=table: other.execute(f"SELECT count(*) FROM {table}") for table in TABLES
            ]
            attempts += [
                lambda: insert_event(other),
                lambda: insert_checkpoint(other),
                lambda: other.execute(
                    "INSERT INTO record.schema_version (version, applied_at) VALUES (2, now())"
                ),
            ]
            for attempt in attempts:
                with pytest.raises(psycopg.Error) as refused:
                    attempt()
                assert refused.value.sqlstate == "42501"
        with record_db.admin() as admin:
            counts = [admin.execute(f"SELECT count(*) FROM {table}").fetchone() for table in TABLES]
        assert counts == [(1,), (0,), (0,)]
    finally:
        with record_db.admin() as admin:
            admin.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(third)))
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(third)))


# --- the no-delete rule (section 11.2) ----------------------------------------------------------

CHANGES = {
    "UPDATE": "UPDATE {table} SET position = position",
    "DELETE": "DELETE FROM {table}",
    "TRUNCATE": "TRUNCATE {table}",
}


@pytest.mark.parametrize("table", ["record.event", "record.checkpoint"])
@pytest.mark.parametrize("statement", sorted(CHANGES))
def test_the_writer_gets_42501_for_a_change(record_db: RecordDatabase, table: str, statement: str) -> None:
    migrated(record_db)
    with connect(record_db.writer_url) as writer:
        insert_event(writer)
        insert_checkpoint(writer)
        with pytest.raises(psycopg.Error) as refused:
            writer.execute(CHANGES[statement].format(table=table))
        assert refused.value.sqlstate == "42501"
        assert writer.execute(f"SELECT count(*) FROM {table}").fetchall() == [(1,)]


@pytest.mark.parametrize("table", ["record.event", "record.checkpoint"])
@pytest.mark.parametrize("statement", sorted(CHANGES))
def test_the_owner_gets_p0001_from_the_trigger(record_db: RecordDatabase, table: str, statement: str) -> None:
    migrated(record_db)
    with connect(record_db.writer_url) as writer:
        insert_event(writer)
        insert_checkpoint(writer)
    with connect(record_db.owner_url) as owner:
        with pytest.raises(psycopg.Error) as refused:
            owner.execute(CHANGES[statement].format(table=table))
        assert refused.value.sqlstate == "P0001"
        assert owner.execute(f"SELECT count(*) FROM {table}").fetchall() == [(1,)]


def test_the_owner_gets_p0001_for_a_truncate_of_an_empty_table(record_db: RecordDatabase) -> None:
    migrated(record_db)
    with connect(record_db.owner_url) as owner:
        for table in ("record.event", "record.checkpoint"):
            with pytest.raises(psycopg.Error) as refused:
                owner.execute(f"TRUNCATE {table}")
            assert refused.value.sqlstate == "P0001"


@pytest.mark.parametrize("table", ["record.event", "record.checkpoint"])
@pytest.mark.parametrize("statement", sorted(CHANGES))
def test_the_trigger_refuses_a_change_also_with_session_replication_role_replica(
    record_db: RecordDatabase, table: str, statement: str
) -> None:
    # Section 11.2: the triggers are enabled ALWAYS, so they fire also in a superuser session whose
    # `session_replication_role` is `replica`.
    migrated(record_db)
    with connect(record_db.writer_url) as writer:
        insert_event(writer)
        insert_checkpoint(writer)
    with record_db.admin() as admin:
        admin.execute("SET session_replication_role = replica")
        with pytest.raises(psycopg.Error) as refused:
            admin.execute(CHANGES[statement].format(table=table))
        assert refused.value.sqlstate == "P0001"
        assert admin.execute(f"SELECT count(*) FROM {table}").fetchall() == [(1,)]


def test_migrate_leaves_public_no_right_after_default_privileges_to_public(record_db: RecordDatabase) -> None:
    with record_db.admin() as admin:
        admin.execute(
            sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} GRANT ALL ON TABLES TO PUBLIC").format(
                sql.Identifier(record_db.owner_role)
            )
        )
    migrated(record_db)
    with record_db.admin() as admin:
        for table in TABLES:
            assert admin.execute(PUBLIC_ACL_QUERIES[table]).fetchall() == [], table
        assert privileges_of(admin, "public") == []


def test_the_writer_inserts_and_reads(record_db: RecordDatabase) -> None:
    migrated(record_db)
    with connect(record_db.writer_url) as writer:
        assert writer.execute("SELECT version FROM record.schema_version").fetchall() == [(1,)]
        insert_event(writer, 1, record_hash="a" * 64)
        insert_checkpoint(writer, 1, record_hash="a" * 64)
        assert writer.execute("SELECT position, record_hash FROM record.event").fetchall() == [(1, "a" * 64)]
        assert writer.execute("SELECT hub_key_id, position FROM record.checkpoint").fetchall() == [("hub-1", 1)]


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO record.schema_version (version, applied_at) VALUES (2, now())",
        "CREATE TABLE record.extra (id integer)",
        "CREATE TABLE public.extra (id integer)",
        "CREATE TEMPORARY TABLE extra (id integer)",
        "CREATE SCHEMA extra",
    ],
)
def test_the_writer_has_no_further_right(record_db: RecordDatabase, statement: str) -> None:
    migrated(record_db)
    with connect(record_db.writer_url) as writer:
        with pytest.raises(psycopg.Error) as refused:
            writer.execute(statement)
        assert refused.value.sqlstate == "42501"


# --- constraints (section 11.3) -----------------------------------------------------------------


@pytest.mark.parametrize("prev_hash", ["-", "b" * 64])
def test_a_fork_of_a_key_chain_is_refused(record_db: RecordDatabase, prev_hash: str) -> None:
    migrated(record_db)
    with connect(record_db.writer_url) as writer:
        insert_event(writer, 1, prev_hash=prev_hash)
        insert_event(writer, 2, actor="example-other-laptop-1", prev_hash=prev_hash)
        with pytest.raises(psycopg.Error) as refused:
            insert_event(writer, 3, prev_hash=prev_hash)
        assert refused.value.sqlstate == "23505"
        assert writer.execute("SELECT count(*) FROM record.event").fetchall() == [(2,)]


@pytest.mark.parametrize("time_zone", ["UTC", "Asia/Kathmandu", "America/St_Johns"])
def test_a_fraction_of_a_second_is_refused_in_any_time_zone(record_db: RecordDatabase, time_zone: str) -> None:
    migrated(record_db)
    with connect(record_db.writer_url) as writer:
        writer.execute(sql.SQL("SET TIME ZONE {}").format(sql.Literal(time_zone)))
        for value in ("2026-11-17 10:00:00.5", "2026-11-17T10:00:00.000001Z"):
            with pytest.raises(psycopg.Error) as refused:
                insert_event(writer, received_at=value)
            assert refused.value.sqlstate == "23514"
            with pytest.raises(psycopg.Error) as refused:
                insert_checkpoint(writer, published_at=value)
            assert refused.value.sqlstate == "23514"
        # A whole second in the session's own time zone is a whole second in UTC.
        insert_event(writer, received_at="2026-11-17 10:00:00")
        insert_checkpoint(writer, published_at="2026-11-17 10:00:00")


EVENT_FAULTS = {
    "position 0": {"position": 0},
    "position -1": {"position": -1},
    "prev_hash of 63 characters": {"prev_hash": "a" * 63},
    "prev_hash in uppercase": {"prev_hash": "A" * 64},
    "event_hash in uppercase": {"event_hash": "A" * 64},
    "event_hash -": {"event_hash": "-"},
    "record_hash of 65 characters": {"record_hash": "a" * 65},
    "empty event_text": {"text": b""},
    "event_text of 65537 bytes": {"text": b"x" * 65537},
    "signature of 63 bytes": {"signature": bytes(63)},
    "signature of 65 bytes": {"signature": bytes(65)},
}


@pytest.mark.parametrize("fault", sorted(EVENT_FAULTS))
def test_an_event_row_outside_its_checks_is_refused(record_db: RecordDatabase, fault: str) -> None:
    migrated(record_db)
    with connect(record_db.writer_url) as writer:
        with pytest.raises(psycopg.Error) as refused:
            insert_event(writer, **EVENT_FAULTS[fault])
        assert refused.value.sqlstate == "23514"


def test_an_event_text_of_65536_bytes_is_stored(record_db: RecordDatabase) -> None:
    migrated(record_db)
    with connect(record_db.writer_url) as writer:
        insert_event(writer, text=b"x" * 65536)


CHECKPOINT_FAULTS = {
    "position 0": {"position": 0},
    "record_hash in uppercase": {"record_hash": "A" * 64},
    "signature of 63 bytes": {"signature": bytes(63)},
}


@pytest.mark.parametrize("fault", sorted(CHECKPOINT_FAULTS))
def test_a_checkpoint_row_outside_its_checks_is_refused(record_db: RecordDatabase, fault: str) -> None:
    migrated(record_db)
    with connect(record_db.writer_url) as writer:
        with pytest.raises(psycopg.Error) as refused:
            insert_checkpoint(writer, **CHECKPOINT_FAULTS[fault])
        assert refused.value.sqlstate == "23514"


@pytest.mark.parametrize("column", ["position", "event_id", "event_hash", "record_hash"])
def test_a_unique_column_of_an_event_is_refused_twice(record_db: RecordDatabase, column: str) -> None:
    migrated(record_db)
    shared = {
        "position": {},
        "event_id": {"event_id": str(uuid.uuid4())},
        "event_hash": {"event_hash": "c" * 64},
        "record_hash": {"record_hash": "d" * 64},
    }[column]
    with connect(record_db.writer_url) as writer:
        insert_event(writer, 1, actor="example-one-laptop-1", **shared)
        with pytest.raises(psycopg.Error) as refused:
            insert_event(writer, 1 if column == "position" else 2, actor="example-two-laptop-1", **shared)
        assert refused.value.sqlstate == "23505"


def test_a_checkpoint_pair_is_refused_twice(record_db: RecordDatabase) -> None:
    migrated(record_db)
    with connect(record_db.writer_url) as writer:
        insert_checkpoint(writer, 5, hub_key_id="hub-1")
        insert_checkpoint(writer, 5, hub_key_id="hub-2")
        with pytest.raises(psycopg.Error) as refused:
            insert_checkpoint(writer, 5, hub_key_id="hub-1")
        assert refused.value.sqlstate == "23505"


# --- the command --------------------------------------------------------------------------------


def run_cli(args: list[str], extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = {name: value for name, value in os.environ.items() if not name.startswith("AGENT_CORE_RECORD")}
    env.update(extra_env)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(SRC), os.environ.get("PYTHONPATH")]))
    return subprocess.run(
        [sys.executable, "-m", "agent_core.record", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_the_command_prints_one_line(record_db: RecordDatabase) -> None:
    first = run_cli(["migrate"], record_db.migrate_env())
    assert (first.stdout, first.stderr, first.returncode) == ("migrated 1\n", "", 0)
    second = run_cli(["migrate"], record_db.migrate_env())
    assert (second.stdout, second.stderr, second.returncode) == ("unchanged 1\n", "", 0)
    assert_no_secret(record_db, first.stdout, first.stderr, second.stdout, second.stderr)


def test_the_command_prints_failed_and_no_secret(record_db: RecordDatabase) -> None:
    with record_db.admin() as admin:
        admin.execute(sql.SQL("ALTER ROLE {} SUPERUSER").format(sql.Identifier(record_db.owner_role)))
    owner = run_cli(["migrate"], record_db.migrate_env())
    assert (owner.stdout, owner.stderr, owner.returncode) == ("failed owner\n", "", 1)
    wrong = run_cli(
        ["migrate"],
        {**record_db.migrate_env(), "AGENT_CORE_RECORD_DATABASE": record_db.writer_url + "&x=1"},
    )
    assert (wrong.stdout, wrong.stderr, wrong.returncode) == ("failed config\n", "", 1)
    assert_no_secret(record_db, owner.stdout, owner.stderr, wrong.stdout, wrong.stderr)


def test_the_command_prints_no_part_of_a_database_error(record_db: RecordDatabase) -> None:
    # A login the server refuses: its message names the role, which the line must not contain.
    missing_role = record_db.owner_role + "_missing"
    refused = run_cli(
        ["migrate"],
        {
            **record_db.migrate_env(),
            "AGENT_CORE_RECORD_OWNER_DATABASE": record_db.url(missing_role, record_db.owner_password),
        },
    )
    assert (refused.stdout, refused.stderr, refused.returncode) == ("failed owner\n", "", 1)
    # A statement of section 11.3 that the server refuses inside step `migrate`: without CREATE on
    # its database the owner cannot create the schema, and the server's message names the
    # database.
    with record_db.admin() as admin:
        admin.execute(
            sql.SQL("REVOKE CREATE ON DATABASE {} FROM {}").format(
                sql.Identifier(record_db.name), sql.Identifier(record_db.owner_role)
            )
        )
    failed = run_cli(["migrate"], record_db.migrate_env())
    assert (failed.stdout, failed.stderr, failed.returncode) == ("failed migrate\n", "", 1)
    assert not schema_exists(record_db)
    outputs = (refused.stdout, refused.stderr, failed.stdout, failed.stderr)
    assert_no_secret(record_db, *outputs)
    for output in outputs:
        for value in (missing_role, record_db.owner_role, record_db.writer_role, record_db.name):
            assert value not in output


def test_the_command_without_variables() -> None:
    result = run_cli(["migrate"], {})
    assert (result.stdout, result.stderr, result.returncode) == ("failed config\n", "", 1)


@pytest.mark.parametrize("args", [[], ["Export"], ["Migrate"], ["-h"]])
def test_an_unknown_command_prints_the_usage(args: list[str]) -> None:
    result = run_cli(args, {})
    assert (result.stdout, result.stderr, result.returncode) == ("", USAGE + "\n", 2)


def test_the_command_refuses_an_argument_of_migrate(record_db: RecordDatabase) -> None:
    result = run_cli(["migrate", "--force"], record_db.migrate_env())
    assert (result.stdout, result.stderr, result.returncode) == ("failed config\n", "", 1)
    assert not schema_exists(record_db)


# The command's entry in process (`main` and the `__main__` guard) is tested in test_record_main.py.
