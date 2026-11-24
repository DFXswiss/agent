"""The real hub for the agent tool (11-agent-access.md section 10): `agent_hub` on the test PostgreSQL, served by
uvicorn on a loopback port, with the hub suites' harness (`tests/platform/hub/conftest.py`) for what a person's
browser and the operator do.

The tool talks to the served app over real HTTP (`agent_platform.cli.run`); the harness app (its test client) plays
the browser and the operator on the same database and keys. The hub's public URL is `http://localhost:<port>` (the
hub wants a DNS name; the tool accepts plain `http` only for a loopback hub) and the server listens on both loopback
addresses, so the name resolves to it either way. The hub runs on the system clock: the tool signs with the real time.

What a browser does is done here as the web client does it:

- `person`: a registered identity with a browser session, its private organization and the credential key `CK_1`
  (the `cred` epoch as the registration stores it and the user's self-wrap, `POST /v1/wraps`);
- `pair` / `remote_pair`: the confirmation `POST /v1/me/agents` from the pairing-tagged sign-in session with the
  identity sealed to the agent under `r`, then the loopback handover (`POST /agent-pairing` to the tool) or, for a
  remote pairing, `handover_ct` and the return code the page shows;
- `ring`: every scope key ever wrapped for a person (`GET /v1/wraps` of its tool), as that person could unwrap it.
"""

from __future__ import annotations

import contextlib
import secrets
import socket
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import uvicorn
from psycopg_pool import ConnectionPool

from agent_core.record.connection import parse_connection_string
from agent_hub import signals
from agent_hub.app import create_app
from agent_hub.cli import HubServer
from agent_hub.config import load_config
from agent_hub.crypto import agent as agent_format
from agent_hub.crypto import wrap as wrap_format
from agent_hub.crypto.encoding import b64u_decode, b64u_encode
from agent_hub.crypto.errors import CryptoError
from agent_hub.crypto.event import open_event
from agent_hub.db.pool import open_pool
from agent_hub.stream.publish import LIVE
from agent_hub.stream.service import service_of
from tests.platform.hub.agents.support import gesture_client
from tests.platform.hub.conftest import (
    FixedClock,
    HubDatabase,
    HubHarness,
    hub_environ,
    raw_public,
    root_private_key,
    start_hub,
)
from tests.platform.hub.device.support import Fleet
from tests.platform.hub.orgs.support import Actor, World
from tests.platform.tool.conftest import PairRun, Tool, fragment, make_tool, post_form, wait_for

ROOT_KEY = b64u_encode(raw_public(root_private_key()))
Ring = dict[tuple[str, str, int], bytes]


