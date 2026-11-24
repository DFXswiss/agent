"""A hub for the tests of the passkey step (PASSKEY-PROTOCOL.md sections 13.2 to 13.6).

Every request goes through the application, as the signed-request tests do (`asgi_call`), with a
fixed clock and the software authenticator of section 13.1. The passkeys of the keys file are
software authenticators with a seeded source of randomness, so that a test can build a second
authenticator that holds the same credential.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import itertools
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest
import yaml
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from fastapi import FastAPI

from agent_core.app import create_app
from agent_core.config import Config
from agent_core.db import Store
from agent_core.github import FakeGitHub, GitHub
from tests.conftest import HUB_KEY_ID, HUB_SEED_HEX, idle_record
from tests.test_signed_requests import Answer, FixedClock, Wire, asgi_call
from tests.webauthn_authenticator import Settings, SoftAuthenticator, point_of

NOW = int(datetime(2027, 1, 15, 8, 0, 0, tzinfo=timezone.utc).timestamp())
HOST = "testserver"
ORIGIN = "http://testserver"
RP_ID = "testserver"
TEAM = "example"
WHOAMI = "/api/key/whoami"
INVITES = "/api/key/invites"
SESSION_END = "/api/key/session/end"
COMMANDS = "/api/key/commands"
OPEN = "/auth/invite/open"
REGISTER = "/auth/invite/finish"
BEGIN = "/auth/passkey/begin"
FINISH = "/auth/passkey/finish"
ANONYMOUS_ROUTES = (OPEN, REGISTER, BEGIN, FINISH)
P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
ISSUE_SECONDS = 43080
RECORD_DATABASE_PASSWORD = "5e" * 32
RECORD_DATABASE_URL = (
    f"postgresql://agent_core_writer:{RECORD_DATABASE_PASSWORD}@localhost/agent_core_record?sslmode=disable"
)

ROSTER = {
    "teams": {
        "example": {"members": ["example-admin", "example-staff", "other-staff", "helperbot"]},
        "ops": {"members": ["example-staff"]},
    },
    "agents": {"helperbot": {"owners": ["example-admin"]}},
}

# The passkeys of the keys file and the seed of each one's randomness.
PASSKEY_SEEDS = {"admin": 1, "staff": 2, "reader": 3, "revoked": 4, "later": 5}
# The Ed25519 keys of the keys file.
ED_SEEDS = {
    "example-staff-laptop-1": 11,
    "example-staff-laptop-cli": 12,
    "example-staff-runner-cli": 13,
    "example-staff-old": 14,
    "example-staff-exec": 15,
    "other-staff-laptop-1": 16,
    "example-guard-1": 17,
}
_NONCES = itertools.count(1)


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def fmt(seconds: int) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


PAST = fmt(NOW - 60)
FUTURE = fmt(NOW + 3600)


def seeded_random(seed: int) -> Callable[[int], bytes]:
    """A deterministic source of bytes: two authenticators with one seed create one credential."""
    counter = itertools.count()

    def random(size: int) -> bytes:
        out = b""
        while len(out) < size:
            out += hashlib.sha256(f"{seed}:{next(counter)}".encode()).digest()
        return out[:size]

    return random


def new_passkey(seed: int, *, counter_start: int = 0, counter_step: int = 0) -> SoftAuthenticator:
    """An authenticator holding one credential for the hub's rp id."""
    passkey = SoftAuthenticator(random=seeded_random(seed), counter_start=counter_start, counter_step=counter_step)
    passkey.create({"rp_id": RP_ID, "user_id": b64(bytes(16)), "challenge": b64(bytes(32))}, ORIGIN)
    return passkey


def ed_private(seed: int) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes([seed]) * 32)


def ed_public(seed: int) -> str:
    return b64(ed_private(seed).public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))


