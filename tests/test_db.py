from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_core.app import create_app
from agent_core.db import Store
from agent_core.github import FakeGitHub


class _FailingReplicaConnection:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def execute(self, sql: str, params=()):
        if sql.startswith("INSERT INTO row_replica"):
            raise sqlite3.OperationalError("forced replica failure")
        return self.connection.execute(sql, params)

    def commit(self) -> None:
        self.connection.commit()

    def rollback(self) -> None:
        self.connection.rollback()

    def close(self) -> None:
        self.connection.close()


def _mark_request_done(
    store: Store, key: tuple[str, str, str], owner: str
) -> None:
    updated = store.execute(
        "UPDATE request_reservation SET status = 'done' "
        "WHERE actor = ? AND activity_type = ? AND request_id = ? "
        "AND status = 'pending' AND owner = ?",
        (*key, owner),
    )
    assert updated.rowcount == 1


def test_store_migrates_columns_without_losing_rows(tmp_path: Path) -> None:
    database = tmp_path / "old-hub.sqlite"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE device ("
            "id TEXT PRIMARY KEY, github_login TEXT NOT NULL, name TEXT NOT NULL, "
            "token_id TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, revoked_at TEXT)"
        )
        connection.execute(
            "INSERT INTO device (id, github_login, name, token_id, created_at, revoked_at) "
            "VALUES (?, ?, ?, ?, ?, NULL)",
            ("old-device", "alice", "old laptop", "old-token", "2026-01-01T00:00:00Z"),
        )
        connection.execute(
            "CREATE TABLE request_reservation ("
            "actor TEXT, activity_type TEXT, request_id TEXT, status TEXT NOT NULL, "
            "lease_until TEXT NOT NULL, PRIMARY KEY (actor, activity_type, request_id))"
        )
        connection.execute(
            "INSERT INTO request_reservation "
            "(actor, activity_type, request_id, status, lease_until) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "alice",
                "assignment.created",
                "old-request",
                "pending",
                "2026-11-05T10:02:00Z",
            ),
        )
        connection.commit()
    finally:
        connection.close()

    store = Store(str(database))
    try:
        columns = {row["name"] for row in store.query("PRAGMA table_info(device)")}
        reservation_columns = {
            row["name"] for row in store.query("PRAGMA table_info(request_reservation)")
        }
        row = store.query_one("SELECT * FROM device WHERE id = ?", ("old-device",))
        reservation = store.query_one(
            "SELECT * FROM request_reservation WHERE request_id = ?", ("old-request",)
        )

        assert "confirmed_by" in columns
        assert "owner" in reservation_columns
        assert "fingerprint" in reservation_columns
        assert "prior_write" in reservation_columns
        assert "created_at" in reservation_columns
        assert row is not None
        assert row["github_login"] == "alice"
        assert row["confirmed_by"] is None
        assert reservation is not None
        assert reservation["owner"] == ""
        assert reservation["fingerprint"] == ""
        assert reservation["prior_write"] == 0
        assert reservation["created_at"] == ""
    finally:
        store.close()

    reopened = Store(str(database))
    try:
        reservation = reopened.query_one(
            "SELECT owner, fingerprint, prior_write, created_at FROM request_reservation WHERE request_id = ?",
            ("old-request",),
        )
        assert reservation is not None
        assert reservation["owner"] == ""
        assert reservation["fingerprint"] == ""
        assert reservation["prior_write"] == 0
        assert reservation["created_at"] == ""
    finally:
        reopened.close()


