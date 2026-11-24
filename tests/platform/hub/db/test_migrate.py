"""U-HUB-migrate and MIG-01 (hub): `agent-hub migrate` (02-data-model.md section 6).

Applies on an empty database (the schema before 0001), `unchanged` on re-run, forward migration on top of an applied
schema, checksum mismatch, refusal of a superuser, of the writer role and of a role that does not own the database,
the lock timeout, and output of exactly one line without configuration values or database error text.
"""

from __future__ import annotations

import hashlib
import shutil
import threading
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql

from agent_hub.db import migrate
from agent_hub.db.migrate import (
    MIGRATE_LOCK,
    OWNER_VARIABLE,
    WRITER_VARIABLE,
    Migration,
    MigrationChecksumMismatch,
    MigrationFileError,
    load_migrations,
    pending,
    run_migrate,
)
from tests.platform.hub.conftest import HubDatabase

MIGRATIONS = Path(migrate.__file__).with_name("migrations")
COUNT = len(load_migrations(MIGRATIONS))
"""The number of migrations the package ships; test files are numbered after them."""


def _name(offset: int, name: str) -> str:
    """The file name of a test migration `offset` places after the last package migration."""
    return f"{COUNT + offset:04d}_{name}.sql"


def _copy_migrations(target: Path) -> Path:
    target.mkdir()
    for path in MIGRATIONS.glob("*.sql"):
        shutil.copy(path, target / path.name)
    return target


def _versions(db: HubDatabase) -> list[tuple[Any, ...]]:
    with db.admin() as admin:
        return admin.execute("SELECT version, name, checksum FROM hub.schema_version ORDER BY version").fetchall()


def _schema_exists(db: HubDatabase) -> bool:
    with db.admin() as admin:
        row = admin.execute("SELECT count(*) FROM pg_namespace WHERE nspname = 'hub'").fetchone()
    return row == (1,)


# --- files -----------------------------------------------------------------------------------------------------------


def test_the_package_migrations_are_a_gapless_series() -> None:
    migrations = load_migrations()
    assert [m.version for m in migrations] == list(range(1, len(migrations) + 1))
    first = migrations[0]
    assert (first.version, first.name) == (1, "hub")
    assert first.checksum == hashlib.sha256((MIGRATIONS / "0001_hub.sql").read_bytes()).hexdigest()


def test_non_sql_files_are_ignored(tmp_path: Path) -> None:
    directory = _copy_migrations(tmp_path / "m")
    (directory / "README.md").write_text("notes")
    assert load_migrations(directory) == load_migrations(MIGRATIONS)


@pytest.mark.parametrize("name", ["1_hub.sql", "0002-hub.sql", "0002_Hub.sql", "0002_.sql"])
def test_a_badly_named_file_is_refused(tmp_path: Path, name: str) -> None:
    directory = _copy_migrations(tmp_path / "m")
    (directory / name).write_text("SELECT 1;")
    with pytest.raises(MigrationFileError):
        load_migrations(directory)


def test_a_gap_in_the_numbering_is_refused(tmp_path: Path) -> None:
    directory = _copy_migrations(tmp_path / "m")
    (directory / _name(2, "late")).write_text("SELECT 1;")
    with pytest.raises(MigrationFileError):
        load_migrations(directory)


def test_pending_refuses_unknown_or_changed_versions() -> None:
    files = [Migration(1, "hub", "", "a" * 64), Migration(2, "next", "", "b" * 64)]
    assert pending(files, []) == files
    assert pending(files, [(1, "hub", "a" * 64)]) == files[1:]
    assert pending(files, [(2, "next", "b" * 64), (1, "hub", "a" * 64)]) == []
    for applied in (
        [(1, "hub", "c" * 64)],
        [(1, "other", "a" * 64)],
        [(2, "next", "b" * 64)],
        [(1, "hub", "a" * 64), (2, "next", "b" * 64), (3, "x", "d" * 64)],
    ):
        with pytest.raises(MigrationChecksumMismatch) as caught:
            pending(files, applied)
        assert caught.value.code == "migration_checksum_mismatch"


# --- MIG-01 ----------------------------------------------------------------------------------------------------------


def test_applies_on_an_empty_database_and_is_unchanged_on_rerun(hub_db: HubDatabase) -> None:
    assert run_migrate(hub_db.migrate_env()) == (f"applied {COUNT}", 0)
    checksum = hashlib.sha256((MIGRATIONS / "0001_hub.sql").read_bytes()).hexdigest()
    assert _versions(hub_db)[0] == (1, "hub", checksum)
    assert [row[0] for row in _versions(hub_db)] == list(range(1, COUNT + 1))
    assert run_migrate(hub_db.migrate_env()) == (f"unchanged {COUNT}", 0)
    assert len(_versions(hub_db)) == COUNT


