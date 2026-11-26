"""Notifications of the support release (ADR-096 section 5, ADR-020): content-free rows like every notification.

- `support.release` (`release_notices`, in the append transaction): for every `support_grant`/`support_revoke` of a
  batch to the org's admins and to the releasing user (`ref` = `grant:<access>` or `revoke`, `wg_id` = the
  workgroup); for a grant also to every active support person (they see the new release in their area);
- `support.access` (`first_access`): the first access of a support person under a release, to the org's admins and
  to the user that released it (`ref` = the person);
- `support.people` (`people_notices`, in the platform-log append transaction): a person joined or left support, to
  the admins of every org with an active release, because the set of readers of its released workgroups changed
  (`ref` = `joined:<user>` / `left:<user>`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg

from agent_hub.notifications.rules import COLUMNS, Notification, new_id, row

Connection = psycopg.Connection[Any]
RELEASE, ACCESS, PEOPLE = "support.release", "support.access", "support.people"
RELEASE_TYPES = frozenset({"support_grant", "support_revoke"})


def _admins(connection: Connection, org: UUID) -> list[UUID]:
    rows = connection.execute(
        "SELECT principal_id FROM hub.org_member WHERE org_id = %s AND role = 'admin' AND removed_seq IS NULL"
        " ORDER BY principal_id",
        (org,),
    ).fetchall()
    return [UUID(str(found[0])) for found in rows]


def _write(
    connection: Connection, recipients: Iterable[UUID], kind: str, at: datetime, *, org: UUID, wg: UUID | None, ref: str
) -> list[Notification]:
    written: list[Notification] = []
    for recipient in sorted(set(recipients), key=str):
        found = connection.execute(
            "INSERT INTO hub.notification (id, recipient_id, kind, org_id, wg_id, task_id, ref, created_at)"
            f" VALUES (%s, %s, %s, %s, %s, NULL, %s, %s) RETURNING {COLUMNS}",
            (new_id(at), recipient, kind, org, wg, ref, at),
        ).fetchone()
        written.append(row(tuple(found or ())))
    return written


def release_notices(
    connection: Connection, org: UUID, bodies: Iterable[Mapping[str, object]], user: UUID, at: datetime
) -> list[Notification]:
    """`support.release` for the release entries of a batch appended by (the user of) `user`."""
    written: list[Notification] = []
    for body in bodies:
        if body["type"] not in RELEASE_TYPES:
            continue
        wg = UUID(str(body["wg"]))
        grant = body["type"] == "support_grant"
        ref = f"grant:{body['access']}" if grant else "revoke"
        written += _write(connection, [*_admins(connection, org), user], RELEASE, at, org=org, wg=wg, ref=ref)
        if grant:
            people = connection.execute("SELECT user_id FROM hub.support_person WHERE active").fetchall()
            written += _write(connection, [UUID(str(p[0])) for p in people], RELEASE, at, org=org, wg=wg, ref=ref)
    return written


def first_access(connection: Connection, org: UUID, wg: UUID, person: UUID, at: datetime) -> list[Notification]:
    """`support.access` for the first access of `person` under the current release of `wg`."""
    releaser = connection.execute(
        "SELECT m.principal_id FROM hub.membership_entry e JOIN hub.support_release r ON r.org_id = e.org_id"
        " AND r.granted_seq = e.seq JOIN hub.org_member m ON m.org_id = e.org_id AND m.principal_id = e.by_id"
        " AND m.removed_seq IS NULL WHERE r.wg_id = %s",
        (wg,),
    ).fetchone()
    recipients = _admins(connection, org) + ([] if releaser is None else [UUID(str(releaser[0]))])
    return _write(connection, recipients, ACCESS, at, org=org, wg=wg, ref=str(person))


def people_notices(
    connection: Connection, joined: Iterable[str], left: Iterable[str], at: datetime
) -> list[Notification]:
    """`support.people` to the admins of every org with an active release."""
    changes = [f"joined:{user}" for user in joined] + [f"left:{user}" for user in left]
    if not changes:
        return []
    orgs = connection.execute("SELECT DISTINCT org_id FROM hub.support_release ORDER BY org_id").fetchall()
    written: list[Notification] = []
    for found in orgs:
        org = UUID(str(found[0]))
        for ref in changes:
            written += _write(connection, _admins(connection, org), PEOPLE, at, org=org, wg=None, ref=ref)
    return written
