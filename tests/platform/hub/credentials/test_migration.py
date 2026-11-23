"""MIG-01 for migration 0003 (ADR-035): it applies on the schema of 0002 with data and adds the credential-change
notices, which the writer may insert, update and delete."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from agent_hub.db import migrate
from agent_hub.db.migrate import load_migrations, run_migrate
from tests.platform.hub.conftest import HubDatabase

MIGRATIONS = Path(migrate.__file__).with_name("migrations")
OWNER = "0190f0f0-0000-7000-8000-000000000001"
DEVICE = "0190f0f0-0000-7000-8000-000000000002"
CREDENTIAL = "0190f0f0-0000-7000-8000-000000000003"
AT = datetime(2026, 11, 22, 10, 0, tzinfo=UTC)


def test_0003_applies_on_the_previous_schema_with_data(hub_db: HubDatabase, tmp_path: Path) -> None:
    previous = tmp_path / "previous"
    previous.mkdir()
    for name in ("0001_hub.sql", "0002_outbox_receipt.sql"):
        shutil.copy(MIGRATIONS / name, previous / name)
    assert run_migrate(hub_db.migrate_env(), directory=previous) == ("applied 2", 0)
    with hub_db.connect(hub_db.writer_role, autocommit=True) as writer:
        writer.execute(
            "INSERT INTO hub.credential (id, owner_id, kind, version, epoch, header_text, ct, sig, updated_at)"
            " VALUES (%s, %s, 'github', 1, 1, '{}', %s, %s, %s)",
            (CREDENTIAL, OWNER, bytes(28), bytes(64), AT),
        )
    assert run_migrate(hub_db.migrate_env()) == (f"applied {len(load_migrations())}", 0)
    with hub_db.connect(hub_db.writer_role, autocommit=True) as writer:
        assert writer.execute("SELECT kind, version FROM hub.credential").fetchall() == [("github", 1)]
        writer.execute(
            "INSERT INTO hub.credential_notice (device_id, owner_id, kind, changed_at) VALUES (%s, %s, 'github', %s)",
            (DEVICE, OWNER, AT),
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            writer.execute(
                "INSERT INTO hub.credential_notice (device_id, owner_id, kind, changed_at) VALUES (%s, %s, 'ssh', %s)",
                (DEVICE, OWNER, AT),
            )
        writer.execute("UPDATE hub.credential_notice SET changed_at = %s", (AT,))
        with pytest.raises(psycopg.errors.UniqueViolation):
            writer.execute(
                "INSERT INTO hub.credential_notice (device_id, owner_id, kind, changed_at)"
                " VALUES (%s, %s, 'github', %s)",
                (DEVICE, OWNER, AT),
            )
        writer.execute("DELETE FROM hub.credential_notice")
    assert run_migrate(hub_db.migrate_env()) == (f"unchanged {len(load_migrations())}", 0)
