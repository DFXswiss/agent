"""Jobs of the authentication area (ADR-027 section 4: `auth/jobs.py:JOBS`).

- `auth.orphan_registrations` (03-api.md section 4.1, ADR-026): a registration through an invitation that is not
  followed within 24 h by `accept` (org invitation) or a private genesis (platform invitation) - both mark the
  invitation used - is deleted with everything the user stored, and the invitation's `registered_user` is reset, so
  the invitation becomes usable again until it expires. A user who meanwhile became an org member (an existing user
  who accepted another invitation, 04-crypto.md section 5.2a), holds a platform role or owns a task is kept.
  Audit: `invite.release` (actor `hub`). `abandon_registration` releases the same way at once, on request of the
  registered user (`DELETE /v1/me/registration`, KOR-22: the new passkey gave no PRF output, so the account could
  not unlock and the registration is withdrawn); actor `user`.
- `auth.ceremony_purge`: ceremonies are deleted one day after their creation (02-data-model.md section 2.1).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.auth.ceremonies import purge_ceremonies
from agent_hub.db.pool import transaction
from agent_hub.errors import HubError
from agent_hub.jobs import Job

ORPHAN_SECONDS = 24 * 3600
ORPHAN_INTERVAL_SECONDS = 300.0
PURGE_INTERVAL_SECONDS = 3600.0

# Rows of an orphaned user, deleted child tables first (`principal` last: identity and settings reference it).
USER_ROWS = (
    "DELETE FROM hub.agent WHERE user_id = %(user)s",
    "DELETE FROM hub.browser_session WHERE user_id = %(user)s",
    "DELETE FROM hub.identity_bundle WHERE user_id = %(user)s",
    "DELETE FROM hub.passkey WHERE user_id = %(user)s",
    "DELETE FROM hub.ceremony WHERE user_id = %(user)s",
    "DELETE FROM hub.key_wrap WHERE recipient_id = %(user)s OR (scope = 'cred' AND scope_id = %(user)s)",
    "DELETE FROM hub.scope_epoch WHERE scope = 'cred' AND scope_id = %(user)s",
    "DELETE FROM hub.subscription_quota WHERE owner_id = %(user)s",
    "DELETE FROM hub.subscription_use WHERE owner_id = %(user)s",
    "DELETE FROM hub.credential WHERE owner_id = %(user)s",
    "DELETE FROM hub.quota_snapshot WHERE owner_id = %(user)s",
    "DELETE FROM hub.quota_unreadable WHERE owner_id = %(user)s",
    "DELETE FROM hub.notification WHERE recipient_id = %(user)s",
    "DELETE FROM hub.user_settings WHERE user_id = %(user)s",
    "DELETE FROM hub.user_identity WHERE user_id = %(user)s",
    "DELETE FROM hub.principal WHERE id = %(user)s AND kind = 'user'",
)


ORPHANS = (
    "SELECT i.id FROM hub.invite i"
    " WHERE i.registered_user IS NOT NULL AND i.used_at IS NULL AND i.registered_at <= %(cutoff)s"
    " ORDER BY i.registered_at, i.id"
)
# A registration that nothing depends on yet: the invitation is unused, the user is no org member, holds no platform
# role and owns no task.
UNFINISHED = (
    " AND i.used_at IS NULL"
    " AND NOT EXISTS (SELECT 1 FROM hub.org_member m WHERE m.principal_id = i.registered_user)"
    " AND NOT EXISTS (SELECT 1 FROM hub.platform_role r WHERE r.user_id = i.registered_user)"
    " AND NOT EXISTS (SELECT 1 FROM hub.task t WHERE t.owner_id = i.registered_user)"
)
# The same condition for one invitation, under the locks of the invitation and the user's principal row.
ORPHAN = (
    "SELECT i.registered_user, i.kind FROM hub.invite i JOIN hub.principal u ON u.id = i.registered_user"
    " WHERE i.id = %(invite)s AND i.registered_at <= %(cutoff)s" + UNFINISHED + " FOR UPDATE OF i, u SKIP LOCKED"
)
# The unfinished registration of one user (the user's own request), under the same locks.
OWN = (
    "SELECT i.id, i.kind FROM hub.invite i JOIN hub.principal u ON u.id = i.registered_user"
    " WHERE i.registered_user = %(user)s" + UNFINISHED + " FOR UPDATE OF i, u"
)


def _release(connection: Any, invite_id: str, user_id: Any, kind: str, audit: AuditRow) -> None:
    """Delete the user's rows, free the invitation and append the audit row (inside the caller's transaction)."""
    for statement in USER_ROWS:
        connection.execute(statement, {"user": user_id})
    connection.execute("UPDATE hub.invite SET registered_user = NULL, registered_at = NULL WHERE id = %s", (invite_id,))
    append_audit(connection, audit)


def abandon_registration(context: Any, user_id: Any) -> dict[str, Any]:
    """Withdraw the signed-in user's unfinished registration: everything the user stored is deleted and the invitation
    is usable again. `403 forbidden` when there is none (a finished account, or a user without an invitation)."""
    now = context.clock()
    with transaction(context.pool) as connection:
        found = connection.execute(OWN, {"user": user_id}).fetchone()
        if found is None:
            raise HubError("forbidden")
        invite_id, kind = found
        audit = AuditRow(
            at=now,
            actor_kind="user",
            actor_id=user_id,
            action="invite.release",
            result="ok",
            target_kind="invite",
            target_id=invite_id,
            detail={"invite_id": invite_id, "kind": kind},
        )
        _release(connection, invite_id, user_id, kind, audit)
    return {}


def release_orphan_registrations(context: Any) -> int:
    """Delete registrations older than 24 h whose invitation is still unused; the number released.

    Every orphan is released in its own transaction (re-checked under the locks of the invitation and the
    principal), with the audit row last, so the audit lock is never held while rows of other users are deleted."""
    now = context.clock()
    cutoff = now - timedelta(seconds=ORPHAN_SECONDS)
    with context.pool.connection() as connection:
        candidates = [invite for (invite,) in connection.execute(ORPHANS, {"cutoff": cutoff}).fetchall()]
    released = 0
    for invite_id in candidates:
        with transaction(context.pool) as connection:
            found = connection.execute(ORPHAN, {"invite": invite_id, "cutoff": cutoff}).fetchone()
            if found is None:
                continue
            user_id, kind = found
            audit = AuditRow(
                at=now,
                actor_kind="hub",
                action="invite.release",
                result="ok",
                target_kind="invite",
                target_id=invite_id,
                detail={"invite_id": invite_id, "kind": kind},
            )
            _release(connection, invite_id, user_id, kind, audit)
        released += 1
    return released


def purge_old_ceremonies(context: Any) -> int:
    """Delete ceremonies older than one day."""
    return purge_ceremonies(context.pool, context.clock())


JOBS = (
    Job("auth.orphan_registrations", ORPHAN_INTERVAL_SECONDS, release_orphan_registrations),
    Job("auth.ceremony_purge", PURGE_INTERVAL_SECONDS, purge_old_ceremonies),
)
