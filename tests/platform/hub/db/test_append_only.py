"""Append-only tables (02-data-model.md section 1.2): UPDATE, DELETE and TRUNCATE fail on every [AO] table.

The owner holds every privilege, so for the owner the trigger `hub.refuse_change()` is what refuses; the writer
additionally lacks the UPDATE, DELETE and TRUNCATE privileges. `ENABLE ALWAYS` keeps the triggers active under
`session_replication_role = replica`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from psycopg import sql

from tests.platform.hub.conftest import HubDatabase
from tests.platform.hub.test_schema import APPEND_ONLY

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
HASH = "a" * 64


def _row(table: str) -> dict[str, Any]:
    """One valid row of an [AO] table."""
    new = uuid.uuid4
    rows: dict[str, dict[str, Any]] = {
        "membership_entry": {
            "org_id": new(),
            "seq": 0,
            "entry_text": "{}",
            "entry_hash": "h",
            "type": "genesis",
            "by_id": new(),
            "subject_id": None,
            "received_at": NOW,
        },
        "platform_entry": {
            "seq": 0,
            "entry_text": "{}",
            "entry_hash": "h",
            "type": "root",
            "subject_id": None,
            "received_at": NOW,
        },
        "event": {
            "scope_id": new(),
            "scope_seq": 1,
            "id": new(),
            "scope": "wg",
            "org_id": new(),
            "wg_id": None,
            "task_id": None,
            "session_id": None,
            "author_id": new(),
            "author_kind": "user",
            "kind": "prompt",
            "ref": "",
            "epoch": 1,
            "ts": NOW,
            "lh": 0,
            "header_text": "{}",
            "ct": b"ct",
            "sig": bytes(64),
            "size": 2,
            "origin_device_id": None,
            "origin_seq": None,
            "received_at": NOW,
        },
        "task_transition": {
            "task_id": new(),
            "seq": 1,
            "state": "scheduling",
            "reason": None,
            "at": NOW,
            "by_id": None,
        },
        "session_transition": {
            "session_id": new(),
            "seq": 1,
            "state": "requested",
            "reason": None,
            "at": NOW,
            "received_at": NOW,
            "reported_by": None,
        },
        "operation_transition": {
            "operation_id": new(),
            "seq": 1,
            "state": "received",
            "error_code": None,
            "at": NOW,
            "by_id": None,
        },
        "audit": {
            "seq": 1,
            "at": NOW,
            "actor_kind": "hub",
            "action": "auth.login",
            "result": "ok",
            "prev_hash": None,
            "hash": HASH,
        },
        "audit_checkpoint": {
            "hub_key_id": "hub-test",
            "seq": 1,
            "hash": HASH,
            "published_at": NOW,
            "signature": bytes(64),
        },
        # billing (KOR-31, ADR-062)
        "price_list": {"version": 1, "valid_from": NOW, "document": "{}", "created_at": NOW},
        "fx_rate": {
            "n": 1,
            "day": NOW.date(),
            "chf_per_usd_e8": 84_860_000,
            "source": "BAZG",
            "set_at": NOW,
            "actor": "operator",
            "reason": None,
        },
        "credit_entry": {
            "org_id": new(),
            "n": 1,
            "at": NOW,
            "kind": "start",
            "amount_micro_usd": 1,
            "balance_micro_usd": 1,
            "reason": None,
            "actor": "hub",
        },
        "charge": {
            "id": new(),
            "org_id": new(),
            "wg_id": new(),
            "task_id": new(),
            "session_id": new(),
            "device_id": new(),
            "origin_seq": 1,
            "kind": "runtime",
            "at": NOW,
            "amount_micro_usd": 1,
            "price_version": 1,
            "quantity": "{}",
        },
    }
    return rows[table]


def _insert(connection: psycopg.Connection[Any], table: str) -> None:
    row = _row(table)
    connection.execute(
        sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            sql.Identifier("hub", table),
            sql.SQL(", ").join(sql.Identifier(column) for column in row),
            sql.SQL(", ").join(sql.Placeholder() for _ in row),
        ),
        list(row.values()),
    )


def _changes(table: str) -> list[sql.Composed]:
    first = next(iter(_row(table)))
    name = sql.Identifier("hub", table)
    return [
        sql.SQL("UPDATE {} SET {} = {}").format(name, sql.Identifier(first), sql.Identifier(first)),
        sql.SQL("DELETE FROM {}").format(name),
        sql.SQL("TRUNCATE {}").format(name),
    ]


def test_every_append_only_table_has_both_triggers_enabled_always(owner: psycopg.Connection[Any]) -> None:
    rows = owner.execute(
        "SELECT c.relname, t.tgname, t.tgenabled, t.tgtype, p.proname FROM pg_trigger t"
        " JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace"
        " JOIN pg_proc p ON p.oid = t.tgfoid WHERE n.nspname = 'hub' AND NOT t.tgisinternal ORDER BY 1, 2"
    ).fetchall()
    expected = []
    for table in sorted(APPEND_ONLY):
        # tgtype bits: 1 row, 2 before, 8 delete, 16 update, 32 truncate.
        expected.append((table, f"{table}_no_change", "A", 1 | 2 | 8 | 16, "refuse_change"))
        expected.append((table, f"{table}_no_truncate", "A", 2 | 32, "refuse_change"))
    # the only other triggers number tasks and subagents before an insert (KOR-52, migration 0037; 4 = insert)
    expected.append(("session", "session_agent_path", "O", 1 | 2 | 4, "session_agent_path"))
    expected.append(("task", "task_number", "O", 1 | 2 | 4, "task_number"))
    assert sorted(rows) == sorted(expected)


@pytest.mark.parametrize("table", APPEND_ONLY)
def test_owner_cannot_change_append_only_rows(owner: psycopg.Connection[Any], table: str) -> None:
    _insert(owner, table)
    for statement in _changes(table):
        with pytest.raises(psycopg.errors.RaiseException, match="append-only rows are never changed or removed"):
            owner.execute(statement)
    count = owner.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier("hub", table))).fetchone()
    assert count == (1,)


@pytest.mark.parametrize("table", APPEND_ONLY)
def test_writer_inserts_but_cannot_change_append_only_rows(writer: psycopg.Connection[Any], table: str) -> None:
    _insert(writer, table)
    for statement in _changes(table):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            writer.execute(statement)


@pytest.mark.parametrize("table", APPEND_ONLY)
def test_triggers_fire_in_replica_mode(migrated_db: HubDatabase, table: str) -> None:
    with migrated_db.admin() as admin:
        _insert(admin, table)
        admin.execute("SET session_replication_role = replica")
        for statement in _changes(table):
            with pytest.raises(psycopg.errors.RaiseException):
                admin.execute(statement)
