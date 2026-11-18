"""The fold and the verification of the record (RECORD-PROTOCOL.md sections 6.1, 11.6, 12.1 and 12.2).

Pure functions over stored events, checkpoints and the keys file: every failure value of section
12.1 except `file_invalid` (tested with the export format), with its position.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from agent_core.keys import KeyRing
from agent_core.record.checkpoint import (
    Checkpoint,
    checkpoint_due,
    checkpoint_signature_valid,
    checkpoint_text,
    checkpoint_valid,
    full_hour,
)
from agent_core.record.event import Change, build_event_text, parse_event, record_hash
from agent_core.record.state import EMPTY_STATE, Head
from agent_core.record.verify import RowCopies, StoredEvent, fold_events, verify_record
from tests.test_record_reducer import (
    ALICE,
    BOB,
    EVENTS,
    STANDARD,
    STEP,
    T0,
    World,
    create_work,
    new_id,
    private,
    public,
)

HUB = "hub-1"
HUB_SEED = 30
HUB_KEYS = {HUB: public(HUB_SEED)}


def story() -> tuple[World, str]:
    """Three events: alice creates a task, bob renames it, alice comments on it."""
    world = World(STANDARD)
    work = create_work(world, ALICE.id, [])
    world.write(BOB.id, "task", work, [("title", "Fix login", "Fix it")], EVENTS)
    world.write(ALICE.id, "comment", new_id(), [("task", None, work), ("body", None, "x")], EVENTS)
    return world, work


def rehash(events: list[StoredEvent]) -> list[StoredEvent]:
    out: list[StoredEvent] = []
    previous = None
    for stored in events:
        value = record_hash(stored.position, stored.received_at, stored.event_hash, previous)
        out.append(replace(stored, record_hash=value))
        previous = value
    return out


def forged(world: World, key_id: str, text: bytes, position: int, received_at: int) -> StoredEvent:
    event = parse_event(text)
    return StoredEvent(
        position=position,
        received_at=received_at,
        event_hash=event.event_hash,
        record_hash="0" * 64,
        text=text,
        signature=private(_seed(key_id)).sign(text),
        copies=RowCopies(event.event_id, event.actor_key_id, event.prev_hash),
    )


def _seed(key_id: str) -> int:
    from tests.test_record_reducer import SEEDS

    return SEEDS[key_id]


def text_of(key_id: str, event_id: str, object_type: str, object_id: str, signed_at: int, prev_hash: str,
            changes: list[Change]) -> bytes:
    return build_event_text(event_id=event_id, object_type=object_type, object_id=object_id, actor_key_id=key_id,
                            signed_at=signed_at, prev_hash=prev_hash, changes=changes, key_certificate=None)


def failure_of(events: list[StoredEvent], keys: KeyRing) -> tuple[str, int] | None:
    result = verify_record(events, [], keys, {}, None)
    return None if result.failure is None else (result.failure.failure, result.failure.position)


def checkpoint(position: int, value: str, published_at: int, hub: str, seed: int) -> Checkpoint:
    signature = private(seed).sign(checkpoint_text(hub, position, value, published_at))
    return Checkpoint(hub_key_id=hub, position=position, record_hash=value, published_at=published_at,
                      signature=signature)


# --- sound records -----------------------------------------------------------------------------------


def test_the_fold_of_the_stored_record_gives_the_state_of_the_hub() -> None:
    world, _ = story()
    result = fold_events(EMPTY_STATE, world.stored, world.keys, None)
    assert result.failure is None
    assert result.state == world.state
    verified = verify_record(world.stored, [], world.keys, {}, None)
    assert verified.failure is None
    assert verified.state.head == world.state.head


def test_an_empty_record_verifies() -> None:
    result = verify_record([], [], KeyRing({}), HUB_KEYS, None)
    assert result.failure is None
    assert result.state.head is None


def test_the_incremental_fold_continues_a_state() -> None:
    world, _ = story()
    first = fold_events(EMPTY_STATE, world.stored[:1], world.keys, None)
    rest = fold_events(first.state, world.stored[1:], world.keys, None)
    assert rest.failure is None
    assert rest.state == world.state
    # The first event it reads has the position after the head of the state.
    again = fold_events(rest.state, world.stored[2:], world.keys, None)
    assert again.failure is not None
    assert (again.failure.failure, again.failure.position) == ("position_gap", 4)
    assert again.state == world.state


def test_a_verification_of_a_running_process_compares_its_head() -> None:
    world, _ = story()
    head = world.state.head
    assert head is not None
    assert verify_record(world.stored, [], world.keys, {}, head).failure is None
    wrong = Head(position=2, received_at=0, record_hash="f" * 64)
    result = verify_record(world.stored, [], world.keys, {}, wrong)
    assert result.failure is not None
    assert (result.failure.failure, result.failure.position) == ("record_hash_mismatch", 2)
    beyond = Head(position=5, received_at=0, record_hash="f" * 64)
    result = verify_record(world.stored, [], world.keys, {}, beyond)
    assert result.failure is not None
    assert (result.failure.failure, result.failure.position) == ("position_gap", 4)
    assert result.failure.event is None
    empty = verify_record([], [], world.keys, {}, beyond)
    assert empty.failure is not None
    assert (empty.failure.failure, empty.failure.position) == ("position_gap", 1)


# --- every failure of section 12.1 -----------------------------------------------------------------


def test_position_gap() -> None:
    world, _ = story()
    events = world.stored
    assert failure_of([events[0], events[2]], world.keys) == ("position_gap", 2)
    assert failure_of([events[0], events[0]], world.keys) == ("position_gap", 2)
    assert failure_of(events[1:], world.keys) == ("position_gap", 1)


def test_event_invalid_for_a_text_and_for_a_value() -> None:
    world, work = story()
    broken = replace(world.stored[2], text=b"agent-event-v1")
    result = verify_record([*world.stored[:2], broken], [], world.keys, {}, None)
    assert result.failure is not None
    assert (result.failure.failure, result.failure.position, result.failure.event) == ("event_invalid", 3, None)
    assert result.state.head is not None
    assert result.state.head.position == 2
    malformed = text_of(ALICE.id, new_id(), "task", work, world.now, world.prev(ALICE.id),
                        [Change("title", b"Fix it", b" x")])
    events = rehash([*world.stored, forged(world, ALICE.id, malformed, 4, world.now)])
    result = verify_record(events, [], world.keys, {}, None)
    assert result.failure is not None
    assert (result.failure.failure, result.failure.position) == ("event_invalid", 4)
    assert result.failure.event is not None


@pytest.mark.parametrize("column", ["event_hash", "event_id", "actor_key_id", "prev_hash"])
def test_row_mismatch(column: str) -> None:
    world, _ = story()
    stored = world.stored[1]
    copies = stored.copies
    assert copies is not None
    if column == "event_hash":
        changed = replace(stored, event_hash="0" * 64)
    elif column == "event_id":
        changed = replace(stored, copies=replace(copies, event_id=new_id()))
    elif column == "actor_key_id":
        changed = replace(stored, copies=replace(copies, actor_key_id=ALICE.id))
    else:
        changed = replace(stored, copies=replace(copies, prev_hash="a" * 64))
    events = [world.stored[0], changed, world.stored[2]]
    assert failure_of(events, world.keys) == ("row_mismatch", 2)


def test_an_export_row_compares_only_the_event_hash() -> None:
    world, _ = story()
    events = [replace(stored, copies=None) for stored in world.stored]
    assert failure_of(events, world.keys) is None


def test_key_unknown_and_event_signature_invalid() -> None:
    world, _ = story()
    without_bob = KeyRing({entry_id: entry for entry_id, entry in world.keys.entries.items() if entry_id != BOB.id})
    assert failure_of(world.stored, without_bob) == ("key_unknown", 2)
    resigned = replace(world.stored[1], signature=private(1).sign(world.stored[1].text))
    assert failure_of([world.stored[0], resigned, world.stored[2]], world.keys) == ("event_signature_invalid", 2)


def test_event_id_taken_for_two_events_of_two_keys() -> None:
    world, work = story()
    used = parse_event(world.stored[0].text).event_id
    text = text_of(BOB.id, used, "task", work, world.now, world.prev(BOB.id), [Change("title", b"Fix it", b"Fix")])
    events = rehash([*world.stored, forged(world, BOB.id, text, 4, world.now)])
    assert failure_of(events, world.keys) == ("event_id_taken", 4)
    # The same text twice fails here too.
    twice = rehash([*world.stored, replace(world.stored[2], position=4)])
    assert failure_of(twice, world.keys) == ("event_id_taken", 4)


def test_chain_broken() -> None:
    world, work = story()
    first, _, third = world.stored
    swapped = rehash([replace(third, position=1), replace(first, position=2)])
    assert failure_of(swapped, world.keys) == ("chain_broken", 1)
    fork = text_of(ALICE.id, new_id(), "task", work, world.now, parse_event(first.text).event_hash,
                   [Change("title", b"Fix it", b"Fix")])
    events = rehash([*world.stored, forged(world, ALICE.id, fork, 4, world.now)])
    assert failure_of(events, world.keys) == ("chain_broken", 4)


def test_event_time_invalid() -> None:
    world, work = story()
    first, second, third = world.stored
    earlier = rehash([first, replace(second, received_at=first.received_at - 1), third])
    assert failure_of(earlier, world.keys) == ("event_time_invalid", 2)
    signed_at = parse_event(first.text).signed_at
    ahead = rehash([replace(first, received_at=signed_at - 121), second, third])
    assert failure_of(ahead, world.keys) == ("event_time_invalid", 1)
    accepted = rehash([replace(first, received_at=signed_at - 120), second, third])
    assert failure_of(accepted, world.keys) is None
    back = text_of(ALICE.id, new_id(), "task", work, T0, world.prev(ALICE.id), [Change("title", b"Fix it", b"Fix")])
    events = rehash([*world.stored, forged(world, ALICE.id, back, 4, world.now)])
    assert failure_of(events, world.keys) == ("event_time_invalid", 4)
    # A received_at outside the form of a time cannot enter a record hash.
    outside = [*world.stored[:2], replace(third, received_at=10**12)]
    assert failure_of(outside, world.keys) == ("event_time_invalid", 3)


def test_record_hash_mismatch() -> None:
    world, _ = story()
    events = [world.stored[0], replace(world.stored[1], record_hash="0" * 64), world.stored[2]]
    assert failure_of(events, world.keys) == ("record_hash_mismatch", 2)


def test_event_refused_and_the_state_before_it() -> None:
    world, work = story()
    stale = text_of(ALICE.id, new_id(), "task", work, world.now, world.prev(ALICE.id),
                    [Change("title", b"Fix login", b"Fix")])
    events = rehash([*world.stored, forged(world, ALICE.id, stale, 4, world.now)])
    result = verify_record(events, [], world.keys, {}, None)
    assert result.failure is not None
    assert (result.failure.failure, result.failure.position) == ("event_refused", 4)
    assert result.failure.event is not None and result.failure.event.object_id == work
    assert result.state == world.state


def test_actor_rules_are_not_checked_in_a_fold() -> None:
    world, work = story()
    # Bob's key is revoked later and loses its scopes: the stored events stay valid.
    revoked = replace(BOB, revoked=T0, scopes=("device:executor",))
    keys = KeyRing({**world.keys.entries, BOB.id: revoked})
    assert failure_of(world.stored, keys) is None


# --- checkpoints -----------------------------------------------------------------------------------


def test_checkpoints_are_checked_for_the_given_hub_key_ids() -> None:
    world, _ = story()
    last = world.stored[2]
    good = checkpoint(3, last.record_hash, T0 + 3600, HUB, HUB_SEED)
    other_hub = checkpoint(9, "0" * 64, T0, "hub-2", 31)
    result = verify_record(world.stored, [good, other_hub], world.keys, HUB_KEYS, None)
    assert result.failure is None
    for bad in (
        checkpoint(9, last.record_hash, T0, HUB, HUB_SEED),
        checkpoint(2, last.record_hash, T0, HUB, HUB_SEED),
        checkpoint(3, last.record_hash, T0, HUB, 31),
        replace(good, position=0),
        replace(good, published_at=10**12),
    ):
        result = verify_record(world.stored, [good, bad], world.keys, HUB_KEYS, None)
        assert result.failure is not None
        assert (result.failure.failure, result.failure.position) == ("checkpoint_invalid", bad.position)
    # Without the hub key id, the same checkpoint is skipped.
    skipped = checkpoint(9, last.record_hash, T0, HUB, HUB_SEED)
    assert verify_record(world.stored, [skipped], world.keys, {}, None).failure is None


def test_a_checkpoint_of_an_empty_record_is_beyond_the_head() -> None:
    result = verify_record([], [checkpoint(1, "a" * 64, T0, HUB, HUB_SEED)], KeyRing({}), HUB_KEYS, None)
    assert result.failure is not None
    assert (result.failure.failure, result.failure.position) == ("checkpoint_invalid", 1)


def test_the_checkpoint_text() -> None:
    text = checkpoint_text(HUB, 21, "a" * 64, 1795003200)
    assert text == b"agent-record-head-v1\nhub-1\n21\n" + b"a" * 64 + b"\n2026-11-18T12:00:00Z"
    assert hashlib.sha256(text).hexdigest() == hashlib.sha256(
        "\n".join(["agent-record-head-v1", "hub-1", "21", "a" * 64, "2026-11-18T12:00:00Z"]).encode()
    ).hexdigest()
    for arguments in (("H", 1, "a" * 64, 0), (HUB, 0, "a" * 64, 0), (HUB, True, "a" * 64, 0),
                      (HUB, 1, "A" * 64, 0), (HUB, 1, "a" * 64, 10**12)):
        with pytest.raises(ValueError):
            checkpoint_text(*arguments)  # type: ignore[arg-type]


def test_a_checkpoint_signature_and_its_check() -> None:
    valid = checkpoint(2, "b" * 64, T0, HUB, HUB_SEED)
    assert checkpoint_signature_valid(public(HUB_SEED), valid)
    assert not checkpoint_signature_valid(public(31), valid)
    assert not checkpoint_signature_valid(public(HUB_SEED), replace(valid, published_at=T0 + 1))
    assert not checkpoint_signature_valid(public(HUB_SEED), replace(valid, hub_key_id="X"))
    assert checkpoint_valid(valid, 2, "b" * 64, public(HUB_SEED))
    assert not checkpoint_valid(valid, 1, "b" * 64, public(HUB_SEED))
    assert not checkpoint_valid(valid, 2, None, public(HUB_SEED))
    assert not checkpoint_valid(valid, 2, "c" * 64, public(HUB_SEED))
    assert repr(valid) == f"Checkpoint(hub_key_id='hub-1', position=2, record_hash='{'b' * 64}')"


def test_full_hours_and_when_a_checkpoint_is_due() -> None:
    hour = 1795003200
    assert hour % 3600 == 0
    assert full_hour(hour) == hour
    assert full_hour(hour + 1799) == hour
    assert full_hour(hour - 1) == hour - 3600
    newest = checkpoint(20, "a" * 64, hour - 3600, HUB, HUB_SEED)
    assert not checkpoint_due(0, None, hour)
    assert checkpoint_due(1, None, hour)
    assert checkpoint_due(21, newest, hour)
    assert not checkpoint_due(20, newest, hour)
    assert not checkpoint_due(21, replace(newest, published_at=hour), hour)
    assert not checkpoint_due(21, replace(newest, published_at=hour + 5), hour)
    # After the clock was set back, a full hour reached again writes nothing.
    assert not checkpoint_due(21, replace(newest, published_at=hour + 3600), hour)
    assert STEP == 60
