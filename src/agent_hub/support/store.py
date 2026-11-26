"""The hub side of the support release (ADR-096; 02-data-model.md section 2.2, 03-api.md section 4.14).

- `sync_people` projects the support roster of the verified platform log (`PlatformState.support_roster`) into
  `support_person` (`active`); it runs in the transaction of every platform-log append and in the rebuild, and returns
  who joined and who left;
- `project_release` writes the difference of the active releases (`OrgState.support`) of one membership-log entry
  into `support_release` (called by `agent_hub.orgs.projection.project_entry`, so a rebuild equals the incremental
  state); a change read <-> write keeps the release's start (`granted_seq`, `granted_at`), so its first access is
  noticed once per release;
- `release_access` / `in_org` answer the rights questions of `agent_hub.orgs.rights` for a support person: an active
  person and an active release of the workgroup (of some workgroup of the org);
- `note_access` records the first access of a person per release and notifies the organization once;
- `owed` gives the rotations a release owes after a person left support: a stored wrap of the **current** epoch of a
  released workgroup to a recipient that is neither a reader of the org log nor in the roster (derived from the
  stored data, never a flag); `mark_pending` adds them to `org.rotation_pending` of every org with a release after a
  platform-log append (`agent_hub.orgs.projection.update_org` adds them on every org append);
- `drop_wraps` deletes the stored wraps of a workgroup to recipients outside the org log (support persons) when its
  release ends.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any, cast
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from agent_hub.log.platform import PlatformState
from agent_hub.log.rotation import as_json
from agent_hub.log.state import WG, OrgState, Roster, Scope

Connection = psycopg.Connection[Any]
ACCESS_KIND = "support.access"


def roster(platform: PlatformState | None) -> Roster:
    """The support roster of the verified platform state (empty without one)."""
    return {} if platform is None else platform.support_roster()


def sync_people(connection: Connection, platform: PlatformState | None, at: datetime) -> tuple[list[str], list[str]]:
    """Project the roster into `support_person`; `(joined, left)` user IDs, sorted."""
    people = set(roster(platform))
    rows = connection.execute("SELECT user_id, active FROM hub.support_person").fetchall()
    known = {str(user): bool(active) for user, active in rows}
    joined = sorted(user for user in people if not known.get(user, False))
    left = sorted(user for user, active in known.items() if active and user not in people)
    for user in joined:
        connection.execute(
            "INSERT INTO hub.support_person (user_id, active, changed_at) VALUES (%s, true, %s)"
            " ON CONFLICT (user_id) DO UPDATE SET active = true, changed_at = EXCLUDED.changed_at",
            (user, at),
        )
    for user in left:
        connection.execute(
            "UPDATE hub.support_person SET active = false, changed_at = %s WHERE user_id = %s", (at, user)
        )
    return joined, left


def project_release(connection: Connection, org: UUID, before: OrgState | None, after: OrgState, at: datetime) -> None:
    """The change of the active releases by one entry (received at `at`)."""
    old = {} if before is None else before.support
    for wg in sorted(set(old) | set(after.support)):
        new = after.support.get(wg)
        if old.get(wg) == new:
            continue
        if new is None:
            connection.execute("DELETE FROM hub.support_release WHERE wg_id = %s", (wg,))
        elif wg in old:
            # read <-> write: the same release (its start and its first-access notices stay)
            connection.execute("UPDATE hub.support_release SET access = %s WHERE wg_id = %s", (new[0], wg))
        else:
            connection.execute(
                "INSERT INTO hub.support_release (wg_id, org_id, access, granted_seq, granted_at)"
                " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (wg_id) DO UPDATE SET access = EXCLUDED.access,"
                " granted_seq = EXCLUDED.granted_seq, granted_at = EXCLUDED.granted_at",
                (wg, org, new[0], new[1], at),
            )


def clear_releases(connection: Connection, org: UUID) -> None:
    """Delete the release rows of the org (before a rebuild)."""
    connection.execute("DELETE FROM hub.support_release WHERE org_id = %s", (org,))


def is_person(connection: Connection, user: UUID | str) -> bool:
    """An active support person."""
    row = connection.execute("SELECT 1 FROM hub.support_person WHERE user_id = %s AND active", (user,)).fetchone()
    return row is not None


def release_access(connection: Connection, wg: UUID | str, user: UUID | str) -> str | None:
    """`read`/`write` of the release of the workgroup for an active support person, else None."""
    row = connection.execute(
        "SELECT r.access FROM hub.support_release r JOIN hub.support_person p ON p.user_id = %s AND p.active"
        " WHERE r.wg_id = %s",
        (user, wg),
    ).fetchone()
    return None if row is None else str(row[0])


def in_org(connection: Connection, org: UUID | str, user: UUID | str) -> bool:
    """An active support person and an active release of some workgroup of the org."""
    row = connection.execute(
        "SELECT 1 FROM hub.support_release r JOIN hub.support_person p ON p.user_id = %s AND p.active"
        " WHERE r.org_id = %s LIMIT 1",
        (user, org),
    ).fetchone()
    return row is not None


def note_access(connection: Connection, wg: UUID | str, user: UUID | str) -> None:
    """The first access of the person under the current release of the workgroup: one `support_access` row and one
    notification `support.access` to the org's admins (ADR-096 section 5); later accesses change nothing."""
    row = connection.execute(
        "INSERT INTO hub.support_access (wg_id, user_id, granted_seq, first_at)"
        " SELECT r.wg_id, %s, r.granted_seq, now() FROM hub.support_release r WHERE r.wg_id = %s"
        " ON CONFLICT DO NOTHING RETURNING first_at, (SELECT org_id FROM hub.support_release WHERE wg_id = %s)",
        (user, wg, wg),
    ).fetchone()
    if row is None:
        return
    from agent_hub.support import notify  # noqa: PLC0415 - rights -> notify -> rules would import in a cycle

    notify.first_access(connection, UUID(str(row[1])), UUID(str(wg)), UUID(str(user)), cast(datetime, row[0]))


