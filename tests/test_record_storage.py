"""The record in PostgreSQL (src/agent_core/record/storage.py).

RECORD-PROTOCOL.md sections 11.4 (steps `schema` and `writer`), 11.5 (the append transaction), 11.6
(the incremental fold), 12.1 (the snapshot and the full verification) and 12.2 (the checkpoint
attempt), with the tests of section 14 that concern them, run on the functions directly against
the real PostgreSQL of AGENT_CORE_TEST_POSTGRES. Every row change "as database superuser with the
triggers bypassed" disables the trigger for that statement and enables it ALWAYS again.
"""

from __future__ import annotations

import secrets
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import psycopg
import pytest
from psycopg import sql

from agent_core.keys import KeyRing
from agent_core.record.checkpoint import Checkpoint, checkpoint_signature_valid, full_hour
from agent_core.record.connection import parse_connection_string
from agent_core.record.event import MAX_TIME, MIN_TIME, STORED_INFINITY, parse_event, record_hash
from agent_core.record.state import Head
from agent_core.record.storage import (
    SCHEMA_MISSING,
    SCHEMA_VERSION,
    STORED_FRACTION,
    Appended,
    CheckpointWritten,
    Refused,
    StorageError,
    Unverified,
    append_event,
    check_schema,
    checkpoint_attempt,
    incremental_fold,
    read_snapshot,
    stored_seconds,
    verify_stored,
    writer_connection,
    writer_rights_hold,
)
from agent_core.record.verify import Failure
from tests.conftest import RecordDatabase
from tests.record_helpers import (
    ALICE,
    BOB,
    HUB,
    HUB_PUBLIC,
    HUB_SIGNER,
    OTHER_HUB_SIGNER,
    SEEDS,
    STEP,
    T0,
    Parallel,
    Process,
    append_lock_held,
    backend_of,
    closed_port,
    counts,
    insert_as_superuser,
    insert_checkpoint,
    load_ring,
    migrated,
    new_id,
    process,
    sign_event,
    signed_checkpoint,
    stored_event,
    tamper,
    task_create,
    terminate,
    timestamp,
    unreachable_url,
    wait_for_append_lock,
    write_key_files,
    writer,
)
from tests.test_record_reducer import private

H1 = full_hour(T0) + 3600
HUB_KEYS = {HUB: HUB_PUBLIC}


@pytest.fixture
def ring(tmp_path: Path) -> KeyRing:
    return load_ring(*write_key_files(tmp_path))


def attempt(hub: Process, hour: int, now: int, **hooks: Callable[[], None]) -> Any:
    hub.clock.now = now
    return checkpoint_attempt(hub.connection, hub.memory, hub.keys, HUB_SIGNER, hour, hub.clock, **hooks)


def failure_of(db: RecordDatabase, keys: KeyRing, memory_head: Head | None = None) -> tuple[str, int] | None:
    with writer(db) as connection:
        result = verify_stored(connection, keys, HUB_KEYS, memory_head)
    return None if result.failure is None else (result.failure.failure, result.failure.position)


def next_event(hub: Process, key_id: str, object_type: str, object_id: str, changes: Any, **options: Any) -> Any:
    """A validly signed and chained event at the position after the head of `hub`'s state, one
    minute after its `received_at`, as a hub would store it."""
    head = hub.memory.state.head
    assert head is not None
    received_at = head.received_at + STEP
    signed_at = options.pop("signed_at", received_at)
    text, signature = sign_event(hub.memory.state, key_id, object_type, object_id, changes, signed_at, **options)
    return stored_event(head.position + 1, received_at, text, signature, head.record_hash)


def comment_on(work: str) -> list[tuple[str, str | None, str | None]]:
    return [("task", None, work), ("body", None, "Again")]


# --- stored values ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("epoch", "seconds"),
    [
        (Decimal("1794909600.000000"), 1794909600),
        (Decimal("253402300800.000000"), MAX_TIME + 1),
        (Decimal("-62135596801.000000"), MIN_TIME - 1),
        (Decimal("Infinity"), STORED_INFINITY),
        (Decimal("-Infinity"), -STORED_INFINITY),
        (Decimal("1794909600.5"), STORED_FRACTION),
    ],
    ids=["a time", "year 10000", "year 0", "infinity", "-infinity", "a fraction"],
)
def test_a_stored_time_maps_to_the_seconds_the_library_takes(epoch: Decimal, seconds: int) -> None:
    assert stored_seconds(epoch) == seconds


@pytest.mark.parametrize(
    ("stored", "seconds"),
    [
        ("2026-11-17 10:02:00+00", T0 + 120),
        ("10000-01-01 00:00:00+00", MAX_TIME + 1),
        ("0001-12-31 23:59:59+00 BC", MIN_TIME - 1),
        ("infinity", STORED_INFINITY),
        ("-infinity", -STORED_INFINITY),
    ],
    ids=["a time", "year 10000", "year 0", "infinity", "-infinity"],
)
def test_the_snapshot_reads_received_at_and_published_at_as_the_library_takes_them(
    record_db: RecordDatabase, ring: KeyRing, stored: str, seconds: int
) -> None:
    migrated(record_db)
    with process(record_db, ring) as hub:
        hub.story()
    tamper(record_db, "record.event", "UPDATE record.event SET received_at = %s::timestamptz WHERE position = 3", (stored,))
    insert_checkpoint(record_db, signed_checkpoint(HUB_SIGNER, 3, "a" * 64, T0), published_at=stored)
    with writer(record_db) as connection:
        snapshot = read_snapshot(connection)
    assert snapshot.events[2].received_at == seconds
    assert [item.published_at for item in snapshot.checkpoints] == [seconds]


def test_the_snapshot_orders_events_by_position_and_checkpoints_by_hub_key_id_in_byte_order(
    record_db_en_us: RecordDatabase, ring: KeyRing
) -> None:
    db = record_db_en_us
    migrated(db)
    with process(db, ring) as hub:
        hub.story()
        texts = [parse_event(stored.text) for stored in read_snapshot(hub.connection).events]
    # In the collation `en_US.utf8` `hub1` sorts before `hub-2`; in byte order it sorts after it.
    for hub_key_id, position in (("hub1", 3), ("hub-2", 3), ("hub-2", 1), ("Hub-2", 2)):
        insert_checkpoint(db, Checkpoint(hub_key_id, position, "b" * 64, T0, bytes(64)))
    with writer(db) as connection:
        snapshot = read_snapshot(connection)
    assert [stored.position for stored in snapshot.events] == [1, 2, 3]
    assert [(stored.copies.event_id, stored.copies.actor_key_id, stored.copies.prev_hash) for stored in snapshot.events] == [  # type: ignore[union-attr]
        (event.event_id, event.actor_key_id, event.prev_hash) for event in texts
    ]
    assert [(item.hub_key_id, item.position) for item in snapshot.checkpoints] == [
        ("Hub-2", 2),
        ("hub-2", 1),
        ("hub-2", 3),
        ("hub1", 3),
    ]