class SystemClock(FixedClock):
    """The real time in milliseconds (the tool signs its requests with the real time)."""

    def __call__(self) -> datetime:
        now = datetime.now(UTC)
        return now.replace(microsecond=now.microsecond // 1000 * 1000)

    def advance(self, seconds: float) -> datetime:
        raise AssertionError("the real hub runs on the system clock")


def loopback_sockets() -> list[socket.socket]:
    """Listening sockets of one port on 127.0.0.1 and, where the system has it, on ::1."""
    first = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    first.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    first.bind(("127.0.0.1", 0))
    port = int(first.getsockname()[1])
    found = [first]
    with contextlib.suppress(OSError):
        second = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        try:
            second.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            second.bind(("::1", port))
        except OSError:
            second.close()
            raise
        found.append(second)
    for item in found:
        item.listen(64)
        item.setblocking(False)
    return found


@dataclass
class RealHub:
    """The served hub, the harness on the same database and the actors of one test."""

    harness: HubHarness
    origin: str
    root: Path
    world: World = field(init=False)
    fleet: Fleet = field(init=False)

    def __post_init__(self) -> None:
        self.world = World(self.harness)
        self.fleet = Fleet(self.world)


@contextlib.contextmanager
def real_hub(database: HubDatabase, root: Path) -> Iterator[RealHub]:
    """The hub app served on both loopback addresses next to a harness app (browser, operator)."""
    sockets = loopback_sockets()
    port = int(sockets[0].getsockname()[1])
    origin = f"http://localhost:{port}"
    config = load_config(hub_environ(database, AGENT_HUB_PUBLIC_URL=origin))
    pool: ConnectionPool[psycopg.Connection[Any]] = open_pool(
        parse_connection_string(database.writer_url), min_size=1, max_size=8
    )
    clock = SystemClock()
    try:
        with contextlib.contextmanager(start_hub)(config, pool, clock) as harness:
            harness_service = service_of(harness.app)
            if harness_service is not None:
                LIVE.detach(harness_service)
            subscribers = dict(signals.subscribers())
            app = create_app(config, pool=pool, clock=clock, resolvers=harness.keys.resolvers(), run_jobs=False)
            signals.configure(subscribers)
            settings = uvicorn.Config(app, lifespan="on", log_level="warning", ws_ping_interval=None)
            server = HubServer(settings, app)
            thread = threading.Thread(target=lambda: server.run(sockets=sockets), daemon=True)
            thread.start()
            deadline = time.monotonic() + 20
            while not server.started and time.monotonic() < deadline and thread.is_alive():
                time.sleep(0.02)
            assert server.started, "the hub did not start"
            try:
                yield RealHub(harness, origin, root)
            finally:
                server.should_exit = True
                thread.join(20)
    finally:
        for item in sockets:
            item.close()
        pool.close()


# --- people and their computers ---


@dataclass
class Person:
    """A registered user and the tool on its own computer."""

    actor: Actor
    tool: Tool
    cred_key: bytes

    @property
    def id(self) -> str:
        return self.actor.id


def person(real: RealHub, name: str) -> Person:
    """A registered user with its private organization (registered through a platform invitation), `CK_1` (epoch
    and self-wrap) and a computer of its own (not paired yet)."""
    actor = real.world.user()
    real.world.private_org(actor)
    key = secrets.token_bytes(32)
    with real.harness.pool.connection() as connection, connection.transaction():
        connection.execute(
            "INSERT INTO hub.scope_epoch (scope, scope_id, epoch, created_at) VALUES ('cred', %s, 1, %s)",
            (actor.id, real.harness.clock()),
        )
    fields: dict[str, Any] = {"scope": "cred", "scope_id": actor.id, "epoch": 1, "recipient": actor.id}
    sealed = wrap_format.wrap_key(actor.identity.kem_public(), **fields, key=key)
    signature = wrap_format.sign_wrap(actor.identity.sig, **fields, wrap=sealed)
    wrap = {"scope": "cred", "scope_id": actor.id, "epoch": 1, "recipient_id": actor.id, "wrap": sealed}
    answer = actor.browser.post("/v1/wraps", json={"wraps": [{**wrap, "sig": signature}]})
    assert answer.json() == {"stored": 1, "ignored": 0}, answer.text
    return Person(actor, make_tool(real.root / name.lower()), key)


def _pairing_keys(real: RealHub, pairing_id: str) -> tuple[bytes, bytes, dict[str, Any]]:
    """The agent keys of an open pairing as the page reads them (`GET /v1/agent-pairings/{id}`)."""
    answer = real.harness.anon.get(f"/v1/agent-pairings/{pairing_id}").json()
    return b64u_decode(answer["pub"]["kem"], 32), b64u_decode(answer["pub"]["sig"], 32), answer


def _confirmation(pairing_id: str, kem: bytes, who: Person, secret: bytes) -> dict[str, Any]:
    """The body of `POST /v1/me/agents`: the identity sealed to the agent under `r`, the label and the claim hash."""
    claim = agent_format.claim(secret, pairing_id, who.id)
    return {
        "pairing_id": pairing_id,
        "bundle_ct": agent_format.seal_identity(kem, secret, pairing_id, who.id, who.actor.identity),
        "label_ct": agent_format.seal_label(
            who.actor.identity.kem_public(), pairing_id, who.id, {"name": "Claude Code", "host": "mac"}
        ),
        "claim_hash": b64u_encode(agent_format.claim_hash(claim)),
    }


def _confirm(real: RealHub, who: Person, pairing_id: str, body: dict[str, Any]) -> None:
    """The confirmation from the sign-in session the pairing tagged (the passkey gesture)."""
    session = gesture_client(real.harness, uuid.UUID(who.id), uuid.UUID(pairing_id))
    response = session.post("/v1/me/agents", json=body)
    assert response.json() == {"agent_id": pairing_id}, response.text


def pair(real: RealHub, who: Person) -> dict[str, Any]:
    """`pair` on the person's computer, confirmed in its browser with the loopback handover."""
    seen = len(who.tool.opened())
    run = PairRun(threading.Thread(target=lambda: None))

    def target() -> None:
        code, document = who.tool.run("pair", "--hub", real.origin, "--name", "Claude Code", "--host", "mac")
        run.result.update(document, rc=code)

    run.thread = threading.Thread(target=target, daemon=True)
    run.thread.start()
    link = fragment(wait_for(lambda: who.tool.opened()[seen:] and who.tool.opened()[seen], 30))
    pairing_id = link["p"]
    kem, sig, _answer = _pairing_keys(real, pairing_id)
    assert link["f"] == agent_format.fingerprint(kem, sig)  # the page shows the fingerprint of the hub's keys
    secret = secrets.token_bytes(32)
    _confirm(real, who, pairing_id, _confirmation(pairing_id, kem, who, secret))
    handover = {"p": pairing_id, "r": b64u_encode(secret), "k": ROOT_KEY, "u": who.id}
    status, headers = post_form(int(link["c"]), handover)
    assert (status, headers["Location"]) == (303, f"{real.origin}/settings?agent=connected")
    result = run.join()
    assert result["ok"] is True, result
    return result


def confirm_remote(real: RealHub, who: Person, pairing_id: str, code: str) -> str:
    """The page of a remote pairing: the agent code the user types must be the one the page derives from the hub's
    pairing, then the confirmation with `handover_ct`; the return code the page shows."""
    kem, sig, answer = _pairing_keys(real, pairing_id)
    nonce = b64u_decode(answer["nonce"], agent_format.NONCE_BYTES)
    derived = agent_format.agent_code(pairing_id, agent_format.fingerprint(kem, sig), nonce, real.origin)
    assert agent_format.format_code(derived) == code
    secret = secrets.token_bytes(32)
    body = _confirmation(pairing_id, kem, who, secret)
    body["handover_ct"] = agent_format.seal_handover(kem, pairing_id, who.id, secret, ROOT_KEY)
    _confirm(real, who, pairing_id, body)
    return agent_format.format_code(agent_format.return_code(secret, pairing_id, who.id))


def run_ok(tool: Tool, *argv: str, stdin: str = "") -> dict[str, Any]:
    code, document = tool.run(*argv, stdin=stdin)
    assert (code, document["ok"]) == (0, True), document
    return document


# --- keys a person holds ---


def ring(who: Person) -> Ring:
    """Every org and workgroup key wrapped for the person (its own `GET /v1/wraps`), unwrapped with its identity."""
    found: Ring = {}
    for item in run_ok(who.tool, "api", "GET", "/v1/wraps")["answer"]["wraps"]:
        if item["recipient_id"] != who.id or item["scope"] == "cred":
            continue
        slot = (str(item["scope"]), str(item["scope_id"]), int(item["epoch"]))
        found[slot] = wrap_format.unwrap_key(
            who.actor.identity.kem,
            scope=slot[0],
            scope_id=slot[1],
            epoch=slot[2],
            recipient=who.id,
            wrap=str(item["wrap"]),
        )
    return found


def opens(keys: Ring, event: dict[str, Any], author: Person) -> bool:
    """Whether any key of `keys` opens the event."""
    for key in keys.values():
        try:
            open_event(key, event, author.actor.identity.sig_public())
        except CryptoError:
            continue
        return True
    return False


def stored_event(reader: Person, scope_id: str, scope_seq: int) -> dict[str, Any]:
    """The stored event at `scope_seq` of a scope, as the hub serves it to `reader`."""
    answer = run_ok(reader.tool, "api", "GET", f"/v1/scopes/{scope_id}/events?after={scope_seq - 1}&limit=1")
    item = answer["answer"]["events"][0]
    assert item["scope_seq"] == scope_seq
    event: dict[str, Any] = item["event"]
    return event
