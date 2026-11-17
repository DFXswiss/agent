from __future__ import annotations

from fastapi.testclient import TestClient

from agent_core.db import utcnow
from agent_core.tokens import issue
from tests.conftest import pair_device, sign_in


def test_me_requires_login(hub: TestClient) -> None:
    assert hub.get("/auth/me").status_code == 401


def test_github_login_and_visibility(hub: TestClient) -> None:
    sign_in(hub, "code-alice")
    me = hub.get("/auth/me")
    assert me.status_code == 200
    body = me.json()
    assert body["login"] == "alice"
    assert body["visible"] == ["alice", "bob"]
    assert body["teams"] == ["dfx"]


def test_dave_sees_only_self(hub: TestClient) -> None:
    sign_in(hub, "code-dave")
    body = hub.get("/auth/me").json()
    assert body["visible"] == ["dave"]
    assert body["teams"] == []


def test_pair_issues_token(hub: TestClient) -> None:
    token = pair_device(hub, "code-alice", "11111111-1111-1111-1111-111111111111")
    assert "." in token
    pushed = hub.post(
        "/sync/push",
        headers={"Authorization": f"Bearer {token}"},
        json={"events": []},
    )
    assert pushed.status_code == 200
    assert pushed.json()["accepted"] == 0


def test_pair_rejects_reserved_web_device_ids(hub: TestClient) -> None:
    for index, device_id in enumerate(("web:alice", "WeB:pretender"), start=1):
        response = hub.post(
            "/pair/prepare",
            json={
                "device_id": device_id,
                "challenge": f"reserved-web-origin-{index}",
                "device_name": "browser impersonator",
            },
        )
        assert response.status_code == 400
        assert response.json()["detail"] == "web: device ids are reserved"


def test_pair_confirm_rejects_preupgrade_reserved_device_id(hub: TestClient) -> None:
    challenge = "reserved-before-upgrade"
    hub.app.state.hub.store.execute(
        "INSERT INTO pending_pair "
        "(challenge, device_id, device_name, created_at, token, github_login) "
        "VALUES (?, ?, ?, ?, NULL, NULL)",
        (challenge, "WeB:legacy", "legacy browser", utcnow()),
    )
    sign_in(hub, "code-alice")

    response = hub.post("/pair/confirm", json={"challenge": challenge})

    assert response.status_code == 400
    assert response.json() == {"detail": "web: device ids are reserved"}


def test_reserved_preupgrade_device_token_is_unknown(hub: TestClient) -> None:
    token_id = "legacy-web-token"
    device_id = "web:legacy"
    hub.app.state.hub.store.execute(
        "INSERT INTO device "
        "(id, github_login, name, token_id, created_at, revoked_at, confirmed_by) "
        "VALUES (?, ?, ?, ?, ?, NULL, ?)",
        (device_id, "alice", "legacy browser", token_id, utcnow(), "alice"),
    )
    token = issue(hub.app.state.hub.cfg.session_secret, "alice", device_id, token_id)

    response = hub.post(
        "/sync/push",
        headers={"Authorization": f"Bearer {token}"},
        json={"events": []},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "unknown device token"}


def test_unknown_code_rejected(hub: TestClient) -> None:
    start = hub.get("/auth/github", follow_redirects=False)
    state = start.headers["location"].split("state=")[1].split("&")[0]
    bad = hub.get("/auth/github/callback", params={"code": "nope", "state": state})
    assert bad.status_code == 401


def test_expired_state_rejected(hub: TestClient) -> None:
    bad = hub.get("/auth/github/callback", params={"code": "code-alice", "state": "wrong"})
    assert bad.status_code == 400


def test_oauth_returns_to_pair_url(hub: TestClient) -> None:
    start = hub.get("/auth/github", params={"next": "/pair?challenge=abc1234567890abc"}, follow_redirects=False)
    state = start.headers["location"].split("state=")[1].split("&")[0]
    done = hub.get("/auth/github/callback", params={"code": "code-alice", "state": state}, follow_redirects=False)
    assert done.status_code == 302
    assert done.headers["location"] == "/pair?challenge=abc1234567890abc"


def test_oauth_rejects_open_redirect(hub: TestClient) -> None:
    bad = hub.get("/auth/github", params={"next": "https://evil.example/"}, follow_redirects=False)
    assert bad.status_code == 400


def test_pair_token_is_one_shot(hub: TestClient) -> None:
    token = pair_device(hub, "code-alice", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa09")
    assert token
    again = hub.get(
        "/pair/wait",
        params={"device_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa09", "challenge": "challenge-aaaaaaaaaaaaaaaa"},
    )
    assert again.status_code == 200
    assert again.json()["status"] == "pending"
