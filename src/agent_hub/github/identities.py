"""Visibility, usability, defaults and the feature gate of GitHub identities at the hub (ADR-076; 03-api.md 4.8).

An identity is a `github` credential record (`agent_hub.credentials.records`), named by its record ID. The hub never
sees a login or token; it knows owner, owner kind, the primary mark and which record a task, session, default or
operation names.

- **Visible** to a user (`GET /v1/github-identities/{id}`, the list): an own record, or a company record (owner an
  organization) of an organization in which the user is an active member (admin or member). Anything else is
  `404 not_found`, so the existence of another person's record does not leak. A member may read the ciphertext of a
  company record; only the organization's devices hold its key (KM-35).
- **Usable** by a human for a task choice or an operation (the hub's lenient form of ADR-076 section 2): an own
  record of the human or a company record of an organization in which the human is an active member. The runner
  applies the strict rule (own, the task's organization or the operator's organization) from its verified logs.
- **Defaults** (`github_default`): one per organization or workgroup, always a company record of that very
  organization (`403 identity_forbidden` otherwise); set by an active admin of the organization or, for a workgroup,
  by a writer of it (`403 forbidden` otherwise; `404 not_found` for an unknown scope). A default goes away with its
  record (foreign key). Every change appends `github.default` (target the organization or workgroup).
- **Feature gate** (ADR-076 section 3, "Old runners"): a non-null task choice, session choice or default is refused
  with `409 peer_outdated` (`detail.devices`) while an active device assigned to a workgroup of the organization has
  not announced `github.identities` (`agent_hub.device.features`); clearing is always allowed.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg

from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.audit.recorder import unchanged
from agent_hub.credentials.eligibility import org_device_ids
from agent_hub.credentials.records import IDENTITY_COLUMNS, ORG, USER, Identity, identity_of, load_identity
from agent_hub.crypto.encoding import format_time
from agent_hub.device.features import GITHUB_IDENTITIES, outdated_devices
from agent_hub.errors import HubError
from agent_hub.orgs import rights

Connection = psycopg.Connection[Any]
SCOPES = frozenset({"org", "wg"})
MEMBER_ROLES = frozenset({"admin", "member"})
MAX_DETAIL_DEVICES = 64
_MEMBER_ORGS = (
    "SELECT org_id FROM hub.org_member WHERE principal_id = %s AND removed_seq IS NULL AND role IN ('admin', 'member')"
)


def is_member(connection: Connection, org_id: UUID | str, user: UUID | str) -> bool:
    """The user is an active member (admin or member) of the organization."""
    return rights.org_role(connection, org_id, user) in MEMBER_ROLES


def visible(connection: Connection, identity: Identity, user: UUID) -> bool:
    """An own record or a company record of an organization in which the user is an active member."""
    if identity.owner.kind == USER:
        return identity.owner.id == user
    return is_member(connection, identity.owner.id, user)


def find_visible(connection: Connection, record_id: UUID, user: UUID) -> Identity:
    """The identity if the user may see it, `404 not_found` otherwise."""
    identity = load_identity(connection, record_id)
    if identity is None or not visible(connection, identity, user):
        raise HubError("not_found")
    return identity


def usable_by(connection: Connection, identity_id: UUID | str, human: UUID | str) -> bool:
    """The lenient hub rule of ADR-076 section 2 (module docstring)."""
    row = connection.execute(
        "SELECT owner_id, owner_kind FROM hub.credential WHERE id = %s AND kind = 'github'", (identity_id,)
    ).fetchone()
    if row is None:
        return False
    if row[1] == USER:
        return str(row[0]) == str(human)
    return is_member(connection, row[0], human)


def require_usable(connection: Connection, identity_id: UUID, human: UUID) -> None:
    """`403 identity_forbidden` unless the human may use the identity."""
    if not usable_by(connection, identity_id, human):
        raise HubError("identity_forbidden")


def require_feature(connection: Connection, org_id: UUID | str, root_key: bytes) -> None:
    """The feature gate (module docstring): `409 peer_outdated` with the outdated devices."""
    devices = [str(device) for device in org_device_ids(connection, org_id, root_key)]
    outdated = outdated_devices(connection, devices, GITHUB_IDENTITIES)
    if outdated:
        raise HubError("peer_outdated", detail={"devices": outdated[:MAX_DETAIL_DEVICES]})


def _default_json(row: Iterable[Any]) -> dict[str, Any]:
    scope, scope_id, identity_id, set_by, set_at = row
    return {
        "scope": scope,
        "scope_id": str(scope_id),
        "identity_id": str(identity_id),
        "set_by": str(set_by),
        "set_at": format_time(set_at),
    }


def list_for(connection: Connection, user: UUID) -> dict[str, Any]:
    """`GET /v1/me/github-identities`: the own records and the company records of the user's organizations (newest
    change first, then ID descending), the defaults of those organizations and of their workgroups the user reads
    (sorted by scope and scope ID)."""
    orgs = [row[0] for row in connection.execute(_MEMBER_ORGS, (user,))]
    rows = connection.execute(
        f"SELECT {IDENTITY_COLUMNS} FROM hub.credential WHERE kind = 'github'"
        " AND ((owner_kind = 'user' AND owner_id = %s) OR (owner_kind = 'org' AND owner_id = ANY(%s::uuid[])))"
        " ORDER BY updated_at DESC, id DESC",
        (user, orgs),
    ).fetchall()
    defaults = connection.execute(
        "SELECT g.scope, g.scope_id, g.identity_id, g.set_by, g.set_at FROM hub.github_default g"
        " WHERE (g.scope = 'org' AND g.scope_id = ANY(%(orgs)s::uuid[])) OR (g.scope = 'wg' AND g.scope_id IN ("
        " SELECT a.wg_id FROM hub.wg_access a JOIN hub.wg w ON w.id = a.wg_id"
        " WHERE a.user_id = %(user)s AND a.revoked_seq IS NULL AND w.org_id = ANY(%(orgs)s::uuid[])))"
        " ORDER BY g.scope, g.scope_id",
        {"orgs": orgs, "user": user},
    ).fetchall()
    return {
        "identities": [identity_of(row).meta() for row in rows],
        "defaults": [_default_json(row) for row in defaults],
    }


def _scope_org(connection: Connection, scope: str, scope_id: UUID, user: UUID) -> UUID:
    """The organization of a default's scope if the user may set it (module docstring)."""
    if scope == ORG:
        rights.require(connection, user, rights.ORG_ADMIN, org_id=scope_id, write=True)
        return scope_id
    org = rights.wg_org(connection, scope_id)
    if org is None:
        raise HubError("not_found")
    if not (
        rights.can(connection, user, rights.ORG_ADMIN, org_id=org)
        or rights.can(connection, user, rights.WG_WRITE, wg_id=scope_id)
    ):
        raise HubError("forbidden")
    return org