def test_one_snapshot_leaves_out_an_append_and_a_checkpoint_committed_between_its_two_reads(
    record_db: RecordDatabase, ring: KeyRing
) -> None:
    migrated(record_db)
    with process(record_db, ring) as first, process(record_db, ring) as second:
        work = first.story()
        second.clock.now = first.clock.now

        def append_and_checkpoint() -> None:
            assert isinstance(second.append(BOB, "comment", new_id(), comment_on(work)), Appended)
            assert attempt(second, H1, H1) == CheckpointWritten(4, second.memory.state.head.record_hash)  # type: ignore[union-attr]

        result = verify_stored(first.connection, ring, HUB_KEYS, None, append_and_checkpoint)
        assert result.failure is None
        assert result.state.head is not None and result.state.head.position == 3
        # The next snapshot holds both.
        snapshot = read_snapshot(first.connection)
        assert (len(snapshot.events), len(snapshot.checkpoints)) == (4, 1)
        assert failure_of(record_db, ring) is None


def test_a_snapshot_whose_connection_ends_during_the_read_is_a_storage_error_of_step_read(
    record_db: RecordDatabase, ring: KeyRing
) -> None:
    migrated(record_db)
    with process(record_db, ring) as hub:
        hub.story()
        with pytest.raises(StorageError) as failed:
            read_snapshot(hub.connection, lambda: terminate(record_db, backend_of(hub.connection)))
    assert failed.value.step == "read"
    assert str(failed.value) == "read"


def test_a_snapshot_of_a_database_without_the_schema_is_a_storage_error(record_db: RecordDatabase) -> None:
    with writer(record_db) as connection:
        with pytest.raises(StorageError) as failed:
            read_snapshot(connection)
    assert failed.value.step == "read"
    assert failed.value.sqlstate in ("42P01", "3F000")
    assert failed.value.sqlstate_text == failed.value.sqlstate


def test_a_connection_that_fails_is_a_storage_error_of_step_connect_without_sqlstate(record_db: RecordDatabase) -> None:
    unreachable = parse_connection_string(unreachable_url(record_db, closed_port()))
    with pytest.raises(StorageError) as failed:
        with writer_connection(unreachable):
            raise AssertionError("no connection is opened")
    assert (failed.value.step, failed.value.sqlstate, failed.value.sqlstate_text) == ("connect", None, "-")


def test_a_writer_connection_acts_as_the_writer_with_pg_catalog_first_and_is_closed_after(
    record_db: RecordDatabase,
) -> None:
    migrated(record_db)
    with writer_connection(parse_connection_string(record_db.writer_url)) as connection:
        row = connection.execute("SELECT session_user, current_user, current_setting('search_path')").fetchone()
        assert connection.autocommit
    assert row == (record_db.writer_role, record_db.writer_role, "pg_catalog, pg_temp")
    assert connection.closed


# --- the full verification (section 12.1) --------------------------------------------------------


def drop_constraint(db: RecordDatabase, name: str) -> None:
    with db.admin() as admin:
        admin.execute(sql.SQL("ALTER TABLE record.event DROP CONSTRAINT {}").format(sql.Identifier(name)))


def update_event(column: str, value: Any, position: int = 2) -> Callable[[RecordDatabase, Process, Path], None]:
    def change(db: RecordDatabase, hub: Process, tmp_path: Path) -> None:
        tamper(
            db,
            "record.event",
            f"UPDATE record.event SET {column} = %s WHERE position = %s",
            (value() if callable(value) else value, position),
        )

    return change


def insert_next(make: Callable[[Process], Any], drop: str | None = None) -> Callable[[RecordDatabase, Process, Path], None]:
    def change(db: RecordDatabase, hub: Process, tmp_path: Path) -> None:
        if drop is not None:
            drop_constraint(db, drop)
        insert_as_superuser(db, make(hub))

    return change


def first_event_id(hub: Process) -> str:
    with writer(hub.db) as connection:
        return parse_event(read_snapshot(connection).events[0].text).event_id


def signature_of_another_key(hub: Process) -> bytes:
    with writer(hub.db) as connection:
        return private(SEEDS[ALICE]).sign(read_snapshot(connection).events[1].text)


def update_checkpoint(column: str, value: Any) -> Callable[[RecordDatabase, Process, Path], None]:
    def change(db: RecordDatabase, hub: Process, tmp_path: Path) -> None:
        tamper(db, "record.checkpoint", f"UPDATE record.checkpoint SET {column} = %s", (value,))

    return change


def other_checkpoint(hub_key_id: str, position: int, published_at: str) -> Callable[[RecordDatabase, Process, Path], None]:
    def change(db: RecordDatabase, hub: Process, tmp_path: Path) -> None:
        insert_checkpoint(db, Checkpoint(hub_key_id, position, "c" * 64, T0, bytes(64)), published_at=published_at)

    return change


def keys_without_bob(db: RecordDatabase, hub: Process, tmp_path: Path) -> KeyRing:
    directory = tmp_path / "without-bob"
    directory.mkdir()
    return load_ring(*write_key_files(directory, (ALICE,)))


def fraction(db: RecordDatabase, hub: Process, tmp_path: Path) -> None:
    drop_constraint(db, "event_received_at_check")
    tamper(db, "record.event", "UPDATE record.event SET received_at = received_at + interval '0.5 second' WHERE position = 3")


def own_checkpoint_beyond_the_head(db: RecordDatabase, hub: Process, tmp_path: Path) -> None:
    insert_checkpoint(db, signed_checkpoint(HUB_SIGNER, 7, "c" * 64, T0))


def work_of(hub: Process) -> str:
    return next(object_id for object_id, item in hub.memory.state.objects.items() if item.object_type == "task")


