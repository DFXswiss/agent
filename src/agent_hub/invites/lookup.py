"""Anonymous invitation lookup (`POST /v1/invites/lookup`, 03-api.md section 4.1, ADR-005).

Whoever holds a code derives `invite_id` and asks the hub what it belongs to. A platform invitation answers
`{kind, scope:"platform", expires_at, registered}`; an org invitation additionally `org_id`, the sealed package
`package_ct`, the current membership-log head `log_head` and the whole membership log up to that head (`log`), so that
the invitee can verify the log before it is a member (04-crypto.md sections 4.6 and 5.2). An unknown, used, revoked or
expired invitation, and an org invitation whose package is gone, all answer `404 invite_unknown` (one code, no
oracle). The route is anonymous and rate-limited by the request pipeline (30 per minute and source address). Every
answered lookup is audited as `invite.lookup` (metadata only).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg

from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.crypto.encoding import b64u_encode, format_time
from agent_hub.errors import HubError


def _org_part(connection: psycopg.Connection[Any], org: UUID) -> dict[str, object]:
    head = connection.execute("SELECT log_head_seq, log_head_hash FROM hub.org WHERE id = %s", (org,)).fetchone()
    if head is None:
        raise HubError("invite_unknown")
    seq, digest = int(head[0]), str(head[1])
    entries = connection.execute(
        "SELECT entry_text FROM hub.membership_entry WHERE org_id = %s AND seq <= %s ORDER BY seq", (org, seq)
    ).fetchall()
    if len(entries) != seq + 1:
        raise HubError("invite_unknown")
    return {"log_head": {"seq": seq, "hash": digest}, "log": [json.loads(text) for (text,) in entries]}


def lookup(connection: psycopg.Connection[Any], invite_id: str, now: datetime) -> dict[str, object]:
    """The lookup answer, or `invite_unknown`."""
    row = connection.execute(
        "SELECT scope, kind, org_id, expires_at, registered_user, used_at, revoked_at, package_ct FROM hub.invite"
        " WHERE id = %s",
        (invite_id,),
    ).fetchone()
    if row is None:
        raise HubError("invite_unknown")
    scope, kind, org, expires_at, registered_user, used_at, revoked_at, package_ct = row
    if used_at is not None or revoked_at is not None or expires_at <= now:
        raise HubError("invite_unknown")
    answer: dict[str, object] = {
        "kind": kind,
        "scope": scope,
        "expires_at": format_time(expires_at),
        "registered": registered_user is not None,
    }
    if scope == "org":
        if package_ct is None:
            raise HubError("invite_unknown")
        answer["org_id"] = str(org)
        answer["package_ct"] = b64u_encode(bytes(package_ct))
        answer.update(_org_part(connection, org))
    append_audit(
        connection,
        AuditRow(
            at=now,
            actor_kind="anonymous",
            action="invite.lookup",
            result="ok",
            org_id=org,
            target_kind="invite",
            target_id=invite_id,
            detail={"invite_id": invite_id, "kind": kind},
        ),
    )
    return answer
