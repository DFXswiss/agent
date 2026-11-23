"""WebAuthn ceremonies of the hub (03-api.md section 4.1, 02-data-model.md section 2.1 `ceremony`).

A ceremony is created by a `…/begin` endpoint with a 32-byte random challenge (registration) or hub value (login),
lives 300 s (03-api.md section 12) and is used at most once: `take_ceremony` marks it used in its own committed
transaction before any verification runs, so a failed `…/finish` consumes it as well (one attempt per challenge,
like `take_sign_in` of the legacy passkey hub). Ceremonies are purged one day after their creation (`auth/jobs.py`).

The relying party is the origin and host of `AGENT_HUB_PUBLIC_URL` (04-crypto.md section 4.9, ADR-004); the options
follow 03-api.md section 4.1 exactly: ES256 only, resident key and user verification required, the PRF input of
04-crypto.md section 3 and the timeout 300000 ms.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from psycopg_pool import ConnectionPool

from agent_core.passkey.relying_party import RelyingParty
from agent_core.signing import b64url_encode
from agent_hub.config import HubConfig
from agent_hub.crypto.ids import uuid7
from agent_hub.crypto.kdf import prf_input
from agent_hub.db.pool import transaction
from agent_hub.errors import HubError

CEREMONY_SECONDS = 300
CEREMONY_RETENTION_SECONDS = 24 * 3600
TIMEOUT_MS = CEREMONY_SECONDS * 1000
CHALLENGE_BYTES = 32
ES256 = -7
RP_NAME = "agent.dfx.swiss"
# A credential ID longer than this (as b64u) is named by its digest in audit rows (`target_id` holds 128 characters).
MAX_AUDIT_REFERENCE = 128

Pool = ConnectionPool[Any]


@dataclass(frozen=True)
class Ceremony:
    """A ceremony row that was taken for use."""

    id: uuid.UUID
    kind: str
    challenge: bytes = field(repr=False)
    user_id: uuid.UUID | None
    invite_id: str | None
    hub_value: bytes | None = field(repr=False)
    pairing_id: uuid.UUID | None = None


def relying_party(config: HubConfig) -> RelyingParty:
    """The origin and RP ID of the hub (the host of `AGENT_HUB_PUBLIC_URL`)."""
    return RelyingParty(origin=config.public.origin, rp_id=config.public.rp_id)


def new_id(now: datetime) -> uuid.UUID:
    """A UUIDv7 for an object created by the hub at `now`."""
    return uuid.UUID(uuid7(int(now.timestamp() * 1000)))


def prf_extension() -> dict[str, Any]:
    """`extensions.prf.eval.first` with the PRF input of 04-crypto.md section 3."""
    return {"prf": {"eval": {"first": b64url_encode(prf_input())}}}


def creation_options(
    config: HubConfig, user_id: uuid.UUID, challenge: bytes, exclude: Sequence[bytes] = ()
) -> dict[str, Any]:
    """`PublicKeyCredentialCreationOptions` JSON of `register/begin` (user handle = the 16 bytes of `user_id`;
    name and display name are the user ID text, the hub knows no names)."""
    return {
        "rp": {"id": config.public.rp_id, "name": RP_NAME},
        "user": {"id": b64url_encode(user_id.bytes), "name": str(user_id), "displayName": str(user_id)},
        "challenge": b64url_encode(challenge),
        "pubKeyCredParams": [{"type": "public-key", "alg": ES256}],
        "authenticatorSelection": {
            "residentKey": "required",
            "userVerification": "required",
            "requireResidentKey": True,
        },
        "extensions": prf_extension(),
        "timeout": TIMEOUT_MS,
        "attestation": "none",
        "excludeCredentials": [{"type": "public-key", "id": b64url_encode(raw)} for raw in exclude],
    }


def request_options(config: HubConfig) -> dict[str, Any]:
    """`PublicKeyCredentialRequestOptions` JSON of `login/begin` without the challenge (the browser uses
    SHA-256(statement), 04-crypto.md section 5.4): discoverable credentials, UV required, PRF."""
    return {
        "rpId": config.public.rp_id,
        "allowCredentials": [],
        "userVerification": "required",
        "extensions": prf_extension(),
        "timeout": TIMEOUT_MS,
    }


def create_ceremony(
    pool: Pool,
    *,
    kind: str,
    now: datetime,
    user_id: uuid.UUID | None = None,
    invite_id: str | None = None,
    hub_value: bytes | None = None,
    pairing_id: uuid.UUID | None = None,
) -> Ceremony:
    """Store a new ceremony; its challenge is the hub value for a login, else 32 random bytes. A login ceremony may
    carry the agent pairing it confirms (ADR-056)."""
    ceremony = Ceremony(
        id=new_id(now),
        kind=kind,
        challenge=secrets.token_bytes(CHALLENGE_BYTES) if hub_value is None else hub_value,
        user_id=user_id,
        invite_id=invite_id,
        hub_value=hub_value,
        pairing_id=pairing_id,
    )
    with transaction(pool) as connection:
        connection.execute(
            "INSERT INTO hub.ceremony (id, kind, challenge, user_id, invite_id, hub_value, created_at, expires_at,"
            " pairing_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                ceremony.id,
                kind,
                ceremony.challenge,
                user_id,
                invite_id,
                hub_value,
                now,
                now + timedelta(seconds=CEREMONY_SECONDS),
                pairing_id,
            ),
        )
    return ceremony


def take_ceremony(
    pool: Pool,
    ceremony_id: str,
    kinds: Collection[str],
    now: datetime,
    check: Callable[[Ceremony], None] | None = None,
) -> Ceremony:
    """Mark an open ceremony of one of `kinds` used and return it; `ceremony_invalid` when it is unknown, of another
    kind, used or expired. `check` may refuse the request before the ceremony is consumed (malformed body for the
    kind, wrong caller)."""
    with transaction(pool) as connection:
        row = connection.execute(
            "SELECT id, kind, challenge, user_id, invite_id, hub_value, pairing_id FROM hub.ceremony"
            " WHERE id = %s AND used_at IS NULL AND expires_at > %s FOR UPDATE",
            (uuid.UUID(ceremony_id), now),
        ).fetchone()
        if row is None or row[1] not in kinds:
            raise HubError("ceremony_invalid")
        ceremony = Ceremony(
            id=row[0],
            kind=row[1],
            challenge=bytes(row[2]),
            user_id=row[3],
            invite_id=row[4],
            hub_value=None if row[5] is None else bytes(row[5]),
            pairing_id=row[6],
        )
        if check is not None:
            check(ceremony)
        connection.execute("UPDATE hub.ceremony SET used_at = %s WHERE id = %s", (now, ceremony.id))
    return ceremony


def purge_ceremonies(pool: Pool, now: datetime) -> int:
    """Delete ceremonies created more than one day ago (02-data-model.md section 2.1)."""
    with transaction(pool) as connection:
        cursor = connection.execute(
            "DELETE FROM hub.ceremony WHERE created_at < %s", (now - timedelta(seconds=CEREMONY_RETENTION_SECONDS),)
        )
        return cursor.rowcount


def credential_reference(credential_id: bytes) -> str:
    """The audit `target_id` of a passkey: its b64u credential ID, or `sha256:<32 hex>` of it when too long."""
    text = b64url_encode(credential_id)
    if len(text) <= MAX_AUDIT_REFERENCE:
        return text
    return "sha256:" + hashlib.sha256(credential_id).hexdigest()[:32]