def test_the_schema_is_owned_by_the_owner_role(migrated_db: HubDatabase) -> None:
    with migrated_db.admin() as admin:
        owners = admin.execute(
            "SELECT DISTINCT pg_get_userbyid(c.relowner) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'hub' UNION SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'hub'"
        ).fetchall()
    assert owners == [(migrated_db.owner_role,)]


def test_a_later_migration_applies_on_the_previous_schema_with_data(
    migrated_db: HubDatabase, writer: psycopg.Connection[Any], tmp_path: Path
) -> None:
    writer.execute(
        "INSERT INTO hub.principal (id, kind, created_at) VALUES (%s, 'user', now())",
        ("0190f0f0-0000-7000-8000-000000000001",),
    )
    directory = _copy_migrations(tmp_path / "m")
    (directory / _name(1, "note")).write_text(
        "ALTER TABLE hub.principal ADD COLUMN note_seq integer NULL;\n"
        "GRANT SELECT ON hub.schema_version TO {writer};\n"
        "SELECT '{{}}'::jsonb, 10 % 3;\n"
    )
    assert run_migrate(migrated_db.migrate_env(), directory=directory) == (f"applied {COUNT + 1}", 0)
    assert _versions(migrated_db)[-1][:2] == (COUNT + 1, "note")
    assert writer.execute("SELECT count(*), max(note_seq) FROM hub.principal").fetchone() == (1, None)
    assert run_migrate(migrated_db.migrate_env(), directory=directory) == (f"unchanged {COUNT + 1}", 0)
    # The package itself knows only its own versions: a database ahead of the code is refused.
    assert run_migrate(migrated_db.migrate_env()) == ("failed checksum", 1)


def test_a_failing_migration_rolls_back_every_pending_file(hub_db: HubDatabase, tmp_path: Path) -> None:
    directory = _copy_migrations(tmp_path / "m")
    (directory / _name(1, "broken")).write_text("SELECT 1 / 0;\n")
    assert run_migrate(hub_db.migrate_env(), directory=directory) == ("failed migrate", 1)
    assert not _schema_exists(hub_db)


def test_a_changed_applied_file_is_a_checksum_mismatch(migrated_db: HubDatabase, tmp_path: Path) -> None:
    directory = _copy_migrations(tmp_path / "m")
    with (directory / "0001_hub.sql").open("a") as handle:
        handle.write("\n-- edited after it was applied\n")
    assert run_migrate(migrated_db.migrate_env(), directory=directory) == ("failed checksum", 1)


def test_a_changed_checksum_row_is_a_checksum_mismatch(migrated_db: HubDatabase) -> None:
    with migrated_db.admin() as admin:
        admin.execute("UPDATE hub.schema_version SET checksum = %s", ("0" * 64,))
    assert run_migrate(migrated_db.migrate_env()) == ("failed checksum", 1)


def test_bad_files_fail_step_files(hub_db: HubDatabase, tmp_path: Path) -> None:
    directory = _copy_migrations(tmp_path / "m")
    (directory / _name(1, "latin")).write_bytes(b"-- caf\xe9\n")
    assert run_migrate(hub_db.migrate_env(), directory=directory) == ("failed files", 1)
    (directory / _name(1, "latin")).unlink()
    (directory / _name(3, "gap")).write_text("SELECT 1;")
    assert run_migrate(hub_db.migrate_env(), directory=directory) == ("failed files", 1)
    assert not _schema_exists(hub_db)


# --- configuration and roles -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        {OWNER_VARIABLE: None},
        {WRITER_VARIABLE: None},
        {OWNER_VARIABLE: ""},
        {OWNER_VARIABLE: "postgresql://owner@localhost/db"},
        {WRITER_VARIABLE: "host=localhost dbname=db"},
    ],
)
def test_missing_or_malformed_configuration_fails_step_config(
    hub_db: HubDatabase, change: dict[str, str | None]
) -> None:
    environ = {**hub_db.migrate_env(), **change}
    assert run_migrate({k: v for k, v in environ.items() if v is not None}) == ("failed config", 1)


def test_the_writer_role_as_owner_is_refused(hub_db: HubDatabase) -> None:
    same = {OWNER_VARIABLE: hub_db.writer_url, WRITER_VARIABLE: hub_db.writer_url}
    assert run_migrate(same) == ("failed config", 1)
    # The writer's credentials as owner with another role as writer: the writer does not own the database.
    swapped = {OWNER_VARIABLE: hub_db.writer_url, WRITER_VARIABLE: hub_db.owner_url}
    assert run_migrate(swapped) == ("failed owner", 1)
    assert not _schema_exists(hub_db)


