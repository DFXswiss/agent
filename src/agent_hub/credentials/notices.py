"""Credential-change notices for devices (`hub.credential_notice`, migration 0003, ADR-035).

After a committed change of a user's credentials (`PUT`, `DELETE`, CK rotation) every device eligible for the user's
`CK` (`agent_hub.credentials.eligibility`) - except the device that made the change - gets

- a durable notice `(device, owner, kind, changed_at)` per changed kind, written in the change's transaction
  (`note_change`) and listed by `GET /v1/device/state` as `credentials_changed`
  (`agent_hub.credentials.device_state`), and
- after the commit the content-free hint `credentials {owner_id}` (`hint_devices`; online devices only, the hub never
  queues hints, 03-api.md section 6.2).

A device's fetch of the owner's record of a kind (`GET /v1/device/credentials/{owner}/{kind}`, also a `404` after a
deletion) clears its notice of that kind, but only up to the time the fetch read (`clear`): a change committed after
the fetch keeps a newer notice. An owner stays listed until every changed kind was re-read, so a re-read of one kind
never hides the deletion of another. Notices of devices that are no longer eligible are never listed and are removed
by the job `credentials.prune_notices`.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg

from agent_hub.credentials.eligibility import eligible_device_ids
from agent_hub.stream.publish import hint

Connection = psycopg.Connection[Any]


def note_change(
    connection: Connection,
    owner_id: UUID,
    kinds: Iterable[str],
    now: datetime,
    *,
    except_device: UUID | None = None,
) -> list[UUID]:
    """Write the notices of a change of the owner's records of `kinds`; returns the noticed devices (sorted)."""
    changed = sorted(set(kinds))
    devices = sorted(eligible_device_ids(connection, owner_id) - {except_device}, key=str) if changed else []
    for device in devices:
        for kind in changed:
            connection.execute(
                "INSERT INTO hub.credential_notice (device_id, owner_id, kind, changed_at) VALUES (%s, %s, %s, %s)"
                " ON CONFLICT (device_id, owner_id, kind) DO UPDATE SET changed_at = EXCLUDED.changed_at",
                (device, owner_id, kind, now),
            )
    return devices


def hint_devices(devices: Iterable[UUID], owner_id: UUID) -> None:
    """The `credentials {owner_id}` hint to every noticed device (after the commit)."""
    for device in devices:
        hint(device, "credentials", {"owner_id": str(owner_id)})


def clear(connection: Connection, device_id: UUID, owner_id: UUID, kind: str, read_at: datetime) -> None:
    """The device has re-read the owner's record of `kind` as of `read_at`."""
    connection.execute(
        "DELETE FROM hub.credential_notice WHERE device_id = %s AND owner_id = %s AND kind = %s AND changed_at <= %s",
        (device_id, owner_id, kind, read_at),
    )


def owners_for(connection: Connection, device_id: UUID) -> list[str]:
    """The owners with a notice for the device for whom the device is still eligible, sorted."""
    rows = connection.execute(
        "SELECT DISTINCT owner_id FROM hub.credential_notice WHERE device_id = %s ORDER BY owner_id", (device_id,)
    ).fetchall()
    return [str(owner) for (owner,) in rows if device_id in eligible_device_ids(connection, owner)]


def prune(connection: Connection) -> int:
    """Remove the notices of devices that are no longer eligible for the owner; the number removed."""
    removed = 0
    for device_id, owner_id in connection.execute(
        "SELECT DISTINCT device_id, owner_id FROM hub.credential_notice ORDER BY device_id, owner_id"
    ).fetchall():
        if device_id not in eligible_device_ids(connection, owner_id):
            removed += connection.execute(
                "DELETE FROM hub.credential_notice WHERE device_id = %s AND owner_id = %s", (device_id, owner_id)
            ).rowcount
    return removed
