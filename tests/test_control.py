from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from tests.conftest import pair_device, sign_in


def session_event(
    device: str,
    seq: int,
    row_id: str = "sess-1",
    *,
    kind: str = "implement",
    status: str = "running",
    host: str = "laptop",
) -> dict:
    return {
        "origin_device_id": device,
        "origin_seq": seq,
        "table": "session",
        "op": "insert",
        "row_id": row_id,
        "payload": {
            "id": row_id,
            "kind": kind,
            "status": status,
            "host": host,
            "last_seen_at": "2026-08-13T12:00:00Z",
        },
        "occurred_at": "2026-08-13T12:00:00Z",
    }


def _recv_until(ws, pred, limit: int = 20) -> dict:
    for _ in range(limit):
        msg = ws.receive_json()
        if pred(msg):
            return msg
    raise AssertionError("expected message not received")


def _wait_control_connected(client: TestClient, session_id: str, *, expect: bool = True, attempts: int = 50) -> dict:
    last: dict = {}
    for _ in range(attempts):
        res = client.get(f"/api/sessions/{session_id}")
        assert res.status_code == 200, res.text
        last = res.json()
        if bool(last.get("control_connected")) is expect:
            return last
    raise AssertionError(f"control_connected did not become {expect}: {last}")


def _wait_terminal_chunks(client: TestClient, session_id: str, *, min_chunks: int = 1, attempts: int = 50) -> dict:
    last: dict = {}
    for _ in range(attempts):
        res = client.get(f"/api/sessions/{session_id}/terminal")
        assert res.status_code == 200, res.text
        last = res.json()
        if len(last.get("chunks") or []) >= min_chunks:
            return last
    raise AssertionError(f"terminal chunks did not appear: {last}")


@pytest.mark.parametrize("session_id", ["a/b", "..", "x y"])
def test_query_session_routes_preserve_path_unsafe_ids(
    hub: TestClient, session_id: str
) -> None:
    device_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaa000"
    token = pair_device(hub, "code-alice", device_id)
    pushed = hub.post(
        "/sync/push",
        headers={"Authorization": f"Bearer {token}"},
        json={"events": [session_event(device_id, 1, session_id)]},
    )
    assert pushed.status_code == 200, pushed.text
    sign_in(hub, "code-alice")

    detail = hub.get("/api/session", params={"id": session_id})
    terminal = hub.get("/api/session/terminal", params={"id": session_id})
    control = hub.post(
        "/api/session/control", params={"id": session_id}, json={"action": "stop"}
    )

    assert detail.status_code == 200, detail.text
    assert detail.json()["id"] == session_id
    assert terminal.status_code == 200, terminal.text
    assert terminal.json() == {"session_id": session_id, "chunks": []}
    assert control.status_code == 409, control.text
    assert control.json() == {"detail": "owning device is not control-connected"}


def test_query_session_routes_keep_session_auth(hub: TestClient) -> None:
    detail = hub.get("/api/session", params={"id": "session"})
    terminal = hub.get("/api/session/terminal", params={"id": "session"})
    control = hub.post(
        "/api/session/control", params={"id": "session"}, json={"action": "stop"}
    )

    for response in (detail, terminal, control):
        assert response.status_code == 401
        assert response.json() == {"detail": "not signed in"}


def test_teammate_can_watch_owner_can_control(hub: TestClient) -> None:
    alice_dev = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaa001"
    bob_dev = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbb001"
    alice_token = pair_device(hub, "code-alice", alice_dev, "alice-laptop")
    pair_device(hub, "code-bob", bob_dev, "bob-laptop")

    sid = "sess-alice-1"
    assert (
        hub.post(
            "/sync/push",
            headers={"Authorization": f"Bearer {alice_token}"},
            json={"events": [session_event(alice_dev, 1, sid)]},
        ).status_code
        == 200
    )

    sign_in(hub, "code-bob")
    bob_detail = hub.get(f"/api/sessions/{sid}")
    assert bob_detail.status_code == 200, bob_detail.text
    bob_body = bob_detail.json()
    assert bob_body["id"] == sid
    assert bob_body["can_control"] is False
    assert bob_body["_github_login"] == "alice"
    assert bob_body["_origin_device_id"] == alice_dev

    sign_in(hub, "code-alice")
    alice_detail = hub.get(f"/api/sessions/{sid}")
    assert alice_detail.status_code == 200
    assert alice_detail.json()["can_control"] is True

    state = hub.get("/api/state").json()
    sessions = {s["id"]: s for s in state["session"]}
    assert sessions[sid]["can_control"] is True
    assert sessions[sid]["control_connected"] is False