def set_default(
    connection: Connection,
    *,
    scope: str,
    scope_id: UUID,
    identity_id: UUID | None,
    user: UUID,
    now: datetime,
    root_key: bytes,
) -> UUID | None:
    """`PUT /v1/github-defaults/{scope}/{scope_id}` (module docstring); the organization when something changed,
    None for an unchanged default (an idempotent repeat)."""
    org = _scope_org(connection, scope, scope_id, user)
    if identity_id is not None:
        owner = connection.execute(
            "SELECT owner_id FROM hub.credential WHERE id = %s AND kind = 'github' AND owner_kind = 'org'",
            (identity_id,),
        ).fetchone()
        if owner is None or owner[0] != org:
            raise HubError("identity_forbidden")
        require_feature(connection, org, root_key)
    current = connection.execute(
        "SELECT identity_id FROM hub.github_default WHERE scope = %s AND scope_id = %s FOR UPDATE", (scope, scope_id)
    ).fetchone()
    if (None if current is None else current[0]) == identity_id:
        unchanged()
        return None
    if identity_id is None:
        connection.execute("DELETE FROM hub.github_default WHERE scope = %s AND scope_id = %s", (scope, scope_id))
    else:
        connection.execute(
            "INSERT INTO hub.github_default (scope, scope_id, identity_id, set_by, set_at) VALUES (%s, %s, %s, %s, %s)"
            " ON CONFLICT (scope, scope_id) DO UPDATE SET identity_id = EXCLUDED.identity_id,"
            " set_by = EXCLUDED.set_by, set_at = EXCLUDED.set_at",
            (scope, scope_id, identity_id, user, now),
        )
    append_audit(
        connection,
        AuditRow(
            at=now,
            actor_kind="user",
            actor_id=user,
            action="github.default",
            result="ok",
            org_id=org,
            wg_id=scope_id if scope == "wg" else None,
            target_kind="org" if scope == ORG else "workgroup",
            target_id=str(scope_id),
            detail={"scope": scope, "identity_id": None if identity_id is None else str(identity_id)},
        ),
    )
    return org


def has_default(connection: Connection, org_id: UUID, wg_id: UUID) -> bool:
    """A default of the workgroup or of its organization exists."""
    row = connection.execute(
        "SELECT 1 FROM hub.github_default WHERE (scope = 'org' AND scope_id = %s) OR (scope = 'wg' AND scope_id = %s)",
        (org_id, wg_id),
    ).fetchone()
    return row is not None


def device_defaults(connection: Connection, assigned: Iterable[tuple[str, str]]) -> list[dict[str, str]]:
    """The defaults of the organizations and workgroups of the `(org, wg)` assignments, sorted by scope and ID."""
    pairs = list(assigned)
    rows = connection.execute(
        "SELECT scope, scope_id, identity_id FROM hub.github_default"
        " WHERE (scope = 'org' AND scope_id = ANY(%s::uuid[])) OR (scope = 'wg' AND scope_id = ANY(%s::uuid[]))"
        " ORDER BY scope, scope_id",
        (sorted({org for org, _wg in pairs}), sorted({wg for _org, wg in pairs})),
    ).fetchall()
    return [
        {"scope": scope, "scope_id": str(scope_id), "identity_id": str(identity)} for scope, scope_id, identity in rows
    ]
