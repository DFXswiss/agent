"""The operator's list of tasks waiting for a device (`GET /v1/platform/waiting`, 03-api.md section 4.3, ADR-060
section 7).

A task waits for the operator when it is `scheduling` with the reason `platform_access_required` (its organization has
no device of its own and no access to the operator's devices: the operator grants it, `agent-hub platform
grant-org-access`), or with `no_device` while nobody in the organization could assign one (no certified, active
platform device the workgroup could get and no device of the organization's own). One row per workgroup with the
number of such tasks and the creation time of the oldest, oldest first. Metadata only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg

from agent_hub.crypto.encoding import format_time
from agent_hub.scheduler.core import NO_DEVICE, PLATFORM_ACCESS_REQUIRED
from agent_hub.workgroups.read import candidates

Connection = psycopg.Connection[Any]


def _own_devices(connection: Connection, org_id: Any) -> bool:
    row = connection.execute(
        "SELECT EXISTS (SELECT 1 FROM hub.org_member m JOIN hub.device d ON d.id = m.principal_id"
        " WHERE m.org_id = %s AND m.kind = 'device' AND m.removed_seq IS NULL AND d.kind = 'org'"
        " AND d.state = 'active')",
        (org_id,),
    ).fetchone()
    return bool(row is not None and row[0])


def waiting(connection: Connection, now: datetime, online_seconds: int) -> list[dict[str, Any]]:
    """The rows of the module docstring."""
    rows = connection.execute(
        "SELECT t.org_id, t.wg_id, t.state_reason, count(*), min(t.created_at), o.platform_access FROM hub.task t"
        " JOIN hub.org o ON o.id = t.org_id WHERE t.state = 'scheduling' AND t.state_reason = ANY(%s)"
        " GROUP BY t.org_id, t.wg_id, t.state_reason, o.platform_access ORDER BY min(t.created_at), t.wg_id",
        ([NO_DEVICE, PLATFORM_ACCESS_REQUIRED],),
    ).fetchall()
    found = []
    for org_id, wg_id, reason, count, oldest, access in rows:
        if reason == NO_DEVICE and (
            _own_devices(connection, org_id) or candidates(connection, wg_id, now, online_seconds)
        ):
            continue
        found.append(
            {
                "org_id": str(org_id),
                "wg_id": str(wg_id),
                "reason": reason,
                "tasks": int(count),
                "oldest_at": format_time(oldest),
                "platform_access": bool(access),
            }
        )
    return found
