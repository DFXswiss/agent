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
