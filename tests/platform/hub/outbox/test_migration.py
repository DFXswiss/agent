"""MIG-01 for migration 0002 (ADR-030): it applies on the schema of 0001 with data, adds the outbox receipts and makes
`quota_snapshot.resets_at` nullable without touching existing rows."""

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
AT = datetime(2026, 11, 22, 10, 0, tzinfo=UTC)


def test_0002_applies_on_the_previous_schema_with_data(hub_db: HubDatabase, tmp_path: Path) -> None:
    previous = tmp_path / "previous"
    previous.mkdir()
    shutil.copy(MIGRATIONS / "0001_hub.sql", previous / "0001_hub.sql")
    assert run_migrate(hub_db.migrate_env(), directory=previous) == ("applied 1", 0)
    with hub_db.connect(hub_db.writer_role, autocommit=True) as writer:
        writer.execute(
            'INSERT INTO hub.quota_snapshot (owner_id, "window", used_percent, resets_at, severity, fetched_at,'
            " device_id) VALUES (%s, 'five_hour', 12.5, %s, NULL, %s, %s)",
            (OWNER, AT, AT, DEVICE),
        )
        writer.execute("INSERT INTO hub.device_stream (device_id, last_origin_seq) VALUES (%s, 7)", (DEVICE,))
    assert run_migrate(hub_db.migrate_env()) == (f"applied {len(load_migrations())}", 0)
    with hub_db.connect(hub_db.writer_role, autocommit=True) as writer:
        assert writer.execute("SELECT used_percent, resets_at FROM hub.quota_snapshot").fetchall() == [(12.5, AT)]
        assert writer.execute("SELECT last_origin_seq FROM hub.device_stream").fetchall() == [(7,)]
        writer.execute(
            'INSERT INTO hub.quota_snapshot (owner_id, "window", used_percent, resets_at, severity, fetched_at,'
            " device_id) VALUES (%s, 'seven_day', 1, NULL, NULL, %s, %s)",
            (OWNER, AT, DEVICE),
        )
        writer.execute(
            "INSERT INTO hub.outbox_receipt (device_id, origin_seq, item_kind, item_sha256, status, error, received_at)"
            " VALUES (%s, 8, 'usage', %s, 'rejected', 'forbidden', %s)",
            (DEVICE, bytes(32), AT),
        )
        writer.execute("DELETE FROM hub.outbox_receipt WHERE origin_seq = 8")


@pytest.mark.parametrize(
    ("status", "error", "digest", "kind"),
    [
        ("ok", "forbidden", bytes(32), "usage"),
        ("rejected", None, bytes(32), "usage"),
        ("ok", None, bytes(31), "usage"),
        ("ok", None, bytes(32), "credential"),
        ("replayed", None, bytes(32), "usage"),
    ],
)
def test_receipt_constraints(
    migrated_db: HubDatabase, status: str, error: str | None, digest: bytes, kind: str
) -> None:
    with (
        migrated_db.connect(migrated_db.writer_role, autocommit=True) as writer,
        pytest.raises(psycopg.errors.CheckViolation),
    ):
        writer.execute(
            "INSERT INTO hub.outbox_receipt (device_id, origin_seq, item_kind, item_sha256, status, error, received_at)"
            " VALUES (%s, 1, %s, %s, %s, %s, %s)",
            (DEVICE, kind, digest, status, error, AT),
        )
