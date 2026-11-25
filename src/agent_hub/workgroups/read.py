"""Workgroup and device lists (03-api.md section 4.5).

- `workgroup(wg)`: `{wg_id, org_id, is_default, epoch, devices, members, placement}`; `epoch` is the current epoch of
  the workgroup key (`scope_epoch`), `devices` the active devices assigned to the workgroup (as the scheduler sees
  them: active org members with `device.state = active`), newest enrolment first, with presence, capacity,
  `capacity_low`, `assigned_via` (`org` for a device assigned to the whole organization, else `workgroup`; display
  data, clients decide key recipients from the verified log, ADR-075 section 5) and `vms` (the workgroup's live VMs
  on the device with their occupying sessions, their golden and the switch status `upgrade` the device reported,
  ADR-093 section 3); `members` the users with access (active members, not revoked),
  newest grant first; `placement` the workgroup's default device and VM (as stored).
- `candidates(wg)`: the platform devices the browser may assign to the workgroup (03-api.md section 4.6.1 step 1):
  certified (`certified_seq` = `platform_seq` of the `device_certify` entry the browser verifies), `active`, not yet
  assigned to it, and only when the org has platform access (ADR-026; otherwise none). Order: online devices first,
  then the highest free VM slots, the highest free VM memory, the smallest ID - the browser picks the first one, and
  an offline device would leave the first task waiting.
- `org_devices(org, user)`: every active device member of the org (org devices and assigned platform devices) with
  state, presence (`last_seen_at` = the last heartbeat while connected), agent version, capacity, `capacity_low`, the
  workgroups it is assigned to, `assigned_org` (assigned to the whole organization) and `vms` (live VMs of the
  workgroups the user can read, with workgroup and occupying sessions, ordered by VM ID); newest enrolment first.

Both lists are bounded (ADR-086 section 2, `agent_hub.events.paging`): without `before`/`limit` at most `DEVICES_MAX`
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
from agent_hub.scheduler.load import OCCUPYING, online_after

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


def _capacity(stored: Any) -> tuple[dict[str, Any], bool]:
    capacity = Capacity.of(stored)
    return capacity.json(), capacity.low


def _vm_rows(connection: Connection, where: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
    """`(device_id, vm_id, wg_id, state, occupying sessions, golden_version, upgrade)` of the live VMs matching
    `where`, ordered by VM ID."""
    return connection.execute(
        "SELECT v.device_id, v.id, v.wg_id, v.state, (SELECT count(*) FROM hub.session s WHERE s.vm_id = v.id"
        f" AND s.state = ANY(%s)), v.golden_version, v.upgrade FROM hub.vm v WHERE v.state <> 'deleted'"
        f" AND v.wg_id IS NOT NULL AND {where}"
        " ORDER BY v.id",
        (list(OCCUPYING), *params),
    ).fetchall()


def workgroup(connection: Connection, wg_id: UUID, now: datetime, online_seconds: int) -> dict[str, Any]:
    """The workgroup (module docstring); the caller checked `wg:read`."""
    row = connection.execute(
        "SELECT w.org_id, w.is_default, (SELECT max(epoch) FROM hub.scope_epoch e WHERE e.scope = 'wg'"
        " AND e.scope_id = w.id) FROM hub.wg w WHERE w.id = %s",
        (wg_id,),
    ).fetchone()
    assert row is not None
    vms: dict[str, list[dict[str, Any]]] = {}
    for device, vm_id, _wg, state, sessions, golden, upgrade in _vm_rows(connection, "v.wg_id = %s", (wg_id,)):
        vms.setdefault(str(device), []).append(
            {"vm_id": vm_id, "state": state, "sessions": int(sessions), "golden_version": golden, "upgrade": upgrade}
        )
    devices = []
    for found in connection.execute(
        "SELECT d.id, d.kind, p.last_heartbeat_at > %s, d.capacity, EXISTS (SELECT 1 FROM hub.org_device o"
        " WHERE o.org_id = m.org_id AND o.device_id = d.id AND o.unassigned_seq IS NULL) FROM hub.wg_device a"
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
                "assigned_via": "org" if found[4] else "workgroup",
                "vms": vms.get(str(found[0]), []),
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
        "placement": placement(connection, wg_id),
    }


def placement(connection: Connection, wg_id: UUID) -> dict[str, Any] | None:
    """The stored default device and VM of the workgroup."""
    row = connection.execute("SELECT device_id, vm_id FROM hub.wg_placement WHERE wg_id = %s", (wg_id,)).fetchone()
    return None if row is None else {"device_id": str(row[0]), "vm_id": row[1]}


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


def org_devices(
    connection: Connection, org_id: UUID, now: datetime, online_seconds: int, *, user: UUID, window: paging.Window
) -> dict[str, Any]:
    """The devices of the org (module docstring); the caller checked `org:member`."""
    vms: dict[str, list[dict[str, Any]]] = {}
    readable = (
        "v.wg_id IN (SELECT a.wg_id FROM hub.wg_access a JOIN hub.wg w ON w.id = a.wg_id WHERE w.org_id = %s"
        " AND a.user_id = %s AND a.revoked_seq IS NULL)"
    )
    for device, vm_id, wg, state, sessions, _golden, _upgrade in _vm_rows(connection, readable, (org_id, user)):
        vms.setdefault(str(device), []).append(
            {"vm_id": vm_id, "wg_id": str(wg), "state": state, "sessions": int(sessions)}
        )
    params: dict[str, Any] = {"org": org_id, "limit": window.limit + 1}
    rows, more = paging.cut(
        connection.execute(
            "SELECT d.id, d.kind, d.state, p.last_heartbeat_at, d.agent_version, d.capacity,"
            " ARRAY(SELECT a.wg_id FROM hub.wg_device a JOIN hub.wg w ON w.id = a.wg_id WHERE w.org_id = m.org_id"
            " AND a.device_id = d.id AND a.unassigned_seq IS NULL ORDER BY a.wg_id),"
            " EXISTS (SELECT 1 FROM hub.org_device o WHERE o.org_id = m.org_id AND o.device_id = d.id"
            " AND o.unassigned_seq IS NULL), d.enrolled_at"
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
                "assigned_org": bool(row[7]),
                "vms": vms.get(str(row[0]), []),
            }
        )
    return {"devices": devices, **window.extra((rows[-1][8], str(rows[-1][0])) if more else None)}
