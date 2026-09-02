from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_cli.hub import HubError
from agent_cli.main import _sync_once
from agent_cli.pending import scan_pending
from agent_cli.store import Store


class FakeHub:
    def __init__(self) -> None:
        self.put_calls: list[list[dict[str, Any]]] = []
        self.query_calls: list[dict[str, Any]] = []
        self.pull_body: dict[str, Any] = {"events": [], "inbox": [], "pings": [], "subscriptions": []}
        self.raise_on_put: Exception | None = None
        self.raise_on_query: Exception | None = None
        self.query_result: Any = {"rows": []}

    def put_subscriptions(self, subscriptions: list[dict[str, Any]]) -> dict[str, Any]:
        if self.raise_on_put is not None:
            raise self.raise_on_put
        self.put_calls.append(subscriptions)
        return {"ok": True}

    def query(self, match: dict[str, Any]) -> Any:
        if self.raise_on_query is not None:
            raise self.raise_on_query
        self.query_calls.append(match)
        return self.query_result

    def push(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        return {"ok": True}

    def pull(self, cursors: dict[str, int]) -> dict[str, Any]:
        return self.pull_body

    def close(self) -> None:
        return None


def _owned_session(store: Store, sid: str = "s1") -> None:
    store.write("session", "insert", sid, {"id": sid, "kind": "human", "status": "active"})


def test_watch_pending_subscription_set_done(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_session(store)
    act_id = "sub-1"
    subs = [{"match": {"type": "message"}}]
    store.write(
        "activity",
        "insert",
        act_id,
        {
            "id": act_id,
            "session_id": "s1",
            "type": "subscription.set",
            "payload": {"subscriptions": subs},
            "execution_status": "pending",
        },
    )
    hub = FakeHub()
    lines = scan_pending(store, hub)  # type: ignore[arg-type]
    assert lines == [f"subscription.set {act_id} done"]
    assert hub.put_calls == [subs]
    row = store.row("activity", act_id)
    assert row is not None
    assert row["execution_status"] == "done"
    assert "execution_error" not in row


def test_watch_pending_subscription_set_hub_error(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_session(store)
    act_id = "sub-err"
    store.write(
        "activity",
        "insert",
        act_id,
        {
            "id": act_id,
            "session_id": "s1",
            "type": "subscription.set",
            "payload": {"subscriptions": [{"match": {"type": "message"}}]},
            "execution_status": "pending",
        },
    )
    hub = FakeHub()
    hub.raise_on_put = HubError("hub PUT /sync/subscriptions → HTTP 400: bad match")
    lines = scan_pending(store, hub)  # type: ignore[arg-type]
    assert lines == [f"subscription.set {act_id} error"]
    row = store.row("activity", act_id)
    assert row is not None
    assert row["execution_status"] == "error"
    assert "HTTP 400" in str(row.get("execution_error"))


def test_watch_pending_query_request_inserts_result(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_session(store)
    act_id = "q-1"
    match = {"type": "pr.open"}
    hub_rows = [
        {
            "table": "activity",
            "row_id": "r1",
            "origin_device_id": "other",
            "payload": {"id": "r1", "type": "pr.open"},
            "updated_at": "2026-08-13T12:00:00Z",
        }
    ]
    store.write(
        "activity",
        "insert",
        act_id,
        {
            "id": act_id,
            "session_id": "s1",
            "type": "query.request",
            "payload": {"match": match},
            "execution_status": "pending",
        },
    )
    hub = FakeHub()
    hub.query_result = {"rows": hub_rows}
    write_ops: list[tuple[str, str, str | None]] = []
    orig_write = store.write

    def tracking_write(table: str, op: str, row_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        typ = payload.get("type") if isinstance(payload.get("type"), str) else None
        write_ops.append((op, row_id, typ))
        return orig_write(table, op, row_id, payload)

    store.write = tracking_write  # type: ignore[method-assign]
    lines = scan_pending(store, hub)  # type: ignore[arg-type]
    assert len(lines) == 1
    assert lines[0].startswith(f"query.request {act_id} done result=")
    result_id = lines[0].split("result=", 1)[1]
    req = store.row("activity", act_id)
    assert req is not None
    assert req["execution_status"] == "done"
    result = store.row("activity", result_id)
    assert result is not None
    assert result["type"] == "query.result"
    assert result["session_id"] == "s1"
    assert result["execution_status"] == "done"
    assert result["payload"]["request_id"] == act_id
    assert result["payload"]["rows"] == hub_rows
    assert hub.query_calls == [match]
    insert_idx = next(i for i, (op, rid, typ) in enumerate(write_ops) if op == "insert" and typ == "query.result")
    done_idx = next(
        i
        for i, (op, rid, _typ) in enumerate(write_ops)
        if op == "update" and rid == act_id
    )
    assert insert_idx < done_idx


def test_watch_pending_query_request_none_response_errors(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_session(store)
    act_id = "q-none"
    store.write(
        "activity",
        "insert",
        act_id,
        {
            "id": act_id,
            "session_id": "s1",
            "type": "query.request",
            "payload": {"match": {"type": "pr.open"}},
            "execution_status": "pending",
        },
    )
    hub = FakeHub()
    hub.query_result = None
    lines = scan_pending(store, hub)  # type: ignore[arg-type]
    assert lines == [f"query.request {act_id} error"]
    req = store.row("activity", act_id)
    assert req is not None
    assert req["execution_status"] == "error"
    assert req["execution_error"] == "query response missing rows"
    assert not any(r.get("type") == "query.result" for r in store.rows("activity"))


def test_watch_pending_query_request_rows_none_errors(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_session(store)
    act_id = "q-rows-none"
    store.write(
        "activity",
        "insert",
        act_id,
        {
            "id": act_id,
            "session_id": "s1",
            "type": "query.request",
            "payload": {"match": {"type": "pr.open"}},
            "execution_status": "pending",
        },
    )
    hub = FakeHub()
    hub.query_result = {"rows": None}
    lines = scan_pending(store, hub)  # type: ignore[arg-type]
    assert lines == [f"query.request {act_id} error"]
    req = store.row("activity", act_id)
    assert req is not None
    assert req["execution_status"] == "error"
    assert req["execution_error"] == "query response missing rows"
    assert not any(r.get("type") == "query.result" for r in store.rows("activity"))


def test_sync_once_applies_subscription_snapshots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path)
    store.set_meta("hub_url", "http://hub.example")
    store.set_meta("device_token", "tok")
    hub = FakeHub()
    hub.pull_body = {
        "events": [],
        "inbox": [],
        "pings": [],
        "subscriptions": [
            {
                "table": "activity",
                "row_id": "sub-act",
                "origin_device_id": "other-device",
                "payload": {
                    "id": "sub-act",
                    "session_id": "foreign",
                    "type": "message",
                    "payload": {"to_session": "x", "body": "hi"},
                    "execution_status": "pending",
                },
                "updated_at": "2026-08-13T12:00:00Z",
            }
        ],
    }
    monkeypatch.setattr("agent_cli.main._hub_from_store", lambda _s: hub)
    _sync_once(store)
    row = store.row("activity", "sub-act")
    assert row is not None
    assert row["type"] == "message"
    assert row["_origin_device_id"] == "other-device"


def _paired_store(tmp_path: Path) -> Store:
    store = Store(tmp_path)
    store.set_meta("hub_url", "http://hub.example")
    store.set_meta("device_token", "tok")
    return store


def test_sync_once_raises_hub_error_on_non_dict_pull_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression test: Hub.request returns None for a 2xx response with an empty
    body. _sync_once used to call pulled.get("events") straight on that, raising a
    raw AttributeError instead of a catchable HubError - which would have escaped
    both _knock_scan_cycle's and cmd_sync --follow's guards and killed whichever
    process called it."""
    hub = FakeHub()
    hub.pull_body = None  # type: ignore[assignment]
    monkeypatch.setattr("agent_cli.main._hub_from_store", lambda _s: hub)
    with pytest.raises(HubError, match="pull response is not an object"):
        _sync_once(_paired_store(tmp_path))


def _valid_pull_event(device_id: str) -> dict[str, Any]:
    return {
        "origin_device_id": device_id,
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
def test_sync_once_raises_hub_error_on_event_missing_one_required_field(
    field: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: a pull event missing any single one of the fields
    _insert_event_idempotent indexes directly used to raise a raw KeyError deep
    inside store.apply_remote instead of a catchable HubError raised before that
    call. Parametrized per field so a future accidental narrowing of
    _PULL_EVENT_FIELDS to any one of them is still caught."""
    store = _paired_store(tmp_path)
    event = _valid_pull_event(store.device_id())
    del event[field]
    hub = FakeHub()
    hub.pull_body = {"events": [event]}
    monkeypatch.setattr("agent_cli.main._hub_from_store", lambda _s: hub)
    with pytest.raises(HubError, match="pull event is missing required fields"):
        _sync_once(store)


def test_sync_once_raises_hub_error_on_event_with_foreign_origin_device_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: DESIGN.md's sync contract is "own events, gapless" -
    foreign-origin data arrives as row snapshots, never as an event. An event
    whose origin_device_id doesn't match this device's own used to pass every
    check and reach apply_remote/mark_origin unchanged, letting a malformed
    hub response poison this device's own ledger under a foreign device's
    identity."""
    store = _paired_store(tmp_path)
    event = {**_valid_pull_event(store.device_id()), "origin_device_id": "other"}
    hub = FakeHub()
    hub.pull_body = {"events": [event]}
    monkeypatch.setattr("agent_cli.main._hub_from_store", lambda _s: hub)
    with pytest.raises(HubError, match="event origin_device_id is not this device's own"):
        _sync_once(store)
    assert store.rows("activity") == []


def test_sync_once_raises_hub_error_on_event_with_non_dict_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: field presence was checked but not payload's type.
    A non-dict payload (e.g. a JSON list) used to pass validation untouched,
    get committed by apply_remote (Store._maybe_wake/_maybe_work each already
    return early on a non-dict payload, so nothing rolls back the write), and
    only fail later - on every future store.rows()/store.row() call for that
    whole table, not at write time. Store._write_in_txn doesn't check this
    shape either for local writes, but that's this codebase's own trusted
    code constructing payloads, not externally-supplied hub data - the risk
    this test guards against is specific to the pull path."""
    store = _paired_store(tmp_path)
    event = {**_valid_pull_event(store.device_id()), "payload": ["not", "an", "object"]}
    hub = FakeHub()
    hub.pull_body = {"events": [event]}
    monkeypatch.setattr("agent_cli.main._hub_from_store", lambda _s: hub)
    with pytest.raises(HubError, match="pull event payload is not an object"):
        _sync_once(store)
    assert store.origin_cursor(store.device_id()) == 0
    assert store.rows("activity") == []


def test_sync_once_raises_hub_error_on_event_with_unknown_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: sibling of the non-dict-payload test above, for op.
    Store._write_in_txn rejects any op outside insert/update/delete for a
    local write; the hub-pull path must reject it too, not silently accept
    and materialize a row under an op nothing else in the codebase expects."""
    store = _paired_store(tmp_path)
    event = {**_valid_pull_event(store.device_id()), "op": "bogus"}
    hub = FakeHub()
    hub.pull_body = {"events": [event]}
    monkeypatch.setattr("agent_cli.main._hub_from_store", lambda _s: hub)
    with pytest.raises(HubError, match="pull event has an unknown op"):
        _sync_once(store)
    assert store.origin_cursor(store.device_id()) == 0
    assert store.rows("activity") == []


def test_sync_once_raises_hub_error_on_event_with_unparseable_occurred_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: the event-side sibling of
    test_sync_once_raises_hub_error_on_snapshot_with_unparseable_updated_at.
    occurred_at was presence-checked only, like updated_at used to be; a
    bogus non-timestamp string used to pass validation and be stored as-is
    by apply_remote (which writes it into row_data.updated_at too, via
    _materialize)."""
    store = _paired_store(tmp_path)
    event = {**_valid_pull_event(store.device_id()), "occurred_at": "not-a-timestamp"}
    hub = FakeHub()
    hub.pull_body = {"events": [event]}
    monkeypatch.setattr("agent_cli.main._hub_from_store", lambda _s: hub)
    with pytest.raises(HubError, match="pull event occurred_at is not a valid timestamp"):
        _sync_once(store)
    assert store.origin_cursor(store.device_id()) == 0
    assert store.rows("activity") == []


def test_sync_once_raises_hub_error_on_event_with_unknown_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: sibling of the unknown-op test above, for table.
    Store._write_in_txn rejects any table outside OWNED_TABLES for a local
    write; the hub-pull path must reject it too, instead of durably
    committing an orphaned row under a table name no application code ever
    reads back."""
    store = _paired_store(tmp_path)
    event = {**_valid_pull_event(store.device_id()), "table": "not_a_real_table"}
    hub = FakeHub()
    hub.pull_body = {"events": [event]}
    monkeypatch.setattr("agent_cli.main._hub_from_store", lambda _s: hub)
    with pytest.raises(HubError, match="pull event has an unknown table"):
        _sync_once(store)


def test_sync_once_raises_hub_error_on_event_with_unhashable_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: `table not in OWNED_TABLES` (a frozenset) requires
    table to be hashable - the exact bug class already fixed for
    payload["type"] elsewhere in this PR (isinstance(typ, str) guards in
    store.py). A JSON-decoded list/dict for table used to raise a raw
    TypeError instead of a catchable HubError."""
    store = _paired_store(tmp_path)
    event = {**_valid_pull_event(store.device_id()), "table": ["activity"]}
    hub = FakeHub()
    hub.pull_body = {"events": [event]}
    monkeypatch.setattr("agent_cli.main._hub_from_store", lambda _s: hub)
    with pytest.raises(HubError, match="pull event has an unknown table"):
        _sync_once(store)


@pytest.mark.parametrize("bad_row_id", ["", ["not", "a", "string"]])
def test_sync_once_raises_hub_error_on_event_with_invalid_row_id(
    bad_row_id: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: row_id was presence-checked only, like every other
    field here before it got its own validation - an empty string or a
    non-string value used to pass validation untouched and only fail later,
    as a raw type/constraint error from whatever eventually stores it."""
    store = _paired_store(tmp_path)
    event = {**_valid_pull_event(store.device_id()), "row_id": bad_row_id}
    hub = FakeHub()
    hub.pull_body = {"events": [event]}
    monkeypatch.setattr("agent_cli.main._hub_from_store", lambda _s: hub)
    with pytest.raises(HubError, match="pull event row_id is not a valid id"):
        _sync_once(store)


@pytest.mark.parametrize(
    "bad_seq",
    [
        "not-a-number",
        1e309,  # float('inf') once parsed - int() raises OverflowError, not ValueError
        True,  # bool is an int subclass in Python - int(True) == 1 would pass silently
        1.5,  # non-integral float - int(1.5) == 1 would silently truncate
    ],
)
def test_sync_once_raises_hub_error_on_non_numeric_origin_seq(
    bad_seq: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: int(event["origin_seq"]) used to run unguarded (raising a
    raw ValueError on garbage, OverflowError on an out-of-range float) and also
    silently accepted a bool or a fractional float as a valid sequence number."""
    store = _paired_store(tmp_path)
    hub = FakeHub()
    hub.pull_body = {"events": [{**_valid_pull_event(store.device_id()), "origin_seq": bad_seq}]}
    monkeypatch.setattr("agent_cli.main._hub_from_store", lambda _s: hub)
    with pytest.raises(HubError, match="non-numeric origin_seq"):
        _sync_once(store)


def test_sync_once_accepts_an_activity_payload_whose_type_is_not_a_wake_type_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: a payload whose "type" is an unhashable value (e.g. a
    list) used to make Store._maybe_wake's `payload.get("type") not in
    WAKE_ACTIVITY_TYPES` raise a raw TypeError from deep inside
    store.apply_remote - a shape neither _PULL_EVENT_FIELDS nor the broad
    except-Exception safety net's introduction actually fixed, just quietly
    converted into a permanent HubError retry loop (the cursor never advances,
    so the hub keeps re-serving the same event forever). _maybe_wake now treats
    a non-string type as simply "not a wake type" and lets the event apply
    normally instead of failing the whole transaction."""
    store = _paired_store(tmp_path)
    event = {**_valid_pull_event(store.device_id()), "payload": {"type": ["not", "hashable"]}}
    hub = FakeHub()
    hub.pull_body = {"events": [event]}
    monkeypatch.setattr("agent_cli.main._hub_from_store", lambda _s: hub)
    _sync_once(store)
    assert store.origin_cursor(store.device_id()) == 1
    row = store.row("activity", "x")
    assert row is not None


def test_sync_once_raises_hub_error_when_apply_remote_hits_an_unanticipated_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test for the general safety net itself: field presence is
    validated, but not every possible shape of every nested value can be
    anticipated and fixed at its root (unlike the non-string-type case above).
    A payload containing a value json.dumps cannot serialize (impossible from a
    real JSON hub response, but a stand-in for "something genuinely
    unanticipated") still reaches the broad except-Exception net around
    apply_remote/mark_origin and becomes a catchable HubError instead of an
    uncaught crash."""
    store = _paired_store(tmp_path)
    event = {**_valid_pull_event(store.device_id()), "payload": {"type": "message", "body": {1, 2, 3}}}
    hub = FakeHub()
    hub.pull_body = {"events": [event]}
    monkeypatch.setattr("agent_cli.main._hub_from_store", lambda _s: hub)
    with pytest.raises(HubError, match="pull event could not be applied"):
        _sync_once(store)


def test_sync_once_accepts_a_numeric_string_origin_seq(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: int(event["origin_seq"]) validated the value but the
    original (still-string) event dict was what reached store.apply_remote(). A
    numeric string like "1" passes int() cleanly, but _insert_event_idempotent's
    `event["origin_seq"] != last_seq + 1` is a plain Python != - "1" != 1 is
    always True - so a genuinely valid next sequence number raised a false
    "origin_seq gap" StoreError."""
    store = _paired_store(tmp_path)
    event = {
        **_valid_pull_event(store.device_id()),
        "origin_seq": "1",
        "table": "task",
        "row_id": "t1",
        "payload": {"id": "t1"},
    }
    hub = FakeHub()
    hub.pull_body = {"events": [event]}
    monkeypatch.setattr("agent_cli.main._hub_from_store", lambda _s: hub)
    _sync_once(store)
    assert store.origin_cursor(store.device_id()) == 1
    row = store.row("task", "t1")
    assert row is not None


def test_sync_once_raises_hub_error_on_non_list_snapshot_field(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression test: list(pulled.get("inbox") or []) used to run unguarded; a
    truthy non-iterable value (e.g. a malformed hub response sending an object
    instead of a list) raised a raw TypeError instead of a catchable HubError."""
    hub = FakeHub()
    hub.pull_body = {"events": [], "inbox": {"not": "a list"}}
    monkeypatch.setattr("agent_cli.main._hub_from_store", lambda _s: hub)
    with pytest.raises(HubError, match="inbox is not a list"):
        _sync_once(_paired_store(tmp_path))


def _valid_pull_row() -> dict[str, Any]:
    return {
        "table": "activity",
        "origin_device_id": "other",
        "row_id": "x",
        "payload": {},
        "updated_at": "2026-08-13T12:00:00Z",
    }


@pytest.mark.parametrize("field", ["table", "origin_device_id", "row_id", "payload", "updated_at"])
def test_sync_once_raises_hub_error_on_snapshot_missing_one_required_field(
    field: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: apply_replica_row indexes row["table"],
    row["origin_device_id"], row["row_id"], row["payload"], row["updated_at"]
    directly; a snapshot row missing any single one of those used to raise a raw
    KeyError instead of a catchable HubError raised before that call.
    Parametrized per field so a future accidental narrowing of _PULL_ROW_FIELDS
    to any one of them is still caught."""
    row = _valid_pull_row()
    del row[field]
    hub = FakeHub()
    hub.pull_body = {"events": [], "inbox": [row]}
    monkeypatch.setattr("agent_cli.main._hub_from_store", lambda _s: hub)
    with pytest.raises(HubError, match="pull snapshot is missing required fields"):
        _sync_once(_paired_store(tmp_path))


def test_sync_once_raises_hub_error_on_snapshot_with_unknown_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: the row-side sibling of
    test_sync_once_raises_hub_error_on_event_with_unknown_table."""
    row = {**_valid_pull_row(), "table": "not_a_real_table"}
    hub = FakeHub()
    hub.pull_body = {"events": [], "inbox": [row]}
    monkeypatch.setattr("agent_cli.main._hub_from_store", lambda _s: hub)
    with pytest.raises(HubError, match="pull snapshot has an unknown table"):
        _sync_once(_paired_store(tmp_path))


def test_sync_once_raises_hub_error_on_snapshot_with_unhashable_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: the row-side sibling of
    test_sync_once_raises_hub_error_on_event_with_unhashable_table."""
    row = {**_valid_pull_row(), "table": ["activity"]}
    hub = FakeHub()
    hub.pull_body = {"events": [], "inbox": [row]}
    monkeypatch.setattr("agent_cli.main._hub_from_store", lambda _s: hub)
    with pytest.raises(HubError, match="pull snapshot has an unknown table"):
        _sync_once(_paired_store(tmp_path))


@pytest.mark.parametrize("bad_row_id", ["", ["not", "a", "string"]])
def test_sync_once_raises_hub_error_on_snapshot_with_invalid_row_id(
    bad_row_id: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: the row-side sibling of
    test_sync_once_raises_hub_error_on_event_with_invalid_row_id."""
    row = {**_valid_pull_row(), "row_id": bad_row_id}
    hub = FakeHub()
    hub.pull_body = {"events": [], "inbox": [row]}
    monkeypatch.setattr("agent_cli.main._hub_from_store", lambda _s: hub)
    with pytest.raises(HubError, match="pull snapshot row_id is not a valid id"):
        _sync_once(_paired_store(tmp_path))


@pytest.mark.parametrize("bad_origin_device_id", ["", ["not", "a", "string"]])
def test_sync_once_raises_hub_error_on_snapshot_with_invalid_origin_device_id(
    bad_origin_device_id: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: unlike an event's origin_device_id (checked for
    ownership), a snapshot row's origin_device_id is legitimately foreign -
    but it was still only presence-checked, never type/emptiness-checked,
    before apply_replica_row compares it against this device's own id and
    stores it as the row's recorded owner."""
    row = {**_valid_pull_row(), "origin_device_id": bad_origin_device_id}
    hub = FakeHub()
    hub.pull_body = {"events": [], "inbox": [row]}
    monkeypatch.setattr("agent_cli.main._hub_from_store", lambda _s: hub)
    with pytest.raises(HubError, match="pull snapshot origin_device_id is not a valid id"):
        _sync_once(_paired_store(tmp_path))


def test_sync_once_raises_hub_error_on_snapshot_with_empty_updated_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: updated_at was presence-checked but not validated as a
    genuine timestamp. An empty string used to pass validation, get stored
    as-is on first insert (the row_data upsert only compares updated_at
    against an existing row on conflict, not on a fresh insert), and only
    fail later - on the next legitimate update to that same row, which would
    raise a raw ::timestamptz cast error trying to compare against it."""
    row = {**_valid_pull_row(), "updated_at": ""}
    hub = FakeHub()
    hub.pull_body = {"events": [], "inbox": [row]}
    monkeypatch.setattr("agent_cli.main._hub_from_store", lambda _s: hub)
    with pytest.raises(HubError, match="pull snapshot updated_at is not a valid timestamp"):
        _sync_once(_paired_store(tmp_path))


def test_sync_once_raises_hub_error_on_snapshot_with_unparseable_updated_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: sibling of the empty-string test above, for a
    non-empty but still bogus value. A non-blank string that isn't a real
    timestamp (e.g. "not-a-timestamp") used to pass a mere non-empty check,
    hitting the identical stuck-row failure mode later on the first
    conflicting update."""
    row = {**_valid_pull_row(), "updated_at": "not-a-timestamp"}
    hub = FakeHub()
    hub.pull_body = {"events": [], "inbox": [row]}
    monkeypatch.setattr("agent_cli.main._hub_from_store", lambda _s: hub)
    with pytest.raises(HubError, match="pull snapshot updated_at is not a valid timestamp"):
        _sync_once(_paired_store(tmp_path))


def test_sync_once_accepts_a_lowercase_z_updated_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression test: str.replace("Z", "+00:00") is case-sensitive, but
    RFC 3339 (SS5.6) permits a lowercase "z" as the UTC designator just as
    validly as an uppercase one. A genuinely valid, standards-conformant
    timestamp ending in a lowercase "z" used to be falsely rejected as an
    invalid timestamp instead of being accepted."""
    row = {**_valid_pull_row(), "updated_at": "2026-08-13T12:00:00z"}
    hub = FakeHub()
    hub.pull_body = {"events": [], "inbox": [row]}
    monkeypatch.setattr("agent_cli.main._hub_from_store", lambda _s: hub)
    store = _paired_store(tmp_path)
    _sync_once(store)
    row_stored = store.row("activity", "x")
    assert row_stored is not None


def test_sync_once_rejects_whole_batch_when_one_of_two_snapshots_is_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test for the validate-all-then-apply-all split itself: the
    row-side sibling of the equivalent tests/test_restore.py test. An earlier
    version of this PR validated and applied snapshot rows in the same loop,
    so a batch with one valid row before an invalid one would durably commit
    the valid row before raising HubError on the invalid one - a partial
    apply of an atomically-intended batch."""
    valid_row = {**_valid_pull_row(), "row_id": "valid-1"}
    invalid_row = {**_valid_pull_row(), "row_id": "invalid-1", "table": "not_a_real_table"}
    hub = FakeHub()
    hub.pull_body = {"events": [], "inbox": [valid_row, invalid_row]}
    monkeypatch.setattr("agent_cli.main._hub_from_store", lambda _s: hub)
    store = _paired_store(tmp_path)
    with pytest.raises(HubError, match="pull snapshot has an unknown table"):
        _sync_once(store)
    assert store.rows("activity") == []


def test_sync_once_rejects_whole_event_batch_when_one_of_two_events_is_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: the event-side sibling of
    test_sync_once_rejects_whole_batch_when_one_of_two_snapshots_is_invalid.
    The events loop kept validating and applying each event in the same
    iteration even after the snapshots loop was split into a
    validate-all-then-apply-all pattern to fix exactly this partial-apply
    shape - a batch with one valid event before an invalid one used to
    durably commit the valid event (and advance its origin cursor) before
    raising HubError on the invalid one."""
    store = _paired_store(tmp_path)
    valid_event = {**_valid_pull_event(store.device_id()), "row_id": "valid-1"}
    invalid_event = {
        **_valid_pull_event(store.device_id()),
        "origin_seq": 2,
        "row_id": "invalid-1",
        "table": "not_a_real_table",
    }
    hub = FakeHub()
    hub.pull_body = {"events": [valid_event, invalid_event]}
    monkeypatch.setattr("agent_cli.main._hub_from_store", lambda _s: hub)
    with pytest.raises(HubError, match="pull event has an unknown table"):
        _sync_once(store)
    assert store.origin_cursor(store.device_id()) == 0
    assert store.rows("activity") == []


def test_sync_once_accepts_a_snapshot_payload_whose_type_is_not_a_wake_type_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: the row-side sibling of
    test_sync_once_accepts_an_activity_payload_whose_type_is_not_a_wake_type_shape.
    apply_replica_row also calls _maybe_wake, so the same non-string-type fix
    must let the row apply normally instead of raising."""
    row = {**_valid_pull_row(), "payload": {"type": ["not", "hashable"]}}
    hub = FakeHub()
    hub.pull_body = {"events": [], "inbox": [row]}
    monkeypatch.setattr("agent_cli.main._hub_from_store", lambda _s: hub)
    store = _paired_store(tmp_path)
    _sync_once(store)
    stored = store.row("activity", "x")
    assert stored is not None


def test_sync_once_raises_hub_error_when_apply_replica_row_hits_an_unanticipated_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: the row-side sibling of
    test_sync_once_raises_hub_error_when_apply_remote_hits_an_unanticipated_shape,
    proving the broad safety net around store.apply_replica_row catches a
    genuinely unanticipated shape too, not just the event-side one."""
    row = {**_valid_pull_row(), "payload": {"type": "message", "body": {1, 2, 3}}}
    hub = FakeHub()
    hub.pull_body = {"events": [], "inbox": [row]}
    monkeypatch.setattr("agent_cli.main._hub_from_store", lambda _s: hub)
    with pytest.raises(HubError, match="pull snapshot could not be applied"):
        _sync_once(_paired_store(tmp_path))


def test_watch_pending_skips_other_executable_types(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_session(store)
    store.write(
        "activity",
        "insert",
        "issue-1",
        {
            "id": "issue-1",
            "session_id": "s1",
            "type": "issue.write",
            "payload": {"title": "x"},
            "execution_status": "pending",
        },
    )
    hub = FakeHub()
    assert scan_pending(store, hub) == []  # type: ignore[arg-type]
    row = store.row("activity", "issue-1")
    assert row is not None
    assert row["execution_status"] == "pending"
