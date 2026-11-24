"""Workgroup and device lists (03-api.md section 4.5).

- `workgroup(wg)`: `{wg_id, org_id, is_default, epoch, devices, members}`; `epoch` is the current epoch of the
  workgroup key (`scope_epoch`), `devices` the active devices assigned to the workgroup (as the scheduler sees them:
  active org members with `device.state = active`), newest enrolment first, with presence, capacity and
  `capacity_low`; `members` the users with access (active members, not revoked), newest grant first.
- `candidates(wg)`: the platform devices the browser may assign to the workgroup (03-api.md section 4.6.1 step 1):
  certified (`certified_seq` = `platform_seq` of the `device_certify` entry the browser verifies), `active`, not yet
  assigned to it, and only when the org has platform access (ADR-026; otherwise none). Order: online devices first,
  then the highest free VM slots, the highest free VM memory, the smallest ID - the browser picks the first one, and
  an offline device would leave the first task waiting.
- `org_devices(org)`: every active device member of the org (org devices and assigned platform devices) with state,
  presence (`last_seen_at` = the last heartbeat while connected), agent version, capacity, `capacity_low` and the
  workgroups it is assigned to; newest enrolment first.

Both lists are bounded (ADR-063 section 2, `agent_hub.events.paging`): without `before`/`limit` at most `DEVICES_MAX`
devices (the newest enrolments; `truncated: true` when cut, the candidates are ranked within them), or one page in
the stable order `enrolled_at DESC, id DESC` with `next_before` (a page of candidates keeps that order: presence and
capacity change between two pages and would make a ranked page skip or repeat devices).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg

from agent_hub.crypto.encoding import b64u_encode, format_time
from agent_hub.events import paging
from agent_hub.scheduler.core import Capacity
from agent_hub.scheduler.load import online_after

Connection = psycopg.Connection[Any]
DEVICES_MAX = paging.CAP_LARGE


def device_window(before: str | None, limit: str | None) -> paging.Window:
    """The window of a device list request (device IDs are UUIDs)."""
    return paging.window(before, limit, DEVICES_MAX, UUID)


def _keyset(window: paging.Window, params: dict[str, Any]) -> str:
    """The keyset condition of a page (empty without a cursor); adds its parameters."""
    if window.before is None:
        return ""
    params["at"], params["id"] = window.before
    return " AND (d.enrolled_at, d.id) < (%(at)s, %(id)s)"


def _capacity(stored: Any) -> tuple[dict[str, int], bool]:
    capacity = Capacity.of(stored)
    return capacity.json(), capacity.low


def workgroup(connection: Connection, wg_id: UUID, now: datetime, online_seconds: int) -> dict[str, Any]:
    """The workgroup (module docstring); the caller checked `wg:read`."""
    row = connection.execute(
        "SELECT w.org_id, w.is_default, (SELECT max(epoch) FROM hub.scope_epoch e WHERE e.scope = 'wg'"
        " AND e.scope_id = w.id) FROM hub.wg w WHERE w.id = %s",
        (wg_id,),
    ).fetchone()
    assert row is not None
    devices = []
    for found in connection.execute(
        "SELECT d.id, d.kind, p.last_heartbeat_at > %s, d.capacity FROM hub.wg_device a"
        " JOIN hub.org_member m ON m.org_id = %s AND m.principal_id = a.device_id AND m.removed_seq IS NULL"
        " JOIN hub.device d ON d.id = a.device_id AND d.state = 'active'"
        " LEFT JOIN hub.device_presence p ON p.device_id = d.id"
        " WHERE a.wg_id = %s AND a.unassigned_seq IS NULL ORDER BY d.enrolled_at DESC, d.id DESC",
        (online_after(now, online_seconds), row[0], wg_id),
    ):
        capacity, low = _capacity(found[3])
        devices.append(
            {
                "device_id": str(found[0]),
                "kind": found[1],
                "online": bool(found[2]),
                "capacity": capacity,
                "capacity_low": low,
            }
        )
    members = [
        {"user_id": str(found[0]), "access": found[1]}
        for found in connection.execute(
            "SELECT a.user_id, a.access FROM hub.wg_access a JOIN hub.org_member m ON m.org_id = %s"
            " AND m.principal_id = a.user_id AND m.removed_seq IS NULL"
            " WHERE a.wg_id = %s AND a.revoked_seq IS NULL ORDER BY a.granted_seq DESC, a.user_id DESC",
            (row[0], wg_id),
        )
    ]
    return {
        "wg_id": str(wg_id),
        "org_id": str(row[0]),
        "is_default": bool(row[1]),
        "epoch": int(row[2] or 1),
        "devices": devices,
        "members": members,
    }


def candidates(
    connection: Connection, wg_id: UUID, now: datetime, online_seconds: int, window: paging.Window
) -> dict[str, Any]:
    """The platform devices the browser may assign to the workgroup (module docstring)."""
    params: dict[str, Any] = {"online": online_after(now, online_seconds), "wg": wg_id, "limit": window.limit + 1}
    found, more = paging.cut(
        connection.execute(
            "SELECT d.id, d.certified_seq, d.kem_pub, d.sig_pub, COALESCE(p.last_heartbeat_at > %(online)s, false),"
            " d.capacity, d.enrolled_at FROM hub.device d LEFT JOIN hub.device_presence p ON p.device_id = d.id"
            " WHERE d.kind = 'platform' AND d.state = 'active' AND d.certified_seq IS NOT NULL"
            " AND EXISTS (SELECT 1 FROM hub.wg w JOIN hub.org o ON o.id = w.org_id WHERE w.id = %(wg)s"
            " AND o.platform_access) AND NOT EXISTS (SELECT 1 FROM hub.wg_device a WHERE a.wg_id = %(wg)s"
            f" AND a.device_id = d.id AND a.unassigned_seq IS NULL){_keyset(window, params)}"
            " ORDER BY d.enrolled_at DESC, d.id DESC LIMIT %(limit)s",
            params,
        ).fetchall(),
        window.limit,
    )
    items = []
    for row in found:
        capacity = Capacity.of(row[5])
        items.append(
            (
                (not row[4], -capacity.vm_slots_free, -capacity.free_memory_mib, str(row[0])),
                {
                    "device_id": str(row[0]),
                    "platform_seq": int(row[1]),
                    "pub": {"kem": b64u_encode(bytes(row[2])), "sig": b64u_encode(bytes(row[3]))},
                    "online": bool(row[4]),
                    "capacity": capacity.json(),
                    "capacity_low": capacity.low,
                },
            )
        )
    if not window.paged:
        items.sort(key=lambda pair: pair[0])
    last = (found[-1][6], str(found[-1][0])) if more else None
    return {"candidates": [item for _key, item in items], **window.extra(last)}


def has_candidates(connection: Connection, wg_id: UUID, now: datetime, online_seconds: int) -> bool:
    """Whether the workgroup has at least one candidate (`no_device` of `POST /v1/tasks`)."""
    return bool(candidates(connection, wg_id, now, online_seconds, paging.Window(paged=True, limit=1))["candidates"])


def org_devices(
    connection: Connection, org_id: UUID, now: datetime, online_seconds: int, window: paging.Window
) -> dict[str, Any]:
    """The devices of the org (module docstring); the caller checked `org:member`."""
    params: dict[str, Any] = {"org": org_id, "limit": window.limit + 1}
    rows, more = paging.cut(
        connection.execute(
            "SELECT d.id, d.kind, d.state, p.last_heartbeat_at, d.agent_version, d.capacity,"
            " ARRAY(SELECT a.wg_id FROM hub.wg_device a JOIN hub.wg w ON w.id = a.wg_id WHERE w.org_id = m.org_id"
            " AND a.device_id = d.id AND a.unassigned_seq IS NULL ORDER BY a.wg_id), d.enrolled_at"
            " FROM hub.org_member m JOIN hub.device d ON d.id = m.principal_id"
            " LEFT JOIN hub.device_presence p ON p.device_id = d.id"
            f" WHERE m.org_id = %(org)s AND m.kind = 'device' AND m.removed_seq IS NULL{_keyset(window, params)}"
            " ORDER BY d.enrolled_at DESC, d.id DESC LIMIT %(limit)s",
            params,
        ).fetchall(),
        window.limit,
    )
    devices = []
    for row in rows:
        capacity, low = _capacity(row[5])
        seen = row[3]
        devices.append(
            {
                "device_id": str(row[0]),
                "kind": row[1],
                "state": row[2],
                "online": seen is not None and seen > online_after(now, online_seconds),
                "last_seen_at": None if seen is None else format_time(seen),
                "agent_version": row[4],
                "capacity": capacity,
                "capacity_low": low,
                "assigned_wgs": [str(wg) for wg in row[6]],
            }
        )
    return {"devices": devices, **window.extra((rows[-1][7], str(rows[-1][0])) if more else None)}
