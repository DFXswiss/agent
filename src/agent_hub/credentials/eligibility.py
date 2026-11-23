"""Which devices may hold a user's credential key `CK` (04-crypto.md sections 2 and 6.3, 03-api.md 4.1 and 4.10).

A device is **eligible** for a user when it is an active device (`device.state = active`, principal not disabled) that
is an active member of an org and currently assigned to a workgroup of that org in which the user has explicit access
(read or write; every member has write on the default workgroup; the inherited read of an admin does not count,
ADR-058) while the user is an active member of that org. This is the
`cred` reader rule W2 of `agent_hub.log.wraps.cred_readers`, answered from the projections of the membership logs,
which are written in the same transaction as the log entries (`agent_hub.orgs.rights`).

The same set serves

- `GET /v1/me/devices`: the devices a user may address with device requests, one row per device and org with the
  workgroups that make it eligible and its presence;
- `GET/PUT /v1/device/credentials/{owner}/{kind}`: the device must be eligible for the owner;
- the `credentials` hints and notices after a change of the owner's credentials (`agent_hub.credentials.notices`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg

Connection = psycopg.Connection[Any]

_ELIGIBLE = (
    "SELECT dv.id, dv.kind, w.org_id, d.wg_id, dv.enrolled_at FROM hub.wg_device d"
    " JOIN hub.wg w ON w.id = d.wg_id"
    " JOIN hub.org_member md ON md.org_id = w.org_id AND md.principal_id = d.device_id AND md.removed_seq IS NULL"
    " JOIN hub.wg_access a ON a.wg_id = d.wg_id AND a.revoked_seq IS NULL AND NOT a.inherited"
    " JOIN hub.org_member mu ON mu.org_id = w.org_id AND mu.principal_id = a.user_id AND mu.removed_seq IS NULL"
    " JOIN hub.device dv ON dv.id = d.device_id AND dv.state = 'active'"
    " JOIN hub.principal p ON p.id = dv.id AND p.disabled_at IS NULL"
    " WHERE d.unassigned_seq IS NULL AND a.user_id = %(user)s"
)


@dataclass(frozen=True)
class EligibleDevice:
    """One eligible device of a user in one org, with the workgroups that make it eligible (sorted)."""

    device_id: UUID
    kind: str
    org_id: UUID
    wg_ids: tuple[UUID, ...]
    enrolled_at: datetime


def eligible_devices(connection: Connection, user_id: UUID | str) -> list[EligibleDevice]:
    """The eligible devices of a user, one item per device and org, newest enrolment first (then device and org ID
    descending, 02-data-model.md section 1.5)."""
    grouped: dict[tuple[UUID, UUID], tuple[str, datetime, set[UUID]]] = {}
    for device_id, kind, org_id, wg_id, enrolled_at in connection.execute(_ELIGIBLE, {"user": user_id}):
        found = grouped.setdefault((device_id, org_id), (kind, enrolled_at, set()))
        found[2].add(wg_id)
    items = [
        EligibleDevice(device_id, kind, org_id, tuple(sorted(wgs, key=str)), enrolled_at)
        for (device_id, org_id), (kind, enrolled_at, wgs) in grouped.items()
    ]
    return sorted(items, key=lambda item: (item.enrolled_at, str(item.device_id), str(item.org_id)), reverse=True)


def eligible_device_ids(connection: Connection, user_id: UUID | str) -> set[UUID]:
    """The IDs of the eligible devices of a user."""
    return {item.device_id for item in eligible_devices(connection, user_id)}


def is_eligible(connection: Connection, device_id: UUID | str, user_id: UUID | str) -> bool:
    """The device is eligible for the user's `CK`."""
    query = _ELIGIBLE + " AND d.device_id = %(device)s LIMIT 1"
    return connection.execute(query, {"user": user_id, "device": device_id}).fetchone() is not None