FAILURES: dict[str, tuple[Callable[[RecordDatabase, Process, Path], Any], tuple[str, int]]] = {
    "an event text outside section 4.2": (update_event("event_text", b"x"), ("event_invalid", 2)),
    "a value outside its form": (
        insert_next(lambda hub: next_event(hub, BOB, "task", work_of(hub), [("title", "Fix the login", "x" * 201)])),
        ("event_invalid", 4),
    ),
    "a key certificate": (
        insert_next(lambda hub: next_event(hub, ALICE, "comment", new_id(), comment_on(work_of(hub)), certificate=b"cert")),
        ("certificate_not_supported", 4),
    ),
    "the event_id column": (update_event("event_id", lambda: new_id()), ("row_mismatch", 2)),
    "the actor_key_id column": (update_event("actor_key_id", "other-laptop-1"), ("row_mismatch", 2)),
    "the prev_hash column": (update_event("prev_hash", "f" * 64), ("row_mismatch", 2)),
    "the event_hash column": (update_event("event_hash", "e" * 64), ("row_mismatch", 2)),
    "a key missing from the keys file": (keys_without_bob, ("key_unknown", 2)),
    "a signature by another key": (
        lambda db, hub, tmp_path: update_event("signature", signature_of_another_key(hub))(db, hub, tmp_path),
        ("event_signature_invalid", 2),
    ),
    "an event_id of an earlier event": (
        insert_next(
            lambda hub: next_event(hub, BOB, "comment", new_id(), comment_on(work_of(hub)), event_id=first_event_id(hub)),
            drop="event_event_id_key",
        ),
        ("event_id_taken", 4),
    ),
    "a wrong prev_hash": (
        insert_next(lambda hub: next_event(hub, ALICE, "comment", new_id(), comment_on(work_of(hub)), prev_hash="1" * 64)),
        ("chain_broken", 4),
    ),
    "a received_at earlier than at the previous position": (
        update_event("received_at", timestamp(T0 + 30), 3),
        ("event_time_invalid", 3),
    ),
    "a signed_at more than 120 seconds after received_at": (
        insert_next(
            lambda hub: next_event(
                hub, ALICE, "comment", new_id(), comment_on(work_of(hub)), signed_at=hub.memory.state.head.received_at + STEP + 121  # type: ignore[union-attr]
            )
        ),
        ("event_time_invalid", 4),
    ),
    "a received_at in the year 10000": (
        lambda db, hub, tmp_path: tamper(
            db, "record.event", "UPDATE record.event SET received_at = '10000-01-01 00:00:00+00' WHERE position = 3"
        ),
        ("event_time_invalid", 3),
    ),
    "a received_at of infinity": (
        lambda db, hub, tmp_path: tamper(
            db, "record.event", "UPDATE record.event SET received_at = 'infinity' WHERE position = 3"
        ),
        ("event_time_invalid", 3),
    ),
    "a received_at with a fraction of a second": (fraction, ("event_time_invalid", 3)),
    "a record hash": (update_event("record_hash", "d" * 64), ("record_hash_mismatch", 2)),
    "an event the reducer refuses": (
        insert_next(lambda hub: next_event(hub, BOB, "task", work_of(hub), [("title", "Fix login", "Stale")])),
        ("event_refused", 4),
    ),
    "a gap in the positions": (update_event("position", 5, 3), ("position_gap", 3)),
    "the record hash of a checkpoint": (update_checkpoint("record_hash", "c" * 64), ("checkpoint_invalid", 3)),
    "the signature of a checkpoint": (update_checkpoint("signature", bytes(64)), ("checkpoint_invalid", 3)),
    "a published_at in the year 10000": (
        lambda db, hub, tmp_path: tamper(
            db, "record.checkpoint", "UPDATE record.checkpoint SET published_at = '10000-01-01 00:00:00+00'"
        ),
        ("checkpoint_invalid", 3),
    ),
    "a published_at of infinity": (
        lambda db, hub, tmp_path: tamper(db, "record.checkpoint", "UPDATE record.checkpoint SET published_at = 'infinity'"),
        ("checkpoint_invalid", 3),
    ),
    "a checkpoint of another hub key id published in the year 10000": (
        other_checkpoint(OTHER_HUB_SIGNER.key_id, 2, "10000-01-01 00:00:00+00"),
        ("checkpoint_invalid", 2),
    ),
    "a checkpoint whose hub key id breaks the id rule": (
        other_checkpoint("Hub-2", 1, "2026-11-17 11:00:00+00"),
        ("checkpoint_invalid", 1),
    ),
    "a checkpoint of the hub beyond the head": (own_checkpoint_beyond_the_head, ("checkpoint_invalid", 7)),
}


@pytest.mark.parametrize("case", sorted(FAILURES))
def test_a_full_verification_names_the_true_failure_and_its_position(
    record_db: RecordDatabase, ring: KeyRing, tmp_path: Path, case: str
) -> None:
    change, expected = FAILURES[case]
    migrated(record_db)
    with process(record_db, ring) as hub:
        hub.story()
        assert isinstance(attempt(hub, H1, H1), CheckpointWritten)
        assert failure_of(record_db, ring) is None
        keys = change(record_db, hub, tmp_path) or ring
    assert failure_of(record_db, keys) == expected


def test_a_verification_with_the_head_of_the_state_finds_a_removal_at_the_end(
    record_db: RecordDatabase, ring: KeyRing
) -> None:
    migrated(record_db)
    with process(record_db, ring) as hub:
        work = hub.story()
        assert isinstance(attempt(hub, H1, H1), CheckpointWritten)
        memory_head = hub.memory.head()
    tamper(record_db, "record.checkpoint", "DELETE FROM record.checkpoint WHERE position >= 2")
    tamper(record_db, "record.event", "DELETE FROM record.event WHERE position >= 2")
    # The rest is sound in itself; only the head of a state above it shows the removal.
    assert failure_of(record_db, ring) is None
    assert failure_of(record_db, ring, memory_head) == ("position_gap", 2)
    # Two other validly signed and chained events in place of the removed ones.
    with writer(record_db) as connection:
        result = verify_stored(connection, ring, HUB_KEYS, None)
    first = result.state
    assert first.head is not None
    text, signature = sign_event(first, BOB, "task", work, [("title", "Fix login", "Other")], T0 + STEP)
    second = stored_event(2, T0 + STEP, text, signature, first.head.record_hash)
    insert_as_superuser(record_db, second)
    text, signature = sign_event(first, ALICE, "comment", new_id(), comment_on(work), T0 + 2 * STEP)
    insert_as_superuser(record_db, stored_event(3, T0 + 2 * STEP, text, signature, second.record_hash))
    assert failure_of(record_db, ring) is None
    assert failure_of(record_db, ring, memory_head) == ("record_hash_mismatch", 3)


