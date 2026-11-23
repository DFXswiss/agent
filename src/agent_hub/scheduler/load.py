"""The scheduler's inputs from the database (03-api.md section 4.6.2).

Candidates are the devices assigned to the workgroup (`wg_device` without `unassigned_seq`) that are active members of
its org and whose `device.state` is `active`; a device is online while its `device_presence.last_heartbeat_at` is less
than `AGENT_HUB_DEVICE_ONLINE_SECONDS` before the hub clock (the presence rule of HUB-07). The VM of the workgroup on
a device comes from `hub.vm` (rows reported by the runner): `running` wins over `stopped`, any other state but
`deleted` counts as `other`.

Refusals for lack of capacity (KOR-14, ADR-049; a session the device ended `failed` with `capacity_exhausted`):

- a device that refused any session at or after its last heartbeat counts with `vm_slots_free = 0` until it reports
  again (the stored capacity is older than the refusal; several starts in one heartbeat window, pool refill). A
  (re)connect writes the heartbeat time without a capacity report, so until the first heartbeat after it a refusal
  within the online window before the connect counts. A heartbeat measured before the refusal but received after it
  still clears the rule (the refusal limit bounds that race); a heartbeat with unchanged capacity emits no capacity
  signal, so a waiting attempt is then placed by the 15 s sweep;
- `Candidate.refused`: the newest session of the workgroup on the device was refused (no later session of the
  workgroup was placed there), so a stopped VM of the workgroup does not exempt it from needing a free slot.

Credit (KOR-31, ADR-059): while the org of the workgroup is exhausted (`agent_hub.billing.credit.exhausted`), its
platform devices are `Candidate.unpaid`.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg

from agent_hub.billing import credit
from agent_hub.scheduler.core import OTHER, RUNNING, STOPPED, Candidate, Capacity, Decision, choose

Connection = psycopg.Connection[Any]


def online_after(now: datetime, online_seconds: int) -> datetime:
    """The oldest heartbeat time that still counts as online."""
    return now - timedelta(seconds=online_seconds)


def _wg_vm(states: list[str] | None) -> str | None:
    if not states:
        return None
    if RUNNING in states:
        return RUNNING
    return STOPPED if STOPPED in states else OTHER


def _capacity(stored: Any, *, stale: bool) -> Capacity:
    """The stored capacity; without free slots while a refusal is newer than the last heartbeat."""
    found = Capacity.of(stored)
    return replace(found, vm_slots_free=0) if stale else found


def candidates(connection: Connection, wg_id: UUID | str, now: datetime, online_seconds: int) -> list[Candidate]:
    """The active devices assigned to the workgroup with presence, capacity and VM state (module docstring)."""
    found = connection.execute(
        "SELECT d.id, d.capacity, p.last_heartbeat_at > %s,"
        " (SELECT array_agg(v.state) FROM hub.vm v WHERE v.device_id = d.id AND v.wg_id = a.wg_id"
        " AND v.state <> 'deleted'),"
        " EXISTS (SELECT 1 FROM hub.session r WHERE r.device_id = d.id AND r.state = 'failed'"
        " AND r.state_reason = 'capacity_exhausted' AND r.ended_at >= CASE WHEN p.last_heartbeat_at > p.connected_at"
        " THEN p.last_heartbeat_at ELSE p.connected_at - make_interval(secs => %s) END),"
        " COALESCE((SELECT r.state = 'failed' AND r.state_reason IS NOT DISTINCT FROM 'capacity_exhausted'"
        " FROM hub.session r WHERE r.device_id = d.id AND r.wg_id = a.wg_id"
        " ORDER BY r.created_at DESC, r.id DESC LIMIT 1), false), d.kind, w.org_id"
        " FROM hub.wg_device a JOIN hub.wg w ON w.id = a.wg_id"
        " JOIN hub.org_member m ON m.org_id = w.org_id AND m.principal_id = a.device_id AND m.removed_seq IS NULL"
        " JOIN hub.device d ON d.id = a.device_id AND d.state = 'active'"
        " LEFT JOIN hub.device_presence p ON p.device_id = d.id"
        " WHERE a.wg_id = %s AND a.unassigned_seq IS NULL ORDER BY d.id",
        (online_after(now, online_seconds), online_seconds, wg_id),
    ).fetchall()
    unpaid = bool(found) and credit.exhausted(connection, found[0][7], now)
    return [
        Candidate(
            device_id=str(row[0]),
            online=bool(row[2]),
            capacity=_capacity(row[1], stale=bool(row[4])),
            wg_vm=_wg_vm(row[3]),
            refused=bool(row[5]),
            unpaid=unpaid and row[6] == "platform",
        )
        for row in found
    ]


def decide(connection: Connection, wg_id: UUID | str, now: datetime, online_seconds: int) -> Decision | None:
    """The scheduler's decision for a new session of the workgroup; None without an active assigned device."""
    found = candidates(connection, wg_id, now, online_seconds)
    return choose(found) if found else None
