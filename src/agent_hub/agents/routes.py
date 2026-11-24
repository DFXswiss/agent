"""Routes of the delegated agent access (11-agent-access.md section 4, ADR-056).

- `POST /v1/agent-pairings` (anon): the agent registers its public keys with a proof of possession.
- `GET /v1/agent-pairings/{id}` (anon): public keys and state while open or confirmed (the browser's fingerprint
  check), else `404 pairing_unknown`.
- `POST /v1/me/agents` (browser session; agent of an account without a passkey): the user confirms with the session
  of the pairing-tagged login (the passkey gesture). The new agent replaces the user's previous main orchestrator,
  which is revoked at once (concept idea 34, `store.confirm`). The agent of an account without a passkey (an account
  of an agent alone, KOR-62) confirms itself, with no gesture; the agent of an account with a passkey gets
  `403 forbidden`.
- `POST /v1/agent-accounts` (anon): an account of an agent alone through an invitation (`agents/accounts.py`).
- `GET /v1/me/agents`, `DELETE /v1/me/agents/{id}`, `DELETE /v1/me/agents` (user: browser session or agent): list,
  revoke one, revoke all. Account-changing actions of an agent notify the user (`agents/notify.py`).
- `POST /v1/agent/claim` (agent key, `confirmed`): the agent proves the claim from the loopback (or from the
  handover of a remote pairing) and gets its keys.
- `GET /v1/agent/handover` (agent key, `confirmed`): the handover of a confirmed remote pairing, sealed to the agent
  by the confirming browser (ADR-067); the confirmation of a remote pairing notifies every client of the user
  (`agent_paired_remote`).
- `GET /v1/agent/me` (agent key, `active`): the agent's own row with the sealed identity.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any
from uuid import UUID

from fastapi import Request
from pydantic import StringConstraints

from agent_hub.agents import accounts, store
from agent_hub.agents.keys import key_id_of
from agent_hub.agents.notify import remote_notice
from agent_hub.agents.revocation import revoke_agents
from agent_hub.auth.fields import B64u32, BundleCt, Strict, Uuid7, decode
from agent_hub.auth.keys import Principal
from agent_hub.db.pool import transaction
from agent_hub.errors import HubError
from agent_hub.http.context import Context, CurrentPrincipal, HubContext
from agent_hub.http.pipeline import source_address
from agent_hub.http.routing import HubRouter
from agent_hub.notifications.rules import publish_rows
from agent_hub.stream.publish import close_keys

router = HubRouter()

B64u64Sig = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{85}[AQgw]$")]


class PublicKeys(Strict):
    kem: B64u32
    sig: B64u32


class PairingCreate(Strict):
    pairing_id: Uuid7
    pub: PublicKeys
    pop: B64u64Sig
    remote: bool = False


class AgentConfirm(Strict):
    pairing_id: Uuid7
    bundle_ct: BundleCt
    label_ct: BundleCt
    claim_hash: B64u32
    handover_ct: BundleCt | None = None


class AgentClaim(Strict):
    claim: B64u32


def _uuid(text: str) -> UUID:
    value = UUID(text) if len(text) == 36 else None
    if value is None or str(value) != text or value.version != 7:
        raise HubError("not_found")
    return value


def _session_user(principal: Principal) -> UUID:
    """The user of a browser session or of an active agent (both may manage the user's agents, ADR-056 §4)."""
    if principal.kind not in ("session", "agent") or principal.principal_id is None or principal.key_id is None:
        raise HubError("forbidden")
    return principal.principal_id


def _agent_id(principal: Principal) -> UUID:
    agent = principal.attributes.get("agent_id") if principal.kind == "agent" else None
    if not isinstance(agent, UUID):
        raise HubError("forbidden")
    return agent


def _create(context: HubContext, body: PairingCreate, source: str) -> dict[str, Any]:
    now = context.clock()
    with transaction(context.pool) as connection:
        pairing = store.create_pairing(
            connection,
            UUID(body.pairing_id),
            kem=body.pub.kem,
            sig=body.pub.sig,
            pop=body.pop,
            source=source,
            now=now,
            remote=body.remote,
        )
    answer = store.pairing_json(pairing, now)
    return {"pairing_id": answer["pairing_id"], "expires_at": answer["expires_at"], **store.remote_json(pairing)}


@router.post("/v1/agent-pairings", auth="anon", right="anon")
async def create_pairing(body: PairingCreate, request: Request, context: Context) -> dict[str, Any]:
    return await asyncio.to_thread(_create, context, body, source_address(request.scope))


def _get(context: HubContext, pairing_id: str) -> dict[str, Any]:
    try:
        key = _uuid(pairing_id)
    except HubError:
        raise HubError("pairing_unknown") from None
    with context.pool.connection() as connection:
        now = context.clock()
        pairing = store.visible_pairing(connection, key, now)
    return store.pairing_json(pairing, now)


@router.get("/v1/agent-pairings/{pairing_id}", auth="anon", right="anon")
async def get_pairing(pairing_id: str, context: Context) -> dict[str, Any]:
    return await asyncio.to_thread(_get, context, pairing_id)


def _sealed(text: str, pointer: str) -> bytes:
    """A sealed value of at least `store.MIN_SEALED_BYTES` (an HPKE seal is never shorter; the general `bundle_ct`
    form of the contract allows 28 bytes, the table refuses fewer than 48)."""
    raw = decode(text)
    if len(raw) < store.MIN_SEALED_BYTES:
        raise HubError("request_invalid", detail={"pointers": [pointer]})
    return raw


def _confirm(context: HubContext, principal: Principal, body: AgentConfirm) -> dict[str, Any]:
    if principal.kind not in ("session", "agent") or principal.principal_id is None or principal.key_id is None:
        raise HubError("forbidden")
    user = principal.principal_id
    confirmation = store.Confirmation(
        pairing_id=UUID(body.pairing_id),
        bundle_ct=_sealed(body.bundle_ct, "/bundle_ct"),
        label_ct=_sealed(body.label_ct, "/label_ct"),
        claim_hash=decode(body.claim_hash),
        handover_ct=None if body.handover_ct is None else _sealed(body.handover_ct, "/handover_ct"),
    )
    now = context.clock()
    with transaction(context.pool) as connection:
        confirmed = store.confirm(
            connection, user, principal.key_id, confirmation, now, by_agent=principal.kind == "agent"
        )
        changed_remote = confirmed.changed and confirmed.remote
        notices = remote_notice(connection, user, now, confirmation.pairing_id) if changed_remote else []
    publish_rows(notices)
    close_keys(user, [key_id_of(agent) for agent in confirmed.replaced])
    return {"agent_id": str(confirmation.pairing_id)}


@router.post("/v1/me/agents", auth="browser", right="user")
async def confirm_agent(body: AgentConfirm, principal: CurrentPrincipal, context: Context) -> dict[str, Any]:
    return await asyncio.to_thread(_confirm, context, principal, body)


def _list(context: HubContext, principal: Principal) -> dict[str, Any]:
    user = _session_user(principal)
    with context.pool.connection() as connection:
        return {"agents": store.list_agents(connection, user, context.clock())}


@router.get("/v1/me/agents", auth="browser", right="user", read_resource="me.agents")
async def list_agents(principal: CurrentPrincipal, context: Context) -> dict[str, Any]:
    return await asyncio.to_thread(_list, context, principal)


def _revoke(context: HubContext, principal: Principal, agent_id: str) -> dict[str, Any]:
    revoke_agents(context, _session_user(principal), _uuid(agent_id))
    return {}


def _revoke_all(context: HubContext, principal: Principal) -> dict[str, Any]:
    return {"revoked": len(revoke_agents(context, _session_user(principal), None))}


@router.delete("/v1/me/agents", auth="browser", right="user")
async def revoke_all_agents(principal: CurrentPrincipal, context: Context) -> dict[str, Any]:
    return await asyncio.to_thread(_revoke_all, context, principal)


@router.delete("/v1/me/agents/{agent_id}", auth="browser", right="user")
async def revoke_agent(agent_id: str, principal: CurrentPrincipal, context: Context) -> dict[str, Any]:
    return await asyncio.to_thread(_revoke, context, principal, agent_id)


def _claim(context: HubContext, principal: Principal, body: AgentClaim) -> dict[str, Any]:
    agent_id = _agent_id(principal)
    with transaction(context.pool) as connection:
        answer = store.claim(connection, agent_id, decode(body.claim), context.clock())
    if answer is None:
        raise HubError("claim_invalid")
    return answer


@router.post("/v1/agent/claim", auth="agent", right="agent:confirmed")
async def claim(body: AgentClaim, principal: CurrentPrincipal, context: Context) -> dict[str, Any]:
    return await asyncio.to_thread(_claim, context, principal, body)


def _me(context: HubContext, principal: Principal) -> dict[str, Any]:
    agent_id = _agent_id(principal)
    with context.pool.connection() as connection:
        return store.me(connection, agent_id)


@router.get("/v1/agent/me", auth="agent", right="agent", read_resource="agent.me")
async def agent_me(principal: CurrentPrincipal, context: Context) -> dict[str, Any]:
    return await asyncio.to_thread(_me, context, principal)


def _handover(context: HubContext, principal: Principal) -> dict[str, Any]:
    agent_id = _agent_id(principal)
    with context.pool.connection() as connection:
        return store.handover(connection, agent_id, context.clock())


@router.get("/v1/agent/handover", auth="agent", right="agent:confirmed", read_resource="agent.handover")
async def agent_handover(principal: CurrentPrincipal, context: Context) -> dict[str, Any]:
    return await asyncio.to_thread(_handover, context, principal)


@router.post("/v1/agent-accounts", auth="anon", right="anon")
async def create_account(body: accounts.AgentAccountCreate, context: Context) -> dict[str, Any]:
    return await asyncio.to_thread(accounts.create, context, body)