def test_request_reservation_busy_expiry_done_and_release(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "reservations.sqlite"))
    key = ("alice", "assignment.created", "request-one")
    first_owner = "owner-one"
    second_owner = "owner-two"
    fingerprint = "fingerprint-one"
    try:
        assert store.reserve_request(
            *key,
            fingerprint,
            first_owner,
            "2026-11-05T10:00:00Z",
            "2026-11-05T10:02:00Z",
        ) == "reserved"
        assert store.reserve_request(
            *key,
            fingerprint,
            second_owner,
            "2026-11-05T10:01:00Z",
            "2026-11-05T10:03:00Z",
        ) == "busy"
        assert store.reserve_request(
            *key,
            "different-fingerprint",
            second_owner,
            "2026-11-05T10:01:00Z",
            "2026-11-05T10:03:00Z",
        ) == "conflict"
        assert store.reserve_request(
            *key,
            fingerprint,
            second_owner,
            "2026-11-05T10:02:00Z",
            "2026-11-05T10:04:00Z",
        ) == "taken_over"
        reservation = store.query_one(
            "SELECT prior_write, created_at FROM request_reservation WHERE request_id = ?",
            (key[2],),
        )
        assert reservation is not None
        assert reservation["prior_write"] == 1
        assert reservation["created_at"] == "2026-11-05T10:00:00Z"
        assert not store.renew_request(
            *key,
            first_owner,
            "2026-11-05T10:02:01Z",
            "2026-11-05T10:04:01Z",
        )
        assert not store.release_request(*key, first_owner)
        assert store.renew_request(
            *key,
            second_owner,
            "2026-11-05T10:02:01Z",
            "2026-11-05T10:04:01Z",
        )
        _mark_request_done(store, key, second_owner)
        assert store.reserve_request(
            *key,
            fingerprint,
            "owner-three",
            "2026-11-05T10:05:00Z",
            "2026-11-05T10:07:00Z",
        ) == "done"

        released = ("alice", "assignment.reply", "request-two")
        assert store.reserve_request(
            *released,
            fingerprint,
            first_owner,
            "2026-11-05T10:00:00Z",
            "2026-11-05T10:02:00Z",
        ) == "reserved"
        assert store.release_request(*released, first_owner)
        assert store.reserve_request(
            *released,
            fingerprint,
            second_owner,
            "2026-11-05T10:00:01Z",
            "2026-11-05T10:02:01Z",
        ) == "reserved"
    finally:
        store.close()


def test_record_web_event_writes_ledger_and_replica_with_per_origin_sequences(
    tmp_path: Path,
) -> None:
    store = Store(str(tmp_path / "events.sqlite"))
    try:
        store.record_web_event(
            "web:alice",
            "alice",
            "activity",
            "insert",
            "one",
            '{"id":"one"}',
            "2026-11-05T10:00:00Z",
        )
        store.record_web_event(
            "web:alice",
            "alice",
            "activity",
            "update",
            "two",
            '{"id":"two"}',
            "2026-11-05T10:01:00Z",
        )
        store.record_web_event(
            "web:bob",
            "bob",
            "activity",
            "insert",
            "three",
            '{"id":"three"}',
            "2026-11-05T10:02:00Z",
        )

        ledger = store.query(
            "SELECT origin_device_id, origin_seq, row_id FROM ledger_event ORDER BY hub_seq"
        )
        replicas = store.query(
            "SELECT origin_device_id, github_login, row_id FROM row_replica ORDER BY row_id"
        )

        assert [
            (row["origin_device_id"], row["origin_seq"], row["row_id"])
            for row in ledger
        ] == [
            ("web:alice", 1, "one"),
            ("web:alice", 2, "two"),
            ("web:bob", 1, "three"),
        ]
        assert [
            (row["origin_device_id"], row["github_login"], row["row_id"])
            for row in replicas
        ] == [
            ("web:alice", "alice", "one"),
            ("web:bob", "bob", "three"),
            ("web:alice", "alice", "two"),
        ]
    finally:
        store.close()


def test_record_web_event_rolls_back_ledger_when_replica_write_fails(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "rollback.sqlite"))
    store._conn = _FailingReplicaConnection(store._conn)
    try:
        with pytest.raises(sqlite3.OperationalError, match="forced replica failure"):
            store.record_web_event(
                "web:alice",
                "alice",
                "activity",
                "insert",
                "one",
                '{"id":"one"}',
                "2026-11-05T10:00:00Z",
            )

        assert store.query("SELECT * FROM ledger_event") == []
        assert store.query("SELECT * FROM row_replica") == []
    finally:
        store.close()


