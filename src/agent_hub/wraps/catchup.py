"""The catch-up list of `GET /v1/orgs/{org}/catch-up` (04-crypto.md sections 5.11 and 6.3 W2).

For the calling member the hub lists, from its verified log, the slots `{scope, scope_id, epoch, recipient_id}` of the
**current** epochs (for an admin reading a workgroup by inheritance: every epoch of it, ADR-058) that have no stored
wrap and that the caller may create (W1/W2, `agent_hub.log.catchup`):

- the `org` scope and every workgroup scope of this org (e.g. the wraps for a new member or a new device, or the
  caller's own self-wraps after `accept`);
- the caller's own `cred` scope for the principals of this org that are entitled to it (the devices assigned to a
  workgroup in which the caller has access, 04-crypto.md section 5.2a: "the user's existing CK is wrapped for the org's
  devices by catch-up"); other users' `cred` scopes are never listed (only their owners and eligible devices hold them).

Decertified platform devices are no recipients. The list is sorted by slot.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from agent_hub.log.catchup import missing_cred_wraps, missing_wraps
from agent_hub.log.wraps import CRED, WrapSlot
from agent_hub.orgs import logs
from agent_hub.wraps.rules import cred_epoch

Connection = psycopg.Connection[Any]


def _existing(connection: Connection, org: UUID, wgs: list[str]) -> set[WrapSlot]:
    rows = connection.execute(
        "SELECT scope, scope_id, epoch, recipient_id FROM hub.key_wrap"
        " WHERE (scope = 'org' AND scope_id = %s) OR (scope = 'wg' AND scope_id = ANY(%s::uuid[]))",
        (org, wgs),
    )
    return {WrapSlot(row[0], str(row[1]), row[2], str(row[3])) for row in rows}


def _existing_cred(connection: Connection, owner: str, epoch: int) -> set[WrapSlot]:
    rows = connection.execute(
        "SELECT recipient_id FROM hub.key_wrap WHERE scope = 'cred' AND scope_id = %s AND epoch = %s", (owner, epoch)
    )
    return {WrapSlot(CRED, owner, epoch, str(row[0])) for row in rows}


def catch_up(connection: Connection, org: logs.OrgRow, caller: str, root_key: bytes) -> list[WrapSlot]:
    """The missing wraps the caller may create, sorted."""
    platform = logs.platform_state(connection, root_key)
    log = logs.verified_log(connection, org, platform)
    excluded = frozenset() if platform is None else platform.excluded(log.state)
    state = log.state
    found = missing_wraps(log, caller, _existing(connection, org.id, sorted(state.workgroups)), excluded)
    epoch = cred_epoch(connection, caller)
    if epoch is not None:
        member_logs = [logs.verified_log(connection, row, platform) for row in logs.member_orgs(connection, caller)]
        all_excluded = (
            frozenset()
            if platform is None
            else frozenset().union(*(platform.excluded(item.state) for item in member_logs))
        )
        cred = missing_cred_wraps(
            member_logs,
            caller,
            caller,
            current_epoch=epoch,
            existing=_existing_cred(connection, caller, epoch),
            excluded=all_excluded,
        )
        found += [slot for slot in cred if slot.recipient in state.principals]
    return sorted(found)
