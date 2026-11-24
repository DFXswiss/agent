"""The operator's list of tasks waiting for a device (`GET /v1/platform/waiting`, 03-api.md section 4.3, ADR-075
section 7).

A task waits for the operator when it is `scheduling` with the reason `platform_access_required` (its organization has
no device of its own and no access to the operator's devices: the operator grants it, `agent-hub platform
grant-org-access`), or with `no_device` while nobody in the organization could assign one (no certified, active
platform device the workgroup could get and no device of the organization's own), or (ADR-079) with
`credit_exhausted` (the operator sets the balance, E-14). One row per workgroup and reason with the
number of such tasks and the creation time of the oldest, oldest first. Metadata only. At most `WAITING_MAX` rows
are read, the oldest; a longer list adds `truncated: true` (ADR-086 section 2).
"""

from __future__ import annotations

from typing import Any

import psycopg

from agent_hub.crypto.encoding import format_time
from agent_hub.scheduler.core import CREDIT_EXHAUSTED, NO_DEVICE, PLATFORM_ACCESS_REQUIRED

Connection = psycopg.Connection[Any]
WAITING_MAX = 10_000

# A `no_device` row counts only while the organization has no active device of its own and the workgroup has no
# candidate (an active, certified platform device not assigned to it while the organization has platform access: the
# condition of `workgroups.read.candidates`). Both are filtered in SQL, so `WAITING_MAX` counts kept rows and
# `truncated` is exact.
_NOBODY_COULD_ASSIGN = (
    "NOT EXISTS (SELECT 1 FROM hub.org_member m JOIN hub.device d ON d.id = m.principal_id"
    " WHERE m.org_id = t.org_id AND m.kind = 'device' AND m.removed_seq IS NULL AND d.kind = 'org'"
    " AND d.state = 'active')"
    " AND NOT EXISTS (SELECT 1 FROM hub.device d WHERE d.kind = 'platform' AND d.state = 'active'"
    " AND d.certified_seq IS NOT NULL"
    " AND EXISTS (SELECT 1 FROM hub.wg w JOIN hub.org wo ON wo.id = w.org_id WHERE w.id = t.wg_id"
    " AND wo.platform_access)"
    " AND NOT EXISTS (SELECT 1 FROM hub.wg_device a WHERE a.wg_id = t.wg_id AND a.device_id = d.id"
    " AND a.unassigned_seq IS NULL))"
)


def waiting(connection: Connection) -> dict[str, Any]:
    """The answer of the module docstring: `{waiting}` and `truncated: true` when the list was cut."""
    cap = WAITING_MAX  # read at call time (a test lowers it)
    rows = connection.execute(
        "SELECT t.org_id, t.wg_id, t.state_reason, count(*), min(t.created_at), o.platform_access FROM hub.task t"
        " JOIN hub.org o ON o.id = t.org_id WHERE t.state = 'scheduling'"
        f" AND (t.state_reason = ANY(%s) OR (t.state_reason = %s AND {_NOBODY_COULD_ASSIGN}))"
        " GROUP BY t.org_id, t.wg_id, t.state_reason, o.platform_access ORDER BY min(t.created_at), t.wg_id"
        " LIMIT %s",
        ([PLATFORM_ACCESS_REQUIRED, CREDIT_EXHAUSTED], NO_DEVICE, cap + 1),
    ).fetchall()
    found = [
        {
            "org_id": str(org_id),
            "wg_id": str(wg_id),
            "reason": reason,
            "tasks": int(count),
            "oldest_at": format_time(oldest),
            "platform_access": bool(access),
        }
        for org_id, wg_id, reason, count, oldest, access in rows[:cap]
    ]
    return {"waiting": found, **({"truncated": True} if len(rows) > cap else {})}
