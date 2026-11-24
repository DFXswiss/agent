"""Replacing the recovery code (06-web.md section 3.2 "generate a new recovery code", 04-crypto.md section 5.5; KOR-01,
AUD-01 B-06).

The browser draws a new recovery code, seals the unlocked identity for the recovery slot with its `KEK_rec`, shows the
code and lets the user confirm it (re-typing the last group, as at onboarding); only then it replaces the slot. The
hub sees `new_recovery_id` and the sealed slot, never the code.

A session alone may not replace the slot: a stolen session key could otherwise plant a recovery code of the thief and
take the identity over. The replacement needs one of:

1. **A fresh passkey assertion** (any time): `POST /v1/me/recovery/begin` (signed) opens a `recovery_rotate` ceremony
   of the caller with a 32-byte challenge and answers `{ceremony_id, challenge, options}` (the request options of a
   login: discoverable credentials, UV required, PRF). The browser runs `get()` with that challenge and sends the
   assertion with the slot: `PUT /v1/me/bundles/recovery {bundle_ct, new_recovery_id, assertion:{ceremony_id,
   credential_id, authenticator_data, client_data_json, signature}}`. The ceremony must be the caller's, open and
   unexpired (it is consumed by this one attempt; else `400 ceremony_invalid`); the credential an active passkey of the
   caller (`credential_invalid`); the assertion is checked like a sign-in (`agent_core.passkey.webauthn`: type
   `webauthn.get`, challenge, origin, RP ID, UP and UV, signature, counter), each refusal with its code.
2. **A finished recovery** (as before, 04-crypto.md section 5.5): a `recovery` registration ceremony of the caller
   (never the proof ceremony with `hub_value`) that added a passkey after the slot was last written. Once per
   recovery.

Both replace `user_identity.recovery_id` (`409 id_conflict` when another user has it) and the slot, and are audited
as `auth.recovery {step: "rotate"}`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

import psycopg
import psycopg.errors

from agent_core.passkey.encoding import verify_passkey_signature
from agent_core.passkey.errors import PasskeyRefusal
from agent_core.passkey.webauthn import TYPE_GET, check_client_data, check_counter, check_sign_in_authenticator_data
from agent_core.signing import b64url_encode
from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.auth.ceremonies import (
    Ceremony,
    create_ceremony,
    credential_reference,
    relying_party,
    request_options,
    take_ceremony,
)
from agent_hub.auth.fields import CredentialId, Strict, Uuid7, b64u_text, decode
from agent_hub.db.pool import transaction
from agent_hub.errors import HubError
from agent_hub.http.context import HubContext

ROTATE = "recovery_rotate"


class RecoveryAssertion(Strict):
    """A passkey assertion over the challenge of a `recovery_rotate` ceremony (field names as in the login
    certificate)."""

    ceremony_id: Uuid7
    credential_id: CredentialId
    authenticator_data: Annotated[str, b64u_text(8192)]
    client_data_json: Annotated[str, b64u_text(8192)]
    signature: Annotated[str, b64u_text(256)]


def begin(context: HubContext, user_id: uuid.UUID) -> dict[str, Any]:
    """`POST /v1/me/recovery/begin`: a `recovery_rotate` ceremony of the caller."""
    now = context.clock()
    ceremony = create_ceremony(context.pool, kind=ROTATE, now=now, user_id=user_id)
    with transaction(context.pool) as connection:
        append_audit(
            connection,
            AuditRow(
                at=now,
                actor_kind="user",
                actor_id=user_id,
                action="auth.recovery",
                result="ok",
                target_kind="ceremony",
                target_id=str(ceremony.id),
                detail={"step": "rotate_begin"},
            ),
        )
    return {
        "ceremony_id": str(ceremony.id),
        "challenge": b64url_encode(ceremony.challenge),
        "options": request_options(context.config),
    }


def take(context: HubContext, user_id: uuid.UUID, assertion: RecoveryAssertion, now: datetime) -> Ceremony:
    """Consume the caller's open `recovery_rotate` ceremony (one attempt per challenge; `ceremony_invalid` for any
    other ceremony, which stays untouched)."""

    def own(ceremony: Ceremony) -> None:
        if ceremony.user_id != user_id:
            raise HubError("ceremony_invalid")

    return take_ceremony(context.pool, assertion.ceremony_id, (ROTATE,), now, own)


def verify_assertion(
    connection: psycopg.Connection[Any],
    context: HubContext,
    *,
    user_id: uuid.UUID,
    assertion: RecoveryAssertion,
    ceremony: Ceremony,
    now: datetime,
) -> str:
    """Check the assertion like a sign-in against an active passkey of the caller (locked) and keep its counter; the
    audit reference of the passkey."""
    credential_id = decode(assertion.credential_id)
    passkey = connection.execute(
        "SELECT public_key, sign_count FROM hub.passkey WHERE credential_id = %s AND user_id = %s"
        " AND removed_at IS NULL FOR UPDATE",
        (credential_id, user_id),
    ).fetchone()
    if passkey is None:
        raise HubError("credential_invalid")
    party = relying_party(context.config)
    client_data = decode(assertion.client_data_json)
    authenticator_data = decode(assertion.authenticator_data)
    try:
        check_client_data(client_data, expected_type=TYPE_GET, challenge=ceremony.challenge, origin=party.origin)
        head = check_sign_in_authenticator_data(authenticator_data, party.rp_id)
        if not verify_passkey_signature(
            bytes(passkey[0]), decode(assertion.signature), authenticator_data, client_data
        ):
            raise PasskeyRefusal("signature_invalid")
        sign_count = check_counter(int(passkey[1]), head.sign_count)
    except PasskeyRefusal as refusal:
        raise HubError(refusal.error) from None
    connection.execute(
        "UPDATE hub.passkey SET sign_count = %s, last_used_at = %s WHERE credential_id = %s",
        (sign_count, now, credential_id),
    )
    return credential_reference(credential_id)


def allowed_after_recovery(connection: psycopg.Connection[Any], user_id: uuid.UUID) -> bool:
    """A finished recovery since the slot was last written: a used `recovery` registration ceremony of the user (not
    the proof ceremony, which carries `hub_value`) whose finish added a passkey (`created_at` = the ceremony's
    `used_at`, both the hub clock of that request). The slot row is locked."""
    row = connection.execute(
        "SELECT 1 FROM hub.identity_bundle b WHERE b.user_id = %(user)s AND b.slot = 'recovery'"
        " AND EXISTS (SELECT 1 FROM hub.ceremony c JOIN hub.passkey p ON p.user_id = c.user_id"
        " AND p.created_at = c.used_at WHERE c.user_id = %(user)s AND c.kind = 'recovery' AND c.hub_value IS NULL"
        " AND c.used_at IS NOT NULL AND c.used_at > b.updated_at) FOR UPDATE OF b",
        {"user": user_id},
    ).fetchone()
    return row is not None


def rotate(
    connection: psycopg.Connection[Any],
    user_id: uuid.UUID,
    new_recovery_id: str,
    proven_by: str | None,
    now: datetime,
    *,
    agent_id: uuid.UUID | None = None,
) -> None:
    """Replace the recovery ID (the caller writes the slot afterwards in the same transaction) and audit it;
    `proven_by` is the passkey of a verified assertion, None for the replacement after a recovery or by an agent
    (`agent_id`: the agent's signed request is its proof, as the assertion is the browser's; ADR-056 §4)."""
    if proven_by is None and agent_id is None and not allowed_after_recovery(connection, user_id):
        raise HubError("forbidden")
    try:
        with connection.transaction():
            connection.execute(
                "UPDATE hub.user_identity SET recovery_id = %s WHERE user_id = %s", (new_recovery_id, user_id)
            )
    except psycopg.errors.UniqueViolation:
        raise HubError("id_conflict") from None
    append_audit(
        connection,
        AuditRow(
            at=now,
            actor_kind="user",
            actor_id=user_id,
            action="auth.recovery",
            result="ok",
            target_kind=("agent" if agent_id is not None else "user") if proven_by is None else "passkey",
            target_id=(str(agent_id or user_id)) if proven_by is None else proven_by,
            detail={"step": "rotate"},
        ),
    )
