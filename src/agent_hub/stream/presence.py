"""Presence of the devices (02-data-model.md section 2.1 `device_presence`, 03-api.md sections 6.2 and 12).

A device is **online** while its heartbeats arrive: its `device_presence` row exists and `last_heartbeat_at` is less
than `AGENT_HUB_DEVICE_ONLINE_SECONDS` (30) ago (`is_online`, `online_devices`; other areas such as the scheduler and
the device lists ask here). The device WebSocket (`agent_hub.stream.ws`) writes the rows:

- `connect`: the device must be usable by the rule of the signature gate (`agent_hub.device.keys`: `active`, its
  principal enabled, an org device still an active member of its org); the row is (re)written for this connection
  (`hub_instance`, `connected_at`, heartbeat = now) and `device.connect` is audited;
- `heartbeat`: the heartbeat time and `device.capacity` are updated - only while the row still belongs to this
  connection and the device is still usable (otherwise the connection was superseded by a newer one, or the device was
  decertified or removed, and the socket closes);
- `disconnect`: the row of this connection is deleted and `device.disconnect` is audited (not again when the row was
  taken over or expired);
- `expire` (job of this area, every few seconds in every hub process): rows whose heartbeat is older than the online
  window are deleted - a hub process that died leaves rows behind, a socket that stopped sending is closed by its own
  process - and `device.disconnect` is audited.

Audit rows carry the device as actor and target and, for org devices, the owning org.

Every change of the online state or of the capacity is announced after the commit as the internal signal
`device.presence` (`device_id`, `online` 1/0) or `device.capacity` (`device_id`) through `agent_hub.signals`; the
scheduler (HUB-09) subscribes in `scheduler/signals.py` to re-run placement. The hub never queues hints for offline
devices (they reconcile on connect).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from agent_hub import signals
from agent_hub.audit.chain import AuditRow, append_audit

Connection = psycopg.Connection[Any]
PRESENCE_SIGNAL = "device.presence"
CAPACITY_SIGNAL = "device.capacity"
SIGNALS = (PRESENCE_SIGNAL, CAPACITY_SIGNAL)
CHANNEL_DETAIL = {"channel": "ws"}


@dataclass(frozen=True)
class Heartbeat:
    """Outcome of a heartbeat: `current` False ends the connection; `capacity_changed` announces the new capacity."""

    current: bool
    capacity_changed: bool = False


# The device rule of the signature gate (agent_hub.device.keys): active, principal enabled, an org device still an
# active member of its org. Gives (usable, capacity).
_DEVICE = (
    "SELECT d.state = 'active' AND p.disabled_at IS NULL AND (d.kind <> 'org' OR EXISTS (SELECT 1 FROM hub.org_member m"
    " WHERE m.org_id = d.owner_org AND m.principal_id = d.id AND m.removed_seq IS NULL)), d.capacity"
    " FROM hub.device d JOIN hub.principal p ON p.id = d.id WHERE d.id = %s"
)


def _device(connection: Connection, device_id: UUID, lock: str) -> tuple[bool, Any] | None:
    row = connection.execute(f"{_DEVICE} FOR {lock} OF d", (device_id,)).fetchone()
    return None if row is None else (bool(row[0]), row[1])


def _audit(connection: Connection, action: str, device_id: UUID, now: datetime) -> None:
    owner = connection.execute("SELECT owner_org FROM hub.device WHERE id = %s", (device_id,)).fetchone()
    row = AuditRow(
        at=now,
        actor_kind="device",
        actor_id=device_id,
        action=action,
        result="ok",
        device_id=device_id,
        org_id=None if owner is None else owner[0],
        target_kind="device",
        target_id=str(device_id),
        detail=CHANNEL_DETAIL,
    )
    append_audit(connection, row)


def _fresh(last_heartbeat_at: datetime | None, now: datetime, window: int) -> bool:
    return last_heartbeat_at is not None and last_heartbeat_at > now - timedelta(seconds=window)


def connect(connection: Connection, device_id: UUID, instance: str, now: datetime, window: int) -> bool | None:
    """Record a new connection; None when the device is not active, else whether it was online before."""
    device = _device(connection, device_id, "SHARE")
    if device is None or not device[0]:
        return None
    previous = connection.execute(
        "SELECT last_heartbeat_at FROM hub.device_presence WHERE device_id = %s FOR UPDATE", (device_id,)
    ).fetchone()
    connection.execute(
        "INSERT INTO hub.device_presence (device_id, hub_instance, connected_at, last_heartbeat_at)"
        " VALUES (%s, %s, %s, %s) ON CONFLICT (device_id) DO UPDATE SET hub_instance = EXCLUDED.hub_instance,"
        " connected_at = EXCLUDED.connected_at, last_heartbeat_at = EXCLUDED.last_heartbeat_at",
        (device_id, instance, now, now),
    )
    _audit(connection, "device.connect", device_id, now)
    return previous is not None and _fresh(previous[0], now, window)


def heartbeat(
    connection: Connection,
    device_id: UUID,
    *,
    instance: str,
    connected_at: datetime,
    now: datetime,
    capacity: Mapping[str, int],
) -> Heartbeat:
    """Record a heartbeat of this connection and the reported capacity (lock order: device row, presence row - the
    same as `connect`)."""
    device = _device(connection, device_id, "UPDATE")
    owned = connection.execute(
        "UPDATE hub.device_presence SET last_heartbeat_at = %s"
        " WHERE device_id = %s AND hub_instance = %s AND connected_at = %s RETURNING 1",
        (now, device_id, instance, connected_at),
    ).fetchone()
    if owned is None or device is None or not device[0]:
        return Heartbeat(current=False)
    if device[1] == dict(capacity):
        return Heartbeat(current=True)
    connection.execute(
        "UPDATE hub.device SET capacity = %s, updated_at = %s WHERE id = %s", (Jsonb(dict(capacity)), now, device_id)
    )
    return Heartbeat(current=True, capacity_changed=True)


def disconnect(connection: Connection, device_id: UUID, instance: str, connected_at: datetime, now: datetime) -> bool:
    """Remove the row of this connection; True when the device went offline by it (audited as `device.disconnect`).
    A row taken over by a newer socket or deleted by `expire` is left alone and not audited again."""
    removed = connection.execute(
        "DELETE FROM hub.device_presence WHERE device_id = %s AND hub_instance = %s AND connected_at = %s RETURNING 1",
        (device_id, instance, connected_at),
    ).fetchone()
    if removed is None:
        return False
    _audit(connection, "device.disconnect", device_id, now)
    return True


def expire(connection: Connection, now: datetime, window: int) -> list[UUID]:
    """Delete the rows whose heartbeat is older than the online window; the devices that went offline."""
    rows = connection.execute(
        "DELETE FROM hub.device_presence WHERE last_heartbeat_at <= %s RETURNING device_id",
        (now - timedelta(seconds=window),),
    ).fetchall()
    devices = sorted(UUID(str(row[0])) for row in rows)
    for device in devices:
        _audit(connection, "device.disconnect", device, now)
    return devices


def online_devices(connection: Connection, device_ids: Iterable[UUID | str], now: datetime, window: int) -> set[UUID]:
    """The devices among `device_ids` that are online now."""
    ids = sorted({str(item) for item in device_ids})
    if not ids:
        return set()
    rows = connection.execute(
        "SELECT device_id FROM hub.device_presence WHERE device_id = ANY(%s::uuid[]) AND last_heartbeat_at > %s",
        (ids, now - timedelta(seconds=window)),
    ).fetchall()
    return {UUID(str(row[0])) for row in rows}


def is_online(connection: Connection, device_id: UUID | str, now: datetime, window: int) -> bool:
    """The device is online now."""
    return bool(online_devices(connection, [device_id], now, window))


def announce_presence(device_id: UUID, online: bool) -> None:
    """The signal `device.presence` (after the commit)."""
    signals.emit(PRESENCE_SIGNAL, device_id=device_id, online=int(online))


def announce_capacity(device_id: UUID) -> None:
    """The signal `device.capacity` (after the commit)."""
    signals.emit(CAPACITY_SIGNAL, device_id=device_id)