# --- the append transaction (section 11.5) -------------------------------------------------------


def test_an_append_stores_the_row_of_section_11_5_and_applies_it_to_the_state(
    record_db: RecordDatabase, ring: KeyRing
) -> None:
    migrated(record_db)
    with process(record_db, ring) as hub:
        text, signature = hub.sign(ALICE, "task", new_id(), task_create())
        outcome = hub.append_text(ALICE, text, signature)
        event = parse_event(text)
        expected_hash = record_hash(1, T0, event.event_hash, None)
        assert outcome == Appended(1, expected_hash)
        assert hub.memory.state.head == Head(1, T0, expected_hash)
    with record_db.admin() as admin:
        rows = admin.execute(
            "SELECT position, event_id::text, actor_key_id, prev_hash, event_hash, received_at = %s::timestamptz,"
            " event_text, signature, record_hash FROM record.event",
            ("2026-11-17T10:00:00Z",),
        ).fetchall()
    assert rows == [(1, event.event_id, ALICE, "-", event.event_hash, True, text, signature, expected_hash)]


def test_a_refused_append_writes_nothing_and_names_the_error_value(record_db: RecordDatabase, ring: KeyRing) -> None:
    migrated(record_db)
    with process(record_db, ring) as hub:
        hub.story()
        head = hub.memory.head()
        text, signature = hub.sign(ALICE, "task", new_id(), task_create(), prev_hash="1" * 64)
        assert hub.append_text(ALICE, text, signature) == Refused("chain_broken")
        assert hub.memory.head() == head
    assert counts(record_db) == (3, 0)


def test_an_append_reads_the_clock_once_and_stores_that_reading(record_db: RecordDatabase, ring: KeyRing) -> None:
    migrated(record_db)
    with process(record_db, ring) as hub:
        work = hub.story()
        hub.clock.now = T0 + 600
        # signed_at 120 seconds after the reading; the previous received_at lies before the set-back clock.
        text, signature = sign_event(hub.memory.state, ALICE, "comment", new_id(), comment_on(work), T0 + 720)
        reads = hub.clock.reads

        def set_back() -> None:
            hub.clock.now -= 5

        outcome = append_event(
            hub.connection, hub.memory, ring, ring.entries[ALICE], "events", text, signature, hub.clock, before_insert=set_back
        )
        assert isinstance(outcome, Appended)
        assert hub.clock.reads - reads == 1
        assert hub.memory.state.head is not None and hub.memory.state.head.received_at == T0 + 600
    with writer(record_db) as connection:
        assert read_snapshot(connection).events[3].received_at == T0 + 600
    assert failure_of(record_db, ring) is None


def test_received_at_keeps_the_previous_value_when_the_clock_is_behind_it(record_db: RecordDatabase, ring: KeyRing) -> None:
    migrated(record_db)
    with process(record_db, ring) as hub:
        work = hub.story()
        kept = T0 + 2 * STEP
        hub.clock.now = T0
        # signed_at up to 120 seconds after the kept received_at is accepted.
        text, signature = sign_event(hub.memory.state, ALICE, "comment", new_id(), comment_on(work), kept + 120)
        assert isinstance(hub.append_text(ALICE, text, signature), Appended)
        assert hub.memory.state.head is not None and hub.memory.state.head.received_at == kept
    assert failure_of(record_db, ring) is None


def test_an_append_folds_the_events_of_another_process_before_it_checks(record_db: RecordDatabase, ring: KeyRing) -> None:
    migrated(record_db)
    with process(record_db, ring) as first, process(record_db, ring) as second:
        work = first.story()
        second.clock.now = first.clock.now
        assert isinstance(second.append(BOB, "comment", new_id(), comment_on(work)), Appended)
        first.clock.now = second.clock.now
        outcome = first.append(ALICE, "comment", new_id(), comment_on(work))
        assert isinstance(outcome, Appended) and outcome.position == 5
        with writer(record_db) as connection:
            stored = verify_stored(connection, ring, HUB_KEYS, None)
        assert stored.failure is None
        assert first.memory.state == stored.state


def test_an_append_whose_fold_fails_writes_nothing_and_answers_unverified(record_db: RecordDatabase, ring: KeyRing) -> None:
    migrated(record_db)
    with process(record_db, ring) as first, process(record_db, ring) as second:
        work = first.story()
        second.clock.now = first.clock.now
        assert isinstance(second.append(BOB, "comment", new_id(), comment_on(work)), Appended)
        tamper(record_db, "record.event", "UPDATE record.event SET event_id = %s WHERE position = 4", (new_id(),))
        first.clock.now = second.clock.now
        outcome = first.append(ALICE, "comment", new_id(), comment_on(work))
        assert outcome == Unverified(Failure("row_mismatch", 4))
        # The state keeps the events before the failing one.
        assert first.memory.head() is not None and first.memory.head().position == 3  # type: ignore[union-attr]
    assert counts(record_db) == (4, 0)


def test_step_5_changes_nothing_when_a_fold_already_folded_the_event(record_db: RecordDatabase, ring: KeyRing) -> None:
    migrated(record_db)
    with process(record_db, ring) as hub, writer(record_db) as other:
        work = hub.story()

        def fold_between_commit_and_step_5() -> None:
            with hub.memory.lock:
                assert incremental_fold(other, hub.memory, ring) is None

        outcome = hub.append(ALICE, "comment", new_id(), comment_on(work), after_commit=fold_between_commit_and_step_5)
        assert isinstance(outcome, Appended) and outcome.position == 4
        stored = verify_stored(other, ring, HUB_KEYS, None)
        assert hub.memory.state == stored.state
        # The next append succeeds on that state.
        assert isinstance(hub.append(BOB, "comment", new_id(), comment_on(work)), Appended)


def run_parallel(*calls: Callable[[], Any]) -> Parallel:
    return Parallel(*calls)


