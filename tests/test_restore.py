from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from agent_cli import main as main_mod
from agent_cli.main import open_store


class _FakeRestoreHub:
    def __init__(self, body: Any) -> None:
        self.body = body

    def restore(self) -> Any:
        return self.body

    def close(self) -> None:
        return None


def _init_store(tmp_path: Path) -> None:
    os.environ["AGENT_HOME"] = str(tmp_path)
    main_mod.main(["init"])


def _run_restore(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: Any) -> None:
    _init_store(tmp_path)
    store = open_store()
    device_id = store.device_id()
    store.close()
    if isinstance(body, dict) and "device_id" not in body:
        body = {**body, "device_id": device_id}
    monkeypatch.setattr(main_mod, "_hub_from_store", lambda _s: _FakeRestoreHub(body))
    main_mod.cmd_restore([])


def test_cmd_restore_dies_on_non_dict_body(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression test: Hub.request (and so Hub.restore) returns None for a 2xx
    response with an empty body - cmd_restore used to call body.get("device_id")
    straight on that, raising a raw AttributeError instead of a clean die()."""
    with pytest.raises(SystemExit, match="restore response is not an object"):
        _run_restore(tmp_path, monkeypatch, None)


def _valid_restore_event() -> dict[str, Any]:
    return {
        "origin_device_id": "other",
        "origin_seq": 1,
        "table": "activity",
        "op": "insert",
        "row_id": "x",
        "payload": {},
        "occurred_at": "2026-08-13T12:00:00Z",
    }


@pytest.mark.parametrize(
    "field",
    ["origin_device_id", "origin_seq", "table", "op", "row_id", "payload", "occurred_at"],
)
def test_cmd_restore_dies_on_event_missing_one_required_field(
    field: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: a restored event missing any single one of the fields
    _insert_event_idempotent indexes used to raise a raw KeyError deep inside
    store.apply_remote instead of a clean die(). Parametrized per field (like
    the equivalent tests/test_pending.py _sync_once tests) so a future
    accidental narrowing of _PULL_EVENT_FIELDS to any one of them is still
    caught, not just the "several fields missing at once" case."""
    event = _valid_restore_event()
    del event[field]
    body = {"own_events": [event]}
    with pytest.raises(SystemExit, match="restore event is missing required fields"):
        _run_restore(tmp_path, monkeypatch, body)


def _valid_restore_row() -> dict[str, Any]:
    return {
        "table": "activity",
        "origin_device_id": "other",
        "row_id": "x",
        "payload": {},
        "updated_at": "2026-08-13T12:00:00Z",
    }


@pytest.mark.parametrize("field", ["table", "origin_device_id", "row_id", "payload", "updated_at"])
def test_cmd_restore_dies_on_snapshot_missing_one_required_field(
    field: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: a restored snapshot row missing any single one of the
    fields apply_replica_row indexes used to raise a raw KeyError instead of a
    clean die(). Parametrized per field, same reasoning as the event test
    above."""
    row = _valid_restore_row()
    del row[field]
    body = {"own_events": [], "inbox": [row]}
    with pytest.raises(SystemExit, match="restore snapshot is missing required fields"):
        _run_restore(tmp_path, monkeypatch, body)


def test_cmd_restore_accepts_a_numeric_string_origin_seq(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: cmd_restore passed event["origin_seq"] to
    apply_remote uncoerced, only int()-converting it afterward for
    mark_origin. A numeric-string origin_seq (e.g. "1") then hit
    _insert_event_idempotent's plain Python `event["origin_seq"] != last_seq +
    1` as a string, deterministically raising a false "origin_seq gap" for the
    very first restored event. cmd_restore now shares _sync_once's coercion,
    applied before apply_remote sees the event."""
    body = {
        "own_events": [
            {
                "origin_device_id": "other",
                "origin_seq": "1",
                "table": "task",
                "op": "insert",
                "row_id": "t1",
                "payload": {"id": "t1"},
                "occurred_at": "2026-08-13T12:00:00Z",
            }
        ]
    }
    _run_restore(tmp_path, monkeypatch, body)
    store = open_store()
    try:
        assert store.origin_cursor("other") == 1
        assert store.row("task", "t1") is not None
    finally:
        store.close()


def test_cmd_restore_applies_events_and_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Happy-path coverage: cmd_restore had none before this change. Replays
    one own event and one inbox snapshot row."""
    body = {
        "own_events": [
            {
                "origin_device_id": "other",
                "origin_seq": 1,
                "table": "task",
                "op": "insert",
                "row_id": "t1",
                "payload": {"id": "t1"},
                "occurred_at": "2026-08-13T12:00:00Z",
            }
        ],
        "inbox": [
            {
                "table": "activity",
                "origin_device_id": "other",
                "row_id": "a1",
                "payload": {"id": "a1", "type": "message"},
                "updated_at": "2026-08-13T12:00:00Z",
            }
        ],
    }
    capsys.readouterr()
    _run_restore(tmp_path, monkeypatch, body)
    captured = capsys.readouterr()
    assert "restored events=1 snapshots=1" in captured.out
    store = open_store()
    try:
        assert store.row("task", "t1") is not None
        assert store.row("activity", "a1") is not None
    finally:
        store.close()
