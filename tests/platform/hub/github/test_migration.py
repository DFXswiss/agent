"""MIG-01 for migration 0022 (ADR-078): it applies on the previous schema with data. Every existing record becomes
primary, user-owned and created at its last change; the new constraints hold (one primary `github` record per user,
one record of every other kind per owner, company records are `github` and never primary); a default goes away with
its record; the new nullable columns start empty."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from agent_hub.db import migrate
from agent_hub.db.migrate import load_migrations, run_migrate
from tests.platform.hub.conftest import HubDatabase

MIGRATIONS = Path(migrate.__file__).with_name("migrations")
NAME = "github_identities"
OWNER = "0190f0f0-0000-7000-8000-000000000001"
ORG = "0190f0f0-0000-7000-8000-000000000002"
RECORDS = [f"0190f0f0-0000-7000-8000-00000000001{index}" for index in range(8)]
AT = datetime(2026, 11, 22, 10, 0, tzinfo=UTC)
LATER = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)


def _insert(writer: psycopg.Connection[Any], record: str, owner: str, kind: str, **columns: Any) -> None:
    names = ["id", "owner_id", "kind", "version", "epoch", "header_text", "ct", "sig", "updated_at", *columns]
    values = [record, owner, kind, 1, 1, "{}", bytes(28), bytes(64), AT, *columns.values()]
    marks = ", ".join(["%s"] * len(values))
    writer.execute(f"INSERT INTO hub.credential ({', '.join(names)}) VALUES ({marks})", values)


def test_the_migration_applies_on_the_previous_schema_with_data(hub_db: HubDatabase, tmp_path: Path) -> None:
    previous = tmp_path / "previous"
    previous.mkdir()
    files = [path for path in sorted(MIGRATIONS.glob("*.sql")) if NAME not in path.name]
    for path in files:
        shutil.copy(path, previous / path.name)
    assert run_migrate(hub_db.migrate_env(), directory=previous) == (f"applied {len(files)}", 0)
    with hub_db.connect(hub_db.writer_role, autocommit=True) as writer:
        _insert(writer, RECORDS[0], OWNER, "github")
        _insert(writer, RECORDS[1], OWNER, "mailbox")
    assert run_migrate(hub_db.migrate_env()) == (f"applied {len(load_migrations())}", 0)
    with hub_db.connect(hub_db.writer_role, autocommit=True) as writer:
        rows = writer.execute(
            "SELECT id::text, owner_kind, is_primary, created_at FROM hub.credential ORDER BY id"
        ).fetchall()
        assert rows == [(RECORDS[0], "user", True, AT), (RECORDS[1], "user", True, AT)]
        for table, column in (
            ("task", "github_identity"),
            ("session", "github_identity"),
            ("operation", "github_identity"),
            ("operation", "triggered_by"),
            ("device_request", "org_id"),
        ):
            found = writer.execute(
                "SELECT is_nullable FROM information_schema.columns WHERE table_schema = 'hub' AND table_name = %s"
                " AND column_name = %s",
                (table, column),
            ).fetchone()
            assert found == ("YES",), (table, column)
        # a further github record of the user that is not primary, and company records of an org
        _insert(writer, RECORDS[2], OWNER, "github", created_at=LATER, is_primary=False)
        _insert(writer, RECORDS[3], ORG, "github", created_at=LATER, is_primary=False, owner_kind="org")
        _insert(writer, RECORDS[4], ORG, "github", created_at=LATER, is_primary=False, owner_kind="org")
        for record, owner, kind, columns, error in (
            (RECORDS[5], OWNER, "github", {"is_primary": True}, psycopg.errors.UniqueViolation),
            (RECORDS[5], OWNER, "mailbox", {"is_primary": True}, psycopg.errors.UniqueViolation),
            (RECORDS[5], ORG, "github", {"is_primary": True, "owner_kind": "org"}, psycopg.errors.CheckViolation),
            (RECORDS[5], ORG, "mailbox", {"is_primary": False, "owner_kind": "org"}, psycopg.errors.CheckViolation),
            (RECORDS[5], OWNER, "github", {"is_primary": False, "owner_kind": "team"}, psycopg.errors.CheckViolation),
            (RECORDS[5], OWNER, "github", {"is_primary": False}, psycopg.errors.NotNullViolation),
        ):
            options = {"created_at": LATER, **columns} if columns != {"is_primary": False} else columns
            with pytest.raises(error):
                _insert(writer, record, owner, kind, **options)
        writer.execute(
            "INSERT INTO hub.github_default (scope, scope_id, identity_id, set_by, set_at)"
            " VALUES ('org', %s, %s, %s, %s)",
            (ORG, RECORDS[3], OWNER, AT),
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            writer.execute(
                "INSERT INTO hub.github_default (scope, scope_id, identity_id, set_by, set_at)"
                " VALUES ('task', %s, %s, %s, %s)",
                (ORG, RECORDS[3], OWNER, AT),
            )
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            writer.execute(
                "INSERT INTO hub.github_default (scope, scope_id, identity_id, set_by, set_at)"
                " VALUES ('wg', %s, %s, %s, %s)",
                (OWNER, RECORDS[7], OWNER, AT),
            )
        writer.execute("DELETE FROM hub.credential WHERE id = %s", (RECORDS[3],))
        assert writer.execute("SELECT count(*) FROM hub.github_default").fetchone() == (0,)
    assert run_migrate(hub_db.migrate_env()) == (f"unchanged {len(load_migrations())}", 0)