def join_all(started: Parallel) -> list[Any]:
    return started.results()


def set_default_isolation(db: RecordDatabase, isolation: str | None) -> None:
    if isolation is None:
        return
    with db.admin() as admin:
        admin.execute(
            sql.SQL("ALTER ROLE {} SET default_transaction_isolation = {}").format(
                sql.Identifier(db.writer_role), sql.Literal(isolation)
            )
        )


@pytest.mark.parametrize("isolation", [None, "repeatable read"])
def test_two_parallel_appends_of_two_keys_in_two_processes_get_consecutive_positions(
    record_db: RecordDatabase, ring: KeyRing, isolation: str | None
) -> None:
    migrated(record_db)
    set_default_isolation(record_db, isolation)
    with process(record_db, ring) as first, process(record_db, ring) as second:
        work = first.story()
        second.clock.now = first.clock.now
        alice = first.sign(ALICE, "comment", new_id(), comment_on(work))
        bob = first.sign(BOB, "comment", new_id(), comment_on(work))
        with append_lock_held(record_db):
            started = run_parallel(
                lambda: first.append_text(ALICE, *alice), lambda: second.append_text(BOB, *bob)
            )
            wait_for_append_lock(record_db, 2)
        outcomes = join_all(started)
    assert all(isinstance(outcome, Appended) for outcome in outcomes)
    assert sorted(outcome.position for outcome in outcomes) == [4, 5]
    assert failure_of(record_db, ring) is None


def test_a_fork_sent_in_parallel_is_refused_with_chain_broken(record_db: RecordDatabase, ring: KeyRing) -> None:
    migrated(record_db)
    with process(record_db, ring) as first, process(record_db, ring) as second:
        work = first.story()
        second.clock.now = first.clock.now
        one = first.sign(ALICE, "comment", new_id(), comment_on(work))
        two = first.sign(ALICE, "comment", new_id(), comment_on(work))
        with append_lock_held(record_db):
            started = run_parallel(lambda: first.append_text(ALICE, *one), lambda: second.append_text(ALICE, *two))
            wait_for_append_lock(record_db, 2)
        outcomes = join_all(started)
    assert [outcome.position for outcome in outcomes if isinstance(outcome, Appended)] == [4]
    assert [outcome for outcome in outcomes if not isinstance(outcome, Appended)] == [Refused("chain_broken")]
    assert counts(record_db) == (4, 0)


@pytest.mark.parametrize("isolation", [None, "repeatable read"])
def test_an_append_that_waited_for_the_lock_sees_a_row_committed_meanwhile(
    record_db: RecordDatabase, ring: KeyRing, isolation: str | None
) -> None:
    # READ COMMITTED (section 11.5): the fold after the lock reads a row committed while the append
    # waited, whatever default isolation the writer role has.
    migrated(record_db)
    set_default_isolation(record_db, isolation)
    with process(record_db, ring) as hub:
        work = hub.story()
        meanwhile = next_event(hub, BOB, "comment", new_id(), comment_on(work))
        hub.clock.now = meanwhile.received_at
        mine = hub.sign(ALICE, "comment", new_id(), comment_on(work))
        with append_lock_held(record_db):
            started = run_parallel(lambda: hub.append_text(ALICE, *mine))
            wait_for_append_lock(record_db, 1)
            insert_as_superuser(record_db, meanwhile)
        (outcome,) = join_all(started)
    assert isinstance(outcome, Appended) and outcome.position == 5
    assert failure_of(record_db, ring) is None


def test_a_database_failure_in_an_append_is_a_storage_error_of_step_append(record_db: RecordDatabase, ring: KeyRing) -> None:
    migrated(record_db)
    with process(record_db, ring) as hub:
        work = hub.story()
        with pytest.raises(StorageError) as failed:
            hub.append(
                ALICE, "comment", new_id(), comment_on(work), before_insert=lambda: terminate(record_db, backend_of(hub.connection))
            )
        assert failed.value.step == "append"
        assert hub.memory.head() is not None and hub.memory.head().position == 3  # type: ignore[union-attr]
    assert counts(record_db) == (3, 0)


def test_a_row_inserted_outside_the_lock_is_refused_by_the_database_as_a_storage_error(
    record_db: RecordDatabase, ring: KeyRing
) -> None:
    # The unique constraint on `position` refuses the second row at a position; section 11.5
    # answers such a failure with `500` `internal`, here a StorageError with its SQLSTATE.
    migrated(record_db)
    with process(record_db, ring) as hub:
        work = hub.story()
        outside = next_event(hub, BOB, "comment", new_id(), comment_on(work))
        with pytest.raises(StorageError) as failed:
            hub.append(ALICE, "comment", new_id(), comment_on(work), before_insert=lambda: insert_as_superuser(record_db, outside))
        assert (failed.value.step, failed.value.sqlstate) == ("append", "23505")
    assert counts(record_db) == (4, 0)


# --- the incremental fold (section 11.6) ---------------------------------------------------------


def gap(hub: Process) -> Any:
    stored = next_event(hub, BOB, "comment", new_id(), comment_on(work_of(hub)))
    return replace(stored, position=stored.position + 1), {}


def earlier_received_at(hub: Process) -> Any:
    head = hub.memory.state.head
    assert head is not None
    text, signature = sign_event(hub.memory.state, BOB, "comment", new_id(), comment_on(work_of(hub)), T0)
    return stored_event(head.position + 1, T0, text, signature, head.record_hash), {}


def late_signed_at(hub: Process) -> Any:
    head = hub.memory.state.head
    assert head is not None
    return next_event(hub, BOB, "comment", new_id(), comment_on(work_of(hub)), signed_at=head.received_at + STEP + 121), {}


def other_event_id_column(hub: Process) -> Any:
    return next_event(hub, BOB, "comment", new_id(), comment_on(work_of(hub))), {"event_id": new_id()}


def used_event_id(hub: Process) -> Any:
    return next_event(hub, BOB, "comment", new_id(), comment_on(work_of(hub)), event_id=first_event_id(hub)), {}


FOLD_FAILURES = {
    "a gap in the positions": (gap, None, ("position_gap", 4)),
    "a received_at earlier than at the previous position": (earlier_received_at, None, ("event_time_invalid", 4)),
    "a signed_at more than 120 seconds after its received_at": (late_signed_at, None, ("event_time_invalid", 4)),
    "an event_id column that differs from its event text": (other_event_id_column, None, ("row_mismatch", 4)),
    "an event_id an earlier event has": (used_event_id, "event_event_id_key", ("event_id_taken", 4)),
}


