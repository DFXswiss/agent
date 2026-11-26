"""The hub's rights API (03-api.md section 3, 04-crypto.md section 4.7): every hub area asks here.

Answers come from the projections of the membership log (`agent_hub.orgs.projection`), which are written in the same
transaction as the log entries, so a check inside an area's transaction sees the log as of that transaction:

- `org_role(org, principal)`: `admin`/`member` for an active user member, `device` for an active device, else None;
- `wg_access(wg, user)`: `read`/`write` of an active user (every member writes the default workgroup);
- `device_assigned(wg, device)`: an active device currently assigned to the workgroup;
- `can(principal, right, org=..., wg=...)`: the rights vocabulary `org:member`, `org:admin`, `wg:read`, `wg:write`
  (not on an archived workgroup), `device:org` and `device:wg`; `task:read`/`task:write` are `wg:read`/`wg:write` on
  the task's workgroup (the tasks area resolves the workgroup);
- `require(...)`: `can` or the refusal of 03-api.md section 1 (reads: `404 not_found` for unknown and forbidden IDs
  alike; writes: `403 forbidden` on a known scope, `404 not_found` on an unknown one).

Decertified platform devices are refused by the signature gate before any right is asked (HUB-04); the projections keep
their assignments until an admin unassigns them (04-crypto.md section 4.7).

Support (ADR-096): an active support person (`agent_hub.support.store`) holds `wg:read`/`task:read` on a workgroup
released to support, and - only where a route asks with `support_write` (prompts, the workgroup whiteboard) -
`wg:write`/`task:write` with a `write` release on a workgroup that is not archived; `org:support` is an active release
of some workgroup of the org (the org's log and its own wraps). Never `org:member`, `org:admin` or a device right. The
first access of a person under a release is recorded and notified once (`note_access`).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from agent_hub.errors import HubError
from agent_hub.support import store as support

Connection = psycopg.Connection[Any]
ORG_MEMBER = "org:member"
ORG_ADMIN = "org:admin"
WG_READ = "wg:read"
WG_WRITE = "wg:write"
TASK_READ = "task:read"
TASK_WRITE = "task:write"
DEVICE_ORG = "device:org"
DEVICE_WG = "device:wg"
ORG_SUPPORT = "org:support"
ORG_RIGHTS = frozenset({ORG_MEMBER, ORG_ADMIN, DEVICE_ORG, ORG_SUPPORT})
WG_RIGHTS = frozenset({WG_READ, WG_WRITE, TASK_READ, TASK_WRITE, DEVICE_WG})
READ, WRITE = "read", "write"


def org_role(connection: Connection, org_id: UUID | str, principal_id: UUID | str) -> str | None:
    """The role of an active member (`admin`, `member`, `device`), None otherwise."""
    row = connection.execute(
        "SELECT role FROM hub.org_member WHERE org_id = %s AND principal_id = %s AND removed_seq IS NULL",
        (org_id, principal_id),
    ).fetchone()
    return None if row is None else str(row[0])


def wg_org(connection: Connection, wg_id: UUID | str) -> UUID | None:
    """The org of a workgroup, None for an unknown workgroup."""
    row = connection.execute("SELECT org_id FROM hub.wg WHERE id = %s", (wg_id,)).fetchone()
    return None if row is None else UUID(str(row[0]))


def wg_access(connection: Connection, wg_id: UUID | str, user_id: UUID | str) -> str | None:
    """`read`/`write` of an active user on the workgroup (archived workgroups stay readable), else None."""
    row = connection.execute(
        "SELECT a.access FROM hub.wg_access a JOIN hub.wg w ON w.id = a.wg_id"
        " JOIN hub.org_member m ON m.org_id = w.org_id AND m.principal_id = a.user_id AND m.removed_seq IS NULL"
        " WHERE a.wg_id = %s AND a.user_id = %s AND a.revoked_seq IS NULL",
        (wg_id, user_id),
    ).fetchone()
    return None if row is None else str(row[0])


def wg_archived(connection: Connection, wg_id: UUID | str) -> bool:
    """The workgroup is archived (False for an unknown workgroup)."""
    row = connection.execute("SELECT archived_seq IS NOT NULL FROM hub.wg WHERE id = %s", (wg_id,)).fetchone()
    return row is not None and bool(row[0])


def device_assigned(connection: Connection, wg_id: UUID | str, device_id: UUID | str) -> bool:
    """An active device member currently assigned to the workgroup."""
    row = connection.execute(
        "SELECT 1 FROM hub.wg_device d JOIN hub.wg w ON w.id = d.wg_id"
        " JOIN hub.org_member m ON m.org_id = w.org_id AND m.principal_id = d.device_id AND m.removed_seq IS NULL"
        " WHERE d.wg_id = %s AND d.device_id = %s AND d.unassigned_seq IS NULL",
        (wg_id, device_id),
    ).fetchone()
    return row is not None


def _org_right(connection: Connection, principal: UUID | str, right: str, org: UUID | str) -> bool:
    if right == ORG_SUPPORT:
        return support.in_org(connection, org, principal)
    role = org_role(connection, org, principal)
    if right == ORG_ADMIN:
        return role == "admin"
    if right == ORG_MEMBER:
        return role in ("admin", "member")
    return role == "device"


def _wg_right(connection: Connection, principal: UUID | str, right: str, wg: UUID | str, support_write: bool) -> bool:
    if right == DEVICE_WG:
        return device_assigned(connection, wg, principal)
    access = wg_access(connection, wg, principal)
    reading = right in (WG_READ, TASK_READ)
    if reading and access is not None:
        return True
    if not reading and access == WRITE:
        return not wg_archived(connection, wg)
    # the support release (ADR-096): a member that is also support writes when either right allows it
    released = support.release_access(connection, wg, principal) if reading or support_write else None
    granted = released is not None and (reading or (released == WRITE and not wg_archived(connection, wg)))
    if granted:
        support.note_access(connection, wg, principal)
    return granted


def can(
    connection: Connection,
    principal_id: UUID | str,
    right: str,
    *,
    org_id: UUID | str | None = None,
    wg_id: UUID | str | None = None,
    support_write: bool = False,
) -> bool:
    """True if the principal holds the right on the org (org rights) or workgroup (workgroup rights); `support_write`:
    a support person with a `write` release holds the write rights too (the route allows support writes)."""
    if right in ORG_RIGHTS and org_id is not None:
        return _org_right(connection, principal_id, right, org_id)
    if right in WG_RIGHTS and wg_id is not None:
        return _wg_right(connection, principal_id, right, wg_id, support_write)
    raise ValueError("unknown right or missing scope")


def _known(connection: Connection, org_id: UUID | str | None, wg_id: UUID | str | None) -> bool:
    if wg_id is not None:
        return wg_org(connection, wg_id) is not None
    row = connection.execute("SELECT 1 FROM hub.org WHERE id = %s", (org_id,)).fetchone()
    return row is not None


def require(
    connection: Connection,
    principal_id: UUID | str,
    right: str,
    *,
    org_id: UUID | str | None = None,
    wg_id: UUID | str | None = None,
    write: bool = False,
    support_write: bool = False,
) -> None:
    """`can`, else `404 not_found` (reads, unknown scopes) or `403 forbidden` (writes on a known scope)."""
    if can(connection, principal_id, right, org_id=org_id, wg_id=wg_id, support_write=support_write):
        return
    if write and _known(connection, org_id, wg_id):
        raise HubError("forbidden")
    raise HubError("not_found")
