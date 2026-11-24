"""An account of an agent alone (rule E-27, KOR-62; 11-agent-access.md section 5.5).

`POST /v1/agent-accounts` (anonymous): an AI agent creates a user account through an invitation without any person,
passkey or browser. The agent tool draws the user's identity, the recovery slot, `CK_1` with its self-wrap, its own
agent key pair and a handover secret `r` of its own, under which it seals the identity to itself (exactly the state
a paired agent holds). The body carries what `register/finish` carries for a first passkey (identity, recovery slot,
`cred_wrap`) without the attestation, the agent (ID, public keys, sealed identity and label, claim hash) and the
proof of possession, which names its method (`proof.method`, today only `agent-signature-v1`): the signatures of the
invitation key `sk_inv` (`inv_sig`), of the new identity (`id_sig`) and of the new agent key (`agent_sig`) over
`crypto.agent.account_text`. A wrong signature is `401 signature_invalid`; the invitation must be open exactly as for
`register/begin` (`404 invite_unknown`, one code for everything) and is bound to the user as by a passkey
registration, so one invitation yields one account either way and the orphan job releases an account that never
created its private organization or accepted its org invitation (`auth/jobs.py`).

In one transaction the hub stores the rows of `auth.register.store_account` (the same as for a person, without a
passkey) and the agent as the user's `active` main orchestrator (`store.install`). Audit `agent.register` (actor the
new user, target the agent, detail `agent`, `invite_id`, `method`) and `invite.register`. The answer is
`{user_id, agent_id}`; an identical repeat after a lost answer returns it again and changes nothing; anything else
for a bound invitation is `invite_unknown`.

The same agent may also become the main orchestrator of an existing account without a passkey through the recovery
code (`auth/recovery.py`, `recover`).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from agent_core.signing import ed25519_public_key_valid
from agent_hub.agents import store
from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.audit.recorder import unchanged
from agent_hub.auth.fields import B64u16Id, B64u32, B64u64, BundleCt, Strict, Uuid7, decode
from agent_hub.auth.register import (
    CredWrap,
    IdentityKeys,
    RecoverySlot,
    check_identity,
    locked_invitation_key,
    store_account,
)
from agent_hub.crypto import agent as agent_format
from agent_hub.crypto.errors import CryptoError
from agent_hub.db.pool import transaction
from agent_hub.errors import HubError
from agent_hub.http.context import HubContext

Method = Literal["agent-signature-v1"]


class AgentPublic(Strict):
    kem: B64u32
    sig: B64u32


class NewAgent(Strict):
    """The agent that becomes the main orchestrator, with the identity and label it sealed itself."""

    agent_id: Uuid7
    pub: AgentPublic
    bundle_ct: BundleCt
    label_ct: BundleCt
    claim_hash: B64u32


class AccountProof(Strict):
    method: Method
    inv_sig: B64u64
    id_sig: B64u64
    agent_sig: B64u64


class AgentAccountCreate(Strict):
    invite_id: B64u16Id
    user_id: Uuid7
    identity: IdentityKeys
    recovery: RecoverySlot
    cred_wrap: CredWrap
    agent: NewAgent
    proof: AccountProof


def sealed(text: str, pointer: str) -> bytes:
    """A sealed value of at least `store.MIN_SEALED_BYTES` (`request_invalid` with the pointer otherwise)."""
    raw = decode(text)
    if len(raw) < store.MIN_SEALED_BYTES:
        raise HubError("request_invalid", detail={"pointers": [pointer]})
    return raw


def installed(agent: NewAgent, pointer: str) -> store.Installed:
    """The decoded agent of a request (`params_invalid` for a signature key that is no valid Ed25519 point)."""
    sig_pub = decode(agent.pub.sig)
    if not ed25519_public_key_valid(sig_pub):
        raise HubError("params_invalid", detail={"pointers": [f"{pointer}/pub/sig"]})
    return store.Installed(
        agent_id=uuid.UUID(agent.agent_id),
        kem_pub=decode(agent.pub.kem),
        sig_pub=sig_pub,
        bundle_ct=sealed(agent.bundle_ct, f"{pointer}/bundle_ct"),
        label_ct=sealed(agent.label_ct, f"{pointer}/label_ct"),
        claim_hash=decode(agent.claim_hash),
    )


def _repeat(connection: Any, body: AgentAccountCreate, agent: store.Installed) -> bool:
    """Whether the invitation is already bound to exactly this account and agent (an identical repeat)."""
    row = connection.execute(
        "SELECT 1 FROM hub.invite i JOIN hub.user_identity u ON u.user_id = i.registered_user"
        " JOIN hub.agent a ON a.user_id = u.user_id WHERE i.id = %s AND i.registered_user = %s AND u.kem_pub = %s"
        " AND u.sig_pub = %s AND a.id = %s AND a.sig_pub = %s AND a.kem_pub = %s AND a.state = 'active'",
        (
            body.invite_id,
            uuid.UUID(body.user_id),
            decode(body.identity.kem_pub),
            decode(body.identity.sig_pub),
            agent.agent_id,
            agent.sig_pub,
            agent.kem_pub,
        ),
    ).fetchone()
    return row is not None


def _verify(pk_inv: bytes, keys: tuple[bytes, bytes], agent: store.Installed, body: AgentAccountCreate) -> None:
    text = agent_format.account_text(
        agent_id=str(agent.agent_id),
        agent_kem=agent.kem_pub,
        agent_sig=agent.sig_pub,
        invite_id=body.invite_id,
        method=body.proof.method,
        user_id=body.user_id,
        user_kem=keys[0],
        user_sig=keys[1],
    )
    try:
        agent_format.verify_text(pk_inv, body.proof.inv_sig, text)
        agent_format.verify_text(keys[1], body.proof.id_sig, text)
        agent_format.verify_text(agent.sig_pub, body.proof.agent_sig, text)
    except CryptoError:
        raise HubError("signature_invalid") from None


def audit_register(
    connection: Any, user: uuid.UUID, agent_id: uuid.UUID, now: datetime, detail: dict[str, Any]
) -> None:
    """The audit row `agent.register` of a new main orchestrator that no pairing confirmed."""
    append_audit(
        connection,
        AuditRow(
            at=now,
            actor_kind="user",
            actor_id=user,
            action="agent.register",
            result="ok",
            target_kind="agent",
            target_id=str(agent_id),
            agent_id=agent_id,
            detail={"agent": str(agent_id), **detail},
        ),
    )


def create(context: HubContext, body: AgentAccountCreate) -> dict[str, Any]:
    """`POST /v1/agent-accounts` (module docstring)."""
    user_id = uuid.UUID(body.user_id)
    agent = installed(body.agent, "/agent")
    keys = check_identity(body.identity, body.cred_wrap, user_id)
    now = context.clock()
    answer = {"user_id": body.user_id, "agent_id": body.agent.agent_id}
    with transaction(context.pool) as connection:
        if _repeat(connection, body, agent):
            unchanged()
            return answer
        pk_inv = locked_invitation_key(connection, body.invite_id, now)
        _verify(pk_inv, keys, agent, body)
        store_account(
            connection,
            user_id,
            keys=keys,
            recovery=body.recovery,
            wrap=body.cred_wrap,
            invite_id=body.invite_id,
            now=now,
        )
        store.lock_user(connection, user_id)
        store.install(connection, user_id, agent, now, replace=False)
        audit_register(
            connection, user_id, agent.agent_id, now, {"invite_id": body.invite_id, "method": body.proof.method}
        )
        append_audit(
            connection,
            AuditRow(
                at=now,
                actor_kind="user",
                actor_id=user_id,
                action="invite.register",
                result="ok",
                target_kind="invite",
                target_id=body.invite_id,
                agent_id=agent.agent_id,
                detail={"invite_id": body.invite_id, "kind": "user"},
            ),
        )
    return answer