@pytest.mark.parametrize("case", sorted(FOLD_FAILURES))
def test_the_incremental_fold_checks_every_row_and_a_checkpoint_attempt_then_writes_nothing(
    record_db: RecordDatabase, ring: KeyRing, case: str
) -> None:
    make, drop, expected = FOLD_FAILURES[case]
    migrated(record_db)
    with process(record_db, ring) as hub:
        hub.story()
        stored, columns = make(hub)
        if drop is not None:
            drop_constraint(record_db, drop)
        insert_as_superuser(record_db, stored, **columns)
        assert attempt(hub, H1, H1) == Unverified(Failure(*expected))
        assert hub.memory.head() is not None and hub.memory.head().position == 3  # type: ignore[union-attr]
    assert counts(record_db)[1] == 0


# --- the checkpoint attempt (section 12.2) -------------------------------------------------------


def test_a_checkpoint_at_a_full_hour_signs_the_head_of_the_state(record_db: RecordDatabase, ring: KeyRing) -> None:
    migrated(record_db)
    with process(record_db, ring) as hub:
        hub.story()
        head = hub.memory.head()
        assert head is not None
        assert attempt(hub, H1, H1) == CheckpointWritten(3, head.record_hash)
        (checkpoint,) = read_snapshot(hub.connection).checkpoints
    assert (checkpoint.hub_key_id, checkpoint.position, checkpoint.record_hash, checkpoint.published_at) == (
        HUB,
        3,
        head.record_hash,
        H1,
    )
    assert checkpoint_signature_valid(HUB_PUBLIC, checkpoint)
    assert failure_of(record_db, ring) is None


def test_no_checkpoint_for_an_empty_record(record_db: RecordDatabase, ring: KeyRing) -> None:
    migrated(record_db)
    with process(record_db, ring) as hub:
        assert attempt(hub, H1, H1) is None
    assert counts(record_db) == (0, 0)


def test_no_checkpoint_when_the_head_did_not_move(record_db: RecordDatabase, ring: KeyRing) -> None:
    migrated(record_db)
    with process(record_db, ring) as hub:
        hub.story()
        assert isinstance(attempt(hub, H1, H1), CheckpointWritten)
        assert attempt(hub, H1 + 3600, H1 + 3600) is None
    assert counts(record_db) == (3, 1)


def test_an_attempt_30_minutes_after_the_full_hour_publishes_that_later_time(record_db: RecordDatabase, ring: KeyRing) -> None:
    migrated(record_db)
    with process(record_db, ring) as hub:
        hub.story()
        assert isinstance(attempt(hub, H1, H1 + 1800), CheckpointWritten)
        assert [item.published_at for item in read_snapshot(hub.connection).checkpoints] == [H1 + 1800]


def test_a_published_at_before_the_received_at_of_the_head_after_the_clock_was_set_back(
    record_db: RecordDatabase, ring: KeyRing
) -> None:
    migrated(record_db)
    with process(record_db, ring) as hub:
        work = hub.story()
        head = hub.memory.head()
        assert head is not None and head.received_at == T0 + 2 * STEP
        # The clock is set back below the received_at of the head and passes the full hour T0.
        assert attempt(hub, T0, T0 + 30) == CheckpointWritten(3, head.record_hash)
        assert failure_of(record_db, ring) is None
        # While the newest checkpoint is published at or after the full hour reached again, nothing.
        hub.clock.now = head.received_at
        assert isinstance(hub.append(ALICE, "comment", new_id(), comment_on(work)), Appended)
        assert attempt(hub, T0, T0 + 40) is None
    assert counts(record_db) == (4, 1)


def test_two_processes_in_one_hour_write_exactly_one_checkpoint_also_when_the_head_moves(
    record_db: RecordDatabase, ring: KeyRing
) -> None:
    migrated(record_db)
    with process(record_db, ring) as first, process(record_db, ring) as second:
        work = first.story()
        assert isinstance(attempt(first, H1, H1), CheckpointWritten)
        second.clock.now = first.clock.now
        assert isinstance(second.append(BOB, "comment", new_id(), comment_on(work)), Appended)
        assert attempt(second, H1, H1 + 10) is None
    assert counts(record_db) == (4, 1)


def test_a_checkpoint_attempt_after_a_changed_row_writes_nothing_and_answers_unverified(
    record_db: RecordDatabase, ring: KeyRing
) -> None:
    migrated(record_db)
    with process(record_db, ring) as first, process(record_db, ring) as second:
        work = first.story()
        assert isinstance(attempt(first, H1, H1), CheckpointWritten)
        second.clock.now = first.clock.now
        assert isinstance(second.append(BOB, "comment", new_id(), comment_on(work)), Appended)
        tamper(record_db, "record.event", "UPDATE record.event SET signature = %s WHERE position = 4", (bytes(64),))
        assert attempt(first, H1 + 3600, H1 + 3600) == Unverified(Failure("event_signature_invalid", 4))
    assert counts(record_db) == (4, 1)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("record_hash", "c" * 64),
        ("signature", bytes(64)),
        ("published_at", "10000-01-01 00:00:00+00"),
        ("published_at", "infinity"),
    ],
    ids=["record hash", "signature", "published_at year 10000", "published_at infinity"],
)
def test_a_changed_newest_checkpoint_of_the_hub_makes_the_next_attempt_unverified(
    record_db: RecordDatabase, ring: KeyRing, column: str, value: Any
) -> None:
    migrated(record_db)
    with process(record_db, ring) as hub:
        hub.story()
        assert isinstance(attempt(hub, H1, H1), CheckpointWritten)
        cast = "::timestamptz" if column == "published_at" else ""
        tamper(record_db, "record.checkpoint", f"UPDATE record.checkpoint SET {column} = %s{cast}", (value,))
        assert attempt(hub, H1 + 3600, H1 + 3600) == Unverified(Failure("checkpoint_invalid", 3))
    assert counts(record_db) == (3, 1)


