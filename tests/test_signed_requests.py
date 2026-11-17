"""Signed requests under /api/key/ and the hub's signed answers (KEY-PROTOCOL.md section 9, the hub).

Requests are sent straight to the ASGI app so that the raw request target, duplicate headers and
bytes outside ASCII reach the hub exactly as written. Every key is generated from a fixed seed and
the hub clock is injected.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import itertools
import json
import logging
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import parse_qs, unquote

import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_core.app import create_app
from agent_core.config import Config
from agent_core.db import Store
from agent_core.github import FakeGitHub
from agent_core.signing import LOG as KEY_LOG
from agent_core.signing import KeyRequest
from tests.conftest import HUB_KEY_ID, HUB_SEED_HEX

NOW = int(datetime(2027, 1, 15, 8, 0, 0, tzinfo=timezone.utc).timestamp())
HOST = "testserver"
WHOAMI = "/api/key/whoami"
LIMIT = 1024 * 1024
HUB_SEED = bytes.fromhex(HUB_SEED_HEX)
HUB_PRIVATE = Ed25519PrivateKey.from_private_bytes(HUB_SEED)
HUB_PUBLIC = HUB_PRIVATE.public_key()
HUB_PUBLIC_TEXT = base64.urlsafe_b64encode(
    HUB_PUBLIC.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
).rstrip(b"=").decode("ascii")
SECRET_FORMS = (
    HUB_SEED_HEX,
    HUB_SEED_HEX.upper(),
    base64.urlsafe_b64encode(HUB_SEED).rstrip(b"=").decode("ascii"),
    base64.b64encode(HUB_SEED).decode("ascii"),
)
SEEDS = {
    "agent-one": 1,
    "agent-two": 2,
    "guard-one": 3,
    "revoked-past": 4,
    "revoked-now": 5,
    "revoked-later": 6,
    "expired-past": 7,
    "expires-now": 8,
}
STRANGER_SEED = 99
_NONCES = itertools.count(1)


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def fmt(seconds: int) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def private(seed: int) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes([seed]) * 32)


def public_text(seed: int) -> str:
    return b64(
        private(seed).public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )


def new_nonce() -> str:
    return b64(next(_NONCES).to_bytes(16, "big"))


def non_canonical(text: str) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    return text[:-1] + alphabet[alphabet.index(text[-1]) + 1]


class FixedClock:
    def __init__(self, now: int) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


def _agent(key_id: str, handle: str, device: str, **extra: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": key_id,
        "kind": "ed25519",
        "principal": "agent",
        "handle": handle,
        "team": "example",
        "device": device,
        "public_key": public_text(SEEDS[key_id]),
        "scopes": ["record:read", "device:executor"],
    }
    entry.update(extra)
    return entry


def _service(key_id: str, device: str, expires: int) -> dict[str, Any]:
    return {
        "id": key_id,
        "kind": "ed25519",
        "principal": "service",
        "handle": "example-guard",
        "team": "example",
        "device": device,
        "public_key": public_text(SEEDS[key_id]),
        "scopes": ["release:query"],
        "subjects": ["example-org/example-repo"],
        "expires": fmt(expires),
    }


def key_entries() -> list[dict[str, Any]]:
    staff_key = ec.derive_private_key(4242, ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return [
        _agent("agent-one", "staff-a", "laptop-1"),
        _agent(
            "agent-two",
            "staff-b",
            "laptop-2",
            program="agent-cli",
            scopes=["record:read", "record:write", "device:controller"],
        ),
        _service("guard-one", "runner-1", NOW + 365 * 86400),
        _agent("revoked-past", "staff-a", "laptop-3", revoked=fmt(NOW - 60)),
        _agent("revoked-now", "staff-a", "laptop-4", revoked=fmt(NOW)),
        _agent("revoked-later", "staff-a", "laptop-5", revoked=fmt(NOW + 3600)),
        _service("expired-past", "runner-2", NOW - 60),
        _service("expires-now", "runner-3", NOW),
        {
            "id": "staff-pass",
            "kind": "webauthn",
            "principal": "staff",
            "handle": "staff-a",
            "team": "example",
            "credential_id": b64(b"credential-0001!"),
            "public_key": b64(staff_key),
            "scopes": ["record:read"],
        },
    ]


# Routes that exist only in the test suite (checks 9 and 10, a failing endpoint).


async def echo(request: KeyRequest) -> tuple[int, dict[str, Any]]:
    return 200, {"key": request.entry.id, "body_sha256": sha256_hex(request.body)}


async def failing(request: KeyRequest) -> tuple[int, dict[str, Any]]:
    raise RuntimeError("the test endpoint failed")


def repo_subject(request: KeyRequest) -> str | None:
    values = parse_qs(request.query).get("repo")
    if values is None or len(values) != 1:
        return None
    return values[0]


def add_test_routes(app: FastAPI) -> None:
    routes = app.state.key_service.routes
    routes.add("POST", "/api/key/test-write", echo, scope="record:write", subject=None)
    routes.add("GET", "/api/key/test-release", echo, scope="release:query", subject=repo_subject)
    routes.add("GET", "/api/key/test-write-subject", echo, scope="record:write", subject=repo_subject)
    routes.add("GET", "/api/key/test-fail", failing, scope=None, subject=None)
    routes.add("GET", "/api/key/test-slash/", echo, scope=None, subject=None)


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


@pytest.fixture
def make_app(cfg: Config, tmp_path: Path, clock: FixedClock) -> Iterator[Callable[..., FastAPI]]:
    stores: list[Store] = []

    def build(public_url: str, name: str) -> FastAPI:
        teams = tmp_path / f"{name}-teams.yaml"
        teams.write_text(
            yaml.safe_dump({"teams": {"example": {"members": ["staff-a", "staff-b"]}}}),
            encoding="utf-8",
        )
        keys = tmp_path / f"{name}-keys.yaml"
        keys.write_text(yaml.safe_dump({"keys": key_entries()}, sort_keys=False), encoding="utf-8")
        app_cfg = replace(
            cfg,
            public_url=public_url,
            teams_path=teams,
            keys_path=keys,
            database=str(tmp_path / f"{name}.sqlite"),
        )
        store = Store(app_cfg.database)
        stores.append(store)
        app = create_app(app_cfg, github=FakeGitHub({"code-staff-a": "staff-a"}), store=store, clock=clock)
        add_test_routes(app)
        return app

    try:
        yield build
    finally:
        for store in stores:
            store.close()


@pytest.fixture
def app(make_app: Callable[..., FastAPI]) -> FastAPI:
    return make_app("http://testserver", "key-hub")


class Lines(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []
        self.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


@pytest.fixture
def log_lines() -> Iterator[list[str]]:
    handler = Lines()
    root = logging.getLogger()
    saved = (KEY_LOG.level, KEY_LOG.propagate, root.level)
    KEY_LOG.addHandler(handler)
    KEY_LOG.setLevel(logging.DEBUG)
    KEY_LOG.propagate = False
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield handler.lines
    finally:
        KEY_LOG.removeHandler(handler)
        root.removeHandler(handler)
        KEY_LOG.setLevel(saved[0])
        KEY_LOG.propagate = saved[1]
        root.setLevel(saved[2])


# Building and sending requests


@dataclass(frozen=True)
class Wire:
    key_id: str
    nonce: str
    time: str
    method: str
    raw_path: bytes
    path: str
    query: bytes
    headers: list[tuple[bytes, bytes]]
    body: bytes

    def value(self, name: bytes) -> bytes:
        return next(value for key, value in self.headers if key == name)

    def with_header(self, name: bytes, value: bytes) -> "Wire":
        return replace(self, headers=[(k, value if k == name else v) for k, v in self.headers])


@dataclass(frozen=True)
class Answer:
    status: int
    headers: list[tuple[str, str]]
    body: bytes

    def header(self, name: str) -> str | None:
        values = [value for key, value in self.headers if key == name]
        assert len(values) <= 1, f"header {name} sent {len(values)} times"
        return values[0] if values else None

    def json(self) -> Any:
        return json.loads(self.body)


def request_signature(
    seed: int, *, key_id: str, method: str, host: str, target: str, time: str, nonce: str, body: bytes
) -> str:
    text = "\n".join(
        ["agent-request-v1", key_id, method, host, target, time, nonce, sha256_hex(body)]
    ).encode("utf-8")
    return b64(private(seed).sign(text))


def build_wire(
    key_id: str = "agent-one",
    *,
    seed: int | None = None,
    method: str = "GET",
    target: str = WHOAMI,
    path: str | None = None,
    body: bytes = b"",
    wire_body: bytes | None = None,
    time_text: str | None = None,
    nonce: str | None = None,
    host: str = HOST,
    sign_as: dict[str, Any] | None = None,
    extra_headers: tuple[tuple[bytes, bytes], ...] = (),
) -> Wire:
    """A request as sent. The signature covers `body` (decoded); `wire_body` is what travels."""
    nonce_text = new_nonce() if nonce is None else nonce
    time_value = fmt(NOW) if time_text is None else time_text
    signed: dict[str, Any] = {"key_id": key_id, "method": method, "host": host, "target": target, "body": body}
    if sign_as is not None:
        signed.update(sign_as)
    signature = request_signature(
        SEEDS[key_id] if seed is None else seed, time=time_value, nonce=nonce_text, **signed
    )
    raw_path, _, query = target.partition("?")
    headers = [
        (b"host", HOST.encode("ascii")),
        (b"agent-key", key_id.encode("utf-8")),
        (b"agent-time", time_value.encode("utf-8")),
        (b"agent-nonce", nonce_text.encode("utf-8")),
        (b"agent-signature", signature.encode("ascii")),
        *extra_headers,
    ]
    return Wire(
        key_id=key_id,
        nonce=nonce_text,
        time=time_value,
        method=method,
        raw_path=raw_path.encode("latin-1"),
        path=unquote(raw_path) if path is None else path,
        query=query.encode("latin-1"),
        headers=headers,
        body=body if wire_body is None else wire_body,
    )


def plain_wire(method: str, raw_path: bytes, path: str, headers: list[tuple[bytes, bytes]]) -> Wire:
    return Wire(
        key_id="",
        nonce="",
        time="",
        method=method,
        raw_path=raw_path,
        path=path,
        query=b"",
        headers=[(b"host", HOST.encode("ascii")), *headers],
        body=b"",
    )


async def asgi_call(app: FastAPI, wire: Wire) -> Answer:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": wire.method,
        "scheme": "http",
        "path": wire.path,
        "raw_path": wire.raw_path,
        "query_string": wire.query,
        "root_path": "",
        "headers": list(wire.headers),
        "client": ("127.0.0.1", 50000),
        "server": (HOST, 80),
    }
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": wire.body, "more_body": False}

    messages: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(scope, receive, send)
    start = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    headers = [(k.decode("latin-1").lower(), v.decode("latin-1")) for k, v in start["headers"]]
    return Answer(status=start["status"], headers=headers, body=body)


def send(app: FastAPI, wire: Wire) -> Answer:
    return asyncio.run(asgi_call(app, wire))


def nonce_rows(app: FastAPI) -> int:
    row = app.state.hub.store.query_one("SELECT COUNT(*) AS n FROM key_nonce")
    assert row is not None
    return int(row["n"])


def assert_signed(wire: Wire, answer: Answer, status: int, hub_now: int) -> None:
    assert answer.status == status
    assert answer.header("agent-hub-key") == HUB_KEY_ID
    hub_time = answer.header("agent-hub-time")
    assert hub_time == fmt(hub_now)
    signature = answer.header("agent-hub-signature")
    assert signature is not None
    text = "\n".join(
        [
            "agent-response-v1",
            wire.key_id,
            wire.nonce,
            wire.time,
            str(answer.status),
            hub_time,
            sha256_hex(answer.body),
        ]
    ).encode("utf-8")
    HUB_PUBLIC.verify(unb64(signature), text)
    assert answer.header("content-encoding") is None


def assert_unsigned(answer: Answer) -> None:
    for name in ("agent-hub-key", "agent-hub-time", "agent-hub-signature"):
        assert answer.header(name) is None


def assert_refused(answer: Answer, status: int, error: str) -> None:
    assert answer.status == status
    assert answer.json() == {"error": error}
    assert_unsigned(answer)


def assert_no_secret(text: str) -> None:
    for secret in SECRET_FORMS:
        assert secret not in text


# GET /api/key/whoami and the signed 200


def test_whoami_answers_the_key_entry_with_a_signed_200(app: FastAPI) -> None:
    wire = build_wire("agent-two")
    answer = send(app, wire)
    assert_signed(wire, answer, 200, NOW)
    assert answer.json() == {
        "key": "agent-two",
        "kind": "ed25519",
        "principal": "agent",
        "handle": "staff-b",
        "team": "example",
        "device": "laptop-2",
        "scopes": ["record:read", "record:write", "device:controller"],
        "hub_key": HUB_KEY_ID,
        "program": "agent-cli",
    }


def test_whoami_of_an_agent_without_program(app: FastAPI) -> None:
    answer = send(app, build_wire("agent-one"))
    assert answer.json() == {
        "key": "agent-one",
        "kind": "ed25519",
        "principal": "agent",
        "handle": "staff-a",
        "team": "example",
        "device": "laptop-1",
        "scopes": ["record:read", "device:executor"],
        "hub_key": HUB_KEY_ID,
    }


def test_whoami_of_a_service_shows_subjects_and_expires(app: FastAPI) -> None:
    answer = send(app, build_wire("guard-one"))
    assert answer.status == 200
    assert answer.json() == {
        "key": "guard-one",
        "kind": "ed25519",
        "principal": "service",
        "handle": "example-guard",
        "team": "example",
        "device": "runner-1",
        "scopes": ["release:query"],
        "hub_key": HUB_KEY_ID,
        "subjects": ["example-org/example-repo"],
        "expires": fmt(NOW + 365 * 86400),
    }


def test_signed_whoami_through_the_http_test_client(app: FastAPI) -> None:
    with TestClient(app) as client:
        for path, status in ((WHOAMI, 200), (WHOAMI + "/", 404)):
            wire = build_wire("agent-one", target=path)
            response = client.get(
                path,
                headers={
                    "Agent-Key": wire.key_id,
                    "Agent-Time": wire.time,
                    "Agent-Nonce": wire.nonce,
                    "Agent-Signature": wire.value(b"agent-signature").decode("ascii"),
                },
                follow_redirects=False,
            )
            answer = Answer(
                status=response.status_code,
                headers=[(k.lower(), v) for k, v in response.headers.items()],
                body=response.content,
            )
            assert_signed(wire, answer, status, NOW)


def test_production_app_ships_only_whoami(cfg: Config) -> None:
    store = Store(cfg.database)
    try:
        app = create_app(cfg, github=FakeGitHub({"code-alice": "alice"}), store=store)
        assert app.state.key_service.routes.registered() == [("GET", WHOAMI)]
    finally:
        store.close()


# Section 9: each refusal of section 5 in its order


def test_check_1_signature_missing(app: FastAPI) -> None:
    answer = send(app, plain_wire("GET", WHOAMI.encode(), WHOAMI, []))
    assert_refused(answer, 401, "signature_missing")


def test_check_2_key_unknown(app: FastAPI) -> None:
    assert_refused(send(app, build_wire("nobody-key", seed=STRANGER_SEED)), 401, "key_unknown")


def test_check_3_key_revoked(app: FastAPI) -> None:
    assert_refused(send(app, build_wire("revoked-past")), 401, "key_revoked")


def test_check_4_key_expired(app: FastAPI) -> None:
    assert_refused(send(app, build_wire("expired-past")), 401, "key_expired")


def test_check_5_key_kind_not_allowed(app: FastAPI) -> None:
    assert_refused(send(app, build_wire("staff-pass", seed=STRANGER_SEED)), 401, "key_kind_not_allowed")


def test_check_6_time_skew(app: FastAPI) -> None:
    assert_refused(send(app, build_wire(time_text=fmt(NOW - 300))), 401, "time_skew")


def test_check_7_signature_invalid(app: FastAPI) -> None:
    assert_refused(send(app, build_wire("agent-one", seed=STRANGER_SEED)), 401, "signature_invalid")


def test_check_8_nonce_replayed(app: FastAPI) -> None:
    wire = build_wire()
    assert send(app, wire).status == 200
    assert_refused(send(app, wire), 401, "nonce_replayed")


def test_check_9_scope_missing_is_signed(app: FastAPI) -> None:
    wire = build_wire("agent-one", method="POST", target="/api/key/test-write", body=b"{}")
    answer = send(app, wire)
    assert_signed(wire, answer, 403, NOW)
    assert answer.json() == {"error": "scope_missing"}


def test_check_10_subject_not_allowed_is_signed(app: FastAPI) -> None:
    wire = build_wire("guard-one", target="/api/key/test-release?repo=example-org/other-repo")
    answer = send(app, wire)
    assert_signed(wire, answer, 403, NOW)
    assert answer.json() == {"error": "subject_not_allowed"}


def test_check_10_subject_in_subjects_is_accepted(app: FastAPI) -> None:
    wire = build_wire("guard-one", target="/api/key/test-release?repo=example-org/example-repo")
    answer = send(app, wire)
    assert_signed(wire, answer, 200, NOW)
    assert answer.json()["key"] == "guard-one"


def test_check_10_applies_only_to_service_keys(app: FastAPI) -> None:
    wire = build_wire("agent-two", target="/api/key/test-write-subject?repo=example-org/other-repo")
    answer = send(app, wire)
    assert_signed(wire, answer, 200, NOW)


@pytest.mark.parametrize(
    ("make", "status", "error"),
    [
        # 1 before 2: a malformed request with an unknown key.
        (lambda: build_wire("nobody-key", seed=STRANGER_SEED, method="get"), 401, "signature_missing"),
        # 2 before 6 and 7.
        (lambda: build_wire("nobody-key", seed=STRANGER_SEED, time_text=fmt(NOW - 999)), 401, "key_unknown"),
        # 3 before 6 and 7.
        (lambda: build_wire("revoked-past", seed=STRANGER_SEED, time_text=fmt(NOW - 999)), 401, "key_revoked"),
        # 4 before 6 and 7.
        (lambda: build_wire("expired-past", seed=STRANGER_SEED, time_text=fmt(NOW - 999)), 401, "key_expired"),
        # 5 before 6 and 7.
        (lambda: build_wire("staff-pass", seed=STRANGER_SEED, time_text=fmt(NOW - 999)), 401, "key_kind_not_allowed"),
        # 6 before 7.
        (lambda: build_wire("agent-one", seed=STRANGER_SEED, time_text=fmt(NOW - 999)), 401, "time_skew"),
    ],
)
def test_checks_stop_at_the_first_failure(
    app: FastAPI, make: Callable[[], Wire], status: int, error: str
) -> None:
    assert_refused(send(app, make()), status, error)
    assert nonce_rows(app) == 0


def test_check_7_before_check_8(app: FastAPI) -> None:
    accepted = build_wire()
    assert send(app, accepted).status == 200
    forged = build_wire(seed=STRANGER_SEED, nonce=accepted.nonce)
    assert_refused(send(app, forged), 401, "signature_invalid")


def test_check_9_before_check_10(app: FastAPI) -> None:
    wire = build_wire("guard-one", target="/api/key/test-write-subject?repo=example-org/other-repo")
    answer = send(app, wire)
    assert_signed(wire, answer, 403, NOW)
    assert answer.json() == {"error": "scope_missing"}


# Section 9: malformed requests refused at check 1, and gzip


def _without(name: bytes) -> Wire:
    wire = build_wire()
    return replace(wire, headers=[(k, v) for k, v in wire.headers if k != name])


def _twice(name: bytes) -> Wire:
    wire = build_wire()
    return replace(wire, headers=[*wire.headers, (name, wire.value(name))])


def _twice_other_case(name: bytes) -> Wire:
    wire = build_wire()
    return replace(wire, headers=[*wire.headers, (name.upper(), wire.value(name))])


def _signature_changed(change: Callable[[str], str]) -> Wire:
    wire = build_wire()
    return wire.with_header(
        b"agent-signature", change(wire.value(b"agent-signature").decode("ascii")).encode("ascii")
    )


def _write(**changes: Any) -> Wire:
    values: dict[str, Any] = {"method": "POST", "target": "/api/key/test-write", "body": b'{"event":1}'}
    values.update(changes)
    return build_wire("agent-two", **values)


def _coded(*codings: bytes, body: bytes, wire_body: bytes) -> Wire:
    return _write(
        body=body,
        wire_body=wire_body,
        extra_headers=tuple((b"content-encoding", coding) for coding in codings),
    )


PADDED_NONCE = base64.urlsafe_b64encode((7).to_bytes(16, "big")).decode("ascii")
SMALL = b'{"event":1}'
CHECK_1_CASES = {
    "no agent headers": lambda: plain_wire("GET", WHOAMI.encode(), WHOAMI, []),
    "agent-key missing": lambda: _without(b"agent-key"),
    "agent-time missing": lambda: _without(b"agent-time"),
    "agent-nonce missing": lambda: _without(b"agent-nonce"),
    "agent-signature missing": lambda: _without(b"agent-signature"),
    "agent-key twice": lambda: _twice(b"agent-key"),
    "agent-time twice": lambda: _twice(b"agent-time"),
    "agent-nonce twice": lambda: _twice(b"agent-nonce"),
    "agent-signature twice": lambda: _twice(b"agent-signature"),
    "agent-nonce twice in other case": lambda: _twice_other_case(b"agent-nonce"),
    "padded nonce": lambda: build_wire(nonce=PADDED_NONCE),
    "padded signature": lambda: _signature_changed(lambda value: value + "=="),
    "non-canonical nonce": lambda: build_wire(nonce=non_canonical(b64((8).to_bytes(16, "big")))),
    "non-canonical signature": lambda: _signature_changed(non_canonical),
    "nonce in standard base64": lambda: build_wire(nonce=base64.b64encode(b"\xfb" * 16).decode().rstrip("=")),
    "nonce of 15 bytes": lambda: build_wire(nonce=b64(b"n" * 15)),
    "nonce of 17 bytes": lambda: build_wire(nonce=b64(b"n" * 17)),
    "signature of 63 bytes": lambda: _signature_changed(lambda value: b64(unb64(value)[:63])),
    "signature of 65 bytes": lambda: _signature_changed(lambda value: b64(unb64(value) + b"\x00")),
    "method in lowercase": lambda: build_wire(method="get"),
    "method in mixed case": lambda: build_wire(method="Get"),
    "agent-key with uppercase": lambda: build_wire("Agent-One", seed=1),
    "agent-key too short": lambda: build_wire("ab", seed=1),
    "agent-key with underscore": lambda: build_wire("agent_one", seed=1),
    "agent-key outside ascii": lambda: build_wire().with_header(b"agent-key", "agent-öne".encode("utf-8")),
    "content-encoding identity": lambda: _coded(b"identity", body=SMALL, wire_body=SMALL),
    "content-encoding br": lambda: _coded(b"br", body=SMALL, wire_body=SMALL),
    "content-encoding deflate": lambda: _coded(b"deflate", body=SMALL, wire_body=SMALL),
    "content-encoding x-gzip": lambda: _coded(b"x-gzip", body=SMALL, wire_body=gzip.compress(SMALL, mtime=0)),
    "content-encoding list gzip, gzip": lambda: _coded(
        b"gzip, gzip", body=SMALL, wire_body=gzip.compress(gzip.compress(SMALL, mtime=0), mtime=0)
    ),
    "content-encoding list gzip, identity": lambda: _coded(
        b"gzip, identity", body=SMALL, wire_body=gzip.compress(SMALL, mtime=0)
    ),
    "two content codings in two headers": lambda: _coded(
        b"gzip", b"gzip", body=SMALL, wire_body=gzip.compress(gzip.compress(SMALL, mtime=0), mtime=0)
    ),
    "corrupt gzip body": lambda: _coded(b"gzip", body=SMALL, wire_body=b"this is not gzip"),
    "truncated gzip body": lambda: _coded(b"gzip", body=SMALL, wire_body=gzip.compress(SMALL, mtime=0)[:-6]),
    "gzip body followed by data": lambda: _coded(
        b"gzip", body=SMALL, wire_body=gzip.compress(SMALL, mtime=0) + b"trailing"
    ),
    "empty gzip body": lambda: _coded(b"gzip", body=b"", wire_body=b""),
    "body over the limit before decoding": lambda: _write(body=b"a" * (LIMIT + 1)),
    "body over the limit after decoding": lambda: _coded(
        b"gzip", body=b"a" * (LIMIT + 1), wire_body=gzip.compress(b"a" * (LIMIT + 1), mtime=0)
    ),
    "target with a byte outside ascii": lambda: build_wire(target="/api/key/whöami"),
    "target in absolute form": lambda: build_wire(target="http://testserver/api/key/whoami", path=WHOAMI),
    "target with a space": lambda: build_wire(target="/api/key/who ami"),
    "query with a byte outside ascii": lambda: build_wire(target="/api/key/whoami?a=é"),
    "agent-time with fractional seconds": lambda: build_wire(time_text="2027-01-15T08:00:00.5Z"),
    "agent-time with an offset": lambda: build_wire(time_text="2027-01-15T08:00:00+00:00"),
    "agent-time with an impossible date": lambda: build_wire(time_text="2027-02-30T08:00:00Z"),
    "agent-time with an impossible hour": lambda: build_wire(time_text="2027-01-15T24:00:00Z"),
    "agent-time with a lowercase z": lambda: build_wire(time_text="2027-01-15T08:00:00z"),
    "agent-time without seconds": lambda: build_wire(time_text="2027-01-15T08:00Z"),
    "agent-time as epoch seconds": lambda: build_wire(time_text=str(NOW)),
}


@pytest.mark.parametrize("case", sorted(CHECK_1_CASES))
def test_malformed_request_is_refused_at_check_1(app: FastAPI, case: str) -> None:
    answer = send(app, CHECK_1_CASES[case]())
    assert_refused(answer, 401, "signature_missing")
    assert nonce_rows(app) == 0


def test_head_refused_at_check_1_has_no_body(app: FastAPI) -> None:
    answer = send(app, plain_wire("HEAD", WHOAMI.encode(), WHOAMI, []))
    assert answer.status == 401
    assert answer.body == b""
    assert_unsigned(answer)


def test_gzip_body_whose_signature_covers_the_decoded_bytes_is_accepted(app: FastAPI) -> None:
    wire = _coded(b"gzip", body=SMALL, wire_body=gzip.compress(SMALL, mtime=0))
    answer = send(app, wire)
    assert_signed(wire, answer, 200, NOW)
    assert answer.json() == {"key": "agent-two", "body_sha256": sha256_hex(SMALL)}


def test_gzip_is_compared_without_regard_to_case(app: FastAPI) -> None:
    wire = _coded(b"GZip", body=SMALL, wire_body=gzip.compress(SMALL, mtime=0))
    assert_signed(wire, send(app, wire), 200, NOW)


def test_gzip_body_signed_over_the_compressed_bytes_is_refused(app: FastAPI) -> None:
    compressed = gzip.compress(SMALL, mtime=0)
    wire = _coded(b"gzip", body=compressed, wire_body=compressed)
    assert_refused(send(app, wire), 401, "signature_invalid")


def test_body_of_exactly_the_limit_is_accepted(app: FastAPI) -> None:
    body = b"a" * LIMIT
    plain = _write(body=body)
    assert_signed(plain, send(app, plain), 200, NOW)
    coded = _coded(b"gzip", body=body, wire_body=gzip.compress(body, mtime=0))
    assert_signed(coded, send(app, coded), 200, NOW)


# Section 9: encoded paths


@pytest.mark.parametrize("raw_path", [b"/api/%6Bey/whoami", b"/api/key%2Fwhoami", b"/api/key/whoami"])
def test_encoded_key_paths_without_agent_headers_are_refused(app: FastAPI, raw_path: bytes) -> None:
    answer = send(app, plain_wire("GET", raw_path, unquote(raw_path.decode("ascii")), []))
    assert_refused(answer, 401, "signature_missing")


def test_correctly_signed_encoded_path_is_accepted(app: FastAPI) -> None:
    wire = build_wire(target="/api/%6Bey/whoami")
    assert wire.path == WHOAMI
    answer = send(app, wire)
    assert_signed(wire, answer, 200, NOW)
    assert answer.json()["key"] == "agent-one"


def test_signature_covers_the_raw_target_not_the_decoded_path(app: FastAPI) -> None:
    wire = build_wire(target="/api/%6Bey/whoami", sign_as={"target": WHOAMI})
    assert_refused(send(app, wire), 401, "signature_invalid")


def test_question_mark_with_empty_query_is_refused_at_check_7(app: FastAPI) -> None:
    wire = build_wire(sign_as={"target": WHOAMI + "?"})
    assert wire.query == b""
    assert_refused(send(app, wire), 401, "signature_invalid")


# Section 9: signed 404, never 405 or a redirect


@pytest.mark.parametrize(
    ("method", "target"),
    [
        ("POST", WHOAMI),
        ("PUT", WHOAMI),
        ("DELETE", WHOAMI),
        ("GET", WHOAMI + "/"),
        ("GET", "/api/key/test-slash"),
        ("GET", "/api/key/unknown"),
        ("GET", "/api/key/"),
        ("GET", "/api/key/WHOAMI"),
    ],
)
def test_no_endpoint_gives_a_signed_404(app: FastAPI, method: str, target: str) -> None:
    wire = build_wire(method=method, target=target)
    answer = send(app, wire)
    assert_signed(wire, answer, 404, NOW)
    assert answer.json() == {"error": "not_found"}
    assert answer.header("location") is None


def test_head_gives_a_signed_404_without_body(app: FastAPI) -> None:
    wire = build_wire(method="HEAD")
    answer = send(app, wire)
    assert answer.body == b""
    assert_signed(wire, answer, 404, NOW)


def test_known_route_with_its_trailing_slash_is_found(app: FastAPI) -> None:
    wire = build_wire(target="/api/key/test-slash/")
    assert_signed(wire, send(app, wire), 200, NOW)


# Section 9: the time window


@pytest.mark.parametrize(("offset", "status"), [(-120, 200), (120, 200), (-121, 401), (121, 401)])
def test_time_window_is_120_seconds(app: FastAPI, offset: int, status: int) -> None:
    answer = send(app, build_wire(time_text=fmt(NOW + offset)))
    if status == 200:
        assert answer.status == 200
    else:
        assert_refused(answer, 401, "time_skew")


def test_hub_clock_moves_the_window(app: FastAPI, clock: FixedClock) -> None:
    clock.now = NOW + 1000
    wire = build_wire(time_text=fmt(NOW + 1000 - 120))
    assert_signed(wire, send(app, wire), 200, NOW + 1000)
    assert_refused(send(app, build_wire(time_text=fmt(NOW))), 401, "time_skew")


# Section 9: replay, parallel copies, the same nonce under two keys


def test_replayed_request_is_refused(app: FastAPI) -> None:
    wire = build_wire()
    assert_signed(wire, send(app, wire), 200, NOW)
    assert_refused(send(app, wire), 401, "nonce_replayed")
    assert_refused(send(app, wire), 401, "nonce_replayed")


def test_two_parallel_copies_exactly_one_accepted(app: FastAPI) -> None:
    wire = build_wire()

    async def both() -> list[Answer]:
        return list(await asyncio.gather(asgi_call(app, wire), asgi_call(app, wire)))

    answers = asyncio.run(both())
    assert sorted(answer.status for answer in answers) == [200, 401]
    refused = next(answer for answer in answers if answer.status == 401)
    assert_refused(refused, 401, "nonce_replayed")
    assert nonce_rows(app) == 1


def test_same_nonce_under_two_keys_both_accepted(app: FastAPI) -> None:
    nonce = new_nonce()
    first = build_wire("agent-one", nonce=nonce)
    second = build_wire("agent-two", nonce=nonce)
    assert_signed(first, send(app, first), 200, NOW)
    assert_signed(second, send(app, second), 200, NOW)


def test_nonce_rows_older_than_ten_minutes_are_deleted(app: FastAPI, clock: FixedClock) -> None:
    first = build_wire()
    assert send(app, first).status == 200
    clock.now = NOW + 600
    assert send(app, build_wire(time_text=fmt(NOW + 600))).status == 200
    assert nonce_rows(app) == 2
    clock.now = NOW + 601
    assert send(app, build_wire(time_text=fmt(NOW + 601))).status == 200
    assert nonce_rows(app) == 2
    store = app.state.hub.store
    assert store.query_one("SELECT 1 AS x FROM key_nonce WHERE nonce = ?", (first.nonce,)) is None


# Section 9: signed for something else


@pytest.mark.parametrize(
    ("changes", "sign_as"),
    [
        ({}, {"key_id": "agent-two"}),
        ({}, {"host": "other.example"}),
        ({}, {"host": "testserver:80"}),
        ({"method": "POST", "target": "/api/key/test-write"}, {"method": "PUT"}),
        ({}, {"target": "/api/key/other"}),
        ({"target": WHOAMI + "?a=2"}, {"target": WHOAMI + "?a=1"}),
        ({"target": WHOAMI + "?a=1"}, {"target": WHOAMI}),
        ({"body": b"sent body"}, {"body": b"signed body"}),
        ({"body": b"sent body"}, {"body": b""}),
    ],
)
def test_request_signed_for_something_else_is_refused(
    app: FastAPI, changes: dict[str, Any], sign_as: dict[str, Any]
) -> None:
    key = "agent-two" if changes.get("target") == "/api/key/test-write" else "agent-one"
    wire = build_wire(key, sign_as=sign_as, **changes)
    assert_refused(send(app, wire), 401, "signature_invalid")
    assert nonce_rows(app) == 0


# Section 9: the host line


def test_host_line_from_uppercase_public_url_with_default_port(make_app: Callable[..., FastAPI]) -> None:
    app = make_app("HTTP://TestServer:80", "upper-hub")
    accepted = build_wire(host="testserver")
    assert_signed(accepted, send(app, accepted), 200, NOW)
    for host in ("TestServer:80", "testserver:80", "TestServer"):
        assert_refused(send(app, build_wire(host=host)), 401, "signature_invalid")


def test_host_line_keeps_a_port_that_is_not_the_default(make_app: Callable[..., FastAPI]) -> None:
    app = make_app("https://Hub.Example.COM:8443/", "port-hub")
    accepted = build_wire(host="hub.example.com:8443")
    assert_signed(accepted, send(app, accepted), 200, NOW)
    assert_refused(send(app, build_wire(host="hub.example.com")), 401, "signature_invalid")


# Section 9: revoked and expired keys, a webauthn key used directly


@pytest.mark.parametrize(
    ("key_id", "error"),
    [
        ("revoked-past", "key_revoked"),
        ("revoked-now", "key_revoked"),
        ("expired-past", "key_expired"),
        ("expires-now", "key_expired"),
    ],
)
def test_revoked_or_expired_key_is_refused(app: FastAPI, key_id: str, error: str) -> None:
    assert_refused(send(app, build_wire(key_id)), 401, error)


def test_key_revoked_in_the_future_is_accepted(app: FastAPI) -> None:
    wire = build_wire("revoked-later")
    assert_signed(wire, send(app, wire), 200, NOW)


def test_revocation_takes_effect_when_the_clock_reaches_it(app: FastAPI, clock: FixedClock) -> None:
    clock.now = NOW + 3600
    assert_refused(send(app, build_wire("revoked-later", time_text=fmt(NOW + 3600))), 401, "key_revoked")


def test_webauthn_key_used_directly_is_refused(app: FastAPI) -> None:
    assert_refused(send(app, build_wire("staff-pass", seed=STRANGER_SEED)), 401, "key_kind_not_allowed")


# Section 9: which answers are signed; existing endpoints are unchanged


def test_no_hub_signature_on_any_refusal_at_checks_1_to_8(app: FastAPI) -> None:
    replayed = build_wire()
    assert send(app, replayed).status == 200
    refusals = [
        plain_wire("GET", WHOAMI.encode(), WHOAMI, []),
        build_wire("nobody-key", seed=STRANGER_SEED),
        build_wire("revoked-past"),
        build_wire("expired-past"),
        build_wire("staff-pass", seed=STRANGER_SEED),
        build_wire(time_text=fmt(NOW - 500)),
        build_wire(seed=STRANGER_SEED),
        replayed,
    ]
    for wire in refusals:
        answer = send(app, wire)
        assert answer.status == 401
        assert_unsigned(answer)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/health"),
        ("GET", "/auth/me"),
        ("GET", "/api/keys"),
        ("GET", "/api/key"),
        ("GET", "/api/%2565y/whoami"),
        ("POST", "/sync/push"),
    ],
)
def test_existing_endpoint_with_agent_headers_behaves_as_before(
    app: FastAPI, method: str, path: str
) -> None:
    signed = build_wire(method=method, target=path)
    with_headers = send(app, signed)
    without = send(app, replace(signed, headers=[(b"host", HOST.encode("ascii"))]))
    assert with_headers.status == without.status
    assert with_headers.body == without.body
    assert_unsigned(with_headers)
    assert nonce_rows(app) == 0


def test_health_with_agent_headers_is_the_plain_health_answer(app: FastAPI) -> None:
    answer = send(app, build_wire(target="/health"))
    assert answer.status == 200
    assert answer.json() == {"status": "ok"}
    assert_unsigned(answer)


# Section 9: 500 before and after check 8


def test_nonce_insert_failure_other_than_the_constraint_gives_unsigned_500(app: FastAPI) -> None:
    app.state.hub.store.execute(
        "CREATE TRIGGER key_nonce_refuse BEFORE INSERT ON key_nonce "
        "BEGIN SELECT RAISE(ABORT, 'refused by the test'); END"
    )
    assert_refused(send(app, build_wire()), 500, "internal")


def test_nonce_store_error_gives_unsigned_500(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(_key_id: str, _nonce: str, _seen_at: int) -> bool:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(app.state.hub.store, "insert_nonce", broken)
    assert_refused(send(app, build_wire()), 500, "internal")


def test_500_after_check_8_is_signed(app: FastAPI) -> None:
    wire = build_wire(target="/api/key/test-fail")
    answer = send(app, wire)
    assert_signed(wire, answer, 500, NOW)
    assert answer.json() == {"error": "internal"}


# Section 9: the start log line, the service log lines and no secret anywhere


def test_start_log_line_names_the_hub_key_and_not_the_seed(
    app: FastAPI, log_lines: list[str]
) -> None:
    with TestClient(app):
        pass
    starts = [line for line in log_lines if " hub key " in line]
    assert starts == [f"uvicorn.error INFO hub key {HUB_KEY_ID} {HUB_PUBLIC_TEXT}"]
    for line in log_lines:
        assert_no_secret(line)


def test_one_log_line_per_service_request_past_check_8_and_no_secret(
    app: FastAPI, log_lines: list[str]
) -> None:
    whoami = build_wire("guard-one")
    sent = [
        whoami,
        build_wire("guard-one", target="/api/key/test-release?repo=example-org/other-repo"),
        build_wire("guard-one", target="/api/key/unknown"),
        build_wire("guard-one", target="/api/key/test-fail"),
        build_wire("guard-one", method="POST", target="/api/key/test-write"),
        build_wire("guard-one", target="/api/key/test-release?repo=example-org/example-repo"),
        build_wire("guard-one", method="HEAD"),
        # Not past check 8: no line.
        build_wire("guard-one", seed=STRANGER_SEED),
        whoami,
        build_wire("expired-past"),
        # Not a service key: no line.
        build_wire("agent-one"),
        build_wire("agent-one", target="/api/key/test-fail"),
    ]
    answers = [send(app, wire) for wire in sent]
    assert [answer.status for answer in answers] == [200, 403, 404, 500, 403, 200, 404, 401, 401, 401, 200, 500]
    prefix = "uvicorn.error INFO key request "
    service_lines = [line[len(prefix):] for line in log_lines if line.startswith(prefix)]
    assert service_lines == [
        "guard-one GET /api/key/whoami 200",
        "guard-one GET /api/key/test-release 403",
        "guard-one GET /api/key/unknown 404",
        "guard-one GET /api/key/test-fail 500",
        "guard-one POST /api/key/test-write 403",
        "guard-one GET /api/key/test-release 200",
        "guard-one HEAD /api/key/whoami 404",
    ]
    for line in log_lines:
        assert_no_secret(line)
    for answer in answers:
        assert_no_secret(answer.body.decode("latin-1"))
        for name, value in answer.headers:
            assert_no_secret(f"{name}: {value}")


def test_hub_signer_repr_has_no_seed(app: FastAPI) -> None:
    service = app.state.key_service
    assert_no_secret(repr(service.signer))
    assert HUB_PUBLIC_TEXT in repr(service.signer)
