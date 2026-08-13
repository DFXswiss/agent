from __future__ import annotations

from pathlib import Path

import pytest

from agent_cli.store import Store, StoreError


def test_write_emits_seq_and_blocks_foreign(tmp_path: Path) -> None:
    store = Store(tmp_path / "ledger.sqlite")
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
    store = Store(tmp_path / "ledger.sqlite")
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


def test_restore_replay_own_and_replica(tmp_path: Path) -> None:
    store = Store(tmp_path / "ledger.sqlite")
    store.write("session", "insert", "s1", {"id": "s1", "status": "active"})
    empty = Store(tmp_path / "empty.sqlite")
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
