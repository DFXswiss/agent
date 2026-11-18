"""The fold and the verification of the record (RECORD-PROTOCOL.md sections 6.1, 11.6 and 12.1).

Pure functions over stored events and checkpoints, the keys file and, for a running process, the
head of its state. They read no database and no clock; the storage part reads the snapshot and
passes it here.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Iterable, Mapping, Sequence

from ..keys import KeyRing
from .checkpoint import Checkpoint, checkpoint_valid
from .event import (
    Event,
    RecordRefusal,
    check_chain,
    parse_event,
    record_hash,
    time_text,
    verify_event_signature,
)
from .reducer import check_event_time, check_stored, check_value_forms
from .state import EMPTY_STATE, Applied, Head, RecordState, StateBuilder

FILE_INVALID = "file_invalid"
POSITION_GAP = "position_gap"
EVENT_INVALID = "event_invalid"
ROW_MISMATCH = "row_mismatch"
EVENT_ID_TAKEN = "event_id_taken"
CHAIN_BROKEN = "chain_broken"
EVENT_TIME_INVALID = "event_time_invalid"
RECORD_HASH_MISMATCH = "record_hash_mismatch"
EVENT_REFUSED = "event_refused"
CHECKPOINT_INVALID = "checkpoint_invalid"


@dataclass(frozen=True)
class RowCopies:
    """The columns of `record.event` copied from the event text (section 11.3)."""

    event_id: str = dataclass_field(repr=False)
    actor_key_id: str
    prev_hash: str = dataclass_field(repr=False)


@dataclass(frozen=True)
class StoredEvent:
    """One stored event: a database row or an event line of an export.

    `copies` holds the copied columns of a database row; an export line has none of them, so it is
    None there and the row `row_mismatch` compares only the event hash.
    """

    position: int
    received_at: int = dataclass_field(repr=False)
    event_hash: str = dataclass_field(repr=False)
    record_hash: str
    text: bytes = dataclass_field(repr=False)
    signature: bytes = dataclass_field(repr=False)
    copies: RowCopies | None = dataclass_field(repr=False)


@dataclass(frozen=True)
class Failure:
    """A failed verification: the value of section 12.1 and the position of the failure.

    `event` is the failing event when its text could be read and the failure concerns it; None for
    `position_gap`, `checkpoint_invalid`, `file_invalid` and an event text that breaks section 4.2.
    """

    failure: str
    position: int
    event: Event | None = dataclass_field(repr=False)


@dataclass(frozen=True)
class FoldResult:
    """The state after the fold, and the failure that ended it, if any.

    On a failure the state holds the events before the failing one (sections 7.9 and 11.6).
    """

    state: RecordState = dataclass_field(repr=False)
    failure: Failure | None


class _Failed(Exception):
    def __init__(self, failure: Failure) -> None:
        super().__init__(failure.failure)
        self.failure = failure


def _fold_one(
    view: StateBuilder, stored: StoredEvent, keys: KeyRing, memory_head: Head | None
) -> Applied:
    """The rows of section 12.1 from `position_gap` to `event_refused`, in the order of the table."""
    head = view.head
    position = 1 if head is None else head.position + 1
    if stored.position != position:
        raise _Failed(Failure(POSITION_GAP, position, None))
    try:
        event = parse_event(stored.text)
    except RecordRefusal:
        raise _Failed(Failure(EVENT_INVALID, position, None)) from None
    try:
        check_value_forms(event)
    except RecordRefusal:
        raise _Failed(Failure(EVENT_INVALID, position, event)) from None
    copies = stored.copies
    if stored.event_hash != event.event_hash or (
        copies is not None
        and (
            copies.event_id != event.event_id
            or copies.actor_key_id != event.actor_key_id
            or copies.prev_hash != event.prev_hash
        )
    ):
        raise _Failed(Failure(ROW_MISMATCH, position, event))
    try:
        verify_event_signature(keys, event, stored.signature)
    except RecordRefusal as refusal:
        raise _Failed(Failure(refusal.error, position, event)) from None
    if event.event_id in view.event_ids:
        raise _Failed(Failure(EVENT_ID_TAKEN, position, event))
    key_head = view.chains.get(event.actor_key_id)
    chain = None if key_head is None else key_head.chain
    try:
        check_chain(chain, event)
    except RecordRefusal:
        raise _Failed(Failure(CHAIN_BROKEN, position, event)) from None
    received_at = stored.received_at
    if time_text(received_at) is None or (head is not None and received_at < head.received_at):
        raise _Failed(Failure(EVENT_TIME_INVALID, position, event))
    try:
        check_event_time(chain, event, received_at)
    except RecordRefusal:
        raise _Failed(Failure(EVENT_TIME_INVALID, position, event)) from None
    expected = record_hash(position, received_at, event.event_hash, None if head is None else head.record_hash)
    if stored.record_hash != expected or (
        memory_head is not None
        and memory_head.position == position
        and memory_head.record_hash != stored.record_hash
    ):
        raise _Failed(Failure(RECORD_HASH_MISMATCH, position, event))
    try:
        return check_stored(view, event, received_at)
    except RecordRefusal:
        # `value_invalid` cannot reach this point: the row `event_invalid` checked the forms.
        raise _Failed(Failure(EVENT_REFUSED, position, event)) from None


def fold_events(
    state: RecordState, events: Iterable[StoredEvent], keys: KeyRing, memory_head: Head | None
) -> FoldResult:
    """Fold stored events in position order onto `state` (section 6.1; the incremental fold of 11.6).

    The first event has the position after the head of `state`. `memory_head` is the head of the
    state of a running process for a full verification on request or a periodic one, otherwise None.
    """
    builder = StateBuilder(state)
    for stored in events:
        try:
            applied = _fold_one(builder, stored, keys, memory_head)
        except _Failed as failed:
            return FoldResult(state=builder.freeze(), failure=failed.failure)
        builder.apply(applied)
    return FoldResult(state=builder.freeze(), failure=None)


def verify_record(
    events: Sequence[StoredEvent],
    checkpoints: Sequence[Checkpoint],
    keys: KeyRing,
    hub_keys: Mapping[str, bytes],
    memory_head: Head | None,
) -> FoldResult:
    """A full verification of section 12.1 (without `file_invalid`, which belongs to a file).

    The events are folded from the empty state; then the end of the record is compared with
    `memory_head`; then every checkpoint whose hub key id is in `hub_keys` is checked, in the given
    order (by hub key id in byte order, then by position). Checkpoints of other hub key ids are
    skipped.
    """
    result = fold_events(EMPTY_STATE, events, keys, memory_head)
    if result.failure is not None:
        return result
    head = result.state.head
    last = 0 if head is None else head.position
    if memory_head is not None and last < memory_head.position:
        return FoldResult(state=result.state, failure=Failure(POSITION_GAP, last + 1, None))
    for checkpoint in checkpoints:
        public_key = hub_keys.get(checkpoint.hub_key_id)
        if public_key is None:
            continue
        at_position = (
            events[checkpoint.position - 1].record_hash if 1 <= checkpoint.position <= last else None
        )
        if not checkpoint_valid(checkpoint, last, at_position, public_key):
            return FoldResult(
                state=result.state, failure=Failure(CHECKPOINT_INVALID, checkpoint.position, None)
            )
    return result
