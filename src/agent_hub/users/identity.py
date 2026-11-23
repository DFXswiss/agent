"""The admissible identity of an account (ADR-060; 04-crypto.md section 5.13).

An account can be admitted directly (`admit`) if it is an active user whose registered identity `(kem, sig)` is
anchored in the verified platform log: by a valid `user_certify` of exactly these keys, or by the identity proof kept
from its registration through a platform invitation (`user_identity.id_sig` at `id_invite_seq`, checked again here
against the platform log). The answer is public data only (user ID, public keys, platform seq, the proof, the account
code); the admitting client verifies it against the platform log it verified itself and never trusts it. Anything else
(an unknown, disabled or unanchored account) is the same `None`, so the hub discloses no more than "not admissible".
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from agent_hub.crypto.encoding import b64u_encode, format_time
from agent_hub.crypto.identity import account_code
from agent_hub.log.platform import PlatformState

Connection = psycopg.Connection[Any]
MAX_INVITEES = 1000
"""The contract's `maxItems` of `GET /v1/me/invitees`."""

_IDENTITY_SQL = (
    "SELECT ui.user_id, ui.kem_pub, ui.sig_pub, ui.id_sig, ui.id_invite_seq FROM hub.user_identity ui"
    " JOIN hub.principal p ON p.id = ui.user_id WHERE p.kind = 'user' AND p.disabled_at IS NULL"
)


def _anchored(platform: PlatformState, record: tuple[Any, ...]) -> dict[str, Any] | None:
    user = str(record[0])
    pub = {"kem": b64u_encode(bytes(record[1])), "sig": b64u_encode(bytes(record[2]))}
    seq, proof = platform.users.get(user), ""
    if seq is None or not platform.identity_anchored(seq, user, pub, "", current=True):
        seq = record[4]
        proof = "" if record[3] is None else b64u_encode(bytes(record[3]))
        if seq is None or not platform.identity_anchored(seq, user, pub, proof, current=True):
            return None
    return {"user_id": user, "pub": pub, "platform_seq": seq, "id_sig": proof, "account_code": account_code(user, pub)}


def proven_identity(connection: Connection, platform: PlatformState | None, user: UUID) -> dict[str, Any] | None:
    """The admissible identity of `user` (module docstring), None if it has none."""
    if platform is None:
        return None
    record = connection.execute(_IDENTITY_SQL + " AND ui.user_id = %s", (user,)).fetchone()
    return None if record is None else _anchored(platform, record)


def identity_by_code(connection: Connection, platform: PlatformState | None, code: str) -> dict[str, Any] | None:
    """The admissible account with the account code `code`, None if there is none. Only anchored accounts can match:
    the certified users of the platform log and the accounts with an identity proof."""
    if platform is None:
        return None
    certified = [UUID(user) for user in platform.users]
    records = connection.execute(
        _IDENTITY_SQL + " AND (ui.user_id = ANY(%s::uuid[]) OR ui.id_sig IS NOT NULL) ORDER BY ui.user_id", (certified,)
    ).fetchall()
    for record in records:
        found = _anchored(platform, record)
        if found is not None and found["account_code"] == code:
            return found
    return None


def invitees(connection: Connection, platform: PlatformState | None, inviter: UUID) -> list[dict[str, Any]]:
    """The accounts registered through the platform invitations `inviter` created, newest first, each with its
    admissible identity or None."""
    rows = connection.execute(
        "SELECT i.id, i.registered_user, i.registered_at FROM hub.invite i JOIN hub.principal p"
        " ON p.id = i.registered_user AND p.disabled_at IS NULL"
        " WHERE i.scope = 'platform' AND i.kind = 'user' AND i.created_by = %s"
        " ORDER BY i.registered_at DESC, i.id LIMIT %s",
        (inviter, MAX_INVITEES),
    ).fetchall()
    return [
        {
            "user_id": str(row[1]),
            "invite_id": row[0],
            "registered_at": format_time(row[2]),
            "identity": proven_identity(connection, platform, row[1]),
        }
        for row in rows
    ]


def registered_identity(connection: Connection, platform: PlatformState | None, user: UUID) -> dict[str, Any] | None:
    """For the operator (`agent-hub platform certify-user`): the registered identity of an active user, its account
    code, the platform invitation it registered through (if any), when, and its valid certification (if any)."""
    record = connection.execute(
        "SELECT ui.kem_pub, ui.sig_pub, p.created_at, i.id, i.registered_at FROM hub.user_identity ui"
        " JOIN hub.principal p ON p.id = ui.user_id LEFT JOIN hub.invite i ON i.registered_user = ui.user_id"
        " AND i.scope = 'platform' WHERE ui.user_id = %s AND p.kind = 'user' AND p.disabled_at IS NULL",
        (user,),
    ).fetchone()
    if record is None:
        return None
    pub = {"kem": b64u_encode(bytes(record[0])), "sig": b64u_encode(bytes(record[1]))}
    certified = None if platform is None or not platform.user_certified(str(user)) else platform.users[str(user)]
    return {
        "user_id": str(user),
        "pub": pub,
        "account_code": account_code(str(user), pub),
        "created_at": format_time(record[2]),
        "platform_invite_id": record[3],
        "registered_at": None if record[4] is None else format_time(record[4]),
        "certified_seq": certified,
    }