def test_record_web_event_for_request_records_once_and_finishes(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "request-event.sqlite"))
    key = ("alice", "assignment.created", "request-one")
    try:
        assert store.reserve_request(
            *key,
            "fingerprint-one",
            "owner-one",
            "2026-11-05T10:00:00Z",
            "2026-11-05T10:02:00Z",
        ) == "reserved"

        assert store.record_web_event_for_request(
            "web:alice",
            "alice",
            "activity",
            "insert",
            "activity-one",
            '{"id":"activity-one"}',
            "2026-11-05T10:01:00Z",
            actor=key[0],
            activity_type=key[1],
            request_id=key[2],
            owner="owner-one",
        )

        reservation = store.query_one(
            "SELECT status FROM request_reservation WHERE actor = ? "
            "AND activity_type = ? AND request_id = ?",
            key,
        )
        assert reservation is not None
        assert reservation["status"] == "done"
        assert len(store.query("SELECT * FROM ledger_event")) == 1
        assert len(store.query("SELECT * FROM row_replica")) == 1
        assert not store.record_web_event_for_request(
            "web:alice",
            "alice",
            "activity",
            "insert",
            "activity-two",
            '{"id":"activity-two"}',
            "2026-11-05T10:01:01Z",
            actor=key[0],
            activity_type=key[1],
            request_id=key[2],
            owner="owner-one",
        )
        assert len(store.query("SELECT * FROM ledger_event")) == 1
        assert len(store.query("SELECT * FROM row_replica")) == 1
    finally:
        store.close()


def test_record_web_event_for_request_rolls_back_event_and_keeps_reservation(
    tmp_path: Path,
) -> None:
    store = Store(str(tmp_path / "request-rollback.sqlite"))
    key = ("alice", "assignment.created", "request-one")
    try:
        assert store.reserve_request(
            *key,
            "fingerprint-one",
            "owner-one",
            "2026-11-05T10:00:00Z",
            "2026-11-05T10:02:00Z",
        ) == "reserved"
        store._conn = _FailingReplicaConnection(store._conn)

        with pytest.raises(sqlite3.OperationalError, match="forced replica failure"):
            store.record_web_event_for_request(
                "web:alice",
                "alice",
                "activity",
                "insert",
                "activity-one",
                '{"id":"activity-one"}',
                "2026-11-05T10:01:00Z",
                actor=key[0],
                activity_type=key[1],
                request_id=key[2],
                owner="owner-one",
            )

        assert store.query("SELECT * FROM ledger_event") == []
        assert store.query("SELECT * FROM row_replica") == []
        reservation = store.query_one(
            "SELECT status, owner FROM request_reservation WHERE actor = ? "
            "AND activity_type = ? AND request_id = ?",
            key,
        )
        assert reservation is not None
        assert dict(reservation) == {"status": "pending", "owner": "owner-one"}
    finally:
        store.close()


def test_record_web_event_for_request_rejects_owner_after_takeover(
    tmp_path: Path,
) -> None:
    store = Store(str(tmp_path / "request-takeover.sqlite"))
    key = ("alice", "assignment.created", "request-one")
    try:
        assert store.reserve_request(
            *key,
            "fingerprint-one",
            "old-owner",
            "2026-11-05T10:00:00Z",
            "2026-11-05T10:01:00Z",
        ) == "reserved"
        assert store.reserve_request(
            *key,
            "fingerprint-one",
            "new-owner",
            "2026-11-05T10:01:00Z",
            "2026-11-05T10:03:00Z",
        ) == "taken_over"

        assert not store.record_web_event_for_request(
            "web:alice",
            "alice",
            "activity",
            "insert",
            "activity-one",
            '{"id":"activity-one"}',
            "2026-11-05T10:01:01Z",
            actor=key[0],
            activity_type=key[1],
            request_id=key[2],
            owner="old-owner",
        )

        assert store.query("SELECT * FROM ledger_event") == []
        assert store.query("SELECT * FROM row_replica") == []
        reservation = store.query_one(
            "SELECT owner, status FROM request_reservation WHERE request_id = ?",
            (key[2],),
        )
        assert reservation is not None
        assert dict(reservation) == {"owner": "new-owner", "status": "pending"}
    finally:
        store.close()


def test_create_app_closes_internally_created_store_after_lifespan(cfg) -> None:
    app = create_app(cfg, github=FakeGitHub({"code-alice": "alice"}))

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200

    with pytest.raises(sqlite3.ProgrammingError):
        app.state.hub.store.query("SELECT 1")


def test_create_app_closes_internal_store_when_hub_construction_fails(
    cfg, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[Store] = []
    original_close = Store.close

    def close_spy(store: Store) -> None:
        closed.append(store)
        original_close(store)

    def fail_loading_tokens(_store: Store):
        raise RuntimeError("construction failed")

    monkeypatch.setattr(Store, "close", close_spy)
    monkeypatch.setattr(Store, "all_oauth_tokens", fail_loading_tokens)

    with pytest.raises(RuntimeError, match="construction failed"):
        create_app(cfg, github=FakeGitHub({"code-alice": "alice"}))

    assert len(closed) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        closed[0].query("SELECT 1")
