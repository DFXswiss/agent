"""The subscriptions of a user with their readings (ADR-090 sections 1 and 4; 03-api.md section 4.8).

A subscription is a `claude_subscription` credential record (`agent_hub.credentials.records`, kind
`SUBSCRIPTION`). The **primary** record is the one the endpoints from before address; it serves the owner's own
sessions and is written only through them, so the endpoints of this area refuse to write it (`forbidden`) but read
and delete it. Further records are created without the mark and never get it.

- `subscription_meta(identity)`: the `subscription_meta` object (no owner kind: the owner is always a user).
- `answer(identity)`: the body of `GET /v1/subscriptions/{id}` and `GET /v1/device/subscriptions/{id}`.
- `own(connection, record_id, user)`: the caller's record, `404 not_found` for anything else (the existence of
  another person's record does not leak).
- `rows_for(connection, user)`: `GET /v1/me/subscriptions` - every record of the user, newest change first (then ID
  descending), each with its newest reading: the primary record's from the owner's `quota_snapshot` (the newest
  fetch and its device, every window of that device's newest report) or `quota_unreadable`; a further record's from
  `subscription_quota`; `null` before the first reading.
- `store_reading(connection, ...)`: the `quota` item of a further subscription (`agent_hub.usage.outbox`): kept when
  the record is a non-primary subscription of the owner (anything else is dropped without an error: the record may
  have been deleted or marked meanwhile), the newer `fetched_at` wins.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from agent_hub.credentials.records import (
    IDENTITY_COLUMNS,
    SUBSCRIPTION,
    Identity,
    identity_of,
    load_identity,
    record_json,
)
from agent_hub.crypto.encoding import format_time
from agent_hub.errors import HubError
from agent_hub.usage.routes import quota_rows

Connection = psycopg.Connection[Any]


def subscription_meta(identity: Identity) -> dict[str, Any]:
    """The `subscription_meta` object."""
    return {
        "id": str(identity.id),
        "owner_id": str(identity.owner.id),
        "primary": identity.primary,
        "version": identity.version,
        "epoch": identity.epoch,
        "created_at": format_time(identity.created_at),
        "updated_at": format_time(identity.updated_at),
    }


def answer(identity: Identity) -> dict[str, Any]:
    """The body of a read of one record."""
    return {
        "owner_id": str(identity.owner.id),
        "primary": identity.primary,
        "record": record_json(identity.header_text, identity.ct, identity.sig),
    }


def own(connection: Connection, record_id: UUID, user: UUID) -> Identity:
    """The caller's subscription record, `404 not_found` otherwise."""
    identity = load_identity(connection, record_id, SUBSCRIPTION)
    if identity is None or identity.owner.id != user:
        raise HubError("not_found")
    return identity


def owner_records(connection: Connection, owner: UUID) -> list[Identity]:
    """Every subscription record of the owner, newest change first (then ID descending)."""
    rows = connection.execute(
        f"SELECT {IDENTITY_COLUMNS} FROM hub.credential WHERE owner_id = %s AND kind = %s"
        " ORDER BY updated_at DESC, id DESC",
        (owner, SUBSCRIPTION),
    ).fetchall()
    return [identity_of(row) for row in rows]


def _primary_reading(connection: Connection, user: UUID) -> dict[str, Any] | None:
    unreadable = connection.execute(
        "SELECT since, device_id FROM hub.quota_unreadable WHERE owner_id = %s", (user,)
    ).fetchone()
    if unreadable is not None:
        return {
            "fetched_at": format_time(unreadable[0]),
            "device_id": str(unreadable[1]),
            "unreadable": True,
            "windows": [],
        }
    windows = quota_rows(connection, user)
    if not windows:
        return None
    newest = windows[0]  # newest fetch first
    found = [item for item in windows if item["device_id"] == newest["device_id"]]
    return {
        "fetched_at": newest["fetched_at"],
        "device_id": newest["device_id"],
        "unreadable": False,
        "windows": [
            {key: item[key] for key in ("window", "used_percent", "resets_at", "severity")}
            for item in sorted(found, key=lambda item: str(item["window"]))
        ],
    }


def rows_for(connection: Connection, user: UUID) -> list[dict[str, Any]]:
    """`GET /v1/me/subscriptions` (module docstring)."""
    readings = {
        row[0]: {
            "fetched_at": format_time(row[3]),
            "device_id": str(row[4]),
            "unreadable": row[2],
            "windows": [] if row[1] is None else list(row[1]),
        }
        for row in connection.execute(
            "SELECT credential_id, windows, unreadable, fetched_at, device_id FROM hub.subscription_quota"
            " WHERE owner_id = %s",
            (user,),
        ).fetchall()
    }
    found = []
    for identity in owner_records(connection, user):
        reading = _primary_reading(connection, user) if identity.primary else readings.get(identity.id)
        found.append({**subscription_meta(identity), "reading": reading})
    return found


UPSERT_READING = (
    "INSERT INTO hub.subscription_quota (credential_id, owner_id, windows, unreadable, fetched_at, device_id)"
    " SELECT c.id, c.owner_id, %(windows)s, %(unreadable)s, %(fetched)s, %(device)s FROM hub.credential c"
    " WHERE c.id = %(id)s AND c.owner_id = %(owner)s AND c.kind = 'claude_subscription' AND NOT c.is_primary"
    " ON CONFLICT (credential_id) DO UPDATE SET windows = EXCLUDED.windows, unreadable = EXCLUDED.unreadable,"
    " fetched_at = EXCLUDED.fetched_at, device_id = EXCLUDED.device_id"
    " WHERE hub.subscription_quota.fetched_at <= EXCLUDED.fetched_at"
)


def store_reading(
    connection: Connection,
    *,
    subscription: UUID,
    owner: UUID,
    windows: list[dict[str, Any]] | None,
    fetched_at: datetime,
    device: UUID,
) -> None:
    """Keep the reading of a further subscription (module docstring); `windows` None = unreadable."""
    connection.execute(
        UPSERT_READING,
        {
            "id": subscription,
            "owner": owner,
            "windows": None if windows is None else Jsonb(json.loads(json.dumps(windows))),
            "unreadable": windows is None,
            "fetched": fetched_at,
            "device": device,
        },
    )