def test_a_checkpoint_left_out_by_the_conflict_rule_is_not_reported_as_written(
    record_db: RecordDatabase, ring: KeyRing
) -> None:
    migrated(record_db)
    with process(record_db, ring) as hub:
        hub.story()
        head = hub.memory.head()
        assert head is not None
        outside = signed_checkpoint(HUB_SIGNER, 3, head.record_hash, H1)
        assert attempt(hub, H1, H1, before_insert=lambda: insert_checkpoint(record_db, outside)) is None
    assert counts(record_db) == (3, 1)


def test_a_database_failure_in_a_checkpoint_attempt_is_a_storage_error_of_step_checkpoint(
    record_db: RecordDatabase, ring: KeyRing
) -> None:
    migrated(record_db)
    with process(record_db, ring) as hub:
        hub.story()
        with pytest.raises(StorageError) as failed:
            attempt(hub, H1, H1, before_insert=lambda: terminate(record_db, backend_of(hub.connection)))
    assert failed.value.step == "checkpoint"
    assert counts(record_db) == (3, 0)


@pytest.mark.parametrize("isolation", [None, "repeatable read"])
def test_a_checkpoint_attempt_that_waited_for_the_lock_signs_a_row_committed_meanwhile(
    record_db: RecordDatabase, ring: KeyRing, isolation: str | None
) -> None:
    migrated(record_db)
    set_default_isolation(record_db, isolation)
    with process(record_db, ring) as hub:
        work = hub.story()
        meanwhile = next_event(hub, BOB, "comment", new_id(), comment_on(work))
        with append_lock_held(record_db):
            started = run_parallel(lambda: attempt(hub, H1, H1))
            wait_for_append_lock(record_db, 1)
            insert_as_superuser(record_db, meanwhile)
        (outcome,) = join_all(started)
    assert outcome == CheckpointWritten(4, meanwhile.record_hash)


# --- the start steps `schema` and `writer` (section 11.4) ----------------------------------------


def test_check_schema_accepts_exactly_the_migrated_version(record_db: RecordDatabase) -> None:
    with writer(record_db) as connection:
        assert check_schema(connection) == SCHEMA_MISSING
    migrated(record_db)
    with writer(record_db) as connection:
        assert check_schema(connection) is None


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO record.schema_version (version, applied_at) VALUES (2, now())",
        "DELETE FROM record.schema_version",
        "DROP TABLE record.schema_version",
    ],
    ids=["an unknown version", "no version row", "no version table"],
)
def test_check_schema_refuses_another_version(record_db: RecordDatabase, statement: str) -> None:
    migrated(record_db)
    with record_db.admin() as admin:
        admin.execute(statement)
    with writer(record_db) as connection:
        assert check_schema(connection) == SCHEMA_VERSION


def rights_hold(db: RecordDatabase) -> bool:
    with writer(db) as connection:
        return writer_rights_hold(connection, db.writer_role)


def test_the_writer_rights_of_a_migrated_database_hold(record_db: RecordDatabase) -> None:
    migrated(record_db)
    assert rights_hold(record_db)


def test_a_database_failure_in_step_writer_is_a_storage_error_of_step_writer(record_db: RecordDatabase) -> None:
    migrated(record_db)
    with writer(record_db) as connection:
        terminate(record_db, backend_of(connection))
        with pytest.raises(StorageError) as failed:
            writer_rights_hold(connection, record_db.writer_role)
        assert failed.value.step == "writer"
        with pytest.raises(StorageError) as failed:
            check_schema(connection)
        assert failed.value.step == "schema"


TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
TRIGGERS = (
    ("record.event", "event_no_change"),
    ("record.event", "event_no_truncate"),
    ("record.checkpoint", "checkpoint_no_change"),
    ("record.checkpoint", "checkpoint_no_truncate"),
)
PREDEFINED = ("pg_execute_server_program", "pg_write_server_files", "pg_write_all_data", "pg_read_all_data")
WRITER = "{writer}"
OWNER = "{owner}"

