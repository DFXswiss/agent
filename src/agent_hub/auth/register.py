"""Passkey registration (03-api.md section 4.1, 04-crypto.md section 5.1, ADR-026).

`register/begin {invite_id}` opens a ceremony bound to an open user invitation (not used, not revoked, not expired,
no `registered_user`; anything else is `404 invite_unknown`, one code for all) and allocates the user ID.
`register/finish` verifies the attestation with `agent_core.passkey.webauthn.verify_registration` (which runs
`check_client_data`: type, challenge, origin) and, for the first passkey, the invitation proof `inv_sig` (ADR-043):
the signature of `sk_inv` over `P || "invite-register\\0" || user_id || "\\0" || challenge` must verify with the
invitation's `pk_inv` (platform invitations: `invite.pk_inv`; org invitations: the `pk_inv` of their membership-log
`invite` entry), else `401 signature_invalid`. An `invite_id` is readable by others (platform log, membership log,
inviter listing); only the holder of the code can register through it. An invitation without a known `pk_inv` is
`404 invite_unknown`. Then it stores in one transaction: the principal,
`user_identity`, default `user_settings`, the recovery slot of `identity_bundle`, the passkey, `scope_epoch(cred,
user, 1)` and the self-wrap of `CK_1` (its signature must verify with the new identity, rule W1), and binds the
invitation (`registered_user`, `registered_at`). Audit: `auth.register` and `invite.register`.

A ceremony of kind `add_passkey` or `recovery` (created by HUB-11) is finished by the same endpoint without
identity, recovery and wrap: the passkey is added to the ceremony's existing user (`auth.passkey_add`, or
`auth.recovery {step: finish}`); the proof ceremony of `recovery/begin` (a `recovery` ceremony with `hub_value`) is
`400 ceremony_invalid` here and stays open (KOR-01). The used recovery ceremony is what allows the one replacement of
the recovery slot afterwards (`me/routes.py`, 04-crypto.md section 5.5). The begin of
those ceremonies (`register/begin {add:true}`, `recovery/finish`) belongs to HUB-11 (`auth/passkeys_manage.py`,
`auth/recovery.py`); `{add:true}` is handed to `passkeys_manage.begin_add` when that module exists.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Annotated, Any, Literal, cast

import psycopg
from pydantic import Field

from agent_core.passkey.errors import PasskeyRefusal
from agent_core.passkey.webauthn import Registration, verify_registration
from agent_core.signing import b64url_encode, ed25519_public_key_valid
from agent_hub.agents.notify import recovery_notice
from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.auth.ceremonies import (
    Ceremony,
    create_ceremony,
    creation_options,
    credential_reference,
    new_id,
    relying_party,
    take_ceremony,
)
from agent_hub.auth.fields import (
    B64u16Id,
    B64u32,
    B64u64,
    B64u80,
    BundleCt,
    CredentialId,
    Strict,
    Transport,
    Uuid7,
    b64u_text,
    decode,
)
from agent_hub.auth.keys import Principal
from agent_hub.crypto.encoding import b64u_decode
from agent_hub.crypto.errors import CryptoError
from agent_hub.crypto.invite import verify_register
from agent_hub.crypto.wrap import verify_wrap
from agent_hub.db.pool import transaction
from agent_hub.errors import HubError
from agent_hub.http.context import HubContext
from agent_hub.notifications.rules import publish_rows

ADD_MODULE = "agent_hub.auth.passkeys_manage"
# An invitation whose `pk_inv` the hub knows (platform: the column; org: the membership-log `invite` entry, ADR-043).
HAS_KEY = "(pk_inv IS NOT NULL OR scope = 'org')"
# The condition of an invitation through which a user can still register.
OPEN_INVITE = (
    "kind = 'user' AND used_at IS NULL AND revoked_at IS NULL AND registered_user IS NULL AND expires_at > %(now)s"
)


class RegisterBegin(Strict):
    """`{invite_id}` (registration through an invitation) or `{add: true}` (further passkey, HUB-11)."""

    invite_id: B64u16Id | None = None
    add: Literal[True] | None = None


class AttestationCredential(Strict):
    id: CredentialId
    clientDataJSON: Annotated[str, b64u_text(8192)]
    attestationObject: Annotated[str, b64u_text(65536)]
    transports: Annotated[list[Transport], Field(max_length=6)]


class IdentityKeys(Strict):
    kem_pub: B64u32
    sig_pub: B64u32


class RecoverySlot(Strict):
    recovery_id: B64u16Id
    bundle_ct: BundleCt


class CredWrap(Strict):
    epoch: Literal[1]
    wrap: B64u80
    sig: B64u64


class RegisterFinish(Strict):
    """First passkey: identity, recovery, `cred_wrap` and `inv_sig` together; a further passkey: none of them."""

    ceremony_id: Uuid7
    credential: AttestationCredential
    identity: IdentityKeys | None = None
    recovery: RecoverySlot | None = None
    cred_wrap: CredWrap | None = None
    inv_sig: B64u64 | None = None


def add_begin_handler() -> Callable[[HubContext, Principal], dict[str, Any]] | None:
    """`begin_add(context, principal)` of HUB-11 when `auth/passkeys_manage.py` exists."""
    if importlib.util.find_spec(ADD_MODULE) is None:
        return None
    handler = getattr(importlib.import_module(ADD_MODULE), "begin_add", None)
    return cast("Callable[[HubContext, Principal], dict[str, Any]] | None", handler)


def begin(context: HubContext, body: RegisterBegin, principal: Principal) -> dict[str, Any]:
    """`POST /v1/passkeys/register/begin`."""
    if (body.invite_id is None) == (body.add is None):
        raise HubError("request_invalid")
    if body.add:
        # No self-registration (R-1.1): without an invitation only a signed-in user may add a passkey.
        if principal.kind != "session":
            raise HubError("unauthorized")
        handler = add_begin_handler()
        if handler is None:
            raise HubError("forbidden")
        return handler(context, principal)
    return begin_invited(context, cast(str, body.invite_id))


def begin_invited(context: HubContext, invite_id: str) -> dict[str, Any]:
    """A registration ceremony bound to an open user invitation."""
    now = context.clock()
    with transaction(context.pool) as connection:
        row = connection.execute(
            f"SELECT 1 FROM hub.invite WHERE id = %(id)s AND {OPEN_INVITE} AND {HAS_KEY}",
            {"id": invite_id, "now": now},
        ).fetchone()
    if row is None:
        raise HubError("invite_unknown")
    user_id = new_id(now)
    ceremony = create_ceremony(context.pool, kind="register", now=now, user_id=user_id, invite_id=invite_id)
    return {
        "ceremony_id": str(ceremony.id),
        "user_id": str(user_id),
        "options": creation_options(context.config, user_id, ceremony.challenge),
    }


def _shape_check(body: RegisterFinish, principal: Principal) -> Callable[[Ceremony], None]:
    first = body.identity is not None

    def check(ceremony: Ceremony) -> None:
        # A `recovery` ceremony with `hub_value` is the proof ceremony of `recovery/begin`, never a registration
        # (KOR-01, AUD-01 A-14); refused before it is consumed, so its proof can still be presented.
        if ceremony.hub_value is not None:
            raise HubError("ceremony_invalid")
        if first != (ceremony.kind == "register"):
            raise HubError("request_invalid")
        if ceremony.kind == "add_passkey" and (
            principal.kind != "session" or principal.principal_id != ceremony.user_id
        ):
            raise HubError("forbidden")

    return check


def finish(context: HubContext, body: RegisterFinish, principal: Principal) -> dict[str, Any]:
    """`POST /v1/passkeys/register/finish`."""
    parts = (body.identity, body.recovery, body.cred_wrap, body.inv_sig)
    if any(part is None for part in parts) and any(part is not None for part in parts):
        raise HubError("request_invalid")
    now = context.clock()
    ceremony = take_ceremony(
        context.pool, body.ceremony_id, ("register", "add_passkey", "recovery"), now, _shape_check(body, principal)
    )
    credential = body.credential
    try:
        registration = verify_registration(
            credential_id=decode(credential.id),
            client_data_json=decode(credential.clientDataJSON),
            attestation_object=decode(credential.attestationObject),
            challenge=ceremony.challenge,
            relying_party=relying_party(context.config),
        )
    except PasskeyRefusal as refusal:
        raise HubError(refusal.error) from None
    user_id = cast(uuid.UUID, ceremony.user_id)
    if ceremony.kind == "register":
        _store_first(context, body, ceremony, registration, now)
    else:
        _store_further(context, ceremony, registration, credential.transports, now)
    return {"user_id": str(user_id), "credential_id": b64url_encode(registration.credential_id)}


def _insert_passkey(
    connection: psycopg.Connection[Any],
    user_id: uuid.UUID,
    registration: Registration,
    transports: list[str],
    now: datetime,
) -> None:
    row = connection.execute(
        "INSERT INTO hub.passkey (credential_id, user_id, public_key, sign_count, transports, created_at,"
        " last_used_at) VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (credential_id) DO NOTHING"
        " RETURNING credential_id",
        (registration.credential_id, user_id, registration.public_key, registration.sign_count, transports, now, now),
    ).fetchone()
    if row is None:
        raise HubError("credential_invalid")


def _store_first(
    context: HubContext, body: RegisterFinish, ceremony: Ceremony, registration: Registration, now: datetime
) -> None:
    identity = cast(IdentityKeys, body.identity)
    recovery = cast(RecoverySlot, body.recovery)
    wrap = cast(CredWrap, body.cred_wrap)
    user_id = cast(uuid.UUID, ceremony.user_id)
    invite_id = cast(str, ceremony.invite_id)
    kem_pub, sig_pub = decode(identity.kem_pub), decode(identity.sig_pub)
    if not ed25519_public_key_valid(sig_pub):
        raise HubError("params_invalid", detail={"pointers": ["/identity/sig_pub"]})
    try:
        verify_wrap(
            sig_pub,
            scope="cred",
            scope_id=str(user_id),
            epoch=1,
            recipient=str(user_id),
            wrap=wrap.wrap,
            signature=wrap.sig,
        )
    except CryptoError:
        raise HubError("wrap_unauthorized") from None
    with transaction(context.pool) as connection:
        invite = connection.execute(
            f"SELECT pk_inv, org_id, log_seq FROM hub.invite WHERE id = %(id)s AND {OPEN_INVITE} FOR UPDATE",
            {"id": invite_id, "now": now},
        ).fetchone()
        if invite is None:
            raise HubError("invite_unknown")
        pk_inv = _invitation_key(connection, *invite)
        try:
            verify_register(pk_inv, str(user_id), ceremony.challenge, body.inv_sig)
        except CryptoError:
            raise HubError("signature_invalid") from None
        connection.execute("INSERT INTO hub.principal (id, kind, created_at) VALUES (%s, 'user', %s)", (user_id, now))
        stored = connection.execute(
            "INSERT INTO hub.user_identity (user_id, kem_pub, sig_pub, recovery_id, created_at)"
            " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (recovery_id) DO NOTHING RETURNING user_id",
            (user_id, kem_pub, sig_pub, recovery.recovery_id, now),
        ).fetchone()
        if stored is None:
            raise HubError("id_conflict")
        connection.execute("INSERT INTO hub.user_settings (user_id, updated_at) VALUES (%s, %s)", (user_id, now))
        connection.execute(
            "INSERT INTO hub.identity_bundle (user_id, slot, ct, updated_at) VALUES (%s, 'recovery', %s, %s)",
            (user_id, decode(recovery.bundle_ct), now),
        )
        _insert_passkey(connection, user_id, registration, list(body.credential.transports), now)
        connection.execute(
            "INSERT INTO hub.scope_epoch (scope, scope_id, epoch, created_at) VALUES ('cred', %s, 1, %s)",
            (user_id, now),
        )
        connection.execute(
            "INSERT INTO hub.key_wrap (scope, scope_id, epoch, recipient_id, wrap, wrapped_by, sig, created_at)"
            " VALUES ('cred', %s, 1, %s, %s, %s, %s, %s)",
            (user_id, user_id, decode(wrap.wrap), user_id, decode(wrap.sig), now),
        )
        connection.execute(
            "UPDATE hub.invite SET registered_user = %s, registered_at = %s WHERE id = %s", (user_id, now, invite_id)
        )
        reference = credential_reference(registration.credential_id)
        for action, kind, target, detail in (
            ("auth.register", "passkey", reference, {"passkey_id": reference, "invite_id": invite_id}),
            ("invite.register", "invite", invite_id, {"invite_id": invite_id, "kind": "user"}),
        ):
            append_audit(
                connection,
                AuditRow(
                    at=now,
                    actor_kind="user",
                    actor_id=user_id,
                    action=action,
                    result="ok",
                    target_kind=kind,
                    target_id=target,
                    detail=detail,
                ),
            )


def _invitation_key(
    connection: psycopg.Connection[Any], stored: bytes | None, org_id: uuid.UUID | None, log_seq: int | None
) -> bytes:
    """`pk_inv` of an invitation: stored for platform invitations, else from its membership-log `invite` entry;
    `invite_unknown` when there is none (a platform invitation created before ADR-043)."""
    if stored is not None:
        return bytes(stored)
    row = None
    if org_id is not None and log_seq is not None:
        row = connection.execute(
            "SELECT entry_text FROM hub.membership_entry WHERE org_id = %s AND seq = %s", (org_id, log_seq)
        ).fetchone()
    if row is None:
        raise HubError("invite_unknown")
    body = json.loads(row[0])["body"]
    return b64u_decode(body["pk_inv"], 32)


def _store_further(
    context: HubContext, ceremony: Ceremony, registration: Registration, transports: Sequence[str], now: datetime
) -> None:
    user_id = cast(uuid.UUID, ceremony.user_id)
    recovery = ceremony.kind == "recovery"
    with transaction(context.pool) as connection:
        user = connection.execute(
            "SELECT 1 FROM hub.principal WHERE id = %s AND kind = 'user' AND disabled_at IS NULL FOR UPDATE",
            (user_id,),
        ).fetchone()
        if user is None:
            raise HubError("ceremony_invalid")
        _insert_passkey(connection, user_id, registration, list(transports), now)
        reference = credential_reference(registration.credential_id)
        append_audit(
            connection,
            AuditRow(
                at=now,
                actor_kind="user",
                actor_id=user_id,
                action="auth.recovery" if recovery else "auth.passkey_add",
                result="ok",
                target_kind="passkey",
                target_id=reference,
                detail={"step": "finish"} if recovery else {"passkey_id": reference},
            ),
        )
        notices = recovery_notice(connection, user_id, now, reference) if recovery else []
    publish_rows(notices)
