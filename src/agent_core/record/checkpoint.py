"""Checkpoints of the record hash (RECORD-PROTOCOL.md section 12.2), as pure functions.

The checkpoint text, its signature check, the full hour, the check of the newest checkpoint of
step 3 of a checkpoint attempt and the decision of step 4. Reading and inserting rows, the clock
and the advisory lock belong to the storage part.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field

from ..signing import valid_key_id
from .event import MAX_POSITION, signature_valid, time_text, valid_hash

CHECKPOINT_LABEL = "agent-record-head-v1"
FULL_HOUR_SECONDS = 3600


@dataclass(frozen=True)
class Checkpoint:
    """A stored checkpoint. Section 13 allows a hub key id, a position and a record hash in a log."""

    hub_key_id: str
    position: int
    record_hash: str
    published_at: int = dataclass_field(repr=False)
    signature: bytes = dataclass_field(repr=False)


def checkpoint_text(hub_key_id: str, position: int, record_hash: str, published_at: int) -> bytes:
    """The signed text of section 12.2; `ValueError` for a part outside its form."""
    if not valid_key_id(hub_key_id):
        raise ValueError("the hub key id breaks the id rule")
    if type(position) is not int or not 1 <= position <= MAX_POSITION:
        raise ValueError("a position is an int from 1 to 9223372036854775807")
    if not valid_hash(record_hash):
        raise ValueError("a record hash is 64 lowercase hex characters")
    published = time_text(published_at)
    if published is None:
        raise ValueError("published_at is not a time")
    return "\n".join([CHECKPOINT_LABEL, hub_key_id, str(position), record_hash, published]).encode("ascii")


def checkpoint_signature_valid(public_key: bytes, checkpoint: Checkpoint) -> bool:
    """Whether the signature verifies with the hub's public key; False for a checkpoint whose parts
    cannot form a checkpoint text."""
    try:
        text = checkpoint_text(
            checkpoint.hub_key_id, checkpoint.position, checkpoint.record_hash, checkpoint.published_at
        )
    except ValueError:
        return False
    return signature_valid(public_key, text, checkpoint.signature)


def checkpoint_valid(
    checkpoint: Checkpoint, head_position: int, record_hash_at_position: str | None, public_key: bytes
) -> bool:
    """The row `checkpoint_invalid` of section 12.1 and step 3 of section 12.2.

    `record_hash_at_position` is the stored record hash of the event at the checkpoint's position,
    None when no event is there.
    """
    return (
        1 <= checkpoint.position <= head_position
        and record_hash_at_position is not None
        and checkpoint.record_hash == record_hash_at_position
        and checkpoint_signature_valid(public_key, checkpoint)
    )


def full_hour(now: int) -> int:
    """The latest full hour at or before `now` (section 12.2)."""
    return now - now % FULL_HOUR_SECONDS


def checkpoint_due(head_position: int, newest: Checkpoint | None, hour: int) -> bool:
    """Step 4 of section 12.2: whether an attempt for the full hour `hour` writes a checkpoint.

    Nothing is written for an empty record, when the head did not move since the newest checkpoint
    of the hub key id, or when that checkpoint was published at or after `hour`.
    """
    if head_position == 0:
        return False
    if newest is None:
        return True
    return newest.position != head_position and newest.published_at < hour