def released(connection: Connection, org: UUID | str) -> list[tuple[str, str]]:
    """`(wg, access)` of the org's active releases, sorted."""
    rows = connection.execute(
        "SELECT wg_id, access FROM hub.support_release WHERE org_id = %s ORDER BY wg_id", (org,)
    ).fetchall()
    return [(str(wg), str(access)) for wg, access in rows]


def owed(connection: Connection, state: OrgState, people: Roster) -> list[Scope]:
    """The released workgroups whose current epoch is wrapped to a recipient outside the log's readers and the
    roster (a person that left support), sorted."""
    found: list[Scope] = []
    for wg in sorted(state.support):
        readers = set(state.readers(WG, wg))
        rows = connection.execute(
            "SELECT DISTINCT recipient_id FROM hub.key_wrap WHERE scope = 'wg' AND scope_id = %s AND epoch = %s",
            (wg, state.epoch(WG, wg)),
        ).fetchall()
        if any(str(r[0]) not in readers and str(r[0]) not in people for r in rows):
            found.append((WG, wg))
    return found


def merge_pending(connection: Connection, org: UUID, scopes: Iterable[Scope]) -> None:
    """Add scopes to `org.rotation_pending` (under the org row lock)."""
    row = connection.execute("SELECT rotation_pending FROM hub.org WHERE id = %s FOR UPDATE", (org,)).fetchone()
    stored = {(str(item["scope"]), str(item["scope_id"])) for item in (row[0] if row is not None else [])}
    merged = stored | set(scopes)
    if merged != stored:
        connection.execute(
            "UPDATE hub.org SET rotation_pending = %s WHERE id = %s", (Jsonb(as_json(frozenset(merged))), org)
        )


def drop_wraps(connection: Connection, state: OrgState, wg: str) -> None:
    """Delete the wraps of the workgroup stored for recipients outside the org log (support persons)."""
    connection.execute(
        "DELETE FROM hub.key_wrap WHERE scope = 'wg' AND scope_id = %s AND NOT (recipient_id = ANY(%s::uuid[]))",
        (wg, sorted(state.principals)),
    )
