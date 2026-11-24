"""Passkey login and browser sessions (03-api.md section 4.1, 04-crypto.md sections 4.9 and 5.4).

`login/begin` opens a ceremony with a 32-byte random hub value. The browser builds the statement `agent-session-v1`
(origin, its P-256 session public key, the hub value, its expiry) and runs one `get()` with challenge
SHA-256(statement) and the PRF extension. `login/finish` verifies the certificate with
`agent_core.passkey.certificate.verify_certificate` (V1-V11 against the stored passkey key, the hub's origin and RP
ID, at most 12 h), requires the ceremony's hub value in the statement (`challenge_mismatch`), applies
`webauthn.check_counter` and stores the `browser_session` with the key ID `session.<32 hex>` of the statement. The
session lasts until the statement's expiry but never longer than 43080 s from the hub's clock (the browser writes its
own clock plus 43080 s; the 120 s between 43080 s and the 12 h bound absorb a browser clock that runs ahead).

The answer carries the identity bundle of the credential's slot when one was uploaded and `prf_expected` (the
passkey delivered PRF before, so a missing PRF output now means an exported passkey or a non-PRF manager). Every
refusal after the body was accepted is audited as `auth.refused`, a success as `auth.login`.
"""

from __future__ import annotations

import hmac
from datetime import UTC, datetime, timedelta
from secrets import token_bytes
from typing import Annotated, Any
from uuid import UUID

from agent_core.passkey.certificate import Certificate, verify_certificate
from agent_core.passkey.encoding import SESSION_KEY_ISSUE_SECONDS, SESSION_KEY_MAX_SECONDS, session_key_id
from agent_core.passkey.errors import PasskeyRefusal
from agent_core.passkey.webauthn import check_counter
from agent_core.signing import b64url_encode
from agent_hub.agents.store import check_open
from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.auth.ceremonies import (
    CHALLENGE_BYTES,
    Ceremony,
    create_ceremony,
    credential_reference,
    relying_party,
    request_options,
    take_ceremony,
)
from agent_hub.auth.fields import CredentialId, Strict, Uuid7, b64u_text, decode
from agent_hub.crypto.bundle import passkey_slot
from agent_hub.crypto.encoding import format_time
from agent_hub.db.pool import transaction
from agent_hub.errors import HubError
from agent_hub.http.context import HubContext


class SessionCertificate(Strict):
    statement: Annotated[str, b64u_text(1024)]
    credential_id: CredentialId
    authenticator_data: Annotated[str, b64u_text(8192)]
    client_data_json: Annotated[str, b64u_text(8192)]
    signature: Annotated[str, b64u_text(256)]


class LoginBegin(Strict):
    pairing_id: Uuid7 | None = None


class LoginFinish(Strict):
    ceremony_id: Uuid7
    certificate: SessionCertificate


def begin(context: HubContext, body: LoginBegin | None = None) -> dict[str, Any]:
    """`POST /v1/passkeys/login/begin`; with `pairing_id` the login confirms that open agent pairing (ADR-056,
    `404 pairing_unknown` otherwise): the ceremony and the session it creates carry the pairing ID."""
    now = context.clock()
    pairing = None if body is None or body.pairing_id is None else UUID(body.pairing_id)
    if pairing is not None:
        with context.pool.connection() as connection:
            check_open(connection, pairing, now)
    ceremony = create_ceremony(
        context.pool, kind="login", now=now, hub_value=token_bytes(CHALLENGE_BYTES), pairing_id=pairing
    )
    return {
        "ceremony_id": str(ceremony.id),
        "hub_value": b64url_encode(ceremony.challenge),
        "options": request_options(context.config),
    }


def _refused(context: HubContext, code: str, key_id: str | None) -> None:
    """The audit row of a refused login (own transaction: the refused one rolled back)."""
    with transaction(context.pool) as connection:
        append_audit(
            connection,
            AuditRow(
                at=context.clock(),
                actor_kind="anonymous",
                action="auth.refused",
                result="denied",
                target_kind="browser_session",
                target_id=key_id,
                detail={"code": code, "key_id": key_id},
            ),
        )


