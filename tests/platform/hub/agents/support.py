"""Test support of the agent area (11-agent-access.md, ADR-056): agents written to the database and their clients.

`agent_client(hub, ...)` stores an agent row in the requested state for a (new) user and returns a client signing
with the agent's key under `agent.<32 hex>`, resolved by the real resolver of `agent_hub.agents.keys`.
`AgentKeys` creates pairings through the API (proof of possession) for the life-cycle tests.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from agent_hub.agents.keys import key_id_of
from agent_hub.auth.keys import Principal
from agent_hub.crypto import agent as agent_format
from agent_hub.crypto.encoding import b64u_encode
from agent_hub.crypto.keys import Identity
from tests.platform.hub.conftest import HubHarness, SignedClient, SigningKey, uuid7


@dataclass
class TestAgent:
    """An agent's keys and IDs."""

    __test__ = False

    identity: Identity
    agent_id: uuid.UUID
    user_id: uuid.UUID | None

    @property
    def key_id(self) -> str:
        return key_id_of(self.agent_id)

    def signing_key(self) -> SigningKey:
        principal = Principal(
            kind="agent",
            key_id=self.key_id,
            principal_id=self.user_id,
            actor_kind="user",
            attributes={"agent_id": self.agent_id},
        )
        return SigningKey(key_id=self.key_id, sign=self.identity.sig.sign, principal=principal)

    def client(self, hub: HubHarness) -> SignedClient:
        return hub.client(self.signing_key())

    def pairing_body(self) -> dict[str, Any]:
        return {
            "pairing_id": str(self.agent_id),
            "pub": self.identity.public(),
            "pop": agent_format.sign_pop(self.identity, str(self.agent_id)),
        }


def new_agent(hub: HubHarness) -> TestAgent:
    return TestAgent(identity=agent_format.new_agent(), agent_id=uuid7(hub.clock()), user_id=None)


def ensure_user(hub: HubHarness, user_id: uuid.UUID) -> None:
    with hub.pool.connection() as connection:
        connection.execute(
            "INSERT INTO hub.principal (id, kind, created_at) VALUES (%s, 'user', %s) ON CONFLICT DO NOTHING",
            (user_id, hub.clock()),
        )


def stored_agent(
    hub: HubHarness,
    user_id: uuid.UUID | None = None,
    *,
    state: str = "active",
    claim: bytes | None = None,
    expires_in: timedelta = timedelta(minutes=10),
) -> TestAgent:
    """An agent row in `state` (`open`, `confirmed`, `active`, `revoked`) of a (new) user."""
    agent = new_agent(hub)
    now = hub.clock()
    user = None if state == "open" else (user_id or uuid7(now))
    if user is not None:
        ensure_user(hub, user)
    agent.user_id = user
    claim_hash = agent_format.claim_hash(claim or secrets.token_bytes(32))
    sealed = secrets.token_bytes(140) if state in ("confirmed", "active") else None
    with hub.pool.connection() as connection:
        connection.execute(
            "INSERT INTO hub.agent (id, user_id, kem_pub, sig_pub, state, bundle_ct, label_ct, claim_hash, created_at,"
            " expires_at, confirmed_at, claimed_at, revoked_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                agent.agent_id,
                user,
                agent.identity.kem_public(),
                agent.identity.sig_public(),
                state,
                sealed,
                None if state == "open" else secrets.token_bytes(80),
                None if state == "open" else claim_hash,
                now - timedelta(seconds=1),
                now + expires_in,
                None if state == "open" else now,
                now if state == "active" else None,
                now if state == "revoked" else None,
            ),
        )
    return agent


def agent_client(hub: HubHarness, user_id: uuid.UUID | None = None, *, state: str = "active") -> SignedClient:
    """A client of an agent in `state` (default `active`) of a (new) user."""
    return stored_agent(hub, user_id, state=state).client(hub)


def b64(raw: bytes) -> str:
    return b64u_encode(raw)


# --- confirmation (the browser's part of 11-agent-access.md section 5.1) ----------------------------------------------


@dataclass
class Handover:
    """What the confirming browser computes: the handover secret `r`, the claim and the body of `POST /v1/me/agents`."""

    secret: bytes
    claim: bytes
    body: dict[str, Any]

    @property
    def claim_body(self) -> dict[str, str]:
        return {"claim": b64u_encode(self.claim)}


def handover(agent: TestAgent, user_id: uuid.UUID | str, identity: Identity, secret: bytes | None = None) -> Handover:
    """Seal the user's identity to the agent under `r`, seal the label to the user and hash the claim."""
    r = secret or secrets.token_bytes(32)
    agent_text, user_text = str(agent.agent_id), str(user_id)
    claim = agent_format.claim(r, agent_text, user_text)
    body = {
        "pairing_id": agent_text,
        "bundle_ct": agent_format.seal_identity(agent.identity.kem_public(), r, agent_text, user_text, identity),
        "label_ct": agent_format.seal_label(
            identity.kem_public(), agent_text, user_text, {"host": "Büro-Mac", "name": "Claude Code"}
        ),
        "claim_hash": b64u_encode(agent_format.claim_hash(claim)),
    }
    return Handover(secret=r, claim=claim, body=body)


def gesture_client(
    hub: HubHarness, user_id: uuid.UUID, pairing_id: uuid.UUID | None, *, age: float = 0.0
) -> SignedClient:
    """A browser session of `user_id` created `age` seconds ago by a login ceremony tagged with `pairing_id`."""
    key = hub.keys.session(user_id)
    with hub.pool.connection() as connection:
        connection.execute(
            "UPDATE hub.browser_session SET pairing_id = %s, created_at = %s WHERE key_id = %s",
            (pairing_id, hub.clock() - timedelta(seconds=age), key.key_id),
        )
    return hub.client(key)


def agent_row(hub: HubHarness, agent_id: uuid.UUID) -> dict[str, Any] | None:
    """The `hub.agent` row of an agent (None when there is none)."""
    with hub.pool.connection() as connection:
        cursor = connection.execute("SELECT * FROM hub.agent WHERE id = %s", (agent_id,))
        found = cursor.fetchone()
        names = [column.name for column in cursor.description or ()]
    return None if found is None else dict(zip(names, found, strict=True))


def rows(hub: HubHarness, query: str, *params: object) -> list[tuple[Any, ...]]:
    with hub.pool.connection() as connection:
        return list(connection.execute(query, params).fetchall())
