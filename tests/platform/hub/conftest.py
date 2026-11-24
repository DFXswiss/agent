"""Fixtures of the hub suites (docs/platform/spec/07-testing.md section 1, 02-data-model.md section 6).

Every database test gets its own database `t_hub_<hex>` on the server of AGENT_HUB_TEST_POSTGRES (a superuser
connection string of a throwaway PostgreSQL >= 16), owned by a fresh owner role, with a fresh writer role; both are
dropped at the end. A missing variable fails the test, it never skips (pattern of the legacy tests/conftest.py).
The app, key and client fixtures (HUB-02) follow the database fixtures.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import uuid
import warnings
from collections import Counter
from collections.abc import Callable, Generator, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import Any

import httpx
import psycopg
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from fastapi import FastAPI
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from agent_core.record.connection import parse_connection_string
from agent_core.signing import b64url_decode, b64url_encode, format_time, request_text, response_text, sha256_hex
from agent_hub.app import create_app
from agent_hub.auth.keys import (
    DEVICE_PREFIX,
    SESSION_PREFIX,
    KeyContext,
    KeyResolver,
    Principal,
    ResolvedKey,
    ed25519_verifier,
    session_verifier,
)
from agent_hub.config import HubConfig, load_config
from agent_hub.db.migrate import OWNER_VARIABLE, WRITER_VARIABLE, run_migrate
from agent_hub.db.pool import open_pool
from agent_hub.errors import HubError
from agent_hub.http.writelog import UNAUDITED
from agent_hub.registry import KEY_RESOLVERS, merge_mappings
from tests.platform.contracts.harness import CONTRACTS, Contracts

with warnings.catch_warnings():
    # Starlette asks for `httpx2`; the platform extras pin httpx (00-overview.md section 4).
    warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient`")
    from starlette.testclient import TestClient

TEST_POSTGRES = "AGENT_HUB_TEST_POSTGRES"
MIN_SERVER_VERSION = 160000
LOOPBACK_HOSTS = frozenset({"localhost", "::1"})
ROLE_ATTRIBUTES = sql.SQL("LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS")
FIXED_NOW = datetime(2026, 11, 21, 12, 0, 0, 123000, tzinfo=UTC)


def admin_params() -> dict[str, Any]:
    """The superuser's connection parameters; fails, never skips, without the variable."""
    value = os.environ.get(TEST_POSTGRES)
    if not value:
        pytest.fail(
            f"{TEST_POSTGRES} is not set: the hub tests need the connection string of a superuser on a "
            "throwaway local PostgreSQL 16 or later (tools/wp/testpg.sh)",
            pytrace=False,
        )
    params = conninfo_to_dict(value)
    if not params.get("host") or str(params["host"]).startswith("/"):
        pytest.fail(f"{TEST_POSTGRES} must name a TCP host", pytrace=False)
    return params


