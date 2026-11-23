"""The rights projections of the membership log (02-data-model.md sections 1.3 and 2.2).

`project_entry` writes the difference between the rights state before and after one entry into the projection tables:

- `org_member`: a principal that becomes active (join, rejoin, a platform device at its first `device_assign`) gets
  `joined_seq` and no `removed_seq`; `remove` sets `removed_seq`; `role` changes the role;
- `wg`: `created_seq` at genesis/`wg_create`, `archived_seq` at `wg_archive`;
- `wg_access`: the **effective** access of every active user (explicit access plus `write` on the default workgroup for
  every member, ADR-024 addendum: not reducible, plus `read` on every other workgroup for every admin without explicit
  access, marked `inherited`, ADR-058); a new or changed access (level or `inherited`) sets `granted_seq` and clears
  `revoked_seq`, a lost access sets `revoked_seq` (ungrant, remove, demotion);
- `wg_device`: the assignments of active devices with `assigned_seq`/`unassigned_seq`;
- `scope_epoch`: every new epoch of an `org`/`wg` scope with `created_seq` and the receipt time.

The incremental path (`agent_hub.orgs.append`) and the rebuild (`rebuild_org`) both fold `project_entry` over the
entries, so a rebuild equals the incremental state by construction (and by test). `org` itself carries the head,
`platform_access` (from the platform log, ADR-026) and `rotation_pending` (`pending_rotation`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from agent_hub.log import rotation
from agent_hub.log.platform import PlatformState
from agent_hub.log.state import USER, OrgState, Scope
from agent_hub.log.verify import VerifiedLog

Connection = psycopg.Connection[Any]


def effective_access(state: OrgState | None) -> dict[tuple[str, str], tuple[str, bool]]:
    """`(user, wg) -> (read|write, inherited)` of every active user, including the default workgroup and the
    inherited read of admins (`OrgState.wg_access`)."""
    if state is None:
        return {}
    found: dict[tuple[str, str], tuple[str, bool]] = {}
    for principal in state.principals.values():
        if principal.active and principal.kind == USER:
            for wg in state.workgroups:
                level = state.wg_access(principal.id, wg)
                if level is not None:
                    found[(principal.id, wg)] = (level, state.inherited(principal.id, wg))
    return found


def effective_assignments(state: OrgState | None) -> set[tuple[str, str]]:
    """`(device, wg)` of every active device."""
    if state is None:
        return set()
    return {key for key in state.assignments if state.member(key[0]) is not None}


def _members(connection: Connection, org: UUID, before: OrgState | None, after: OrgState, seq: int) -> None:
    for principal in after.principals.values():
        old = None if before is None else before.principals.get(principal.id)
        if principal.active and (old is None or not old.active):
            connection.execute(
                "INSERT INTO hub.org_member (org_id, principal_id, kind, role, joined_seq, removed_seq)"
                " VALUES (%s, %s, %s, %s, %s, NULL) ON CONFLICT (org_id, principal_id) DO UPDATE"
                " SET kind = EXCLUDED.kind, role = EXCLUDED.role, joined_seq = EXCLUDED.joined_seq, removed_seq = NULL",
                (org, principal.id, principal.kind, principal.role, seq),
            )
        elif old is not None and old.active and not principal.active:
            connection.execute(
                "UPDATE hub.org_member SET removed_seq = %s WHERE org_id = %s AND principal_id = %s",
                (seq, org, principal.id),
            )
        elif old is not None and old.role != principal.role:
            connection.execute(
                "UPDATE hub.org_member SET role = %s WHERE org_id = %s AND principal_id = %s",
                (principal.role, org, principal.id),
            )


def _workgroups(connection: Connection, org: UUID, before: OrgState | None, after: OrgState, seq: int) -> None:
    for wg in after.workgroups.values():
        old = None if before is None else before.workgroups.get(wg.id)
        if old is None:
            connection.execute(
                "INSERT INTO hub.wg (id, org_id, is_default, created_seq, archived_seq) VALUES (%s, %s, %s, %s, NULL)",
                (wg.id, org, wg.default, seq),
            )
        elif old.archived_seq is None and wg.archived_seq is not None:
            connection.execute("UPDATE hub.wg SET archived_seq = %s WHERE id = %s", (seq, wg.id))


def _access(connection: Connection, before: OrgState | None, after: OrgState, seq: int) -> None:
    old, new = effective_access(before), effective_access(after)
    for (user, wg), (level, inherited) in sorted(new.items()):
        if old.get((user, wg)) != (level, inherited):
            connection.execute(
                "INSERT INTO hub.wg_access (wg_id, user_id, access, inherited, granted_seq, revoked_seq)"
                " VALUES (%s, %s, %s, %s, %s, NULL) ON CONFLICT (wg_id, user_id) DO UPDATE"
                " SET access = EXCLUDED.access, inherited = EXCLUDED.inherited, granted_seq = EXCLUDED.granted_seq,"
                " revoked_seq = NULL",
                (wg, user, level, inherited, seq),
            )
    for user, wg in sorted(set(old) - set(new)):
        connection.execute(
            "UPDATE hub.wg_access SET revoked_seq = %s WHERE wg_id = %s AND user_id = %s", (seq, wg, user)
        )


def _devices(connection: Connection, before: OrgState | None, after: OrgState, seq: int) -> None:
    old, new = effective_assignments(before), effective_assignments(after)
    for device, wg in sorted(new - old):
        connection.execute(
            "INSERT INTO hub.wg_device (wg_id, device_id, assigned_seq, unassigned_seq) VALUES (%s, %s, %s, NULL)"
            " ON CONFLICT (wg_id, device_id) DO UPDATE SET assigned_seq = EXCLUDED.assigned_seq, unassigned_seq = NULL",
            (wg, device, seq),
        )
    for device, wg in sorted(old - new):
        connection.execute(
            "UPDATE hub.wg_device SET unassigned_seq = %s WHERE wg_id = %s AND device_id = %s", (seq, wg, device)
        )


def _epochs(connection: Connection, before: OrgState | None, after: OrgState, seq: int, at: datetime) -> None:
    for (scope, scope_id), epoch in sorted(after.epochs.items()):
        if before is None or before.epoch(scope, scope_id) != epoch:
            connection.execute(
                "INSERT INTO hub.scope_epoch (scope, scope_id, epoch, created_at, created_seq)"
                " VALUES (%s, %s, %s, %s, %s)",
                (scope, scope_id, epoch, at, seq),
            )


def project_entry(
    connection: Connection, org: UUID, before: OrgState | None, after: OrgState, *, seq: int, at: datetime
) -> None:
    """Write the projection changes of the entry `seq` (received at `at`)."""
    _members(connection, org, before, after, seq)
    _workgroups(connection, org, before, after, seq)
    _access(connection, before, after, seq)
    _devices(connection, before, after, seq)
    _epochs(connection, before, after, seq, at)


def pending_rotation(state: OrgState, platform: PlatformState | None) -> list[Scope]:
    """`org.rotation_pending`: the scopes still owed a rotation, sorted.

    These are the scopes a member that left by itself could read (`OrgState.pending`, cleared by the `rotate`) and,
    for every platform device of the org whose certification has ended but that is still assigned, the rotate set R of
    unassigning it everywhere (04-crypto.md section 4.7: the next admin batch is `device_unassign` + R).
    """
    owed = set(state.pending)
    excluded = set() if platform is None else platform.excluded(state)
    for device in sorted(excluded):
        if state.device_wgs(device):
            unassigned = replace(
                state, assignments={key: seq for key, seq in state.assignments.items() if key[0] != device}
            )
            owed |= rotation.required_set(state, unassigned, device)
    return sorted(owed)


def pending_json(scopes: list[Scope]) -> Jsonb:
    """`rotation_pending` as stored (`[{scope, scope_id}]`)."""
    return Jsonb(rotation.as_json(frozenset(scopes)))


def update_org(
    connection: Connection, org: UUID, log: VerifiedLog, platform: PlatformState | None, platform_access: bool
) -> None:
    """Head, platform access and pending rotations of the org after its log changed."""
    connection.execute(
        "UPDATE hub.org SET log_head_seq = %s, log_head_hash = %s, platform_access = %s, rotation_pending = %s"
        " WHERE id = %s",
        (log.head.seq, log.head.hash, platform_access, pending_json(pending_rotation(log.state, platform)), org),
    )


def clear_org(connection: Connection, org: UUID, wgs: Iterable[str] = ()) -> None:
    """Delete every projection row of the org (before a rebuild). The rows keyed by workgroup are found through the
    org's `wg` rows and through `wgs`, the workgroups of the source (the verified log): a rebuild must also clear them
    when `wg` rows are missing or name another org (a damaged table, NA-08), or the rebuilt rows would collide with the
    stale ones."""
    ids = [UUID(wg) for wg in wgs]
    scoped = "(SELECT id FROM hub.wg WHERE org_id = %s UNION SELECT unnest(%s::uuid[]))"
    connection.execute(
        "DELETE FROM hub.scope_epoch WHERE (scope = 'org' AND scope_id = %s)"
        f" OR (scope = 'wg' AND scope_id IN {scoped})",
        (org, org, ids),
    )
    connection.execute(f"DELETE FROM hub.wg_access WHERE wg_id IN {scoped}", (org, ids))
    connection.execute(f"DELETE FROM hub.wg_device WHERE wg_id IN {scoped}", (org, ids))
    connection.execute("DELETE FROM hub.wg WHERE org_id = %s OR id = ANY(%s::uuid[])", (org, ids))
    connection.execute("DELETE FROM hub.org_member WHERE org_id = %s", (org,))


def project_log(
    connection: Connection, org: UUID, log: VerifiedLog, received: Mapping[int, datetime], first: int = 0
) -> None:
    """Fold `project_entry` over the entries `first..head` (`received` maps `seq` to the receipt time)."""
    for seq in range(first, log.head.seq + 1):
        before = log.states[seq - 1] if seq > 0 else None
        project_entry(connection, org, before, log.states[seq], seq=seq, at=received[seq])
