from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from agent_cli.pg import PgError, create_database, require_loopback_dsn
from agent_cli.store import Store, StoreError


def test_require_loopback_dsn() -> None:
    require_loopback_dsn("host=127.0.0.1 port=5432 user=agent dbname=postgres")
    require_loopback_dsn("host=localhost port=5432 user=agent dbname=postgres")
    with pytest.raises(PgError, match="127.0.0.1"):
        require_loopback_dsn("host=8.8.8.8 port=5432 user=agent dbname=postgres")
    with pytest.raises(PgError, match="127.0.0.1"):
        require_loopback_dsn("postgresql://agent@8.8.8.8/postgres")
    with pytest.raises(PgError, match="service"):
        require_loopback_dsn("service=remote")


def test_write_emits_seq_and_blocks_foreign(tmp_path: Path) -> None:
    store = Store(tmp_path)
    ev = store.write("session", "insert", "s1", {"id": "s1", "kind": "human", "status": "active"})
    assert ev["origin_seq"] == 1
    assert ev["origin_device_id"] == store.device_id()
    store.apply_remote(
        {
            "origin_device_id": "other-device",
            "origin_seq": 1,
            "table": "task",
            "op": "insert",
            "row_id": "t-foreign",
            "payload": {"id": "t-foreign", "title": "theirs"},
            "occurred_at": "2026-08-13T12:00:00Z",
        }
    )
    with pytest.raises(StoreError, match="another device"):
        store.write("task", "update", "t-foreign", {"id": "t-foreign", "title": "hacked"})
    pending = store.pending_events()
    assert len(pending) == 1
    assert pending[0]["origin_seq"] == 1


def test_remote_gap_fail_closed(tmp_path: Path) -> None:
    store = Store(tmp_path)
    with pytest.raises(StoreError, match="gap"):
        store.apply_remote(
            {
                "origin_device_id": "other",
                "origin_seq": 2,
                "table": "task",
                "op": "insert",
                "row_id": "x",
                "payload": {"id": "x"},
                "occurred_at": "2026-08-13T12:00:00Z",
            }
        )


def test_restore_replay_own_and_replica(tmp_path: Path, pg_admin_dsn: str) -> None:
    store = Store(tmp_path / "main")
    store.write("session", "insert", "s1", {"id": "s1", "status": "active"})
    empty = Store(tmp_path / "empty", dsn=create_database(pg_admin_dsn, "t" + uuid.uuid4().hex[:16]))
    empty.set_meta("device_id", store.device_id())
    empty.apply_remote(
        {
            "origin_device_id": store.device_id(),
            "origin_seq": 1,
            "table": "session",
            "op": "insert",
            "row_id": "s1",
            "payload": {"id": "s1", "status": "active"},
            "occurred_at": "2026-08-13T12:00:00Z",
        }
    )
    empty.apply_replica_row(
        {
            "table": "task",
            "row_id": "t1",
            "origin_device_id": "teammate",
            "payload": {"id": "t1", "title": "team"},
            "updated_at": "2026-08-13T12:00:00Z",
        }
    )
    assert empty.row("session", "s1")["status"] == "active"
    assert empty.row("task", "t1")["title"] == "team"


def test_apply_replica_row_updates_own_ping_ack(tmp_path: Path) -> None:
    store = Store(tmp_path)
    pid = "ping-1"
    store.write(
        "ping",
        "insert",
        pid,
        {"id": pid, "from_login": "alice", "to_login": "bob", "acked_at": None},
    )
    store.apply_replica_row(
        {
            "table": "ping",
            "row_id": pid,
            "origin_device_id": store.device_id(),
            "payload": {"id": pid, "from_login": "alice", "to_login": "bob", "acked_at": "2026-08-13T12:00:01Z"},
            "updated_at": "2000-01-01T00:00:00Z",
        }
    )
    assert store.row("ping", pid)["acked_at"] == "2026-08-13T12:00:01Z"
    store.apply_replica_row(
        {
            "table": "ping",
            "row_id": pid,
            "origin_device_id": store.device_id(),
            "payload": {"id": pid, "from_login": "alice", "to_login": "bob", "acked_at": None},
            "updated_at": "2099-01-01T00:00:00Z",
        }
    )
    assert store.row("ping", pid)["acked_at"] == "2026-08-13T12:00:01Z"