@dataclass(frozen=True)
class HubDatabase:
    """A fresh database of one test with its own owner and writer roles."""

    admin_params: dict[str, Any] = field(repr=False)
    name: str
    owner_role: str
    writer_role: str
    owner_password: str = field(repr=False)
    writer_password: str = field(repr=False)
    host: str
    port: int

    def url(self, role: str, password: str, *, database: str | None = None) -> str:
        """A connection string in the strict form of `agent_core.record.connection`."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        loopback = self.host in LOOPBACK_HOSTS or self.host.startswith("127.")
        sslmode = "disable" if loopback else "require"
        return f"postgresql://{role}:{password}@{host}:{self.port}/{database or self.name}?sslmode={sslmode}"

    @property
    def owner_url(self) -> str:
        return self.url(self.owner_role, self.owner_password)

    @property
    def writer_url(self) -> str:
        return self.url(self.writer_role, self.writer_password)

    def migrate_env(self) -> dict[str, str]:
        return {OWNER_VARIABLE: self.owner_url, WRITER_VARIABLE: self.writer_url}

    def admin(self, *, autocommit: bool = True) -> psycopg.Connection[Any]:
        """A superuser connection to this database."""
        return psycopg.connect(**{**self.admin_params, "dbname": self.name}, autocommit=autocommit)

    def connect(self, role: str, *, autocommit: bool = False) -> psycopg.Connection[Any]:
        """A connection as `owner` or `writer`, in UTC."""
        password = self.owner_password if role == self.owner_role else self.writer_password
        connection = psycopg.connect(
            host=self.host,
            port=self.port,
            dbname=self.name,
            user=role,
            password=password,
            options="-c TimeZone=UTC",
            autocommit=autocommit,
        )
        return connection

    def secret_values(self) -> tuple[str, ...]:
        """Values that must never appear in printed output."""
        return (self.owner_password, self.writer_password, self.owner_url, self.writer_url)


def create_role(admin: psycopg.Connection[Any], role: str, password: str) -> None:
    admin.execute(
        sql.SQL("CREATE ROLE {} {} PASSWORD {}").format(sql.Identifier(role), ROLE_ATTRIBUTES, sql.Literal(password))
    )


def drop_database(admin: psycopg.Connection[Any], name: str, roles: tuple[str, ...]) -> None:
    admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
    for role in roles:
        admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def unaudited_since(before: Counter[str]) -> dict[str, int]:
    """The successful writes without an audit row of their own since `before` (a copy of `UNAUDITED`); the routes the
    pipeline tests declare for themselves (`/v1/test/...`) are no product routes."""
    return {key: count for key, count in (UNAUDITED - before).items() if " /v1/test/" not in key}


@pytest.fixture(autouse=True)
def every_write_audited() -> Iterator[None]:
    """ADR-063 section 1: a successful write whose route left no audit row of its own (outside
    `agent_hub.audit.generic_routes.GENERIC` and the handler's `unchanged()`) fails the test that made it - so a new
    route without its own audit fails the suite the first time its success path is tested."""
    before = Counter(UNAUDITED)
    yield
    added = unaudited_since(before)
    if added:
        pytest.fail(f"successful writes left no audit row of their own (ADR-063 section 1): {sorted(added)}")


@pytest.fixture
def hub_db() -> Iterator[HubDatabase]:
    """An empty database owned by a fresh owner role, and a fresh writer role; all dropped at the end."""
    params = admin_params()
    suffix = secrets.token_hex(6)
    host = str(params["host"])
    database = HubDatabase(
        admin_params=params,
        name=f"t_hub_{suffix}",
        owner_role=f"t_hub_owner_{suffix}",
        writer_role=f"t_hub_writer_{suffix}",
        owner_password=secrets.token_hex(32),
        writer_password=secrets.token_hex(32),
        host=host,
        port=int(params.get("port") or 5432),
    )
    try:
        with psycopg.connect(**params, autocommit=True) as admin:
            if admin.info.server_version < MIN_SERVER_VERSION:
                pytest.fail("the hub tests need PostgreSQL 16 or later", pytrace=False)
            create_role(admin, database.owner_role, database.owner_password)
            create_role(admin, database.writer_role, database.writer_password)
            admin.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(database.name), sql.Identifier(database.owner_role)
                )
            )
        yield database
    finally:
        with psycopg.connect(**params, autocommit=True) as admin:
            drop_database(admin, database.name, (database.writer_role, database.owner_role))


@pytest.fixture
def migrated_db(hub_db: HubDatabase) -> HubDatabase:
    """`hub_db` with every migration applied."""
    line, code = run_migrate(hub_db.migrate_env())
    assert code == 0, line
    return hub_db


@pytest.fixture
def writer(migrated_db: HubDatabase) -> Iterator[psycopg.Connection[Any]]:
    """A writer connection to the migrated database (autocommit, transactions explicit)."""
    with migrated_db.connect(migrated_db.writer_role, autocommit=True) as connection:
        yield connection


@pytest.fixture
def owner(migrated_db: HubDatabase) -> Iterator[psycopg.Connection[Any]]:
    """An owner connection to the migrated database (autocommit)."""
    with migrated_db.connect(migrated_db.owner_role, autocommit=True) as connection:
        yield connection


class FixedClock:
    """A clock that stands still until a test advances it."""

    def __init__(self, now: datetime = FIXED_NOW) -> None:
        self._now = now

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> datetime:
        self._now += timedelta(seconds=seconds)
        return self._now


@pytest.fixture
def clock() -> FixedClock:
    """The fixed hub clock (2026-11-21T12:00:00.123Z) injected into code under test."""
    return FixedClock()


# ---------------------------------------------------------------------------------------------------------------------
# HUB-02: configuration, app, keys and signed test clients.
#
# `hub` gives a running app (lifespan entered) on a migrated database with the fixed clock and these clients:
# `hub.anon`, `hub.browser()` (a browser session key, P-256), `hub.device()` (a device key, Ed25519) and `hub.root`
# (the platform root key). Every request and response of an endpoint of `contracts/api/INDEX.json` is validated
# against its schema (pass `extensions={"contract": False}` for a deliberately invalid request), and every response
# to a signed request must carry a valid `agent-response-v1` signature (a success without one fails the test).
# Session and device keys are written to the database in the form of 02-data-model.md section 2.1, so the real
# resolvers of HUB-03 and HUB-08 find them; until those exist, in-memory resolvers of this file stand in for them.
# ---------------------------------------------------------------------------------------------------------------------


TEST_HUB_SEED = "4a" * 32
TEST_HUB_KEY_ID = "hub-test"
TEST_ROOT_SEED = bytes(range(1, 33))
TEST_PUBLIC_URL = "http://localhost:8788"
TEST_MODEL_CATALOG = CONTRACTS / "model-catalog.example.json"
UUID7_VERSION = 7


def uuid7(moment: datetime = FIXED_NOW) -> uuid.UUID:
    """A UUIDv7 for `moment` with random tail bits (RFC 9562)."""
    millis = int(moment.timestamp() * 1000)
    value = (millis << 80) | (0x7 << 76) | (secrets.randbits(12) << 64) | (0b10 << 62) | secrets.randbits(62)
    return uuid.UUID(int=value)


def root_private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(TEST_ROOT_SEED)


def raw_public(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def hub_environ(database: HubDatabase | None = None, **overrides: str) -> dict[str, str]:
    """A complete, valid hub environment; `overrides` replace or (with "") empty single variables."""
    environ = {
        "AGENT_HUB_PUBLIC_URL": TEST_PUBLIC_URL,
        "AGENT_HUB_DATABASE": database.writer_url
        if database is not None
        else "postgresql://writer:" + "0" * 64 + "@127.0.0.1:5432/hub?sslmode=disable",
        "AGENT_HUB_SIGNING_KEY": TEST_HUB_SEED,
        "AGENT_HUB_HUB_KEY_ID": TEST_HUB_KEY_ID,
        "AGENT_HUB_PLATFORM_ROOT_KEY": b64url_encode(raw_public(root_private_key())),
        "AGENT_HUB_HOST": "127.0.0.1",
        "AGENT_HUB_MODEL_CATALOG": str(TEST_MODEL_CATALOG),
    }
    environ.update(overrides)
    return environ


@pytest.fixture
def hub_config(migrated_db: HubDatabase) -> HubConfig:
    """The hub configuration for the migrated test database."""
    return load_config(hub_environ(migrated_db))


@pytest.fixture
def hub_pool(migrated_db: HubDatabase) -> Iterator[ConnectionPool[psycopg.Connection[Any]]]:
    """A writer pool on the migrated test database."""
    pool = open_pool(parse_connection_string(migrated_db.writer_url), min_size=1, max_size=4)
    try:
        yield pool
    finally:
        pool.close()


@dataclass(frozen=True)
class SigningKey:
    """A signing key of a test client."""

    key_id: str
    sign: Callable[[bytes], bytes] = field(repr=False)
    principal: Principal
    attributes: dict[str, Any] = field(default_factory=dict)


def p256_raw_signature(key: ec.EllipticCurvePrivateKey, text: bytes) -> bytes:
    """The session-key signature encoding: raw `r‖s`, 32 bytes each."""
    r, s = decode_dss_signature(key.sign(text, ec.ECDSA(hashes.SHA256())))
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


class KeyRing:
    """Keys of the test clients, written to the database (02-data-model.md section 2.1) and resolvable in memory."""

    def __init__(self, pool: ConnectionPool[psycopg.Connection[Any]], clock: FixedClock) -> None:
        self._pool = pool
        self._clock = clock
        self._lock = threading.Lock()
        self._keys: dict[str, tuple[SigningKey, Callable[[bytes, bytes], bool]]] = {}
        root = root_private_key()
        self.root = SigningKey(
            key_id="platform-root",
            sign=root.sign,
            principal=Principal(kind="root", key_id="platform-root", principal_id=None, actor_kind="operator"),
        )

    def _remember(self, key: SigningKey, verify: Callable[[bytes, bytes], bool]) -> SigningKey:
        with self._lock:
            self._keys[key.key_id] = (key, verify)
        return key

    def session(self, user_id: uuid.UUID | None = None, *, expired: bool = False, ended: bool = False) -> SigningKey:
        """A browser session key of a (new) user, valid for 12 h unless `expired`; `ended` = session ended."""
        user = user_id or uuid7(self._clock())
        private = ec.generate_private_key(ec.SECP256R1())
        point = private.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        key_id = SESSION_PREFIX + secrets.token_hex(16)
        now = self._clock()
        credential = secrets.token_bytes(16)
        expires = now - timedelta(seconds=1) if expired else now + timedelta(hours=12)
        with self._pool.connection() as connection, connection.transaction():
            connection.execute(
                "INSERT INTO hub.principal (id, kind, created_at) VALUES (%s, 'user', %s) ON CONFLICT DO NOTHING",
                (user, now),
            )
            connection.execute(
                "INSERT INTO hub.passkey (credential_id, user_id, public_key, created_at, last_used_at)"
                " VALUES (%s, %s, %s, %s, %s)",
                (credential, user, point, now, now),
            )
            connection.execute(
                "INSERT INTO hub.browser_session (key_id, user_id, credential_id, public_key, statement, created_at,"
                " expires_at, ended_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (key_id, user, credential, point, "test", now - timedelta(seconds=2), expires, now if ended else None),
            )
        principal = Principal(kind="session", key_id=key_id, principal_id=user, actor_kind="user")
        key = SigningKey(
            key_id=key_id,
            sign=lambda text: p256_raw_signature(private, text),
            principal=principal,
            attributes={"expired": expired, "ended": ended},
        )
        return self._remember(key, session_verifier(point))

    def device(
        self, device_id: uuid.UUID | None = None, *, state: str = "active", kind: str = "platform"
    ) -> SigningKey:
        """A device key of a (new) platform device in `state`."""
        device = device_id or uuid7(self._clock())
        private = Ed25519PrivateKey.generate()
        public = raw_public(private)
        now = self._clock()
        with self._pool.connection() as connection, connection.transaction():
            connection.execute(
                "INSERT INTO hub.principal (id, kind, created_at) VALUES (%s, 'device', %s)", (device, now)
            )
            connection.execute(
                "INSERT INTO hub.device (id, kind, kem_pub, sig_pub, owner_org, state, enrolled_at, agent_version, os,"
                " arch, capacity, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, '0.1.0', 'macos', 'arm64', %s, %s)",
                (
                    device,
                    kind,
                    secrets.token_bytes(32),
                    public,
                    uuid7(now) if kind == "org" else None,
                    state,
                    now,
                    Jsonb({}),
                    now,
                ),
            )
        principal = Principal(
            kind="device",
            key_id=DEVICE_PREFIX + device.hex,
            principal_id=device,
            actor_kind="device",
            attributes={"state": state},
        )
        key = SigningKey(key_id=principal.key_id or "", sign=private.sign, principal=principal)
        return self._remember(key, ed25519_verifier(public))

    def resolve(self, _context: KeyContext, key_id: str) -> ResolvedKey | None:
        """In-memory stand-in for the session and device resolvers."""
        with self._lock:
            found = self._keys.get(key_id)
        if found is None:
            return None
        key, verify = found
        if key.attributes.get("ended"):
            raise HubError("key_revoked")
        if key.attributes.get("expired"):
            raise HubError("session_expired")
        return ResolvedKey(principal=key.principal, verify=verify)

    def resolvers(self) -> dict[str, KeyResolver]:
        """The discovered resolvers, with in-memory ones for prefixes no area registers yet."""
        found: dict[str, KeyResolver] = {SESSION_PREFIX: self.resolve, DEVICE_PREFIX: self.resolve}
        found.update(merge_mappings(KEY_RESOLVERS))
        return found


@cache
def contracts_registry() -> Contracts:
    """`contracts/` loaded once per test run."""
    return Contracts.load()


@dataclass(frozen=True)
class Endpoint:
    """One row of contracts/api/INDEX.json with its path pattern."""

    row: dict[str, Any]
    pattern: re.Pattern[str]


@cache
def endpoints() -> tuple[Endpoint, ...]:
    index = json.loads((CONTRACTS / "api" / "INDEX.json").read_text(encoding="utf-8"))
    found = []
    for row in index["endpoints"]:
        parts = re.split(r"\{[a-z_]+\}", row["path"])
        pattern = "[^/]+".join(re.escape(part) for part in parts)
        found.append(Endpoint(row=row, pattern=re.compile(pattern)))
    return tuple(found)


def endpoint_of(method: str, path: str) -> Endpoint | None:
    """The INDEX row of a request, None outside the API index."""
    for endpoint in endpoints():
        if endpoint.row["method"] == method and endpoint.pattern.fullmatch(path):
            return endpoint
    return None


def query_instance(schema: Mapping[str, Any], params: httpx.QueryParams) -> dict[str, Any]:
    """The query parameters as the request schema describes them (repeatable parameters as arrays)."""
    properties = schema.get("properties", {})
    out: dict[str, Any] = {}
    for name in params:
        values = params.get_list(name)
        repeatable = properties.get(name, {}).get("type") == "array"
        out[name] = values if repeatable else values[-1]
    return out


def check_contract_request(request: httpx.Request) -> None:
    """Validate a request of an INDEX endpoint against its schema."""
    endpoint = endpoint_of(request.method, request.url.path)
    if endpoint is None or not request.extensions.get("contract", True) or endpoint.row["request"] is None:
        return
    registry = contracts_registry()
    rel = endpoint.row["request"]
    if endpoint.row["request_kind"] == "body":
        registry.validate(rel, json.loads(request.content or b"null"))
    else:
        registry.validate(rel, query_instance(registry.schemas[rel], request.url.params))


def check_contract_response(response: httpx.Response) -> None:
    """Validate a response of an INDEX endpoint against its schema (errors against the error body)."""
    request = response.request
    endpoint = endpoint_of(request.method, request.url.path)
    if endpoint is None or not request.extensions.get("contract", True) or endpoint.row["streaming"]:
        return
    response.read()
    rel = endpoint.row["response"] if response.status_code < 300 else "api/error.response.json"
    if rel is not None and request.method != "HEAD":
        contracts_registry().validate(rel, response.json())


class RequestSigner(httpx.Auth):
    """Signs a request with `agent-request-v1` and verifies the hub's `agent-response-v1` answer."""

    requires_request_body = True
    requires_response_body = True

    def __init__(self, key: SigningKey, clock: FixedClock, hub_key: Ed25519PublicKey, hub_key_id: str) -> None:
        self.key = key
        self.clock = clock
        self.hub_key = hub_key
        self.hub_key_id = hub_key_id

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        agent_time = request.headers.get("agent-time") or format_time(int(self.clock().timestamp()))
        nonce = request.headers.get("agent-nonce") or b64url_encode(secrets.token_bytes(16))
        body = request.content
        text = request_text(
            self.key.key_id,
            request.method,
            request.url.netloc.decode("ascii"),
            request.url.raw_path.decode("ascii"),
            agent_time,
            nonce,
            sha256_hex(body),
        )
        request.headers["Agent-Key"] = self.key.key_id
        request.headers["Agent-Time"] = agent_time
        request.headers["Agent-Nonce"] = nonce
        request.headers.setdefault("Agent-Signature", b64url_encode(self.key.sign(text)))
        response = yield request
        check_response_signature(response, Exchange(self.key.key_id, nonce, agent_time, self.hub_key, self.hub_key_id))


@dataclass(frozen=True)
class Exchange:
    """What a response signature binds: the request's key ID, nonce and time, and the expected hub key."""

    key_id: str
    nonce: str
    agent_time: str
    hub_key: Ed25519PublicKey
    hub_key_id: str


def response_signature_valid(response: httpx.Response, exchange: Exchange) -> bool | None:
    """True/False for a signed response, None when the response carries no signature."""
    key_id, nonce, agent_time = exchange.key_id, exchange.nonce, exchange.agent_time
    hub_key, hub_key_id = exchange.hub_key, exchange.hub_key_id
    signature = response.headers.get("agent-hub-signature")
    if signature is None:
        return None
    hub_time = response.headers.get("agent-hub-time", "")
    body = b"" if response.request.method == "HEAD" else response.read()
    text = response_text(key_id, nonce, agent_time, response.status_code, hub_time, sha256_hex(body))
    try:
        raw = b64url_decode(signature)
        hub_key.verify(raw or b"", text)
    except Exception:
        return False
    return bool(response.headers.get("agent-hub-key") == hub_key_id)


def check_response_signature(response: httpx.Response, exchange: Exchange) -> None:
    """Fail on an invalid signature and on an unsigned success; record the outcome in `extensions["hub_signature"]`."""
    valid = response_signature_valid(response, exchange)
    response.extensions["hub_signature"] = valid
    if valid is False:
        raise AssertionError(f"the hub's response signature does not verify ({response.status_code})")
    if valid is None and response.status_code < 400 and not response.request.extensions.get("streaming", False):
        raise AssertionError(f"a successful response to a signed request is not signed ({response.status_code})")


class SignedClient:
    """Requests of one key (or anonymous) through the hub's test client."""

    def __init__(self, base: TestClient, auth: RequestSigner | None) -> None:
        self.base = base
        self.auth = auth

    @property
    def key(self) -> SigningKey | None:
        return None if self.auth is None else self.auth.key

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        if self.auth is not None:
            kwargs.setdefault("auth", self.auth)
        response: httpx.Response = self.base.request(method, url, **kwargs)
        return response

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("PUT", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("DELETE", url, **kwargs)


@dataclass
class HubHarness:
    """A running hub app with its keys and clients."""

    app: FastAPI
    config: HubConfig
    clock: FixedClock
    keys: KeyRing
    pool: ConnectionPool[psycopg.Connection[Any]]
    base: TestClient

    def client(self, key: SigningKey | None = None) -> SignedClient:
        if key is None:
            return SignedClient(self.base, None)
        signer = self.config.signer()
        hub_key = Ed25519PublicKey.from_public_bytes(signer.public_key)
        return SignedClient(self.base, RequestSigner(key, self.clock, hub_key, signer.key_id))

    @property
    def anon(self) -> SignedClient:
        return self.client()

    @property
    def root(self) -> SignedClient:
        return self.client(self.keys.root)

    def browser(self, user_id: uuid.UUID | None = None, **options: bool) -> SignedClient:
        return self.client(self.keys.session(user_id, **options))

    def device(self, device_id: uuid.UUID | None = None, **options: str) -> SignedClient:
        return self.client(self.keys.device(device_id, **options))

    def flush_read_log(self) -> int:
        """Write the queued read-log rows now."""
        return int(self.app.state.hub.read_log.flush())


def start_hub(
    config: HubConfig,
    pool: ConnectionPool[psycopg.Connection[Any]],
    clock: FixedClock,
    **options: Any,
) -> Iterator[HubHarness]:
    """Build and run an app (lifespan entered) with contract checks on its test client."""
    keys = KeyRing(pool, clock)
    options.setdefault("resolvers", keys.resolvers())
    options.setdefault("run_jobs", False)
    app = create_app(config, pool=pool, clock=clock, **options)
    with TestClient(app, base_url=config.public.origin) as base:
        base.event_hooks = {"request": [check_contract_request], "response": [check_contract_response]}
        yield HubHarness(app=app, config=config, clock=clock, keys=keys, pool=pool, base=base)


@pytest.fixture
def hub(
    hub_config: HubConfig, hub_pool: ConnectionPool[psycopg.Connection[Any]], clock: FixedClock
) -> Iterator[HubHarness]:
    """The running hub with every discovered extension point, the fixed clock and the test keys."""
    yield from start_hub(hub_config, hub_pool, clock)
