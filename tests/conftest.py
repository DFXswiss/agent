from __future__ import annotations

import os
import secrets
from contextlib import contextmanager
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
from agent_core.keys import KeyRing
from agent_core.record.connection import parse_connection_string
from agent_core.record.runtime import NO_HOOKS, RecordRuntime
from agent_core.record.storage import MemoryState
from agent_core.signing import Clock, HubSigner

# The hub signing key of the test configuration. Never used outside the test suite.
HUB_SEED_HEX = "5e" * 32
HUB_KEY_ID = "hub-test-1"

# RECORD-PROTOCOL.md section 14: the administrator connection string of a PostgreSQL 16 test server.
TEST_POSTGRES = "AGENT_CORE_TEST_POSTGRES"
LOOPBACK_HOSTS = ("localhost", "::1")
# Section 14: held as a session advisory lock on an administrator connection from before a test
# creates its first database until after it drops its last one, because step `writer` refuses a
# server with a second user database (section 11.4).
DATABASES_LOCK = 742180004
# Section 14: held while the roles and the database of the identifier test, whose names are fixed
# SQL keywords, exist.
IDENTIFIER_LOCK = 742180003


# A writer connection string that no database server serves: port 9 (discard) of the loopback
# address, on which no PostgreSQL listens, so every connection to it fails.
IDLE_RECORD_URL = "postgresql://idle_writer:" + "0" * 64 + "@127.0.0.1:9/idle_record?sslmode=disable"


def idle_record(keys: KeyRing, signer: HubSigner, clock: Clock) -> RecordRuntime:
    """The record part of a hub for the tests of the session store, pairing, sync and signed
    requests, which never reach the record: an empty state, no start of RECORD-PROTOCOL.md section
    11.4 and no background thread. No database server serves it, so a test that reached the record by
    mistake would get `500` instead of an answer. The hub itself always starts the record through
    `start_record` (agent_core.__main__); the record tests do so against PostgreSQL."""
    return RecordRuntime(
        writer=parse_connection_string(IDLE_RECORD_URL),
        keys=keys,
        signer=signer,
        clock=clock,
        monotonic=lambda: 0.0,
        memory=MemoryState(),
        pool_size=1,
        poll_seconds=None,
        hooks=NO_HOOKS,
        last_verification=0.0,
    )


@contextmanager
def advisory_lock(params: dict[str, Any], number: int) -> Iterator[None]:
    """A session advisory lock on its own administrator connection, held for the block."""
    with psycopg.connect(**params, autocommit=True) as holder:
        holder.execute("SELECT pg_advisory_lock(%s)", (number,))
        yield


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
    # The collation and character type of the database, None for the default of the server.
    locale: str | None = None

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
    yield from _record_database(None)


@pytest.fixture
def record_db_en_us() -> Iterator[RecordDatabase]:
    """As `record_db`, in a database created with the collation `en_US.utf8` (section 14)."""
    yield from _record_database("en_US.utf8")


def record_database_for(
    params: dict[str, Any], name: str, owner_role: str, writer_role: str, locale: str | None = None
) -> RecordDatabase:
    """The description of a test database on the server of `params`, with fresh passwords."""
    host = str(params["host"])
    loopback = host in LOOPBACK_HOSTS or host.startswith("127.")
    return RecordDatabase(
        admin_params=params,
        name=name,
        owner_role=owner_role,
        writer_role=writer_role,
        owner_password=secrets.token_hex(32),
        writer_password=secrets.token_hex(32),
        host=f"[{host}]" if ":" in host else host,
        port=int(params.get("port") or 5432),
        sslmode="disable" if loopback else "require",
        locale=locale,
    )


def admin_params() -> dict[str, Any]:
    return _admin_params()


def _record_database(locale: str | None) -> Iterator[RecordDatabase]:
    params = _admin_params()
    suffix = secrets.token_hex(6)
    database = record_database_for(params, f"t_record_{suffix}", f"t_owner_{suffix}", f"t_writer_{suffix}", locale)
    with advisory_lock(params, DATABASES_LOCK):
        try:
            with psycopg.connect(**params, autocommit=True) as admin:
                if admin.info.server_version < 160000:
                    pytest.fail("the record tests need PostgreSQL 16 or later", pytrace=False)
                create_roles_and_database(
                    admin,
                    database.name,
                    (database.owner_role, database.owner_password),
                    (database.writer_role, database.writer_password),
                    locale,
                )
            yield database
        finally:
            with psycopg.connect(**params, autocommit=True) as admin:
                drop_roles_and_database(admin, database.name, (database.writer_role, database.owner_role))


ROLE_ATTRIBUTES = sql.SQL("LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS")


def create_roles_and_database(
    admin: psycopg.Connection[Any],
    name: str,
    owner: tuple[str, str],
    writer: tuple[str, str],
    locale: str | None = None,
) -> None:
    """Section 11.2: both roles with LOGIN and no other attribute, the database owned by the owner."""
    for role, password in (owner, writer):
        admin.execute(
            sql.SQL("CREATE ROLE {} {} PASSWORD {}").format(
                sql.Identifier(role), ROLE_ATTRIBUTES, sql.Literal(password)
            )
        )
    create_database(admin, name, owner[0], locale)


def create_database(admin: psycopg.Connection[Any], name: str, owner: str, locale: str | None) -> None:
    statement = sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(name), sql.Identifier(owner))
    if locale is not None:
        statement = sql.SQL("{} TEMPLATE template0 LC_COLLATE {} LC_CTYPE {}").format(
            statement, sql.Literal(locale), sql.Literal(locale)
        )
    admin.execute(statement)


def recreate_database(db: RecordDatabase) -> None:
    """Drop the test database and create it again, empty, with the same name, owner and locale.

    For a test that exports from one database and restores into an empty one: the roles stay, and
    the server never holds two user databases at once (section 14).
    """
    with psycopg.connect(**db.admin_params, autocommit=True) as admin:
        admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(db.name)))
        create_database(admin, db.name, db.owner_role, db.locale)


def drop_roles_and_database(admin: psycopg.Connection[Any], name: str, roles: tuple[str, ...]) -> None:
    admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
    for role in roles:
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
        app = create_app(cfg, github=github, store=store, record=idle_record)
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
        app = create_app(agent_cfg, github=github, store=store, record=idle_record)
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
        app = create_app(agent_cfg, github=github, store=store, record=idle_record)
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
        app = create_app(agent_cfg, github=github, store=store, record=idle_record)
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