def test_owner_and_writer_must_name_the_same_database(hub_db: HubDatabase) -> None:
    other = hub_db.url(hub_db.writer_role, hub_db.writer_password, database="other_db")
    assert run_migrate({OWNER_VARIABLE: hub_db.owner_url, WRITER_VARIABLE: other}) == ("failed config", 1)


def test_an_old_libpq_fails_step_config(hub_db: HubDatabase, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(migrate, "libpq_supported", lambda: False)
    assert run_migrate(hub_db.migrate_env()) == ("failed config", 1)


def test_a_superuser_owner_is_refused(hub_db: HubDatabase) -> None:
    with psycopg.connect(**hub_db.admin_params, autocommit=True) as admin:
        admin.execute(sql.SQL("ALTER ROLE {} SUPERUSER").format(sql.Identifier(hub_db.owner_role)))
    assert run_migrate(hub_db.migrate_env()) == ("failed owner", 1)
    assert not _schema_exists(hub_db)


def test_a_failed_connection_fails_step_owner(hub_db: HubDatabase) -> None:
    # The test server trusts loopback logins, so an unknown role is what makes the connection fail.
    wrong = hub_db.url("t_hub_nobody", "f" * 64)
    assert run_migrate({OWNER_VARIABLE: wrong, WRITER_VARIABLE: hub_db.writer_url}) == ("failed owner", 1)


def test_a_missing_writer_role_fails_step_migrate(hub_db: HubDatabase) -> None:
    with psycopg.connect(**hub_db.admin_params, autocommit=True) as admin:
        admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(hub_db.writer_role)))
    assert run_migrate(hub_db.migrate_env()) == ("failed migrate", 1)
    assert not _schema_exists(hub_db)


def test_role_and_search_path_settings_of_the_owner_are_ignored(hub_db: HubDatabase) -> None:
    with psycopg.connect(**hub_db.admin_params, autocommit=True) as admin:
        owner = sql.Identifier(hub_db.owner_role)
        writer = sql.Identifier(hub_db.writer_role)
        admin.execute(sql.SQL("GRANT {} TO {}").format(writer, owner))
        admin.execute(sql.SQL("ALTER ROLE {} SET role = {}").format(owner, sql.Literal(hub_db.writer_role)))
        admin.execute(sql.SQL("ALTER ROLE {} SET search_path = public").format(owner))
        admin.execute(
            sql.SQL("ALTER DATABASE {} SET default_transaction_isolation = 'serializable'").format(
                sql.Identifier(hub_db.name)
            )
        )
    assert run_migrate(hub_db.migrate_env()) == (f"applied {COUNT}", 0)
    with hub_db.admin() as admin:
        schema_owner = admin.execute("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'hub'")
        assert schema_owner.fetchone() == (hub_db.owner_role,)


# --- lock --------------------------------------------------------------------------------------------------------


def test_the_lock_timeout_fails_step_lock(hub_db: HubDatabase) -> None:
    with psycopg.connect(**{**hub_db.admin_params, "dbname": hub_db.name}, autocommit=True) as holder:
        holder.execute("SELECT pg_advisory_lock(%s)", (MIGRATE_LOCK,))
        assert run_migrate(hub_db.migrate_env(), lock_timeout_ms=200) == ("failed lock", 1)
    assert not _schema_exists(hub_db)
    assert run_migrate(hub_db.migrate_env(), lock_timeout_ms=200) == (f"applied {COUNT}", 0)


def test_the_default_lock_timeout_is_sixty_seconds() -> None:
    assert migrate.LOCK_TIMEOUT_MS == 60000
    assert MIGRATE_LOCK == 742180100


def test_concurrent_runs_apply_once(hub_db: HubDatabase) -> None:
    results: list[tuple[str, int]] = []
    barrier = threading.Barrier(3)

    def run() -> None:
        barrier.wait()
        results.append(run_migrate(hub_db.migrate_env()))

    threads = [threading.Thread(target=run) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [(f"applied {COUNT}", 0), (f"unchanged {COUNT}", 0), (f"unchanged {COUNT}", 0)]
    assert len(_versions(hub_db)) == COUNT


def test_output_never_contains_secrets(hub_db: HubDatabase, tmp_path: Path) -> None:
    directory = _copy_migrations(tmp_path / "m")
    (directory / _name(1, "broken")).write_text(f"SELECT '{hub_db.owner_password}'::integer;\n")
    lines = [
        run_migrate(hub_db.migrate_env(), directory=directory)[0],
        run_migrate({**hub_db.migrate_env(), WRITER_VARIABLE: hub_db.owner_url})[0],
        run_migrate(hub_db.migrate_env())[0],
    ]
    assert lines == ["failed migrate", "failed config", f"applied {COUNT}"]
    for line in lines:
        for secret in hub_db.secret_values():
            assert secret not in line