# Each case of section 14 that fails step `writer`, as statements of the superuser in the test
# database; `{writer}` and `{owner}` are the role names, composed as identifiers.
WIDER_RIGHTS: dict[str, list[str]] = {
    **{
        f"{privilege} on {table} for the writer": [f"GRANT {privilege} ON {table} TO {WRITER}"]
        for privilege in ("UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
        for table in ("record.event", "record.checkpoint")
    },
    "INSERT on record.schema_version": [f"GRANT INSERT ON record.schema_version TO {WRITER}"],
    "a right on one column of record.event": [f"GRANT UPDATE (event_id) ON record.event TO {WRITER}"],
    "a right of section 11.2 with grant option": [f"GRANT SELECT ON record.event TO {WRITER} WITH GRANT OPTION"],
    "SELECT on a table in the schema public": [
        "CREATE TABLE public.t_extra (id integer)",
        f"GRANT SELECT ON public.t_extra TO {WRITER}",
    ],
    "USAGE on a sequence in the schema public": [
        "CREATE SEQUENCE public.t_sequence",
        f"GRANT USAGE ON SEQUENCE public.t_sequence TO {WRITER}",
    ],
    "a default privilege that names the writer": [
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER} GRANT SELECT ON TABLES TO {WRITER}"
    ],
    "CREATE on the database": [f"GRANT CREATE ON DATABASE {{database}} TO {WRITER}"],
    "TEMPORARY on the database": [f"GRANT TEMPORARY ON DATABASE {{database}} TO {WRITER}"],
    "CREATE on the database through PUBLIC": ["GRANT CREATE ON DATABASE {database} TO PUBLIC"],
    "TEMPORARY on the database through PUBLIC": ["GRANT TEMPORARY ON DATABASE {database} TO PUBLIC"],
    "CREATE on the schema record": [f"GRANT CREATE ON SCHEMA record TO {WRITER}"],
    "CREATE on the schema public": [f"GRANT CREATE ON SCHEMA public TO {WRITER}"],
    "CREATE on the schema public through PUBLIC": ["GRANT CREATE ON SCHEMA public TO PUBLIC"],
    "EXECUTE on record.refuse_change()": [f"GRANT EXECUTE ON FUNCTION record.refuse_change() TO {WRITER}"],
    "EXECUTE on record.refuse_change() through PUBLIC": ["GRANT EXECUTE ON FUNCTION record.refuse_change() TO PUBLIC"],
    "ownership of a table": ["CREATE TABLE public.t_owned (id integer)", f"ALTER TABLE public.t_owned OWNER TO {WRITER}"],
    "membership in the owner role": [f"GRANT {OWNER} TO {WRITER}"],
    **{f"membership in {role}": [f"GRANT {role} TO {WRITER}"] for role in PREDEFINED},
    **{f"the attribute {attribute}": [f"ALTER ROLE {WRITER} {attribute}"] for attribute in (
        "CREATEDB", "CREATEROLE", "BYPASSRLS", "REPLICATION", "SUPERUSER"
    )},
    "a right on the schema record through PUBLIC": ["GRANT USAGE ON SCHEMA record TO PUBLIC"],
    **{
        f"{privilege} on {table} through PUBLIC": [f"GRANT {privilege} ON {table} TO PUBLIC"]
        for privilege in TABLE_PRIVILEGES
        for table in ("record.schema_version", "record.event", "record.checkpoint")
    },
    "a right on one column of record.event through PUBLIC": ["GRANT SELECT (position) ON record.event TO PUBLIC"],
    **{f"{trigger} disabled": [f"ALTER TABLE {table} DISABLE TRIGGER {trigger}"] for table, trigger in TRIGGERS},
    **{f"{trigger} enabled for the origin only": [f"ALTER TABLE {table} ENABLE TRIGGER {trigger}"] for table, trigger in TRIGGERS},
    **{f"{trigger} enabled for replicas only": [f"ALTER TABLE {table} ENABLE REPLICA TRIGGER {trigger}"] for table, trigger in TRIGGERS},
    "a fifth trigger on record.event": [
        "CREATE TRIGGER t_fifth BEFORE INSERT ON record.event FOR EACH ROW EXECUTE FUNCTION record.refuse_change()"
    ],
    "a SECURITY DEFINER function in public of the owner executable through PUBLIC": [
        "CREATE FUNCTION public.t_definer() RETURNS integer LANGUAGE sql SECURITY DEFINER AS 'SELECT 1'",
        f"ALTER FUNCTION public.t_definer() OWNER TO {OWNER}",
    ],
    "a SECURITY DEFINER function in record executable only by the writer": [
        "CREATE FUNCTION record.t_definer() RETURNS integer LANGUAGE sql SECURITY DEFINER AS 'SELECT 1'",
        "REVOKE ALL ON FUNCTION record.t_definer() FROM PUBLIC",
        f"GRANT EXECUTE ON FUNCTION record.t_definer() TO {WRITER}",
    ],
    **{f"row-level security enabled on {table}": [f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"] for table in (
        "record.schema_version", "record.event", "record.checkpoint"
    )},
    **{f"row-level security forced on {table}": [f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"] for table in (
        "record.schema_version", "record.event", "record.checkpoint"
    )},
    "a policy on record.event without row-level security": ["CREATE POLICY t_policy ON record.event USING (true)"],
    "a rule on record.event": ["CREATE RULE t_rule AS ON INSERT TO record.event DO ALSO NOTIFY t_record"],
}


def as_superuser(db: RecordDatabase, statements: list[str], database: str | None = None) -> None:
    names = {
        "writer": sql.Identifier(db.writer_role),
        "owner": sql.Identifier(db.owner_role),
        "database": sql.Identifier(db.name),
    }
    with psycopg.connect(**{**db.admin_params, "dbname": database or db.name}, autocommit=True) as admin:
        for statement in statements:
            admin.execute(sql.SQL(statement).format(**names))


@pytest.mark.parametrize("case", sorted(WIDER_RIGHTS))
def test_step_writer_refuses_a_writer_with_a_wider_right(record_db: RecordDatabase, case: str) -> None:
    migrated(record_db)
    as_superuser(record_db, WIDER_RIGHTS[case])
    assert not rights_hold(record_db)


def test_step_writer_refuses_a_membership_in_a_role_without_any_right(record_db: RecordDatabase) -> None:
    migrated(record_db)
    other = f"t_empty_{secrets.token_hex(6)}"
    with record_db.admin() as admin:
        admin.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(other)))
        admin.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(other), sql.Identifier(record_db.writer_role)))
    try:
        assert not rights_hold(record_db)
    finally:
        with record_db.admin() as admin:
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(other)))


def test_step_writer_refuses_connect_granted_by_name_on_the_database_postgres(record_db: RecordDatabase) -> None:
    migrated(record_db)
    as_superuser(record_db, [f"GRANT CONNECT ON DATABASE postgres TO {WRITER}"], "postgres")
    try:
        assert not rights_hold(record_db)
    finally:
        as_superuser(record_db, [f"REVOKE CONNECT ON DATABASE postgres FROM {WRITER}"], "postgres")
    assert rights_hold(record_db)


def test_step_writer_refuses_a_table_owned_in_the_database_postgres(record_db: RecordDatabase) -> None:
    migrated(record_db)
    table = f"t_owned_{secrets.token_hex(6)}"
    as_superuser(
        record_db,
        [f"CREATE TABLE public.{table} (id integer)", f"ALTER TABLE public.{table} OWNER TO {WRITER}"],
        "postgres",
    )
    try:
        assert not rights_hold(record_db)
    finally:
        as_superuser(record_db, [f"DROP TABLE public.{table}"], "postgres")
    assert rights_hold(record_db)


def test_step_writer_refuses_a_second_user_database_on_the_server(record_db: RecordDatabase) -> None:
    migrated(record_db)
    other = f"t_second_{secrets.token_hex(6)}"
    with psycopg.connect(**record_db.admin_params, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(other)))
    try:
        assert not rights_hold(record_db)
    finally:
        with psycopg.connect(**record_db.admin_params, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(other)))
    assert rights_hold(record_db)


def test_step_writer_accepts_a_security_definer_function_the_writer_cannot_execute(record_db: RecordDatabase) -> None:
    migrated(record_db)
    as_superuser(
        record_db,
        [
            "CREATE FUNCTION public.t_definer() RETURNS integer LANGUAGE sql SECURITY DEFINER AS 'SELECT 1'",
            "REVOKE ALL ON FUNCTION public.t_definer() FROM PUBLIC",
        ],
    )
    assert rights_hold(record_db)


def test_step_writer_compares_the_role_of_the_connection_string(record_db: RecordDatabase) -> None:
    # `current_user` is the role of the connection string: a name passed for another role fails.
    migrated(record_db)
    with writer(record_db) as connection:
        assert not writer_rights_hold(connection, record_db.owner_role)
