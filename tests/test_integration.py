from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("agent_core")
import httpx

from fastapi.testclient import TestClient

from agent_core.app import create_app
from agent_core.config import Config
from agent_core.db import Store as HubStore
from agent_core.github import FakeGitHub
from agent_cli.hub import Hub
from agent_cli.store import Store


class _Client:
    """Starlette TestClient dressed as the httpx client Hub expects."""

    def __init__(self, inner: TestClient) -> None:
        self.inner = inner

    def request(self, method: str, url: str, **kwargs):
        parsed = httpx.URL(url)
        path = parsed.path
        if parsed.query:
            path = f"{path}?{parsed.query}"
        return self.inner.request(method, path, **kwargs)

    def close(self) -> None:
        self.inner.close()


def _cfg(tmp_path: Path) -> Config:
    teams = tmp_path / "teams.yaml"
    teams.write_text("teams:\n  dfx:\n    members: [alice, bob]\n", encoding="utf-8")
    return Config(
        public_url="http://hub",
        session_secret="s" * 32,
        github_client_id="id",
        github_client_secret="secret",
        github_authorize_url="https://github.test/login/oauth/authorize",
        github_token_url="https://github.test/login/oauth/access_token",
        github_user_url="https://github.test/user",
        database=str(tmp_path / "hub.sqlite"),
        teams_path=teams,
        host="127.0.0.1",
        port=8787,
    )


def _sign_in(client: TestClient, code: str) -> None:
    start = client.get("/auth/github", follow_redirects=False)
    state = start.headers["location"].split("state=")[1].split("&")[0]
    client.get("/auth/github/callback", params={"code": code, "state": state}, follow_redirects=False)


def _pair(client: TestClient, store: Store, code: str, name: str) -> None:
    challenge = "c" + store.device_id().replace("-", "")[:20]
    hub = Hub("http://hub", client=_Client(client))
    hub.prepare(store.device_id(), challenge, name)
    _sign_in(client, code)
    confirm = client.post("/pair/confirm", json={"challenge": challenge})
    assert confirm.status_code == 200, confirm.text
    wait = hub.wait(store.device_id(), challenge)
    store.set_meta("device_token", wait["token"])
    store.set_meta("github_login", wait["login"])
    store.set_meta("hub_url", "http://hub")


def test_two_devices_team_sync_and_restore(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    app = create_app(cfg, github=FakeGitHub({"code-alice": "alice", "code-bob": "bob"}), store=HubStore(cfg.database))
    alice_http = TestClient(app)
    bob_http = TestClient(app)

    alice = Store(tmp_path / "alice" / "ledger.sqlite")
    bob = Store(tmp_path / "bob" / "ledger.sqlite")
    _pair(alice_http, alice, "code-alice", "alice-box")
    _pair(bob_http, bob, "code-bob", "bob-box")

    alice.write("session", "insert", "sess-a", {"id": "sess-a", "kind": "human", "status": "active", "title": "A"})
    alice.write(
        "task",
        "insert",
        "task-a",
        {"id": "task-a", "session_id": "sess-a", "title": "Alice work", "state": "open", "workflow": "implement"},
    )

    alice_hub = Hub("http://hub", token=alice.meta("device_token"), client=_Client(alice_http))
    bob_hub = Hub("http://hub", token=bob.meta("device_token"), client=_Client(bob_http))
    alice_hub.push(alice.pending_events())
    pulled = bob_hub.pull({})
    titles = [e["payload"].get("title") for e in pulled["events"]]
    assert "Alice work" in titles
    for event in pulled["events"]:
        bob.apply_remote(event)
    assert bob.row("task", "task-a")["title"] == "Alice work"

    wiped = Store(tmp_path / "alice-wiped" / "ledger.sqlite")
    wiped.set_meta("device_id", alice.device_id())
    wiped.set_meta("device_token", alice.meta("device_token"))
    wiped.set_meta("github_login", "alice")
    restore = alice_hub.restore()
    assert restore["device_id"] == alice.device_id()
    for event in restore.get("events") or restore.get("own_events") or []:
        wiped.apply_remote(event)
    assert wiped.row("task", "task-a")["title"] == "Alice work"
