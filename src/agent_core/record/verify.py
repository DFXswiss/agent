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
from .checkpoint import Checkpoint, checkpoint_valid, checkpoint_well_formed
from .event import (
    ERROR_CHAIN_BROKEN,
    ERROR_EVENT_INVALID,
    ERROR_EVENT_TIME_INVALID,
    RecordRefusal,
    check_chain,
    parse_event,
    record_hash,
    time_text,
    verify_event_signature,
)
from .failures import CHECKPOINT_INVALID, EVENT_REFUSED, POSITION_GAP, RECORD_HASH_MISMATCH, ROW_MISMATCH
from .reducer import (
    ERROR_CERTIFICATE_NOT_SUPPORTED,
    ERROR_EVENT_ID_TAKEN,
    check_event_time,
    check_stored,
    check_value_forms,
)
from .state import EMPTY_STATE, Applied, Head, RecordState, StateBuilder


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
    None there and the row `row_mismatch` compares only the event hash. A stored `received_at` that
    is no time of section 2 is passed as event.STORED_INFINITY describes.
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
    """A failed verification: the true value of section 12.1 and the position of the failure.

    `failure` is the value that the log lines of section 13 name (`verify_failed <position>
    <failure>` and `record verify failed <position> <failure>`); the answer of section 7.9 gives
    `visibility.answered_failure(failure)` instead.
    """

    failure: str
    position: int


@dataclass(frozen=True)
class FoldResult:
    """The state after the fold, and the failure that ended it, if any.

    On a failure the state holds the events before the failing one (section 11.6).
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
        raise _Failed(Failure(POSITION_GAP, position))
    try:
        event = parse_event(stored.text)
        check_value_forms(event)
    except RecordRefusal:
        raise _Failed(Failure(ERROR_EVENT_INVALID, position)) from None
    # Until section 10 is built, check 5 of section 6.7 as a row of section 12.1.
    if event.key_certificate is not None:
        raise _Failed(Failure(ERROR_CERTIFICATE_NOT_SUPPORTED, position))
    copies = stored.copies
    if stored.event_hash != event.event_hash or (
        copies is not None
        and (
            copies.event_id != event.event_id
            or copies.actor_key_id != event.actor_key_id
            or copies.prev_hash != event.prev_hash
        )
    ):
        raise _Failed(Failure(ROW_MISMATCH, position))
    try:
        verify_event_signature(keys, event, stored.signature)
    except RecordRefusal as refusal:
        raise _Failed(Failure(refusal.error, position)) from None
    if event.event_id in view.event_ids:
        raise _Failed(Failure(ERROR_EVENT_ID_TAKEN, position))
    key_head = view.chains.get(event.actor_key_id)
    chain = None if key_head is None else key_head.chain
    try:
        check_chain(chain, event)
    except RecordRefusal:
        raise _Failed(Failure(ERROR_CHAIN_BROKEN, position)) from None
    # A `received_at` without the form of a time of section 2 gives no record hash (section 12.1).
    received_at = stored.received_at
    if time_text(received_at) is None or (head is not None and received_at < head.received_at):
        raise _Failed(Failure(ERROR_EVENT_TIME_INVALID, position))
    try:
        check_event_time(chain, event, received_at)
    except RecordRefusal:
        raise _Failed(Failure(ERROR_EVENT_TIME_INVALID, position)) from None
    expected = record_hash(position, received_at, event.event_hash, None if head is None else head.record_hash)
    if stored.record_hash != expected or (
        memory_head is not None
        and memory_head.position == position
        and memory_head.record_hash != stored.record_hash
    ):
        raise _Failed(Failure(RECORD_HASH_MISMATCH, position))
    try:
        return check_stored(view, event, received_at)
    except RecordRefusal:
        # `value_invalid` cannot reach this point: the row `event_invalid` checked the forms.
        raise _Failed(Failure(EVENT_REFUSED, position)) from None


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


def _checkpoint_passes(
    checkpoint: Checkpoint, events: Sequence[StoredEvent], last: int, hub_keys: Mapping[str, bytes]
) -> bool:
    """The row `checkpoint_invalid` of section 12.1 for one stored checkpoint.

    The form of `published_at` and of the hub key id is checked for every checkpoint; the rest of
    the row only for a checkpoint whose hub key id is in `hub_keys`, and skipped for any other.
    """
    if not checkpoint_well_formed(checkpoint):
        return False
    public_key = hub_keys.get(checkpoint.hub_key_id)
    if public_key is None:
        return True
    at_position = events[checkpoint.position - 1].record_hash if 1 <= checkpoint.position <= last else None
    return checkpoint_valid(checkpoint, last, at_position, public_key)


def verify_record(
    events: Sequence[StoredEvent],
    checkpoints: Sequence[Checkpoint],
    keys: KeyRing,
    hub_keys: Mapping[str, bytes],
    memory_head: Head | None,
) -> FoldResult:
    """A full verification of section 12.1 (without `file_invalid`, which belongs to a file).

    The events are folded from the empty state; then the end of the record is compared with
    `memory_head`; then the checkpoints are checked in the given order (by hub key id in byte
    order, then by position): every one for the form of its `published_at` and hub key id, and
    the ones whose hub key id is in `hub_keys` also for position, record hash and signature. The
    first failure ends the run.
    """
    result = fold_events(EMPTY_STATE, events, keys, memory_head)
    if result.failure is not None:
        return result
    head = result.state.head
    last = 0 if head is None else head.position
    if memory_head is not None and last < memory_head.position:
        return FoldResult(state=result.state, failure=Failure(POSITION_GAP, last + 1))
    for checkpoint in checkpoints:
        if not _checkpoint_passes(checkpoint, events, last, hub_keys):
            return FoldResult(state=result.state, failure=Failure(CHECKPOINT_INVALID, checkpoint.position))
    return result
