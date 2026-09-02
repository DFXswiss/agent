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
    applied before apply_remote sees the event. own_events replays this
    device's own history, so origin_device_id must genuinely be this
    device's own id, not a foreign one - same reasoning as
    test_cmd_restore_applies_events_and_snapshots below."""
    _init_store(tmp_path)
    store = open_store()
    device_id = store.device_id()
    store.close()
    body = {
        "own_events": [
            {
                "origin_device_id": device_id,
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
        assert store.origin_cursor(device_id) == 1
        assert store.row("task", "t1") is not None
    finally:
        store.close()


def test_cmd_restore_dies_on_non_list_snapshot_field(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression test: the restore-side sibling of
    test_sync_once_raises_hub_error_on_non_list_snapshot_field. A truthy
    non-list "inbox" (e.g. a malformed hub response sending an object instead
    of a list) used to raise a raw TypeError from list(...) instead of a
    clean die()."""
    body = {"own_events": [], "inbox": {"not": "a list"}}
    with pytest.raises(SystemExit, match="restore response inbox is not a list"):
        _run_restore(tmp_path, monkeypatch, body)


def test_cmd_restore_dies_when_apply_remote_hits_an_unanticipated_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: the restore-side sibling of
    test_sync_once_raises_hub_error_when_apply_remote_hits_an_unanticipated_shape.
    A payload containing a value json.dumps cannot serialize (a stand-in for
    "something genuinely unanticipated") reaches the broad except-Exception net
    around apply_remote/mark_origin and becomes a clean die() instead of an
    uncaught crash."""
    body = {
        "own_events": [
            {
                "origin_device_id": "other",
                "origin_seq": 1,
                "table": "activity",
                "op": "insert",
                "row_id": "x",
                "payload": {"type": "message", "body": {1, 2, 3}},
                "occurred_at": "2026-08-13T12:00:00Z",
            }
        ]
    }
    with pytest.raises(SystemExit, match="restore event could not be applied"):
        _run_restore(tmp_path, monkeypatch, body)


def test_cmd_restore_dies_when_apply_replica_row_hits_an_unanticipated_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: the restore-side sibling of
    test_sync_once_raises_hub_error_when_apply_replica_row_hits_an_unanticipated_shape."""
    row = {**_valid_restore_row(), "payload": {"type": "message", "body": {1, 2, 3}}}
    body = {"own_events": [], "inbox": [row]}
    with pytest.raises(SystemExit, match="restore snapshot could not be applied"):
        _run_restore(tmp_path, monkeypatch, body)


def test_cmd_restore_accepts_an_event_whose_type_is_not_a_wake_type_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: apply_remote's wake=False branch (the only one
    cmd_restore ever uses) has its own inline isinstance(typ, str) guard
    before the WAKE_ACTIVITY_TYPES membership check (store.py's
    apply_remote, elif inserted: branch). Unlike the wake=True path
    (_maybe_wake, covered by tests/test_pending.py), nothing exercised this
    wake=False guard specifically - a regression that reintroduced the
    unguarded check only there would stay green on the sync side while
    breaking every restore whose event has a non-string type."""
    _init_store(tmp_path)
    store = open_store()
    device_id = store.device_id()
    store.close()
    body = {
        "own_events": [
            {
                "origin_device_id": device_id,
                "origin_seq": 1,
                "table": "activity",
                "op": "insert",
                "row_id": "x",
                "payload": {"type": ["not", "hashable"]},
                "occurred_at": "2026-08-13T12:00:00Z",
            }
        ]
    }
    _run_restore(tmp_path, monkeypatch, body)
    store = open_store()
    try:
        assert store.row("activity", "x") is not None
    finally:
        store.close()


def test_cmd_restore_accepts_a_snapshot_whose_type_is_not_a_wake_type_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: the row-side sibling of
    test_cmd_restore_accepts_an_event_whose_type_is_not_a_wake_type_shape.
    apply_replica_row's wake=False branch has the same inline guard."""
    row = {**_valid_restore_row(), "payload": {"type": ["not", "hashable"]}}
    body = {"own_events": [], "inbox": [row]}
    _run_restore(tmp_path, monkeypatch, body)
    store = open_store()
    try:
        assert store.row("activity", "x") is not None
    finally:
        store.close()


def test_cmd_restore_applies_events_and_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Happy-path coverage: cmd_restore had none before this change. Replays
    one own event (own_events replays this device's own history, so its
    origin_device_id must genuinely be this device's own id, not a foreign
    one - foreign data only ever arrives via inbox/pings) and one inbox
    snapshot row (from another device, correctly foreign)."""
    _init_store(tmp_path)
    store = open_store()
    device_id = store.device_id()
    store.close()
    body = {
        "own_events": [
            {
                "origin_device_id": device_id,
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
