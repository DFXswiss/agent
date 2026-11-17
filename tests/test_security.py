from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_core.app import create_app
from agent_core.config import Config, ConfigError
from agent_core.db import Store
from agent_core.github import FakeGitHub
from agent_core.security import (
    FakeSecurityTracker,
    PlaneSecurityTracker,
    SecurityCounts,
    SecurityTrackerError,
    count_by_group,
)
from tests.conftest import sign_in

STATES = [
    {"id": "s-backlog", "group": "backlog"},
    {"id": "s-open", "group": "unstarted"},
    {"id": "s-work", "group": "started"},
    {"id": "s-done", "group": "completed"},
    {"id": "s-cancel", "group": "cancelled"},
]


def _client(cfg: Config, github: FakeGitHub, security=None) -> TestClient:
    return TestClient(create_app(cfg, github=github, store=Store(cfg.database), security=security))


def test_count_by_group_buckets_states() -> None:
    items = [{"state": s} for s in ("s-backlog", "s-open", "s-open", "s-work", "s-done", "s-cancel")]
    assert count_by_group(STATES, items) == SecurityCounts(open=3, in_progress=1, done=1)


def test_count_by_group_rejects_unknown_state() -> None:
    with pytest.raises(SecurityTrackerError):
        count_by_group(STATES, [{"state": "nope"}])


def test_api_security_requires_login(cfg: Config, github: FakeGitHub) -> None:
    res = _client(cfg, github, FakeSecurityTracker()).get("/api/security")
    assert res.status_code == 401


def test_api_security_refuses_login_without_team(cfg: Config, github: FakeGitHub) -> None:
    client = _client(cfg, github, FakeSecurityTracker(SecurityCounts(5, 1, 2)))
    sign_in(client, "code-dave")
    res = client.get("/api/security")
    assert res.status_code == 403
    assert "5" not in res.text


def test_api_security_counts_for_team_member(tmp_path: Path, cfg: Config, github: FakeGitHub) -> None:
    linked = Config(**{**cfg.__dict__, "security_tracker_link": "https://tracker.test/sec"})
    client = _client(linked, github, FakeSecurityTracker(SecurityCounts(open=5, in_progress=1, done=2)))
    sign_in(client, "code-alice")
    body = client.get("/api/security").json()
    assert body["configured"] is True
    assert (body["open"], body["in_progress"], body["done"]) == (5, 1, 2)
    assert body["link"] == "https://tracker.test/sec"


def test_api_security_not_configured(cfg: Config, github: FakeGitHub) -> None:
    client = _client(cfg, github)
    sign_in(client, "code-alice")
    assert client.get("/api/security").json()["configured"] is False


def test_api_security_tracker_failure_is_502(cfg: Config, github: FakeGitHub) -> None:
    client = _client(cfg, github, FakeSecurityTracker(error="security tracker request failed: HTTP 500"))
    sign_in(client, "code-alice")
    res = client.get("/api/security")
    assert res.status_code == 502
    assert "HTTP 500" in res.text


def _plane(handler) -> PlaneSecurityTracker:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return PlaneSecurityTracker("https://tracker.test/api/v1/workspaces/w/projects/p/", "tok", client=client)


def test_plane_tracker_pages_and_caches() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path + "?" + request.url.query.decode())
        assert request.headers["X-API-Key"] == "tok"
        if request.url.path.endswith("/states/"):
            return httpx.Response(200, json={"results": STATES})
        if request.url.params.get("cursor") == "c2":
            return httpx.Response(200, json={"results": [{"id": "3", "state": "s-done"}], "next_page_results": False})
        return httpx.Response(
            200,
            json={"results": [{"id": "1", "state": "s-open"}, {"id": "2", "state": "s-work"}], "next_page_results": True, "next_cursor": "c2"},
        )

    tracker = _plane(handler)
    assert tracker.counts() == SecurityCounts(open=1, in_progress=1, done=1)
    first = len(calls)
    assert first == 3
    assert all(c.startswith("/api/v1/workspaces/w/projects/p/") for c in calls)
    tracker.counts()
    assert len(calls) == first


def test_plane_tracker_http_error() -> None:
    tracker = _plane(lambda request: httpx.Response(403, text="error code: 1010"))
    with pytest.raises(SecurityTrackerError, match="HTTP 403"):
        tracker.counts()


def test_plane_tracker_missing_cursor() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/states/"):
            return httpx.Response(200, json={"results": STATES})
        return httpx.Response(200, json={"results": [], "next_page_results": True})

    with pytest.raises(SecurityTrackerError, match="next cursor"):
        _plane(handler).counts()


KEYS = ("AGENT_CORE_SECURITY_TRACKER_URL", "AGENT_CORE_SECURITY_TRACKER_TOKEN", "AGENT_CORE_SECURITY_TRACKER_LINK")


def _base_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    teams = tmp_path / "teams.yaml"
    teams.write_text("teams:\n  dfx:\n    members: []\n", encoding="utf-8")
    env = {
        "AGENT_CORE_PUBLIC_URL": "http://127.0.0.1:8787",
        "AGENT_CORE_SESSION_SECRET": "x" * 32,
        "AGENT_CORE_GITHUB_CLIENT_ID": "id",
        "AGENT_CORE_GITHUB_CLIENT_SECRET": "secret",
        "AGENT_CORE_GITHUB_AUTHORIZE_URL": "https://github.test/a",
        "AGENT_CORE_GITHUB_TOKEN_URL": "https://github.test/t",
        "AGENT_CORE_GITHUB_USER_URL": "https://github.test/u",
        "AGENT_CORE_DATABASE": str(tmp_path / "hub.sqlite"),
        "AGENT_CORE_TEAMS": str(teams),
        "AGENT_CORE_HOST": "127.0.0.1",
        "AGENT_CORE_PORT": "8787",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)


def test_config_tracker_absent_is_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(tmp_path, monkeypatch)
    assert Config.from_env().security_tracker_enabled is False


def test_config_tracker_all_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(tmp_path, monkeypatch)
    monkeypatch.setenv(KEYS[0], "https://tracker.test/api/v1/workspaces/w/projects/p/")
    monkeypatch.setenv(KEYS[1], "tok")
    monkeypatch.setenv(KEYS[2], "https://tracker.test/w/projects/p/issues")
    cfg = Config.from_env()
    assert cfg.security_tracker_enabled is True
    assert cfg.security_tracker_url == "https://tracker.test/api/v1/workspaces/w/projects/p"


@pytest.mark.parametrize("unset", KEYS)
def test_config_tracker_partial_aborts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unset: str) -> None:
    _base_env(tmp_path, monkeypatch)
    for k in KEYS:
        monkeypatch.setenv(k, "https://tracker.test/x")
    monkeypatch.delenv(unset)
    with pytest.raises(ConfigError, match=unset):
        Config.from_env()


def test_config_tracker_requires_https(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(tmp_path, monkeypatch)
    monkeypatch.setenv(KEYS[0], "http://tracker.test/api")
    monkeypatch.setenv(KEYS[1], "tok")
    monkeypatch.setenv(KEYS[2], "https://tracker.test/ui")
    with pytest.raises(ConfigError, match="https"):
        Config.from_env()
