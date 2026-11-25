"""Profile definitions, assignments and the effective profile of a session (ADR-090 sections 1-3).

- **Definitions**: the built-in roles (`model.BUILTIN`) and the own profiles of an organization or a workgroup
  (`hub.agent_profile`). A name resolves in the order workgroup, organization, built-in; the built-in names are
  reserved, so an own profile never shadows a role.
- **Assignments** are snapshots: `task.agent_profile` for the task's main sessions, `session.agent_profile` for a child
  that was started with a profile of its own (NULL: the child takes what its parent has).
- **Effective profile** of a session: the intersection of the task's snapshot and every snapshot along the session's
  chain of parents (itself included): the operation types all of them allow and the narrowest reach, named after the
  innermost snapshot. A restriction set on the task or on any ancestor therefore applies at once to every session
  below it, also to children started before it was set; no session ever has more than an ancestor. Without any
  snapshot: None, every right (A1, E-11).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from agent_hub.crypto.encoding import format_time
from agent_hub.errors import HubError
from agent_hub.profiles.model import (
    BUILTIN,
    NAME_RE,
    REACHES,
    SCOPE_BUILTIN,
    SCOPE_ORG,
    SCOPE_WG,
    Profile,
    from_json,
)

Connection = psycopg.Connection[Any]


@dataclass(frozen=True)
class Definition:
    """A profile a name resolves to in a workgroup, with where it is defined."""

    scope: str
    profile: Profile
    updated_at: datetime | None


def intersect(profiles: Iterable[Profile]) -> Profile | None:
    """The intersection of `profiles` (innermost first), None for none."""
    found = list(profiles)
    if not found:
        return None
    operations = frozenset.intersection(*(item.operations for item in found))
    reads = REACHES[min(REACHES.index(item.reads) for item in found)]
    return Profile(found[0].name, operations, reads)


def definitions(connection: Connection, org_id: UUID | str, wg_id: UUID | str) -> dict[str, Definition]:
    """Every profile name usable in the workgroup with what it resolves to (module docstring)."""
    found: dict[str, Definition] = {name: Definition(SCOPE_BUILTIN, profile, None) for name, profile in BUILTIN.items()}
    rows = connection.execute(
        "SELECT scope, name, operations, reads, updated_at FROM hub.agent_profile"
        " WHERE (scope = 'org' AND scope_id = %s) OR (scope = 'wg' AND scope_id = %s)"
        " ORDER BY scope = 'wg', name",
        (org_id, wg_id),
    ).fetchall()
    for scope, name, operations, reads, updated_at in rows:
        found[name] = Definition(scope, Profile(name, frozenset(operations), reads), updated_at)
    return found


def resolve(connection: Connection, org_id: UUID | str, wg_id: UUID | str, name: str) -> Profile | None:
    """The profile `name` resolves to in the workgroup, None for an unknown name."""
    found = definitions(connection, org_id, wg_id).get(name)
    return None if found is None else found.profile


def task_profile(connection: Connection, task_id: UUID | str) -> Profile | None:
    row = connection.execute("SELECT agent_profile FROM hub.task WHERE id = %s", (task_id,)).fetchone()
    return None if row is None else from_json(row[0])


def effective(connection: Connection, session_id: UUID | str) -> Profile | None:
    """The effective profile of a session (module docstring)."""
    rows = connection.execute(
        "WITH RECURSIVE chain AS ("
        " SELECT id, parent_session_id, task_id, agent_profile, 0 AS up FROM hub.session WHERE id = %s"
        " UNION ALL SELECT p.id, p.parent_session_id, p.task_id, p.agent_profile, c.up + 1"
        " FROM hub.session p JOIN chain c ON p.id = c.parent_session_id)"
        " SELECT c.agent_profile, t.agent_profile FROM chain c JOIN hub.task t ON t.id = c.task_id ORDER BY c.up",
        (session_id,),
    ).fetchall()
    snapshots: list[Mapping[str, Any]] = [row[0] for row in rows if row[0] is not None]
    if rows and rows[0][1] is not None:
        snapshots.append(rows[0][1])
    return intersect(profile for profile in map(from_json, snapshots) if profile is not None)


def check_name(name: str) -> None:
    """`404 not_found` for a name that cannot be a profile's."""
    if NAME_RE.fullmatch(name) is None:
        raise HubError("not_found")


def put_definition(
    connection: Connection,
    *,
    scope: str,
    scope_id: UUID,
    org_id: UUID,
    profile: Profile,
    user: UUID,
    now: datetime,
) -> bool:
    """Create or change an own profile; False when it was unchanged. A built-in name is `400 request_invalid`."""
    if profile.name in BUILTIN:
        raise HubError("request_invalid", detail={"pointers": ["/name"]})
    row = connection.execute(
        "SELECT operations, reads FROM hub.agent_profile WHERE scope = %s AND scope_id = %s AND name = %s FOR UPDATE",
        (scope, scope_id, profile.name),
    ).fetchone()
    operations = sorted(profile.operations)
    if row is not None and (list(row[0]), row[1]) == (operations, profile.reads):
        return False
    connection.execute(
        "INSERT INTO hub.agent_profile (scope, scope_id, org_id, name, operations, reads, updated_by, updated_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (scope, scope_id, name) DO UPDATE SET"
        " operations = EXCLUDED.operations, reads = EXCLUDED.reads, updated_by = EXCLUDED.updated_by,"
        " updated_at = EXCLUDED.updated_at",
        (scope, scope_id, org_id, profile.name, operations, profile.reads, user, now),
    )
    return True


def delete_definition(connection: Connection, *, scope: str, scope_id: UUID, name: str) -> bool:
    """Delete an own profile; False when there was none. Assignments keep their snapshot."""
    row = connection.execute(
        "DELETE FROM hub.agent_profile WHERE scope = %s AND scope_id = %s AND name = %s RETURNING name",
        (scope, scope_id, name),
    ).fetchone()
    return row is not None


def snapshot(profile: Profile | None) -> Jsonb | None:
    """The stored form of an assignment."""
    return None if profile is None else Jsonb(profile.as_json())


def listing(connection: Connection, org_id: UUID, wg_id: UUID) -> list[dict[str, Any]]:
    """`profiles` of `GET /v1/workgroups/{wg}/agent-profiles`: every usable profile, by name."""
    return [
        {
            **found.profile.as_json(),
            "scope": found.scope,
            "updated_at": None if found.updated_at is None else format_time(found.updated_at),
        }
        for _name, found in sorted(definitions(connection, org_id, wg_id).items())
    ]


__all__ = [
    "SCOPE_ORG",
    "SCOPE_WG",
    "Definition",
    "check_name",
    "definitions",
    "delete_definition",
    "effective",
    "intersect",
    "listing",
    "put_definition",
    "resolve",
    "snapshot",
    "task_profile",
]