def test_apply_replica_row_compares_updated_at_as_timestamptz(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.apply_replica_row(
        {
            "table": "task",
            "row_id": "t1",
            "origin_device_id": "teammate",
            "payload": {"id": "t1", "title": "old"},
            "updated_at": "2026-08-13T12:00:00Z",
        }
    )
    store.apply_replica_row(
        {
            "table": "task",
            "row_id": "t1",
            "origin_device_id": "teammate",
            "payload": {"id": "t1", "title": "new"},
            "updated_at": "2026-08-13T12:00:00.5Z",
        }
    )
    assert store.row("task", "t1")["title"] == "new"


def test_apply_replica_row_rejects_invented_web_origin(tmp_path: Path) -> None:
    store = Store(tmp_path)
    pid = "ping-1"
    store.apply_replica_row(
        {
            "table": "ping",
            "row_id": pid,
            "origin_device_id": "web",
            "payload": {"id": pid, "from_login": "alice", "to_login": "bob", "acked_at": "2026-08-13T12:00:01Z"},
            "updated_at": "2026-08-13T12:00:01Z",
        }
    )
    with pytest.raises(StoreError, match="cannot steal"):
        store.apply_replica_row(
            {
                "table": "ping",
                "row_id": pid,
                "origin_device_id": "sender-device",
                "payload": {"id": pid, "from_login": "alice", "to_login": "bob", "acked_at": "2026-08-13T12:00:01Z"},
                "updated_at": "2026-08-13T12:00:02Z",
            }
        )


def test_identity_survives_database_wipe(tmp_path: Path, pg_admin_dsn: str) -> None:
    store = Store(tmp_path)
    device_id = store.device_id()
    store.set_meta("github_login", "alice")
    store.write("session", "insert", "s1", {"id": "s1", "status": "active"})
    store.close()
    wiped = Store(tmp_path, dsn=create_database(pg_admin_dsn, "t" + uuid.uuid4().hex[:16]))
    assert wiped.device_id() == device_id
    assert wiped.meta("github_login") == "alice"
    assert wiped.row("session", "s1") is None


def test_store_rejects_mismatched_device_json(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.close()
    ident = json.loads((tmp_path / "device.json").read_text(encoding="utf-8"))
    ident["device_id"] = "00000000-0000-0000-0000-000000000000"
    (tmp_path / "device.json").write_text(json.dumps(ident) + "\n", encoding="utf-8")
    with pytest.raises(StoreError, match="does not match this database"):
        Store(tmp_path)


def test_notify_on_owned_message_and_pr_merged(tmp_path: Path) -> None:
    import psycopg

    store = Store(tmp_path)
    store.write("session", "insert", "inbox", {"id": "inbox", "kind": "human", "status": "active"})
    with psycopg.connect(store.dsn, autocommit=True) as conn:
        conn.execute("LISTEN agent_inbox")
        mail_id = "act-mail"
        store.write(
            "activity",
            "insert",
            mail_id,
            {
                "id": mail_id,
                "session_id": "other",
                "type": "message",
                "payload": {"to_session": "inbox", "body": "hi"},
                "execution_status": "pending",
            },
        )
        got = next(conn.notifies(timeout=2.0), None)
        assert got is not None
        assert got.payload == mail_id
        merged_id = "act-merged"
        store.write(
            "activity",
            "insert",
            merged_id,
            {
                "id": merged_id,
                "session_id": "inbox",
                "type": "pr.merged",
                "payload": {"repo": "o/r", "number": 1},
                "execution_status": "done",
            },
        )
        got = next(conn.notifies(timeout=2.0), None)
        assert got is not None
        assert got.payload == merged_id


def test_no_notify_for_message_to_foreign_session(tmp_path: Path) -> None:
    import psycopg

    store = Store(tmp_path)
    store.apply_replica_row(
        {
            "table": "session",
            "row_id": "foreign",
            "origin_device_id": "other-device",
            "payload": {"id": "foreign", "kind": "human", "status": "active"},
            "updated_at": "2026-08-13T12:00:00Z",
        }
    )
    with psycopg.connect(store.dsn, autocommit=True) as conn:
        conn.execute("LISTEN agent_inbox")
        store.write(
            "activity",
            "insert",
            "act-out",
            {
                "id": "act-out",
                "session_id": "local",
                "type": "message",
                "payload": {"to_session": "foreign", "body": "hi"},
                "execution_status": "pending",
            },
        )
        assert next(conn.notifies(timeout=0.4), None) is None


def test_apply_remote_idempotent_does_not_renotify(tmp_path: Path) -> None:
    import psycopg

    store = Store(tmp_path)
    store.write("session", "insert", "inbox", {"id": "inbox", "kind": "human", "status": "active"})
    event = store.write(
        "activity",
        "insert",
        "act-mail",
        {
            "id": "act-mail",
            "session_id": "other",
            "type": "message",
            "payload": {"to_session": "inbox", "body": "hi"},
            "execution_status": "pending",
        },
    )
    with psycopg.connect(store.dsn, autocommit=True) as conn:
        conn.execute("LISTEN agent_inbox")
        store.apply_remote(event)
        assert next(conn.notifies(timeout=0.4), None) is None
