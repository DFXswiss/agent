"""Which device polls the subscription quota of which owner (03-api.md section 6.1 `quota_poll_owners`, 05-runner.md
section 10.6).

An owner is a user with a stored `claude_subscription` credential. The devices that may poll for the owner are the
**eligible** devices of the owner (`agent_hub.credentials.eligibility`: active, member of the org, assigned to a
workgroup in which the owner has access while the owner is an active member - exactly the devices that can decrypt the
owner's `CK`) that are **online** (`agent_hub.stream.presence`: a heartbeat within `DEVICE_ONLINE_SECONDS`). Of these,
the device with the smallest ID polls. The answer is computed on every read, so the duty moves to the next device as
soon as the polling device's heartbeat is older than the online window, and back when it returns; a device that is not
online itself polls for nobody. `poll_owners(connection, device, now, window)` gives the owners of one device, sorted.

The same rule decides which `quota` items the hub accepts (`agent_hub.usage.outbox`): only the polling device may
report an owner's windows.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg

Connection = psycopg.Connection[Any]
SUBSCRIPTION_KIND = "claude_subscription"

# The eligibility rule of agent_hub.credentials.eligibility (_ELIGIBLE) for every owner of a subscription credential,
# restricted to online devices; a test compares both rules. Device IDs compare as canonical text (= byte order).
_POLL_OWNERS = (
    "SELECT e.owner_id FROM (SELECT DISTINCT a.user_id AS owner_id, d.device_id FROM hub.wg_device d"
    " JOIN hub.wg w ON w.id = d.wg_id"
    " JOIN hub.org_member md ON md.org_id = w.org_id AND md.principal_id = d.device_id AND md.removed_seq IS NULL"
    " JOIN hub.wg_access a ON a.wg_id = d.wg_id AND a.revoked_seq IS NULL AND NOT a.inherited"
    " JOIN hub.org_member mu ON mu.org_id = w.org_id AND mu.principal_id = a.user_id AND mu.removed_seq IS NULL"
    " AND mu.consented"
    " JOIN hub.device dv ON dv.id = d.device_id AND dv.state = 'active'"
    " JOIN hub.principal p ON p.id = dv.id AND p.disabled_at IS NULL"
    " JOIN hub.credential c ON c.owner_id = a.user_id AND c.kind = %(kind)s"
    " JOIN hub.device_presence dp ON dp.device_id = d.device_id AND dp.last_heartbeat_at > %(since)s"
    " WHERE d.unassigned_seq IS NULL AND (%(owner)s::uuid IS NULL OR a.user_id = %(owner)s::uuid)) e"
    " GROUP BY e.owner_id HAVING min(e.device_id::text) = %(device)s ORDER BY e.owner_id"
)


def poll_owners(
    connection: Connection, device_id: UUID | str, now: datetime, window: int, owner: UUID | str | None = None
) -> list[UUID]:
    """The owners whose subscription quota the device polls (module docstring), only `owner` when given."""
    params = {
        "kind": SUBSCRIPTION_KIND,
        "since": now - timedelta(seconds=window),
        "device": str(UUID(str(device_id))),
        "owner": None if owner is None else str(owner),
    }
    return [UUID(str(row[0])) for row in connection.execute(_POLL_OWNERS, params).fetchall()]


def polls_for(connection: Connection, device_id: UUID | str, owner_id: UUID | str, now: datetime, window: int) -> bool:
    """The device is the one that polls the owner's quota now."""
    return bool(poll_owners(connection, device_id, now, window, owner_id))
