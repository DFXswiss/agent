from __future__ import annotations

from fastapi.testclient import TestClient

from agent_core.db import loads
from tests.conftest import pair_device, sign_in


def event(device: str, seq: int, table: str = "task", row_id: str = "row-1", title: str = "Work") -> dict:
    return {
        "origin_device_id": device,
        "origin_seq": seq,
        "table": table,
        "op": "insert",
        "row_id": row_id,
        "payload": {"id": row_id, "title": title, "state": "open"},
        "occurred_at": "2026-08-13T12:00:00Z",
    }


def test_push_pull_restore_and_team_isolation(hub: TestClient) -> None:
    alice_dev = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    bob_dev = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    dave_dev = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    alice = pair_device(hub, "code-alice", alice_dev, "alice-laptop")
    bob = pair_device(hub, "code-bob", bob_dev, "bob-laptop")
    dave = pair_device(hub, "code-dave", dave_dev, "dave-laptop")

    r = hub.post(
        "/sync/push",
        headers={"Authorization": f"Bearer {alice}"},
        json={"events": [event(alice_dev, 1, title="Alice task")]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] == 1

    hub.post(
        "/sync/push",
        headers={"Authorization": f"Bearer {bob}"},
        json={"events": [event(bob_dev, 1, row_id="row-b", title="Bob task")]},
    )
    hub.post(
        "/sync/push",
        headers={"Authorization": f"Bearer {dave}"},
        json={"events": [event(dave_dev, 1, row_id="row-d", title="Dave secret")]},
    )

    alice_pull = hub.get("/sync/pull", headers={"Authorization": f"Bearer {alice}"})
    alice_body = alice_pull.json()
    titles = {e["payload"]["title"] for e in alice_body["events"]}
    assert titles == {"Alice task"}
    assert "Bob task" not in titles
    assert "Dave secret" not in titles
    assert alice_body["inbox"] == []
    assert alice_body["pings"] == []

    dave_pull = hub.get("/sync/pull", headers={"Authorization": f"Bearer {dave}"})
    dave_titles = {e["payload"]["title"] for e in dave_pull.json()["events"]}
    assert dave_titles == {"Dave secret"}

    restore = hub.get("/sync/restore", headers={"Authorization": f"Bearer {alice}"})
    body = restore.json()
    assert body["login"] == "alice"
    assert "events" not in body
    assert len(body["own_events"]) == 1
    restore_titles = {e["payload"]["title"] for e in body["own_events"]}
    assert restore_titles == {"Alice task"}
    sign_in(hub, "code-alice")
    website = hub.get("/api/state").json()
    site_titles = {t["title"] for t in website["task"]}
    assert site_titles == {"Alice task", "Bob task"}


def test_foreign_origin_rejected(hub: TestClient) -> None:
    alice_dev = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa01"
    token = pair_device(hub, "code-alice", alice_dev)
    r = hub.post(
        "/sync/push",
        headers={"Authorization": f"Bearer {token}"},
        json={"events": [event("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbb01", 1)]},
    )
    assert r.status_code == 403


def test_device_push_rejects_reserved_web_origin(hub: TestClient) -> None:
    device_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa40"
    token = pair_device(hub, "code-alice", device_id)
    pushed = hub.post(
        "/sync/push",
        headers={"Authorization": f"Bearer {token}"},
        json={"events": [event("WeB:alice", 1)]},
    )
    assert pushed.status_code == 403
    assert pushed.json()["detail"] == "web: origins are reserved"


def test_gap_rejected_idempotent_ok(hub: TestClient) -> None:
    dev = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa02"
    token = pair_device(hub, "code-alice", dev)
    first = event(dev, 1)
    assert hub.post("/sync/push", headers={"Authorization": f"Bearer {token}"}, json={"events": [first]}).status_code == 200
    gap = hub.post("/sync/push", headers={"Authorization": f"Bearer {token}"}, json={"events": [event(dev, 3)]})
    assert gap.status_code == 409
    again = hub.post("/sync/push", headers={"Authorization": f"Bearer {token}"}, json={"events": [first]})
    assert again.status_code == 200
    clash = event(dev, 1, title="other")
    bad = hub.post("/sync/push", headers={"Authorization": f"Bearer {token}"}, json={"events": [clash]})
    assert bad.status_code == 409
    time_clash = event(dev, 1)
    time_clash["occurred_at"] = "2026-08-13T12:00:01Z"
    assert hub.post("/sync/push", headers={"Authorization": f"Bearer {token}"}, json={"events": [time_clash]}).status_code == 409


def test_cannot_overwrite_teammate_row(hub: TestClient) -> None:
    alice_dev = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa03"
    bob_dev = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbb03"
    alice = pair_device(hub, "code-alice", alice_dev)
    bob = pair_device(hub, "code-bob", bob_dev)
    shared = "shared-row"
    assert (
        hub.post(
            "/sync/push",
            headers={"Authorization": f"Bearer {alice}"},
            json={"events": [event(alice_dev, 1, row_id=shared, title="Alice")]},
        ).status_code
        == 200
    )
    stolen = hub.post(
        "/sync/push",
        headers={"Authorization": f"Bearer {bob}"},
        json={"events": [event(bob_dev, 1, row_id=shared, title="Bob stole it")]},
    )
    assert stolen.status_code == 403


def test_ping_team_only(hub: TestClient) -> None:
    sign_alice = hub
    from tests.conftest import sign_in

    sign_in(sign_alice, "code-alice")
    ok = sign_alice.post("/api/pings", json={"to": "bob", "kind": "review-request", "body": "please review"})
    assert ok.status_code == 200, ok.text
    denied = sign_alice.post("/api/pings", json={"to": "dave", "kind": "ping"})
    assert denied.status_code == 403
    replica = hub.app.state.hub.store.query_one(
        "SELECT origin_device_id FROM row_replica WHERE table_name = 'ping' AND row_id = ?",
        (ok.json()["id"],),
    )
    assert replica["origin_device_id"] == "web:alice"

    sign_in(sign_alice, "code-bob")
    ping_id = ok.json()["id"]
    ack = sign_alice.post(f"/api/pings/{ping_id}/ack")
    assert ack.status_code == 200
    state = sign_alice.get("/api/state").json()
    found = [p for p in state["pings"] if p["id"] == ping_id]
    assert len(found) == 1
    assert found[0]["acked_at"]


def test_ping_ack_uses_web_origin_not_device_seq(hub: TestClient) -> None:
    alice_dev = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa20"
    alice = pair_device(hub, "code-alice", alice_dev)
    first = event(alice_dev, 1, row_id="task-ack-seq", title="own")
    assert (
        hub.post("/sync/push", headers={"Authorization": f"Bearer {alice}"}, json={"events": [first]}).status_code
        == 200
    )
    ping = {
        "origin_device_id": alice_dev,
        "origin_seq": 2,
        "table": "ping",
        "op": "insert",
        "row_id": "ping-device",
        "payload": {
            "id": "ping-device",
            "from_login": "alice",
            "to_login": "bob",
            "kind": "ping",
            "body": "hi",
        },
        "occurred_at": "2026-08-13T12:00:00Z",
    }
    assert hub.post("/sync/push", headers={"Authorization": f"Bearer {alice}"}, json={"events": [ping]}).status_code == 200
    sign_in(hub, "code-bob")
    import asyncio

    before_web = hub.app.state.hub.store.query_one(
        "SELECT COALESCE(MAX(origin_seq), 0) AS m FROM ledger_event WHERE origin_device_id = ?",
        ("web:bob",),
    )
    seen: asyncio.Queue[dict] = asyncio.Queue(maxsize=8)
    hub.app.state.hub.queues.append(("alice", seen))
    try:
        ack = hub.post("/api/pings/ping-device/ack")
        assert ack.status_code == 200, ack.text
        fanout = seen.get_nowait()
    finally:
        item = ("alice", seen)
        if item in hub.app.state.hub.queues:
            hub.app.state.hub.queues.remove(item)
    assert fanout["type"] == "ping"
    assert fanout["id"] == "ping-device"
    assert fanout["from"] == "alice"
    assert fanout["to"] == "bob"
    assert fanout["login"] == "alice"
    replica = hub.app.state.hub.store.query_one(
        "SELECT origin_device_id, payload FROM row_replica WHERE table_name = 'ping' AND row_id = ?",
        ("ping-device",),
    )
    assert replica["origin_device_id"] == alice_dev
    replica_payload = loads(replica["payload"])
    assert replica_payload["acked_at"] == ack.json()["acked_at"]
    ack_event = hub.app.state.hub.store.query_one(
        "SELECT origin_device_id, origin_seq, payload FROM ledger_event WHERE table_name = 'ping' AND op = 'update' AND row_id = ?",
        ("ping-device",),
    )
    assert ack_event["origin_device_id"] == "web:bob"
    assert ack_event["origin_seq"] == int(before_web["m"]) + 1
    assert loads(ack_event["payload"])["acked_at"] == ack.json()["acked_at"]
    again = event(alice_dev, 3, row_id="task-after-ack", title="after")
    pushed = hub.post("/sync/push", headers={"Authorization": f"Bearer {alice}"}, json={"events": [again]})
    assert pushed.status_code == 200, pushed.text


def _session_event(device: str, seq: int, session_id: str, status: str = "active") -> dict:
    return {
        "origin_device_id": device,
        "origin_seq": seq,
        "table": "session",
        "op": "insert",
        "row_id": session_id,
        "payload": {"id": session_id, "kind": "human", "status": status},
        "occurred_at": "2026-08-13T12:00:00Z",
    }


def _activity_event(device: str, seq: int, row_id: str, payload: dict) -> dict:
    return {
        "origin_device_id": device,
        "origin_seq": seq,
        "table": "activity",
        "op": "insert",
        "row_id": row_id,
        "payload": payload,
        "occurred_at": "2026-08-13T12:00:00Z",
    }


def _recv_until(ws, pred, limit: int = 20) -> dict:
    for _ in range(limit):
        msg = ws.receive_json()
        if pred(msg):
            return msg
    raise AssertionError("expected message not received")


def test_open_session_id_is_unique(hub: TestClient) -> None:
    alice_dev = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa10"
    bob_dev = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbb10"
    alice = pair_device(hub, "code-alice", alice_dev)
    bob = pair_device(hub, "code-bob", bob_dev)
    first = _session_event(alice_dev, 1, "shared-session")
    assert (
        hub.post(
            "/sync/push",
            headers={"Authorization": f"Bearer {alice}"},
            json={"events": [first]},
        ).status_code
        == 200
    )
    assert (
        hub.post(
            "/sync/push",
            headers={"Authorization": f"Bearer {alice}"},
            json={"events": [first]},
        ).status_code
        == 200
    )
    clash = hub.post(
        "/sync/push",
        headers={"Authorization": f"Bearer {bob}"},
        json={"events": [_session_event(bob_dev, 1, "shared-session")]},
    )
    assert clash.status_code == 409


def test_session_mail_inbox_and_visibility(hub: TestClient) -> None:
    alice_dev = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa11"
    bob_dev = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbb11"
    dave_dev = "dddddddd-dddd-dddd-dddd-dddddddddd11"
    alice = pair_device(hub, "code-alice", alice_dev)
    bob = pair_device(hub, "code-bob", bob_dev)
    dave = pair_device(hub, "code-dave", dave_dev)
    assert (
        hub.post(
            "/sync/push",
            headers={"Authorization": f"Bearer {alice}"},
            json={"events": [_session_event(alice_dev, 1, "sess-a")]},
        ).status_code
        == 200
    )
    assert (
        hub.post(
            "/sync/push",
            headers={"Authorization": f"Bearer {bob}"},
            json={"events": [_session_event(bob_dev, 1, "sess-b")]},
        ).status_code
        == 200
    )
    hidden = hub.post(
        "/sync/push",
        headers={"Authorization": f"Bearer {dave}"},
        json={
            "events": [
                _activity_event(
                    dave_dev,
                    1,
                    "mail-hidden",
                    {
                        "id": "mail-hidden",
                        "session_id": "sess-d",
                        "type": "message",
                        "payload": {"to_session": "sess-a", "body": "nope"},
                    },
                )
            ]
        },
    )
    assert hidden.status_code == 403
    missing = hub.post(
        "/sync/push",
        headers={"Authorization": f"Bearer {alice}"},
        json={
            "events": [
                _activity_event(
                    alice_dev,
                    2,
                    "mail-missing",
                    {
                        "id": "mail-missing",
                        "session_id": "sess-a",
                        "type": "message",
                        "payload": {"to_session": "no-such-session", "body": "hi"},
                    },
                )
            ]
        },
    )
    assert missing.status_code == 404
    ok = hub.post(
        "/sync/push",
        headers={"Authorization": f"Bearer {alice}"},
        json={
            "events": [
                _activity_event(
                    alice_dev,
                    2,
                    "mail-1",
                    {
                        "id": "mail-1",
                        "session_id": "sess-a",
                        "type": "message",
                        "payload": {"to_session": "sess-b", "body": "hello bob"},
                    },
                )
            ]
        },
    )
    assert ok.status_code == 200, ok.text
    bob_pull = hub.get("/sync/pull", headers={"Authorization": f"Bearer {bob}"}).json()
    inbox_ids = {row["row_id"] for row in bob_pull["inbox"]}
    assert "mail-1" in inbox_ids
    assert "sess-a" in inbox_ids
    alice_pull = hub.get("/sync/pull", headers={"Authorization": f"Bearer {alice}"}).json()
    assert alice_pull["inbox"] == []


def test_session_mail_update_cannot_bypass_visibility(hub: TestClient) -> None:
    dave_dev = "dddddddd-dddd-dddd-dddd-dddddddddd14"
    alice_dev = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa14"
    alice = pair_device(hub, "code-alice", alice_dev)
    dave = pair_device(hub, "code-dave", dave_dev)
    assert (
        hub.post(
            "/sync/push",
            headers={"Authorization": f"Bearer {alice}"},
            json={"events": [_session_event(alice_dev, 1, "sess-a14")]},
        ).status_code
        == 200
    )
    note = {
        "id": "note-1",
        "session_id": "sess-d14",
        "type": "investigate.step",
        "payload": {"text": "local"},
    }
    assert (
        hub.post(
            "/sync/push",
            headers={"Authorization": f"Bearer {dave}"},
            json={"events": [_activity_event(dave_dev, 1, "note-1", note)]},
        ).status_code
        == 200
    )
    note["type"] = "message"
    note["payload"] = {"to_session": "sess-a14", "body": "sneak"}
    sneak_event = _activity_event(dave_dev, 2, "note-1", note)
    sneak_event["op"] = "update"
    sneak = hub.post(
        "/sync/push",
        headers={"Authorization": f"Bearer {dave}"},
        json={"events": [sneak_event]},
    )
    assert sneak.status_code == 403
    alice_pull = hub.get("/sync/pull", headers={"Authorization": f"Bearer {alice}"}).json()
    assert all(row.get("row_id") != "note-1" for row in alice_pull["inbox"])


def test_restore_pings_are_sent_or_received_only(hub: TestClient) -> None:
    alice_dev = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa12"
    bob_dev = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbb12"
    dave_dev = "dddddddd-dddd-dddd-dddd-dddddddddd12"
    alice = pair_device(hub, "code-alice", alice_dev)
    bob = pair_device(hub, "code-bob", bob_dev)
    dave = pair_device(hub, "code-dave", dave_dev)
    sign_in(hub, "code-alice")
    ping = hub.post("/api/pings", json={"to": "bob", "kind": "ping", "body": "hi"})
    assert ping.status_code == 200, ping.text
    ping_id = ping.json()["id"]
    alice_ids = {row["row_id"] for row in hub.get("/sync/restore", headers={"Authorization": f"Bearer {alice}"}).json()["pings"]}
    bob_ids = {row["row_id"] for row in hub.get("/sync/restore", headers={"Authorization": f"Bearer {bob}"}).json()["pings"]}
    dave_ids = {row["row_id"] for row in hub.get("/sync/restore", headers={"Authorization": f"Bearer {dave}"}).json()["pings"]}
    assert ping_id in alice_ids
    assert ping_id in bob_ids
    assert ping_id not in dave_ids


def test_device_cannot_spoof_ping_from_login(hub: TestClient) -> None:
    dave_dev = "dddddddd-dddd-dddd-dddd-dddddddddd15"
    dave = pair_device(hub, "code-dave", dave_dev)
    spoof = {
        "origin_device_id": dave_dev,
        "origin_seq": 1,
        "table": "ping",
        "op": "insert",
        "row_id": "ping-spoof",
        "payload": {
            "id": "ping-spoof",
            "from_login": "alice",
            "to_login": "bob",
            "kind": "ping",
            "body": "nope",
        },
        "occurred_at": "2026-08-13T12:00:00Z",
    }
    r = hub.post("/sync/push", headers={"Authorization": f"Bearer {dave}"}, json={"events": [spoof]})
    assert r.status_code == 403
    mixed = dict(spoof)
    mixed["payload"] = dict(spoof["payload"])
    mixed["payload"]["from_login"] = "dave"
    mixed["payload"]["to_login"] = "Dave"
    case = hub.post("/sync/push", headers={"Authorization": f"Bearer {dave}"}, json={"events": [mixed]})
    assert case.status_code == 400
    missing = dict(spoof)
    missing["op"] = "update"
    missing["payload"] = dict(spoof["payload"])
    missing["payload"]["from_login"] = "dave"
    ghost = hub.post("/sync/push", headers={"Authorization": f"Bearer {dave}"}, json={"events": [missing]})
    assert ghost.status_code == 404


def test_subscriptions_put_validation(hub: TestClient) -> None:
    alice_dev = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa30"
    alice = pair_device(hub, "code-alice", alice_dev)
    unknown = hub.put(
        "/sync/subscriptions",
        headers={"Authorization": f"Bearer {alice}"},
        json={"subscriptions": [{"match": {"payload.unknown": "x"}}]},
    )
    assert unknown.status_code == 400
    assert unknown.json()["detail"] == "unknown match path"
    empty_in = hub.put(
        "/sync/subscriptions",
        headers={"Authorization": f"Bearer {alice}"},
        json={"subscriptions": [{"match": {"type": {"in": []}}}]},
    )
    assert empty_in.status_code == 400
    no_auth = hub.put("/sync/subscriptions", json={"subscriptions": []})
    assert no_auth.status_code == 401


def test_subscriptions_put_get_replace(hub: TestClient) -> None:
    alice_dev = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa31"
    alice = pair_device(hub, "code-alice", alice_dev)
    first = hub.put(
        "/sync/subscriptions",
        headers={"Authorization": f"Bearer {alice}"},
        json={
            "subscriptions": [
                {"match": {"type": "investigate.step"}},
                {"match": {"type": "pr.open", "payload.repo": "owner/repo"}},
            ]
        },
    )
    assert first.status_code == 200, first.text
    assert first.json()["subscriptions"] == [
        {"match": {"type": "investigate.step"}},
        {"match": {"payload.repo": "owner/repo", "type": "pr.open"}},
    ]
    got = hub.get("/sync/subscriptions", headers={"Authorization": f"Bearer {alice}"})
    assert got.status_code == 200
    assert got.json() == first.json()
    replaced = hub.put(
        "/sync/subscriptions",
        headers={"Authorization": f"Bearer {alice}"},
        json={"subscriptions": [{"match": {"type": "task.open"}}]},
    )
    assert replaced.status_code == 200
    assert replaced.json()["subscriptions"] == [{"match": {"type": "task.open"}}]
    again = hub.get("/sync/subscriptions", headers={"Authorization": f"Bearer {alice}"})
    assert again.json()["subscriptions"] == [{"match": {"type": "task.open"}}]
    assert all(item["match"].get("type") != "investigate.step" for item in again.json()["subscriptions"])


def test_subscriptions_pull_and_query_visibility(hub: TestClient) -> None:
    alice_dev = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa32"
    bob_dev = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbb32"
    dave_dev = "dddddddd-dddd-dddd-dddd-dddddddddd32"
    alice = pair_device(hub, "code-alice", alice_dev)
    bob = pair_device(hub, "code-bob", bob_dev)
    dave = pair_device(hub, "code-dave", dave_dev)

    bob_push = hub.post(
        "/sync/push",
        headers={"Authorization": f"Bearer {bob}"},
        json={
            "events": [
                _activity_event(
                    bob_dev,
                    1,
                    "act-step-32",
                    {
                        "id": "act-step-32",
                        "session_id": "sess-b32",
                        "type": "investigate.step",
                        "payload": {"text": "look"},
                    },
                ),
                event(bob_dev, 2, table="task", row_id="task-b32", title="Bob task"),
            ]
        },
    )
    assert bob_push.status_code == 200, bob_push.text
    dave_push = hub.post(
        "/sync/push",
        headers={"Authorization": f"Bearer {dave}"},
        json={
            "events": [
                _activity_event(
                    dave_dev,
                    1,
                    "act-dave-32",
                    {
                        "id": "act-dave-32",
                        "session_id": "sess-d32",
                        "type": "investigate.step",
                        "payload": {"text": "secret"},
                    },
                )
            ]
        },
    )
    assert dave_push.status_code == 200, dave_push.text

    matcher = {"subscriptions": [{"match": {"type": "investigate.step"}}]}
    assert (
        hub.put("/sync/subscriptions", headers={"Authorization": f"Bearer {alice}"}, json=matcher).status_code
        == 200
    )
    assert (
        hub.put("/sync/subscriptions", headers={"Authorization": f"Bearer {dave}"}, json=matcher).status_code
        == 200
    )

    alice_pull = hub.get("/sync/pull", headers={"Authorization": f"Bearer {alice}"}).json()
    assert "subscriptions" in alice_pull
    alice_sub_ids = {row["row_id"] for row in alice_pull["subscriptions"]}
    assert "act-step-32" in alice_sub_ids
    assert "task-b32" not in alice_sub_ids
    assert "act-dave-32" not in alice_sub_ids

    dave_pull = hub.get("/sync/pull", headers={"Authorization": f"Bearer {dave}"}).json()
    dave_sub_ids = {row["row_id"] for row in dave_pull["subscriptions"]}
    assert "act-step-32" not in dave_sub_ids
    assert "act-dave-32" in dave_sub_ids
    assert all(row.get("github_login") not in {"alice", "bob"} for row in dave_pull["subscriptions"])

    query_body = {"match": {"type": "investigate.step"}}
    alice_query = hub.post(
        "/sync/query",
        headers={"Authorization": f"Bearer {alice}"},
        json=query_body,
    )
    assert alice_query.status_code == 200, alice_query.text
    alice_rows = {row["row_id"] for row in alice_query.json()["rows"]}
    assert "act-step-32" in alice_rows
    assert "act-dave-32" not in alice_rows

    dave_query = hub.post(
        "/sync/query",
        headers={"Authorization": f"Bearer {dave}"},
        json=query_body,
    )
    assert dave_query.status_code == 200, dave_query.text
    dave_rows = {row["row_id"] for row in dave_query.json()["rows"]}
    assert "act-step-32" not in dave_rows
    assert all(row.get("github_login") not in {"alice", "bob"} for row in dave_query.json()["rows"])

    restore = hub.get("/sync/restore", headers={"Authorization": f"Bearer {alice}"}).json()
    assert set(restore.keys()) >= {"own_events", "inbox", "pings", "device_id", "login"}
    assert "subscriptions" not in restore


def test_subscriptions_ws_push(hub: TestClient) -> None:
    alice_dev = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa33"
    bob_dev = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbb33"
    alice = pair_device(hub, "code-alice", alice_dev)
    bob = pair_device(hub, "code-bob", bob_dev)

    matcher = {"subscriptions": [{"match": {"type": "investigate.step"}}]}
    assert (
        hub.put("/sync/subscriptions", headers={"Authorization": f"Bearer {alice}"}, json=matcher).status_code
        == 200
    )

    row_id = "act-step-33"
    with hub.websocket_connect(f"/sync/ws?token={alice}") as ws:
        push = hub.post(
            "/sync/push",
            headers={"Authorization": f"Bearer {bob}"},
            json={
                "events": [
                    _activity_event(
                        bob_dev,
                        1,
                        row_id,
                        {
                            "id": row_id,
                            "session_id": "sess-b33",
                            "type": "investigate.step",
                            "payload": {"text": "look"},
                        },
                    )
                ]
            },
        )
        assert push.status_code == 200, push.text
        frame = _recv_until(
            ws,
            lambda m: m.get("type") == "subscription"
            and any(row.get("row_id") == row_id for row in (m.get("rows") or [])),
        )
        assert frame["type"] == "subscription"
        assert any(row.get("row_id") == row_id for row in frame["rows"])
