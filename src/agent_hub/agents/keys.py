"""The agent resolver of the signature gate (11-agent-access.md section 4, ADR-056).

An agent signs every request with its Ed25519 key under the key ID `agent.<32 hex>` (the agent ID without dashes).
`resolve_agent` finds the `hub.agent` row and answers

- an unknown agent, an `open` pairing, or a `confirmed` pairing past its claim deadline: None (`401 key_unknown`);
- a revoked agent or a disabled user: `401 key_revoked` (after the signature verified, response-signed);
- otherwise the principal of kind `agent` acting for its user (`principal_id` = user ID, audit actor `user`) with
  `attributes` `agent_id`, `state` (`confirmed` or `active`) and `own_identity` (the agent signs log entries, events,
  wraps and records under its own ID, ADR-082; `agents.identity.actor`) and the verifier of the agent's key.

The pipeline then admits the principal only on the routes of `agent_hub.agents.scope`. `last_used_at` is written by
`touch`, which the pipeline calls only after the signature verified (an unsigned or forged request writes nothing,
review L2), at most once per `LAST_USED_SECONDS` (an `UPDATE` with the threshold in its condition, so concurrent
requests write it once).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from types import MappingProxyType
from uuid import UUID

from agent_hub.auth.keys import AGENT_PREFIX, KeyContext, KeyResolver, Principal, ResolvedKey, ed25519_verifier

LAST_USED_SECONDS = 60


def key_id_of(agent_id: UUID | str) -> str:
    """The key ID of an agent: `agent.` and the UUID without dashes."""
    return AGENT_PREFIX + UUID(str(agent_id)).hex


def touch(context: KeyContext, agent_id: UUID) -> None:
    """Record the use of an active agent whose request signature verified, at most once per `LAST_USED_SECONDS`."""
    now = context.clock()
    with context.pool.connection() as connection:
        connection.execute(
            "UPDATE hub.agent SET last_used_at = %s WHERE id = %s AND state = 'active'"
            " AND (last_used_at IS NULL OR last_used_at <= %s)",
            (now, agent_id, now - timedelta(seconds=LAST_USED_SECONDS)),
        )


def resolve_agent(context: KeyContext, key_id: str) -> ResolvedKey | None:
    """The principal and verifier of an agent key (module docstring)."""
    agent_id = UUID(hex=key_id[len(AGENT_PREFIX) :])
    now = context.clock()
    with context.pool.connection() as connection:
        row = connection.execute(
            "SELECT a.state, a.sig_pub, a.user_id, a.expires_at, u.disabled_at IS NOT NULL, a.own_identity"
            " FROM hub.agent a LEFT JOIN hub.principal u ON u.id = a.user_id WHERE a.id = %s",
            (agent_id,),
        ).fetchone()
        if row is None:
            return None
        state, sig_pub, user_id, expires_at, disabled, own_identity = row
    if state == "open" or (state == "confirmed" and expires_at <= now):
        return None
    principal = Principal(
        kind="agent",
        key_id=key_id,
        principal_id=user_id,
        actor_kind="user",
        attributes=MappingProxyType({"agent_id": agent_id, "state": state, "own_identity": own_identity}),
    )
    refusal = "key_revoked" if state == "revoked" or disabled else None
    return ResolvedKey(principal=principal, verify=ed25519_verifier(bytes(sig_pub)), refusal=refusal)


RESOLVERS: Mapping[str, KeyResolver] = {AGENT_PREFIX: resolve_agent}