def finish(context: HubContext, body: LoginFinish) -> dict[str, Any]:
    """`POST /v1/passkeys/login/finish`; refusals after the ceremony was taken are audited (an unknown or used
    ceremony is refused without an audit row, so random IDs cannot flood the audit chain)."""
    now = context.clock()
    ceremony = take_ceremony(context.pool, body.ceremony_id, ("login",), now)
    key_id = session_key_id(decode(body.certificate.statement))
    try:
        return _finish(context, body, ceremony, now)
    except HubError as error:
        _refused(context, error.code, key_id)
        raise


def _finish(context: HubContext, body: LoginFinish, ceremony: Ceremony, now: datetime) -> dict[str, Any]:
    fields = body.certificate
    certificate = Certificate(
        statement=decode(fields.statement),
        credential_id=decode(fields.credential_id),
        authenticator_data=decode(fields.authenticator_data),
        client_data_json=decode(fields.client_data_json),
        signature=decode(fields.signature),
    )
    with transaction(context.pool) as connection:
        passkey = connection.execute(
            "SELECT p.user_id, p.public_key, p.sign_count, p.prf_seen FROM hub.passkey p"
            " JOIN hub.principal u ON u.id = p.user_id"
            " WHERE p.credential_id = %s AND p.removed_at IS NULL AND u.disabled_at IS NULL FOR UPDATE OF p",
            (certificate.credential_id,),
        ).fetchone()
        if passkey is None:
            raise HubError("credential_invalid")
        user_id, public_key, stored_count, prf_seen = passkey
        try:
            certified = verify_certificate(
                certificate,
                passkey_public_key=bytes(public_key),
                relying_party=relying_party(context.config),
                now=int(now.timestamp()),
                max_lifetime=SESSION_KEY_MAX_SECONDS,
            )
            if not hmac.compare_digest(certified.statement.hub_value, ceremony.challenge):
                raise PasskeyRefusal("challenge_mismatch")
            sign_count = check_counter(int(stored_count), certified.sign_count)
        except PasskeyRefusal as refusal:
            raise HubError(refusal.error) from None
        expires_at = min(
            datetime.fromtimestamp(certified.statement.expires, UTC), now + timedelta(seconds=SESSION_KEY_ISSUE_SECONDS)
        )
        connection.execute(
            "UPDATE hub.passkey SET sign_count = %s, last_used_at = %s WHERE credential_id = %s",
            (sign_count, now, certificate.credential_id),
        )
        connection.execute(
            "INSERT INTO hub.browser_session (key_id, user_id, credential_id, public_key, statement, created_at,"
            " expires_at, pairing_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                certified.session_key_id,
                user_id,
                certificate.credential_id,
                certified.statement.session_public_key,
                certificate.statement.decode("ascii"),
                now,
                expires_at,
                ceremony.pairing_id,
            ),
        )
        bundle = connection.execute(
            "SELECT ct FROM hub.identity_bundle WHERE user_id = %s AND slot = %s",
            (user_id, passkey_slot(certificate.credential_id)),
        ).fetchone()
        append_audit(
            connection,
            AuditRow(
                at=now,
                actor_kind="user",
                actor_id=user_id,
                action="auth.login",
                result="ok",
                target_kind="passkey",
                target_id=credential_reference(certificate.credential_id),
                detail={"passkey_id": credential_reference(certificate.credential_id)},
            ),
        )
    answer: dict[str, Any] = {
        "user_id": str(user_id),
        "key_id": certified.session_key_id,
        "expires_at": format_time(expires_at),
        "prf_expected": bool(prf_seen),
    }
    if bundle is not None:
        answer["bundle_ct"] = b64url_encode(bytes(bundle[0]))
    return answer