def staff_entry(key_id: str, handle: str, passkey: SoftAuthenticator, scopes: list[str], **extra: Any) -> dict[str, Any]:
    credential = passkey.credentials[0]
    entry: dict[str, Any] = {
        "id": key_id,
        "kind": "webauthn",
        "principal": "staff",
        "handle": handle,
        "team": TEAM,
        "credential_id": b64(credential.credential_id),
        "public_key": b64(credential.public_key),
        "scopes": scopes,
    }
    entry.update(extra)
    return entry


def agent_entry(key_id: str, handle: str, device: str, scopes: list[str], **extra: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": key_id,
        "kind": "ed25519",
        "principal": "agent",
        "handle": handle,
        "team": TEAM,
        "device": device,
        "public_key": ed_public(ED_SEEDS[key_id]),
        "scopes": scopes,
    }
    entry.update(extra)
    return entry


def service_entry(key_id: str) -> dict[str, Any]:
    return {
        "id": key_id,
        "kind": "ed25519",
        "principal": "service",
        "handle": "example-guard",
        "team": TEAM,
        "device": "runner-1",
        "public_key": ed_public(ED_SEEDS[key_id]),
        "scopes": ["record:read", "release:query"],
        "subjects": ["example-org/example-repo"],
        "expires": fmt(NOW + 365 * 86400),
    }


CONTROLLER = ["record:read", "record:write", "device:controller"]


def default_entries(passkeys: dict[str, SoftAuthenticator]) -> list[dict[str, Any]]:
    return [
        staff_entry("admin-passkey-1", "example-admin", passkeys["admin"], ["record:read", "record:write", "keys:invite"]),
        staff_entry("staff-passkey-1", "example-staff", passkeys["staff"], ["record:read", "record:write"]),
        staff_entry("reader-passkey-1", "other-staff", passkeys["reader"], ["record:read"]),
        staff_entry("revoked-passkey-1", "other-staff", passkeys["revoked"], ["record:read"], revoked=PAST),
        staff_entry("later-passkey-1", "example-staff", passkeys["later"], ["record:read", "record:write"], revoked=FUTURE),
        agent_entry("example-staff-laptop-1", "example-staff", "laptop-1", CONTROLLER),
        agent_entry("example-staff-laptop-cli", "example-staff", "laptop-1", CONTROLLER, program="agent-cli"),
        agent_entry("example-staff-runner-cli", "example-staff", "runner-2", CONTROLLER, program="runner-cli"),
        agent_entry("example-staff-old", "example-staff", "laptop-9", CONTROLLER, revoked=PAST),
        agent_entry("example-staff-exec", "example-staff", "desk-1", ["record:read", "record:write", "device:executor"]),
        agent_entry("other-staff-laptop-1", "other-staff", "laptop-1", CONTROLLER),
        service_entry("example-guard-1"),
    ]