def test_outsider_session_is_404(hub: TestClient) -> None:
    alice_dev = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaa002"
    alice_token = pair_device(hub, "code-alice", alice_dev)
    sid = "sess-secret"
    assert (
        hub.post(
            "/sync/push",
            headers={"Authorization": f"Bearer {alice_token}"},
            json={"events": [session_event(alice_dev, 1, sid)]},
        ).status_code
        == 200
    )

    sign_in(hub, "code-dave")
    assert hub.get(f"/api/sessions/{sid}").status_code == 404
    assert hub.get(f"/api/sessions/{sid}/terminal").status_code == 404
    assert hub.post(f"/api/sessions/{sid}/control", json={"action": "stop"}).status_code == 404


def test_teammate_control_forbidden(hub: TestClient) -> None:
    alice_dev = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaa003"
    bob_dev = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbb003"
    alice_token = pair_device(hub, "code-alice", alice_dev)
    pair_device(hub, "code-bob", bob_dev)
    sid = "sess-alice-3"
    hub.post(
        "/sync/push",
        headers={"Authorization": f"Bearer {alice_token}"},
        json={"events": [session_event(alice_dev, 1, sid)]},
    )
    sign_in(hub, "code-bob")
    denied = hub.post(f"/api/sessions/{sid}/control", json={"action": "stop"})
    assert denied.status_code == 403


def test_control_without_ready_is_409(hub: TestClient) -> None:
    alice_dev = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaa004"
    alice_token = pair_device(hub, "code-alice", alice_dev)
    sid = "sess-alice-4"
    hub.post(
        "/sync/push",
        headers={"Authorization": f"Bearer {alice_token}"},
        json={"events": [session_event(alice_dev, 1, sid)]},
    )
    sign_in(hub, "code-alice")
    r = hub.post(f"/api/sessions/{sid}/control", json={"action": "input", "data": "x"})
    assert r.status_code == 409
    assert r.json()["detail"] == "owning device is not control-connected"


def test_control_ready_forwards_input(hub: TestClient) -> None:
    alice_dev = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaa005"
    alice_token = pair_device(hub, "code-alice", alice_dev)
    sid = "sess-alice-5"
    hub.post(
        "/sync/push",
        headers={"Authorization": f"Bearer {alice_token}"},
        json={"events": [session_event(alice_dev, 1, sid)]},
    )
    sign_in(hub, "code-alice")
    with hub.websocket_connect(f"/sync/ws?token={alice_token}") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["device_id"] == alice_dev
        ws.send_json({"type": "control-ready"})
        _wait_control_connected(hub, sid, expect=True)
        r = hub.post(f"/api/sessions/{sid}/control", json={"action": "input", "data": "ls\n"})
        assert r.status_code == 202, r.text
        assert r.json() == {"queued": True}
        frame = _recv_until(ws, lambda m: m.get("type") == "control")
        assert frame["session_id"] == sid
        assert frame["action"] == "input"
        assert frame["payload"] == {"data": "ls\n"}


def test_terminal_ring_visibility(hub: TestClient) -> None:
    alice_dev = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaa006"
    bob_dev = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbb006"
    alice_token = pair_device(hub, "code-alice", alice_dev)
    pair_device(hub, "code-bob", bob_dev)
    sid = "sess-alice-6"
    hub.post(
        "/sync/push",
        headers={"Authorization": f"Bearer {alice_token}"},
        json={"events": [session_event(alice_dev, 1, sid)]},
    )
    chunk = base64.b64encode(b"hello-term").decode("ascii")
    sign_in(hub, "code-alice")
    with hub.websocket_connect(f"/sync/ws?token={alice_token}") as ws:
        ws.receive_json()
        ws.send_json({"type": "control-ready"})
        ws.send_json({"type": "terminal", "session_id": sid, "seq": 1, "data": chunk})
        body = _wait_terminal_chunks(hub, sid, min_chunks=1)
        assert body["session_id"] == sid
        assert body["chunks"] == [{"seq": 1, "data": chunk}]

    sign_in(hub, "code-bob")
    bob_term = hub.get(f"/api/sessions/{sid}/terminal")
    assert bob_term.status_code == 200
    assert bob_term.json()["chunks"] == [{"seq": 1, "data": chunk}]

    sign_in(hub, "code-dave")
    assert hub.get(f"/api/sessions/{sid}/terminal").status_code == 404


