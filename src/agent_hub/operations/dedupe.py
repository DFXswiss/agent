"""Dedupe slots `(wg_id, epoch, tag)` (02-data-model.md section 2.5, 03-api.md section 8.3 H5/H6, ADR-012).

The runner computes the tag with the workgroup key of `epoch` (04-crypto.md section 4.10); the hub only compares tags.
A slot is **held** while its operation is `accepted`, `executing` or `succeeded` and `expires_at` lies in the future.
`claim` decides H5 and H6 in one step inside the registration's transaction:

- no slot row: the new operation takes it (`INSERT ... ON CONFLICT DO NOTHING`; a concurrent registration of the same
  tag that inserted first makes this one wait for its commit and then see its row);
- a held slot: the operation is a duplicate of the holder (`Held`);
- a slot whose window ended, or whose holder no longer holds it: the new operation takes it over.

Slots are released (deleted) when their operation ends `failed` or `expired` (the outbox base handler and the expiry
job); `rejected`, `denied` and `duplicate` operations never take one. Tags of different epochs never match: a key
rotation inside a window allows one duplicate (documented limitation of 04-crypto.md section 4.10).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg

Connection = psycopg.Connection[Any]
HOLDING_STATES = ("accepted", "executing", "succeeded")


@dataclass(frozen=True)
class Slot:
    """The slot an operation asks for."""

    wg_id: UUID
    epoch: int
    tag: str


@dataclass(frozen=True)
class Held:
    """The operation that holds the slot: its ID, state and result event (if any)."""

    operation_id: UUID
    state: str
    result_event_id: UUID | None


def claim(connection: Connection, slot: Slot, operation_id: UUID, expires_at: datetime, now: datetime) -> Held | None:
    """Take the slot for `operation_id` until `expires_at` (None) or name the operation that holds it (module
    docstring)."""
    key = (slot.wg_id, slot.epoch, slot.tag)
    inserted = connection.execute(
        "INSERT INTO hub.dedupe_slot (wg_id, epoch, tag, operation_id, expires_at) VALUES (%s, %s, %s, %s, %s)"
        " ON CONFLICT (wg_id, epoch, tag) DO NOTHING RETURNING operation_id",
        (*key, operation_id, expires_at),
    ).fetchone()
    if inserted is not None:
        return None
    row = connection.execute(
        "SELECT d.operation_id, d.expires_at, o.state, o.result_event_id FROM hub.dedupe_slot d"
        " JOIN hub.operation o ON o.id = d.operation_id WHERE d.wg_id = %s AND d.epoch = %s AND d.tag = %s"
        " FOR UPDATE OF d",
        key,
    ).fetchone()
    if row is not None and row[2] in HOLDING_STATES and row[1] > now:
        return Held(operation_id=row[0], state=row[2], result_event_id=row[3])
    connection.execute(
        "INSERT INTO hub.dedupe_slot (wg_id, epoch, tag, operation_id, expires_at) VALUES (%s, %s, %s, %s, %s)"
        " ON CONFLICT (wg_id, epoch, tag) DO UPDATE SET operation_id = EXCLUDED.operation_id,"
        " expires_at = EXCLUDED.expires_at",
        (*key, operation_id, expires_at),
    )
    return None


def release(connection: Connection, operation_id: UUID) -> None:
    """Free the slot of an operation that ended without success (`failed`, `expired`)."""
    connection.execute("DELETE FROM hub.dedupe_slot WHERE operation_id = %s", (operation_id,))