class FailingGitHub(GitHub):
    """A GitHub fake that fails on every call and records it (section 13.6)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def _fail(self, name: str) -> Any:
        self.calls.append(name)
        raise RuntimeError(f"GitHub was called: {name}")

    def authorize_url(self, *args: Any, **kwargs: Any) -> Any:
        return self._fail("authorize_url")

    def login_for_code(self, *args: Any, **kwargs: Any) -> Any:
        return self._fail("login_for_code")

    def search_open_prs(self, *args: Any, **kwargs: Any) -> Any:
        return self._fail("search_open_prs")

    def create_issue(self, *args: Any, **kwargs: Any) -> Any:
        return self._fail("create_issue")

    def recent_issues_by_creator(self, *args: Any, **kwargs: Any) -> Any:
        return self._fail("recent_issues_by_creator")

    def repository(self, *args: Any, **kwargs: Any) -> Any:
        return self._fail("repository")

    def issue(self, *args: Any, **kwargs: Any) -> Any:
        return self._fail("issue")

    def add_assignees(self, *args: Any, **kwargs: Any) -> Any:
        return self._fail("add_assignees")

    def create_issue_comment(self, *args: Any, **kwargs: Any) -> Any:
        return self._fail("create_issue_comment")

    def issue_comments(self, *args: Any, **kwargs: Any) -> Any:
        return self._fail("issue_comments")


@dataclass
class Session:
    """A signed-in session key: its id, its private key and the finish answer."""

    id: str
    private: ec.EllipticCurvePrivateKey
    public: bytes
    answer: dict[str, Any]


def session_signature(private: ec.EllipticCurvePrivateKey, text: bytes) -> bytes:
    """The session key signature of section 2: 64 bytes `r ‖ s`, as WebCrypto writes it."""
    r, s = decode_dss_signature(private.sign(text, ec.ECDSA(hashes.SHA256())))
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def request_text(key_id: str, method: str, target: str, time_text: str, nonce: str, body: bytes, host: str = HOST) -> bytes:
    lines = ["agent-request-v1", key_id, method, host, target, time_text, nonce, hashlib.sha256(body).hexdigest()]
    return "\n".join(lines).encode("utf-8")


def command_text(sender: str, target: str, action: str, sequence: object, time_text: str, content_hash: str = "-") -> bytes:
    return "\n".join(["agent-command-v1", sender, target, action, str(sequence), time_text, content_hash]).encode("utf-8")


def new_nonce() -> str:
    """16 bytes, base64url; disjoint from the nonces of `tests.test_signed_requests`."""
    return b64((10**9 + next(_NONCES)).to_bytes(16, "big"))


def anonymous_wire(path: str, body: bytes, headers: tuple[tuple[bytes, bytes], ...] = ()) -> Wire:
    return Wire(
        key_id="",
        nonce="",
        time="",
        method="POST",
        raw_path=path.encode("ascii"),
        path=path,
        query=b"",
        headers=[(b"host", HOST.encode("ascii")), (b"content-type", b"application/json"), *headers],
        body=body,
    )


def signed_wire(
    key_id: str,
    sign: Callable[[bytes], bytes],
    *,
    method: str = "GET",
    target: str = WHOAMI,
    body: bytes = b"",
    time_text: str,
    nonce: str | None = None,
    sign_as: dict[str, Any] | None = None,
    extra_headers: tuple[tuple[bytes, bytes], ...] = (),
) -> Wire:
    nonce_text = new_nonce() if nonce is None else nonce
    signed: dict[str, Any] = {"key_id": key_id, "method": method, "target": target, "body": body}
    if sign_as is not None:
        signed.update(sign_as)
    text = request_text(signed["key_id"], signed["method"], signed["target"], time_text, nonce_text, signed["body"])
    raw_path, _, query = target.partition("?")
    headers = [
        (b"host", HOST.encode("ascii")),
        (b"agent-key", key_id.encode("ascii")),
        (b"agent-time", time_text.encode("ascii")),
        (b"agent-nonce", nonce_text.encode("ascii")),
        (b"agent-signature", b64(sign(text)).encode("ascii")),
        *extra_headers,
    ]
    return Wire(
        key_id=key_id,
        nonce=nonce_text,
        time=time_text,
        method=method,
        raw_path=raw_path.encode("ascii"),
        path=raw_path,
        query=query.encode("ascii"),
        headers=headers,
        body=body,
    )


@dataclass
class PasskeyHub:
    """One started hub of a test, with the factory's clock."""

    app: FastAPI
    store: Store
    clock: FixedClock
    cfg: Config
    env: dict[str, str]

    def send(self, wire: Wire) -> Answer:
        return asyncio.run(asgi_call(self.app, wire))

    def post(self, path: str, body: Any, *, headers: tuple[tuple[bytes, bytes], ...] = ()) -> Answer:
        raw = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        return self.send(anonymous_wire(path, raw, headers))

    def time(self) -> str:
        return fmt(self.clock.now)

    def key_wire(self, key_id: str, **options: Any) -> Wire:
        seed = ED_SEEDS[key_id]
        options.setdefault("time_text", self.time())
        return signed_wire(key_id, lambda text: ed_private(seed).sign(text), **options)

    def session_wire(self, session: Session, **options: Any) -> Wire:
        options.setdefault("time_text", self.time())
        return signed_wire(session.id, lambda text: session_signature(session.private, text), **options)

    def begin(self, private: ec.EllipticCurvePrivateKey | None = None) -> tuple[Answer, ec.EllipticCurvePrivateKey]:
        key = private if private is not None else ec.generate_private_key(ec.SECP256R1())
        return self.post(BEGIN, {"session_public_key": b64(point_of(key))}), key

    def assertion(self, passkey: SoftAuthenticator, begun: Answer, settings: Settings | None = None) -> dict[str, Any]:
        answer = begun.json()
        asserted = passkey.get({"challenge": answer["challenge"], "rp_id": answer["rp_id"]}, ORIGIN, settings)
        return asserted.body(answer["sign_in"])

    def sign_in(self, passkey: SoftAuthenticator, settings: Settings | None = None) -> Session:
        begun, private = self.begin()
        assert begun.status == 200, begun.body
        finish = self.post(FINISH, self.assertion(passkey, begun, settings))
        assert finish.status == 200, finish.body
        answer = finish.json()
        return Session(id=answer["session_key"], private=private, public=point_of(private), answer=answer)

    def create_invite(self, session: Session, *, handle: str = "example-staff", team: str = TEAM, lifetime: int = 3600) -> dict[str, Any]:
        body = json.dumps({"handle": handle, "team": team, "lifetime_seconds": lifetime}).encode()
        answer = self.send(self.session_wire(session, method="POST", target=INVITES, body=body))
        assert answer.status == 201, answer.body
        return answer.json()

    def open(self, link: str) -> Answer:
        return self.post(OPEN, {"token": link.split("#", 1)[1]})

    def register(self, passkey: SoftAuthenticator, opened: Answer, settings: Settings | None = None, **changes: Any) -> Answer:
        options = opened.json()
        created = passkey.create(options, ORIGIN, settings)
        body = created.body(options["registration"])
        body.update(changes)
        return self.post(REGISTER, body)

    def invites(self, session: Session) -> list[dict[str, Any]]:
        answer = self.send(self.session_wire(session, target=INVITES))
        assert answer.status == 200, answer.body
        return answer.json()["invites"]

    def rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return [dict(row) for row in self.store.query(sql, params)]


