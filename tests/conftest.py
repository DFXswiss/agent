from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterator

import psycopg
import pytest
import yaml
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from agent_core.app import create_app
from agent_core.config import Config
from agent_core.db import Store
from agent_core.github import FakeGitHub

# The hub signing key of the test configuration. Never used outside the test suite.
HUB_SEED_HEX = "5e" * 32
HUB_KEY_ID = "hub-test-1"

# RECORD-PROTOCOL.md section 14: the administrator connection string of a PostgreSQL 16 test server.
TEST_POSTGRES = "AGENT_CORE_TEST_POSTGRES"
LOOPBACK_HOSTS = ("localhost", "::1")


def _admin_params() -> dict[str, Any]:
    """The administrator's connection parameters; fails, never skips, without the variable."""
    value = os.environ.get(TEST_POSTGRES)
    if not value:
        pytest.fail(
            f"{TEST_POSTGRES} is not set: the record tests need the connection string of a "
            "superuser on a throwaway local PostgreSQL 16 test server (see README.md)",
            pytrace=False,
        )
    try:
        params = conninfo_to_dict(value)
    except psycopg.Error:
        pytest.fail(f"{TEST_POSTGRES} is not a valid connection string", pytrace=False)
    if not params.get("host") or str(params["host"]).startswith("/"):
        pytest.fail(f"{TEST_POSTGRES} must name a TCP host", pytrace=False)
    return params


@dataclass(frozen=True)
class RecordDatabase:
    """A fresh database of one test, with its own owner and writer roles (section 11.2)."""

    admin_params: dict[str, Any] = field(repr=False)
    name: str
    owner_role: str
    writer_role: str
    owner_password: str = field(repr=False)
    writer_password: str = field(repr=False)
    host: str
    port: int
    sslmode: str

    def url(
        self,
        role: str,
        password: str,
        *,
        host: str | None = None,
        port: int | None = None,
        database: str | None = None,
        sslmode: str | None = None,
    ) -> str:
        """A connection string of section 15 for this server."""
        return (
            f"postgresql://{role}:{password}@{host or self.host}:{port or self.port}"
            f"/{database or self.name}?sslmode={sslmode or self.sslmode}"
        )

    @property
    def owner_url(self) -> str:
        return self.url(self.owner_role, self.owner_password)

    @property
    def writer_url(self) -> str:
        return self.url(self.writer_role, self.writer_password)

    def migrate_env(self) -> dict[str, str]:
        return {
            "AGENT_CORE_RECORD_OWNER_DATABASE": self.owner_url,
            "AGENT_CORE_RECORD_DATABASE": self.writer_url,
        }

    def admin(self, *, autocommit: bool = True) -> psycopg.Connection[Any]:
        """A superuser connection to this test database."""
        return psycopg.connect(**{**self.admin_params, "dbname": self.name}, autocommit=autocommit)

    def secret_values(self) -> tuple[str, ...]:
        """Every value that must never appear in a printed or logged line."""
        return (self.owner_password, self.writer_password, self.owner_url, self.writer_url)


@pytest.fixture
def record_db() -> Iterator[RecordDatabase]:
    """A fresh database, owned by a fresh owner role, and a fresh writer role, dropped at the end."""
    params = _admin_params()
    host = str(params["host"])
    loopback = host in LOOPBACK_HOSTS or host.startswith("127.")
    suffix = secrets.token_hex(6)
    database = RecordDatabase(
        admin_params=params,
        name=f"t_record_{suffix}",
        owner_role=f"t_owner_{suffix}",
        writer_role=f"t_writer_{suffix}",
        owner_password=secrets.token_hex(32),
        writer_password=secrets.token_hex(32),
        host=f"[{host}]" if ":" in host else host,
        port=int(params.get("port") or 5432),
        sslmode="disable" if loopback else "require",
    )
    attributes = sql.SQL("LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS")
    try:
        with psycopg.connect(**params, autocommit=True) as admin:
            if admin.info.server_version < 160000:
                pytest.fail("the record tests need PostgreSQL 16 or later", pytrace=False)
            for role, password in (
                (database.owner_role, database.owner_password),
                (database.writer_role, database.writer_password),
            ):
                admin.execute(
                    sql.SQL("CREATE ROLE {} {} PASSWORD {}").format(
                        sql.Identifier(role), attributes, sql.Literal(password)
                    )
                )
            admin.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(database.name), sql.Identifier(database.owner_role)
                )
            )
        yield database
    finally:
        with psycopg.connect(**params, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database.name))
            )
            for role in (database.writer_role, database.owner_role):
                admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def schema_exists(db: RecordDatabase) -> bool:
    """Whether the schema `record` exists in the test database, as the administrator sees it."""
    with db.admin() as admin:
        row = admin.execute("SELECT count(*) FROM pg_namespace WHERE nspname = 'record'").fetchone()
    assert row is not None
    return row[0] == 1


def write_teams(path: Path, teams: dict) -> Path:
    path.write_text(yaml.safe_dump({"teams": teams}), encoding="utf-8")
    return path


@pytest.fixture
def keys_file(tmp_path: Path) -> Path:
    path = tmp_path / "keys.yaml"
    path.write_text("keys: []\n", encoding="utf-8")
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
def cfg(tmp_path: Path, teams_file: Path, keys_file: Path) -> Config:
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
        keys_path=keys_file,
        signing_key=HUB_SEED_HEX,
        hub_key_id=HUB_KEY_ID,
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
