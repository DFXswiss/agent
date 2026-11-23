"""Platform invitations of inviters (03-api.md section 4.2, ADR-005, ADR-026).

A user with the platform role `inviter` (projection `platform_role` of the platform log) creates platform user
invitations for customers: the browser generates the code, sends only `invite_id`, the public invitation key `pk_inv`
and the expiry, and hands the code to the customer. The hub keeps an `invite` row (scope `platform`, kind `user`,
`created_by` = the inviter, `pk_inv`); registering through it needs a signature of `sk_inv` (ADR-043), so other
inviters, who see the `invite_id` in the listing, cannot take it. A `pk_inv` that is no Ed25519 public key is
`400 params_invalid`. Expiry: in
the future and at most 30 days ahead (`03-api.md` section 12). Repeating a create with identical content succeeds
again; another invitation with the same ID is `409 id_conflict`. Inviters list the platform user invitations (newest
first, the operator's ones with `created_by` null) and revoke open ones; a used invitation cannot be revoked
(`invite_consumed`). The registration through a platform invitation (HUB-03) and its use by the private-org genesis
(HUB-05) work on the same rows.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg

from agent_core.signing import ed25519_public_key_valid
from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.config import LIMITS
from agent_hub.crypto.encoding import b64u_decode, format_time, parse_time
from agent_hub.errors import HubError

MAX_LISTED = 1000


def is_inviter(connection: psycopg.Connection[Any], user: UUID) -> bool:
    """The user holds the platform role `inviter` (granted and not revoked in the platform log)."""
    row = connection.execute(
        "SELECT 1 FROM hub.platform_role WHERE user_id = %s AND role = 'inviter' AND revoked_seq IS NULL", (user,)
    ).fetchone()
    return row is not None


def require_inviter(connection: psycopg.Connection[Any], user: UUID) -> None:
    """`403 forbidden` unless the user is an inviter."""
    if not is_inviter(connection, user):
        raise HubError("forbidden")


def _audit(connection: psycopg.Connection[Any], action: str, user: UUID, invite_id: str, now: datetime) -> None:
    append_audit(
        connection,
        AuditRow(
            at=now,
            actor_kind="user",
            actor_id=user,
            action=action,
            result="ok",
            target_kind="invite",
            target_id=invite_id,
            detail={"invite_id": invite_id, "kind": "user"},
        ),
    )


def create(
    connection: psycopg.Connection[Any], user: UUID, invite_id: str, *, pk_inv: str, expires_text: str, now: datetime
) -> None:
    """Create a platform user invitation (idempotent for an identical repeat)."""
    require_inviter(connection, user)
    expires = parse_time(expires_text)
    if not now < expires <= now + timedelta(seconds=LIMITS["invite_max_seconds"]):
        raise HubError("request_invalid", detail={"pointers": ["/expires_at"]})
    key = b64u_decode(pk_inv, 32)
    if not ed25519_public_key_valid(key):
        raise HubError("params_invalid", detail={"pointers": ["/pk_inv"]})
    existing = connection.execute(
        "SELECT scope, kind, created_by, expires_at, pk_inv FROM hub.invite WHERE id = %s FOR UPDATE", (invite_id,)
    ).fetchone()
    if existing is not None:
        scope, kind, created_by, stored_expires, stored_key = existing
        if (scope, kind, created_by, stored_expires, stored_key and bytes(stored_key)) != (
            "platform",
            "user",
            user,
            expires,
            key,
        ):
            raise HubError("id_conflict")
        return
    connection.execute(
        "INSERT INTO hub.invite (id, scope, kind, created_by, created_at, expires_at, pk_inv)"
        " VALUES (%s, 'platform', 'user', %s, %s, %s, %s)",
        (invite_id, user, now, expires, key),
    )
    _audit(connection, "invite.create", user, invite_id, now)


def state_of(used_at: datetime | None, revoked_at: datetime | None, expires_at: datetime, now: datetime) -> str:
    """`used`, `revoked`, `expired` or `open` (in this order of precedence)."""
    if used_at is not None:
        return "used"
    if revoked_at is not None:
        return "revoked"
    if expires_at <= now:
        return "expired"
    return "open"


def _time(value: datetime | None) -> str | None:
    return None if value is None else format_time(value)


def listing(connection: psycopg.Connection[Any], user: UUID, now: datetime) -> list[dict[str, object]]:
    """The platform user invitations, newest first."""
    require_inviter(connection, user)
    rows = connection.execute(
        "SELECT id, created_by, created_at, expires_at, registered_user, used_at, revoked_at FROM hub.invite"
        " WHERE scope = 'platform' AND kind = 'user' ORDER BY created_at DESC, id DESC LIMIT %s",
        (MAX_LISTED,),
    ).fetchall()
    return [
        {
            "invite_id": invite_id,
            "kind": "user",
            "state": state_of(used_at, revoked_at, expires_at, now),
            "created_by": None if created_by is None else str(created_by),
            "created_at": format_time(created_at),
            "expires_at": format_time(expires_at),
            "registered": registered_user is not None,
            "used_at": _time(used_at),
            "revoked_at": _time(revoked_at),
        }
        for invite_id, created_by, created_at, expires_at, registered_user, used_at, revoked_at in rows
    ]


def revoke(connection: psycopg.Connection[Any], user: UUID, invite_id: str, now: datetime) -> None:
    """Revoke a platform user invitation (`404 not_found` for others; repeating is a no-op)."""
    require_inviter(connection, user)
    row = connection.execute(
        "SELECT used_at, revoked_at FROM hub.invite WHERE id = %s AND scope = 'platform' AND kind = 'user' FOR UPDATE",
        (invite_id,),
    ).fetchone()
    if row is None:
        raise HubError("not_found")
    used_at, revoked_at = row
    if used_at is not None:
        raise HubError("invite_consumed")
    if revoked_at is not None:
        return
    connection.execute("UPDATE hub.invite SET revoked_at = %s, package_ct = NULL WHERE id = %s", (now, invite_id))
    _audit(connection, "invite.revoke", user, invite_id, now)
