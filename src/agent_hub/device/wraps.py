"""Key wraps of a device (`GET/POST /v1/device/wraps`, 03-api.md sections 4.3 and 6.1, 04-crypto.md section 6.3).

The device is the recipient (`GET`: the wraps stored for it, optionally of one scope / scope ID / epoch, newest first)
or the wrapper (`POST`: catch-up wraps it creates per W2 for principals the verified log entitles). Storing runs the
same W3 check as for browsers (`agent_hub.wraps.rules.store_wraps` with the device as `wrapped_by`): the device must
hold the scope key itself (an assigned device holds `OK` and the `WK` of its workgroups, and the `CK` of the members
of those workgroups), the recipient must be entitled, decertified platform devices are no recipients, and one wrap
that violates the rules refuses the whole request (`400 wrap_unauthorized`). After the commit the route publishes the
`wraps` frame (users) or hint (devices) `{}` to every recipient of a new wrap.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

import psycopg

from agent_hub.stream.publish import Audience, AudienceKind, publish
from agent_hub.wraps.audit import audit_wraps
from agent_hub.wraps.rules import Stored, store_wraps, wraps_for

Connection = psycopg.Connection[Any]


def list_wraps(
    connection: Connection, device: UUID, *, scope: str | None, scope_id: str | None, epoch: int | None
) -> list[dict[str, Any]]:
    """The device's wraps, newest first (ties by slot)."""
    found = wraps_for(connection, device, scope=scope, scope_id=scope_id, epoch=epoch)
    return sorted(found, key=lambda wrap: wrap["created_at"], reverse=True)


def store(
    connection: Connection, device: UUID, wraps: Sequence[object], **options: Any
) -> tuple[Stored, dict[str, str]]:
    """Store the wraps of the device (W3) and return them with the principal kinds of their recipients."""
    stored = store_wraps(connection, wraps, wrapper=str(device), **options)
    audit_wraps(connection, stored, actor_kind="device", actor_id=device, now=options["now"])
    kinds = {
        str(row[0]): str(row[1])
        for row in connection.execute(
            "SELECT id, kind FROM hub.principal WHERE id = ANY(%s::uuid[])", (sorted(stored.recipients),)
        )
    }
    return stored, kinds


def publish_wraps(stored: Stored, kinds: dict[str, str]) -> None:
    """The `wraps` frame/hint to every recipient of a new wrap (after the commit)."""
    for recipient in sorted(stored.recipients):
        kind: AudienceKind = "device" if kinds.get(recipient) == "device" else "user"
        publish(Audience(kind, UUID(recipient)), "wraps", {})
