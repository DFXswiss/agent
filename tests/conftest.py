from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from agent_core.app import create_app
from agent_core.config import Config
from agent_core.db import Store
from agent_core.github import FakeGitHub


def write_teams(path: Path, teams: dict) -> Path:
    path.write_text(yaml.safe_dump({"teams": teams}), encoding="utf-8")
    return path


@pytest.fixture
def teams_file(tmp_path: Path) -> Path:
    return write_teams(
        tmp_path / "teams.yaml",
        {
            "dfx": {"members": ["alice", "bob"]},
            "ops": {"members": ["bob", "cara"]},
        },
    )


@pytest.fixture
def cfg(tmp_path: Path, teams_file: Path) -> Config:
    return Config(
        public_url="http://testserver",
        session_secret="s" * 32,
        github_client_id="client",
        github_client_secret="secret",
        github_authorize_url="https://github.test/login/oauth/authorize",
        github_token_url="https://github.test/login/oauth/access_token",
        github_user_url="https://github.test/user",
        database=str(tmp_path / "hub.sqlite"),
        teams_path=teams_file,
        host="127.0.0.1",
        port=8787,
    )


@pytest.fixture
def github() -> FakeGitHub:
    return FakeGitHub({"code-alice": "Alice", "code-bob": "bob", "code-cara": "cara", "code-dave": "dave"})


@pytest.fixture
def hub(cfg: Config, github: FakeGitHub) -> TestClient:
    store = Store(cfg.database)
    try:
        app = create_app(cfg, github=github, store=store)
        with TestClient(app) as client:
            yield client
    finally:
        store.close()


@pytest.fixture
def agent_hub(cfg: Config, tmp_path: Path) -> TestClient:
    roster_path = tmp_path / "agents-teams.yaml"
    roster_path.write_text(
        yaml.safe_dump(
            {
                "teams": {"dfx": {"members": ["alice", "bob", "helperbot"]}},
                "agents": {"helperbot": {"owners": ["alice"]}},
            }
        ),
        encoding="utf-8",
    )
    agent_cfg = replace(cfg, teams_path=roster_path, database=str(tmp_path / "agent-hub.sqlite"))
    github = FakeGitHub(
        {
            "code-alice": "alice",
            "code-bob": "bob",
            "code-dave": "dave",
            "code-helperbot": "helperbot",
        }
    )
    store = Store(agent_cfg.database)
    try:
        app = create_app(agent_cfg, github=github, store=store)
        with TestClient(app) as client:
            yield client
    finally:
        store.close()


@pytest.fixture
def two_agent_hub(cfg: Config, tmp_path: Path) -> TestClient:
    roster_path = tmp_path / "two-agent-teams.yaml"
    roster_path.write_text(
        yaml.safe_dump(
            {
                "teams": {
                    "dfx": {
                        "members": ["alice", "helperbot", "secondbot"],
                    }
                },
                "agents": {
                    "helperbot": {"owners": ["alice"]},
                    "secondbot": {"owners": ["alice"]},
                },
            }
        ),
        encoding="utf-8",
    )
    agent_cfg = replace(
        cfg,
        teams_path=roster_path,
        database=str(tmp_path / "two-agent-hub.sqlite"),
    )
    github = FakeGitHub({"code-alice": "alice"})
    store = Store(agent_cfg.database)
    try:
        app = create_app(agent_cfg, github=github, store=store)
        with TestClient(app) as client:
            yield client
    finally:
        store.close()


@pytest.fixture
def mixed_owner_hub(cfg: Config, tmp_path: Path) -> TestClient:
    roster_path = tmp_path / "mixed-owner-teams.yaml"
    roster_path.write_text(
        yaml.safe_dump(
            {
                "teams": {"dfx": {"members": ["alice", "bob", "helperbot", "secondbot"]}},
                "agents": {
                    "helperbot": {"owners": ["alice"]},
                    "secondbot": {"owners": ["bob"]},
                },
            }
        ),
        encoding="utf-8",
    )
    agent_cfg = replace(
        cfg,
        teams_path=roster_path,
        database=str(tmp_path / "mixed-owner-hub.sqlite"),
    )
    github = FakeGitHub({"code-alice": "alice", "code-bob": "bob"})
    store = Store(agent_cfg.database)
    try:
        app = create_app(agent_cfg, github=github, store=store)
        with TestClient(app) as client:
            yield client
    finally:
        store.close()


def sign_in(client: TestClient, code: str) -> None:
    start = client.get("/auth/github", follow_redirects=False)
    assert start.status_code == 302
    loc = start.headers["location"]
    assert "state=" in loc
    state = loc.split("state=")[1].split("&")[0]
    done = client.get("/auth/github/callback", params={"code": code, "state": state}, follow_redirects=False)
    assert done.status_code == 302


def pair_device(client: TestClient, code: str, device_id: str, name: str = "laptop") -> str:
    challenge = "challenge-" + device_id.replace("-", "")[:16]
    prep = client.post(
        "/pair/prepare",
        json={"device_id": device_id, "challenge": challenge, "device_name": name},
    )
    assert prep.status_code == 200, prep.text
    sign_in(client, code)
    confirm = client.post("/pair/confirm", json={"challenge": challenge})
    assert confirm.status_code == 200, confirm.text
    wait = client.get("/pair/wait", params={"device_id": device_id, "challenge": challenge})
    assert wait.status_code == 200
    body = wait.json()
    assert body["status"] == "paired"
    return body["token"]
