"""Org invitations (03-api.md section 4.2, 04-crypto.md sections 4.6, 4.7 and 5.7, ADR-005).

An admin browser draws a code, appends the `invite` entry and uploads the sealed package in **one** request
(`POST /v1/orgs/{org}/invites {entry, package_ct}`); the hub appends the entry through the log service of the orgs
area (`agent_hub.orgs.append.append_batch`, L1-L8 included) and, in the same transaction, stores the invitation row:
`scope = org`, `kind` and `expires_at` of the entry, `log_seq` = the entry's `seq`, `created_by` = the admin and the
package. Hub rules on top of the log rules: the expiry lies in the future and at most 30 days ahead (03-api.md
section 12; `400 request_invalid` with the pointer of `expires`), the invitation ID is not used by any other
invitation (`409 id_conflict`). Repeating a create with the identical entry (and package) answers the original
`{invite_id, seq}` again; the defaults 72 h (user) and 24 h (device) are chosen by the browser, the hub stores what the
signed entry says.

`invite_revoke` goes through `POST /v1/orgs/{org}/invites/{id}/revoke {entry}`: the entry must name the invitation of
the path; the log rules refuse a used (`invite_consumed`) or revoked (`invite_revoked`) invitation; the row gets
`revoked_at` and its package is deleted. An identical repeat of a stored revocation answers its `seq` again.

The package is also deleted when the invitation is used (the `accept` of the orgs area) and when it expires (job
`invites.expire_packages`, `agent_hub.invites.jobs`). Admins list the org's invitations newest first with the state
`used` > `revoked` > `expired` > `open` (the precedence of the platform invitations, `agent_hub.platform.invites`).
Audit: `invite.create`/`invite.revoke` (metadata only) next to the `log.<type>` rows of the log service.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

import psycopg
import psycopg.errors

from agent_hub.agents.notify import account_change
from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.config import LIMITS
from agent_hub.crypto.encoding import format_time, parse_time
from agent_hub.errors import HubError
from agent_hub.orgs import logs, rights
from agent_hub.orgs.append import Appender, AppendResult, Extra, append_batch
from agent_hub.platform.invites import state_of

Connection = psycopg.Connection[Any]
MAX_LISTED = 1000
EXPIRES_POINTER = "/entry/body/expires"


@dataclass(frozen=True)
class Stored:
    """The answer of a create or revoke and, unless it was a replay, the committed append."""

    invite_id: str
    seq: int
    result: AppendResult | None


def entry_body(entry: Mapping[str, Any], entry_type: str) -> dict[str, Any]:
    """The body of a submitted entry of `entry_type` (`400 request_invalid` with the pointer `/entry` otherwise)."""
    body = entry.get("body")
    if (
        not isinstance(body, dict)
        or body.get("type") != entry_type
        or not isinstance(body.get("invite_id"), str)
        or type(body.get("seq")) is not int
    ):
        raise HubError("request_invalid", detail={"pointers": ["/entry"]})
    return cast(dict[str, Any], body)


def stored_entry(connection: Connection, org_id: UUID, seq: int) -> dict[str, Any] | None:
    """The stored log entry at `seq`, None when there is none."""
    found = logs.entries(connection, org_id, seq - 1, 1)
    return found[0] if found else None


def lock_org(connection: Connection, org_id: UUID) -> None:
    """Lock the org row before the invitation row: the order of every log append (`accept` locks the org, then the
    invitation), so a revoke or create racing an accept waits instead of deadlocking."""
    logs.org_row(connection, org_id, lock=True)


def _audit(
    connection: Connection, action: str, *, user: UUID, org_id: UUID, invite_id: str, kind: str, now: datetime
) -> None:
    append_audit(
        connection,
        AuditRow(
            at=now,
            actor_kind="user",
            actor_id=user,
            action=action,
            result="ok",
            org_id=org_id,
            target_kind="invite",
            target_id=invite_id,
            detail={"invite_id": invite_id, "kind": kind},
        ),
    )


def _append(
    connection: Connection,
    *,
    org_id: UUID,
    user: UUID,
    entry: Mapping[str, Any],
    seq: int,
    now: datetime,
    root_key: bytes,
    extra: Extra,
) -> AppendResult:
    return append_batch(
        connection,
        org_id=org_id,
        appender=Appender(str(user), "user"),
        expected_seq=seq,
        entries=[dict(entry)],
        wraps=[],
        events=[],
        now=now,
        root_key=root_key,
        extra=extra,
    )


def create(
    connection: Connection,
    *,
    org_id: UUID,
    user: UUID,
    entry: Mapping[str, Any],
    package: bytes,
    now: datetime,
    root_key: bytes,
) -> Stored:
    """Append the `invite` entry and store the invitation with its package (module docstring)."""
    body = entry_body(entry, "invite")
    invite_id = cast(str, body["invite_id"])
    rights.require(connection, user, rights.ORG_ADMIN, org_id=org_id, write=True)
    lock_org(connection, org_id)
    existing = connection.execute(
        "SELECT org_id, log_seq, package_ct FROM hub.invite WHERE id = %s FOR UPDATE", (invite_id,)
    ).fetchone()
    if existing is not None:
        other_org, log_seq, package_ct = existing
        same_package = package_ct is None or bytes(package_ct) == package
        if other_org == org_id and same_package and stored_entry(connection, org_id, cast(int, log_seq)) == dict(entry):
            return Stored(invite_id, cast(int, log_seq), None)
        raise HubError("id_conflict")

    def store(connection: Connection, result: AppendResult) -> None:
        expires = parse_time(cast(str, body["expires"]))  # the format was checked by the log rules
        if not now < expires <= now + timedelta(seconds=LIMITS["invite_max_seconds"]):
            raise HubError("request_invalid", detail={"pointers": [EXPIRES_POINTER]})
        try:
            with connection.transaction():
                connection.execute(
                    "INSERT INTO hub.invite (id, scope, kind, org_id, log_seq, created_by, created_at, expires_at,"
                    " package_ct) VALUES (%s, 'org', %s, %s, %s, %s, %s, %s, %s)",
                    (invite_id, body["kind"], org_id, result.head.seq, user, now, expires, package),
                )
        except psycopg.errors.UniqueViolation:  # an invitation with this ID was created meanwhile
            raise HubError("id_conflict") from None
        _audit(connection, "invite.create", user=user, org_id=org_id, invite_id=invite_id, kind=body["kind"], now=now)
        if body.get("role") == "admin":
            # an agent invited an admin: the user is told (KOR-24), live after the commit (`publish_append`)
            result.notices.extend(account_change(connection, user, "invite_created", now, target=org_id))

    result = _append(
        connection, org_id=org_id, user=user, entry=entry, seq=body["seq"], now=now, root_key=root_key, extra=store
    )
    return Stored(invite_id, result.head.seq, result)


def revoke(
    connection: Connection,
    *,
    org_id: UUID,
    user: UUID,
    invite_id: str,
    entry: Mapping[str, Any],
    now: datetime,
    root_key: bytes,
) -> Stored:
    """Append the `invite_revoke` entry, mark the invitation revoked and delete its package."""
    body = entry_body(entry, "invite_revoke")
    if body["invite_id"] != invite_id:
        raise HubError("request_invalid", detail={"pointers": ["/entry/body/invite_id"]})
    rights.require(connection, user, rights.ORG_ADMIN, org_id=org_id, write=True)
    lock_org(connection, org_id)
    row = connection.execute(
        "SELECT kind, revoked_at FROM hub.invite WHERE id = %s AND org_id = %s FOR UPDATE", (invite_id, org_id)
    ).fetchone()
    if row is None:
        raise HubError("not_found")
    kind, revoked_at = row
    if revoked_at is not None:
        for seq, text in connection.execute(
            "SELECT seq, entry_text FROM hub.membership_entry WHERE org_id = %s AND type = 'invite_revoke'"
            " ORDER BY seq",
            (org_id,),
        ).fetchall():
            if json.loads(text) == dict(entry):
                return Stored(invite_id, seq, None)

    def store(connection: Connection, _result: AppendResult) -> None:
        connection.execute("UPDATE hub.invite SET revoked_at = %s, package_ct = NULL WHERE id = %s", (now, invite_id))
        _audit(connection, "invite.revoke", user=user, org_id=org_id, invite_id=invite_id, kind=kind, now=now)

    result = _append(
        connection, org_id=org_id, user=user, entry=entry, seq=body["seq"], now=now, root_key=root_key, extra=store
    )
    return Stored(invite_id, result.head.seq, result)


def _time(value: datetime | None) -> str | None:
    return None if value is None else format_time(value)


def listing(connection: Connection, *, org_id: UUID, user: UUID, now: datetime) -> list[dict[str, Any]]:
    """The org's invitations, newest first (at most 1000)."""
    rights.require(connection, user, rights.ORG_ADMIN, org_id=org_id)
    rows = connection.execute(
        "SELECT id, kind, log_seq, created_by, created_at, expires_at, registered_user, used_at, used_by, revoked_at"
        " FROM hub.invite WHERE org_id = %s ORDER BY created_at DESC, id DESC LIMIT %s",
        (org_id, MAX_LISTED),
    ).fetchall()
    return [
        {
            "invite_id": invite_id,
            "kind": kind,
            "state": state_of(used_at, revoked_at, expires_at, now),
            "log_seq": log_seq,
            "created_by": str(created_by),
            "created_at": format_time(created_at),
            "expires_at": format_time(expires_at),
            "registered": registered_user is not None,
            "used_at": _time(used_at),
            "used_by": None if used_by is None else str(used_by),
            "revoked_at": _time(revoked_at),
        }
        for (
            invite_id,
            kind,
            log_seq,
            created_by,
            created_at,
            expires_at,
            registered_user,
            used_at,
            used_by,
            revoked_at,
        ) in rows
    ]