class HubFactory:
    """Builds hubs on one database and one clock, as a deployment of a new keys file does."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.clock = FixedClock(NOW)
        self.database = str(tmp_path / "passkey.sqlite")
        self.passkeys = {name: new_passkey(seed) for name, seed in PASSKEY_SEEDS.items()}
        self.stores: list[Store] = []
        self.builds = 0

    def entries(self) -> list[dict[str, Any]]:
        return default_entries(self.passkeys)

    def files(self, entries: list[dict[str, Any]], roster: dict[str, Any]) -> tuple[Path, Path]:
        self.builds += 1
        teams = self.tmp_path / f"teams-{self.builds}.yaml"
        teams.write_text(yaml.safe_dump(roster), encoding="utf-8")
        keys = self.tmp_path / f"keys-{self.builds}.yaml"
        keys.write_text(yaml.safe_dump({"keys": entries}, sort_keys=False), encoding="utf-8")
        return teams, keys

    def build(
        self,
        entries: list[dict[str, Any]] | None = None,
        *,
        github: GitHub | None = None,
        roster: dict[str, Any] | None = None,
    ) -> PasskeyHub:
        teams, keys = self.files(self.entries() if entries is None else entries, ROSTER if roster is None else roster)
        cfg = Config(
            public_url=ORIGIN,
            session_secret="s" * 32,
            github_client_id="client",
            github_client_secret="secret",
            github_authorize_url="https://github.test/login/oauth/authorize",
            github_token_url="https://github.test/login/oauth/access_token",
            github_user_url="https://github.test/user",
            database=self.database,
            teams_path=teams,
            host="127.0.0.1",
            port=8787,
            keys_path=keys,
            signing_key=HUB_SEED_HEX,
            hub_key_id=HUB_KEY_ID,
        )
        store = Store(cfg.database)
        self.stores.append(store)
        app = create_app(
            cfg,
            github=github if github is not None else FakeGitHub({"code-example-staff": "example-staff"}),
            store=store,
            clock=self.clock,
            record=idle_record,
        )
        env = {
            "AGENT_CORE_PUBLIC_URL": ORIGIN,
            "AGENT_CORE_SESSION_SECRET": cfg.session_secret,
            "AGENT_CORE_GITHUB_CLIENT_ID": cfg.github_client_id,
            "AGENT_CORE_GITHUB_CLIENT_SECRET": cfg.github_client_secret,
            "AGENT_CORE_GITHUB_AUTHORIZE_URL": cfg.github_authorize_url,
            "AGENT_CORE_GITHUB_TOKEN_URL": cfg.github_token_url,
            "AGENT_CORE_GITHUB_USER_URL": cfg.github_user_url,
            "AGENT_CORE_DATABASE": cfg.database,
            "AGENT_CORE_TEAMS": str(teams),
            "AGENT_CORE_KEYS": str(keys),
            "AGENT_CORE_SIGNING_KEY": HUB_SEED_HEX,
            "AGENT_CORE_HUB_KEY_ID": HUB_KEY_ID,
            "AGENT_CORE_HOST": "127.0.0.1",
            "AGENT_CORE_PORT": "8787",
            # Step `config` of the record start (RECORD-PROTOCOL.md section 11.4) checks only the
            # form; the operator commands never connect with it.
            "AGENT_CORE_RECORD_DATABASE": RECORD_DATABASE_URL,
        }
        return PasskeyHub(app=app, store=store, clock=self.clock, cfg=cfg, env=env)

    def changed(self, key_id: str, **changes: Any) -> list[dict[str, Any]]:
        """The default entries with fields of one entry changed."""
        return [dict(entry, **changes) if entry["id"] == key_id else entry for entry in self.entries()]

    def close(self) -> None:
        for store in self.stores:
            store.close()


@pytest.fixture
def hubs(tmp_path: Path) -> Iterator[HubFactory]:
    factory = HubFactory(tmp_path)
    try:
        yield factory
    finally:
        factory.close()


@pytest.fixture
def pkhub(hubs: HubFactory) -> PasskeyHub:
    """The hub with the default keys file."""
    return hubs.build()


def twin(seed: int) -> SoftAuthenticator:
    """An authenticator without a credential whose next `create` makes the same credential as
    `new_passkey(seed)` did."""
    return SoftAuthenticator(random=seeded_random(seed))


def gather(hub: PasskeyHub, *wires: Wire) -> list[Answer]:
    """The wires sent in parallel through the application."""

    async def run() -> list[Answer]:
        return list(await asyncio.gather(*(asgi_call(hub.app, wire) for wire in wires)))

    return asyncio.run(run())


def private_key_texts(private: ec.EllipticCurvePrivateKey) -> list[str]:
    """Every text form a stored private key could take: the scalar and PKCS#8, hex and base64."""
    scalar = private.private_numbers().private_value.to_bytes(32, "big")
    pkcs8 = private.private_bytes(
        serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return [scalar.hex(), b64(scalar), base64.b64encode(scalar).decode(), b64(pkcs8), base64.b64encode(pkcs8).decode()]


def with_header(wire: Wire, name: bytes, value: bytes) -> Wire:
    return replace(wire, headers=[*wire.headers, (name, value)])
