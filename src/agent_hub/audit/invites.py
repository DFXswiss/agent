"""The audit row `invite.use` (02-data-model.md section 2.9): an invitation is consumed.

An invitation is used exactly once, in the transaction that sets its `used_at`: an org invitation by the `accept` entry
of the invited user or device (`agent_hub.orgs.append`), a platform user invitation by the genesis of the invited
user's private organization (`agent_hub.orgs.genesis`), a platform device invitation by the `device_certify` entry of
the platform log (`agent_hub.platform.log`). The detail is the contract of `contracts/audit/invite.use.json`
(`invite_id`, `kind`); the target is the invitation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

import psycopg

from agent_hub.audit.chain import AuditRow, append_audit


def audit_invite_use(
    connection: psycopg.Connection[Any],
    *,
    invite_id: str,
    kind: Literal["user", "device"],
    actor_kind: str,
    actor_id: UUID | None,
    at: datetime,
    org_id: UUID | None = None,
    device_id: UUID | None = None,
) -> None:
    """Append the `invite.use` row of one consumed invitation inside the caller's transaction."""
    append_audit(
        connection,
        AuditRow(
            at=at,
            actor_kind=actor_kind,
            actor_id=actor_id,
            action="invite.use",
            result="ok",
            org_id=org_id,
            device_id=device_id,
            target_kind="invite",
            target_id=invite_id,
            detail={"invite_id": invite_id, "kind": kind},
        ),
    )