def test_second_device_does_not_receive_control(hub: TestClient) -> None:
    alice_a = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaa007"
    alice_b = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaa017"
    token_a = pair_device(hub, "code-alice", alice_a, "alice-a")
    token_b = pair_device(hub, "code-alice", alice_b, "alice-b")
    sid = "sess-alice-7"
    hub.post(
        "/sync/push",
        headers={"Authorization": f"Bearer {token_a}"},
        json={"events": [session_event(alice_a, 1, sid)]},
    )
    sign_in(hub, "code-alice")

    with hub.websocket_connect(f"/sync/ws?token={token_b}") as ws_b:
        ws_b.receive_json()
        ws_b.send_json({"type": "control-ready"})
        # B is control-ready, but origin is A — still not control-connected for this session.
        # HTTP GET pumps the TestClient websocket receive loop.
        for _ in range(50):
            hub.get(f"/api/sessions/{sid}")
            if hub.app.state.hub.control_ready.get(alice_b) is not None:
                break
        assert hub.app.state.hub.control_ready.get(alice_b) is not None
        only_b = hub.post(f"/api/sessions/{sid}/control", json={"action": "stop"})
        assert only_b.status_code == 409
        assert only_b.json()["detail"] == "owning device is not control-connected"
        assert hub.get(f"/api/sessions/{sid}").json()["control_connected"] is False

    with hub.websocket_connect(f"/sync/ws?token={token_a}") as ws_a:
        with hub.websocket_connect(f"/sync/ws?token={token_b}") as ws_b:
            assert ws_a.receive_json()["type"] == "hello"
            assert ws_b.receive_json()["type"] == "hello"
            ws_a.send_json({"type": "control-ready"})
            ws_b.send_json({"type": "control-ready"})
            _wait_control_connected(hub, sid, expect=True)
            assert hub.app.state.hub.control_ready.get(alice_a) is not None
            assert hub.app.state.hub.control_ready.get(alice_b) is not None
            assert hub.app.state.hub.control_ready[alice_a] is not hub.app.state.hub.control_ready[alice_b]
            r = hub.post(
                f"/api/sessions/{sid}/control",
                json={"action": "input", "key": "enter"},
            )
            assert r.status_code == 202, r.text
            frame = _recv_until(ws_a, lambda m: m.get("type") == "control")
            assert frame["action"] == "input"
            assert frame["payload"] == {"key": "enter"}
            assert frame["session_id"] == sid
            qb = hub.app.state.hub.control_ready[alice_b]
            assert qb.empty()


def test_start_provider_grok_forwards(hub: TestClient) -> None:
    alice_dev = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaa009"
    alice_token = pair_device(hub, "code-alice", alice_dev)
    sid = "sess-alice-9"
    hub.post(
        "/sync/push",
        headers={"Authorization": f"Bearer {alice_token}"},
        json={"events": [session_event(alice_dev, 1, sid)]},
    )
    sign_in(hub, "code-alice")
    with hub.websocket_connect(f"/sync/ws?token={alice_token}") as ws:
        ws.receive_json()
        ws.send_json({"type": "control-ready"})
        _wait_control_connected(hub, sid, expect=True)
        r = hub.post(f"/api/sessions/{sid}/control", json={"action": "start", "provider": "grok"})
        assert r.status_code == 202, r.text
        frame = _recv_until(ws, lambda m: m.get("type") == "control")
        assert frame["payload"] == {"provider": "grok"}


def test_start_provider_not_grok_is_400(hub: TestClient) -> None:
    alice_dev = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaa010"
    alice_token = pair_device(hub, "code-alice", alice_dev)
    sid = "sess-alice-10"
    hub.post(
        "/sync/push",
        headers={"Authorization": f"Bearer {alice_token}"},
        json={"events": [session_event(alice_dev, 1, sid)]},
    )
    sign_in(hub, "code-alice")
    r = hub.post(f"/api/sessions/{sid}/control", json={"action": "start", "provider": "claude"})
    assert r.status_code == 400
    both = hub.post(
        f"/api/sessions/{sid}/control",
        json={"action": "start", "provider": "grok", "command": "bash"},
    )
    assert both.status_code == 400


def test_control_does_not_write_session_ledger(hub: TestClient) -> None:
    alice_dev = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaa008"
    alice_token = pair_device(hub, "code-alice", alice_dev)
    sid = "sess-alice-8"
    hub.post(
        "/sync/push",
        headers={"Authorization": f"Bearer {alice_token}"},
        json={"events": [session_event(alice_dev, 1, sid)]},
    )
    before = hub.get("/sync/pull", headers={"Authorization": f"Bearer {alice_token}"}).json()["events"]
    before_session = [e for e in before if e["table"] == "session"]
    sign_in(hub, "code-alice")
    with hub.websocket_connect(f"/sync/ws?token={alice_token}") as ws:
        ws.receive_json()
        ws.send_json({"type": "control-ready"})
        _wait_control_connected(hub, sid, expect=True)
        r = hub.post(
            f"/api/sessions/{sid}/control",
            json={"action": "start", "command": "agent", "cols": 80, "rows": 24},
        )
        assert r.status_code == 202, r.text
        _recv_until(ws, lambda m: m.get("type") == "control")
    after = hub.get("/sync/pull", headers={"Authorization": f"Bearer {alice_token}"}).json()["events"]
    after_session = [e for e in after if e["table"] == "session"]
    assert after_session == before_session
    assert len(after) == len(before)
