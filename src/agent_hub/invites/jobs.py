"""Jobs of the invitations area (ADR-027 section 4: `invites/jobs.py:JOBS`).

`invites.expire_packages` (every 60 s, first run when the process starts its jobs): the sealed package of an org
invitation that expired unused and unrevoked is deleted (`package_ct = NULL`, 02-data-model.md section 2.1, ADR-005)
and the expiry is audited once as `invite.expire` (actor `hub`; the deleted package marks it done). At most 1000
invitations per run, oldest expiry first; rows locked by a running accept or revoke are left for the next run. The job
also binds the hub context for the `invite.accepted` subscriber (`agent_hub.invites.signals`).

Expired invitations are refused independently of this job: the lookup answers `invite_unknown`, registration
`invite_unknown` and the `accept` `invite_expired` (log rule at hub receipt).
"""

from __future__ import annotations

from typing import Any

from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.db.pool import transaction
from agent_hub.invites import signals
from agent_hub.jobs import Job

EXPIRE_INTERVAL_SECONDS = 60.0
EXPIRE_BATCH = 1000

EXPIRED = (
    "UPDATE hub.invite SET package_ct = NULL WHERE id IN (SELECT id FROM hub.invite WHERE scope = 'org'"
    " AND package_ct IS NOT NULL AND used_at IS NULL AND revoked_at IS NULL AND expires_at <= %(now)s"
    " ORDER BY expires_at, id LIMIT %(limit)s FOR UPDATE SKIP LOCKED) RETURNING id, kind, org_id, expires_at"
)


def expire_packages(context: Any) -> int:
    """Delete the packages of expired org invitations; the number deleted."""
    signals.bind(context)
    now = context.clock()
    with transaction(context.pool) as connection:
        expired = connection.execute(EXPIRED, {"now": now, "limit": EXPIRE_BATCH}).fetchall()
        for invite_id, kind, org_id, _expires in sorted(expired, key=lambda row: (row[3], row[0])):
            append_audit(
                connection,
                AuditRow(
                    at=now,
                    actor_kind="hub",
                    action="invite.expire",
                    result="ok",
                    org_id=org_id,
                    target_kind="invite",
                    target_id=invite_id,
                    detail={"invite_id": invite_id, "kind": kind},
                ),
            )
    return len(expired)


JOBS = (Job("invites.expire_packages", EXPIRE_INTERVAL_SECONDS, expire_packages),)
