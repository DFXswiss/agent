"""The record in PostgreSQL (RECORD-PROTOCOL.md sections 11.4 to 11.6, 12.1 to 12.3).

Every statement here runs as the writer role. This module reads and writes rows; every rule of the
record is decided by the pure library (`verify`, `reducer`, `checkpoint`), which this module calls.

A database failure leaves this module as `StorageError` with the step and the SQLSTATE, never with
the message, detail or hint of the database error, because such a message can carry a stored value
(section 13).
"""

from __future__ import annotations

import threading
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Iterator, Mapping, Sequence

import psycopg

from ..keys import KeyEntry, KeyRing
from ..signing import HubSigner, b64url_decode
from .checkpoint import Checkpoint, checkpoint_due, checkpoint_text, checkpoint_valid
from .connection import ConnectionString
from .event import STORED_INFINITY, RecordRefusal, next_received_at, parse_event
from .failures import CHECKPOINT_INVALID
from .migrate import KNOWN_VERSIONS, SESSION_STATEMENTS
from .reducer import AppendContext, check_append, check_signed_event
from .state import EMPTY_STATE, Head, RecordState, applied_state
from .verify import Failure, FoldResult, RowCopies, StoredEvent, fold_events, verify_record

# Section 11.5: serialises every append, every checkpoint attempt and the insert of `restore`.
APPEND_LOCK = 742180001

# Section 11.5: every transaction is started explicitly at its isolation level, whatever
# `default_transaction_isolation` the server or the role sets; only the snapshot of section 12.1
# reads at REPEATABLE READ.
BEGIN_READ_COMMITTED = "BEGIN ISOLATION LEVEL READ COMMITTED"
BEGIN_SNAPSHOT = "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY"

# The steps a `StorageError` names. `connect`, `read` and `append` are the steps of the request
# line and of `record database error` (section 13); `checkpoint` is the step of a checkpoint
# attempt; `schema` and `writer` are the start steps of section 11.4 that `restore` runs too;
# `restore` is the transaction of steps 3 and 4 of `restore` (section 12.3).
STEP_CONNECT = "connect"
STEP_READ = "read"
STEP_APPEND = "append"
STEP_CHECKPOINT = "checkpoint"
STEP_SCHEMA = "schema"
STEP_WRITER = "writer"
STEP_RESTORE = "restore"

# The two ways step `schema` of section 11.4 fails; `restore` prints them as two lines (section
# 12.3).
SCHEMA_MISSING = "schema_missing"
SCHEMA_VERSION = "schema_version"

# A stored `timestamptz` with a fraction of a second, which the `CHECK` of section 11.3 refuses and
# which therefore exists only after a role outside this document dropped that `CHECK`. It is no
# time of section 2 either, so it reaches the library as an int outside the range of a time, like
# `infinity` (event.STORED_INFINITY), and fails verification and export as such.
STORED_FRACTION = STORED_INFINITY + 1

_LOCK = "SELECT pg_catalog.pg_advisory_xact_lock(%s)"

_EVENT_COLUMNS = (
    "SELECT position, event_id::text, actor_key_id, prev_hash, event_hash,"
    " EXTRACT(EPOCH FROM received_at), event_text, signature, record_hash"
    " FROM record.event"
)
_ALL_EVENTS = _EVENT_COLUMNS + " ORDER BY position"
_EVENTS_AFTER = _EVENT_COLUMNS + " WHERE position > %s ORDER BY position"
# EXTRACT of PostgreSQL 14 and later gives `numeric`: exact seconds, `Infinity` for `infinity` and
# `-Infinity` for `-infinity`, also for a year outside 0001 to 9999, which no `datetime` of
# Python can hold.
_CHECKPOINT_COLUMNS = (
    "SELECT hub_key_id, position, record_hash, EXTRACT(EPOCH FROM published_at), signature"
    " FROM record.checkpoint"
)
# Section 12.1: by hub key id in byte order, then by position.
_ALL_CHECKPOINTS = _CHECKPOINT_COLUMNS + ' ORDER BY hub_key_id COLLATE "C", position'
_NEWEST_CHECKPOINT = _CHECKPOINT_COLUMNS + " WHERE hub_key_id = %s ORDER BY position DESC LIMIT 1"
_RECORD_HASH_AT = "SELECT (SELECT record_hash FROM record.event WHERE position = %s)"
_INSERT_EVENT = (
    "INSERT INTO record.event (position, event_id, actor_key_id, prev_hash, event_hash, received_at,"
    " event_text, signature, record_hash) VALUES (%s, %s::uuid, %s, %s, %s, %s, %s, %s, %s)"
)
_INSERT_CHECKPOINT = (
    "INSERT INTO record.checkpoint (hub_key_id, position, record_hash, published_at, signature)"
    " VALUES (%s, %s, %s, %s, %s)"
)
_OCCUPIED = "SELECT EXISTS (SELECT 1 FROM record.event) OR EXISTS (SELECT 1 FROM record.checkpoint)"


class StorageError(Exception):
    """A database operation failed, also a connection that failed or that the connect timeout of
    section 15 ended. `sqlstate` is None when the failure has none (a failed connection)."""

    def __init__(self, step: str, sqlstate: str | None) -> None:
        super().__init__(step)
        self.step = step
        self.sqlstate = sqlstate

    @property
    def sqlstate_text(self) -> str:
        """`<SQLSTATE or ->` of the lines of section 13."""
        return self.sqlstate or "-"


@contextmanager
def database_step(step: str) -> Iterator[None]:
    """Turn a failure of the driver inside the block into `StorageError(step, sqlstate)`."""
    try:
        yield
    except psycopg.Error as error:
        raise StorageError(step, error.sqlstate) from None


def prepare_session(connection: psycopg.Connection[Any]) -> None:
    """The first statements of every connection of the writer role.

    The session acts as the role it logged in with and resolves every unqualified name in
    `pg_catalog` first, as `migrate` does (migrate.SESSION_STATEMENTS), so that a setting of the
    role or the database cannot bind a statement of this module to another object.
    """
    for statement in SESSION_STATEMENTS:
        connection.execute(statement)


@contextmanager
def writer_connection(writer: ConnectionString) -> Iterator[psycopg.Connection[Any]]:
    """One connection of the writer role, in autocommit mode so that every transaction of this
    module is begun explicitly; closed at the end of the block. The connect timeout of section 15
    comes with ConnectionString.connect_kwargs."""
    with database_step(STEP_CONNECT):
        connection = writer.connect(autocommit=True)
    with connection:
        with database_step(STEP_CONNECT):
            prepare_session(connection)
        yield connection


@contextmanager
def _transaction(connection: psycopg.Connection[Any], begin: str) -> Iterator[None]:
    """`begin`, the block, `COMMIT`; `ROLLBACK` when anything in the block or the commit fails.

    A rollback that fails itself, as on a connection that is gone, changes nothing for the caller:
    the first failure is the one raised.
    """
    connection.execute(begin)
    try:
        yield
        connection.execute("COMMIT")
    except BaseException:
        with suppress(psycopg.Error):
            connection.execute("ROLLBACK")
        raise


# --- stored values -------------------------------------------------------------------------------


def stored_seconds(epoch: Decimal) -> int:
    """A stored `timestamptz` (`received_at`, `published_at`), read as its seconds since the epoch,
    as the library takes it (event.STORED_INFINITY).

    A finite whole second is its seconds, also with a year outside 0001 to 9999; `infinity` is
    STORED_INFINITY and `-infinity` is -STORED_INFINITY; a value with a fraction of a second is
    STORED_FRACTION.
    """
    if epoch.is_infinite():
        return -STORED_INFINITY if epoch.is_signed() else STORED_INFINITY
    if epoch != epoch.to_integral_value():
        return STORED_FRACTION
    return int(epoch)


def _timestamp(seconds: int) -> datetime:
    """A time of section 2, as seconds since the epoch, for a `timestamptz` column."""
    return datetime.fromtimestamp(seconds, timezone.utc)


def _stored_event(row: Sequence[Any]) -> StoredEvent:
    position, event_id, actor_key_id, prev_hash, event_hash, epoch, text, signature, record_hash = row
    return StoredEvent(
        position=position,
        received_at=stored_seconds(epoch),
        event_hash=event_hash,
        record_hash=record_hash,
        text=bytes(text),
        signature=bytes(signature),
        copies=RowCopies(event_id=event_id, actor_key_id=actor_key_id, prev_hash=prev_hash),
    )


def _stored_checkpoint(row: Sequence[Any]) -> Checkpoint:
    hub_key_id, position, record_hash, epoch, signature = row
    return Checkpoint(
        hub_key_id=hub_key_id,
        position=position,
        record_hash=record_hash,
        published_at=stored_seconds(epoch),
        signature=bytes(signature),
    )


# --- the snapshot and the full verification (section 12.1) ---------------------------------------


@dataclass(frozen=True)
class Snapshot:
    """Every stored event in position order and every stored checkpoint by hub key id in byte order,
    then by position, read in one snapshot. The head of the snapshot is its last event."""

    events: tuple[StoredEvent, ...] = dataclass_field(repr=False)
    checkpoints: tuple[Checkpoint, ...] = dataclass_field(repr=False)


def _no_pause() -> None:
    """The default of a test point: nothing happens there."""


def read_snapshot(
    connection: psycopg.Connection[Any], between_reads: Callable[[], None] = _no_pause
) -> Snapshot:
    """Section 12.1, "One snapshot": the events and the checkpoints in one read-only transaction at
    REPEATABLE READ. `between_reads` runs after the read of the events and before the read of the
    checkpoints; it is a test point. `StorageError` with step `read` when the database fails."""
    with database_step(STEP_READ), _transaction(connection, BEGIN_SNAPSHOT):
        events = tuple(_stored_event(row) for row in connection.execute(_ALL_EVENTS).fetchall())
        between_reads()
        checkpoints = tuple(
            _stored_checkpoint(row) for row in connection.execute(_ALL_CHECKPOINTS).fetchall()
        )
    return Snapshot(events=events, checkpoints=checkpoints)


def verify_stored(
    connection: psycopg.Connection[Any],
    keys: KeyRing,
    hub_keys: Mapping[str, bytes],
    memory_head: Head | None,
    between_reads: Callable[[], None] = _no_pause,
) -> FoldResult:
    """A full verification of the stored record (section 12.1): the snapshot, then `verify_record`.

    The stored rows carry their copied columns, so the row `row_mismatch` compares `event_id`,
    `actor_key_id`, `prev_hash` and `event_hash`; the stored record hashes and positions are checked
    by their rows. `hub_keys` maps the hub key ids whose checkpoints are checked in full to their
    public keys: for the hub, its own hub key id. `memory_head` is the head of the state of a
    running process, read under the state lock before this call, for a verification on request or
    a periodic one; None at start. The result names the true failure value and its position.
    `StorageError` with step `read` when the database fails; that says nothing about the record.
    """
    snapshot = read_snapshot(connection, between_reads)
    return verify_record(snapshot.events, snapshot.checkpoints, keys, hub_keys, memory_head)


# --- the state in memory and the incremental fold (section 11.6) ---------------------------------


class MemoryState:
    """The state in memory of one hub process and its state lock (section 11.6).

    `state` is changed only while `lock` is held.
    """

    def __init__(self, state: RecordState = EMPTY_STATE) -> None:
        self.lock = threading.Lock()
        self.state = state

    def head(self) -> Head | None:
        """The head of the state, read under the state lock (section 12.1)."""
        with self.lock:
            return self.state.head


def incremental_fold(connection: psycopg.Connection[Any], memory: MemoryState, keys: KeyRing) -> Failure | None:
    """Section 11.6: fold the stored events after the head of the state, in position order, with
    every row of section 12.1 from `position_gap` to `event_refused`.

    The caller holds `memory.lock` and the connection it reads through. On a failure the state keeps
    the events before the failing one, and the failure is returned. A failure of the database is
    raised as the driver raised it; the caller names its step (`database_step`).
    """
    head = memory.state.head
    after = 0 if head is None else head.position
    rows = connection.execute(_EVENTS_AFTER, (after,)).fetchall()
    result = fold_events(memory.state, [_stored_event(row) for row in rows], keys, None)
    memory.state = result.state
    return result.failure


# --- the reads of the endpoints (sections 7.4 and 7.10) ------------------------------------------


_EVENT_PAGE = _EVENT_COLUMNS + " WHERE position > %s AND position <= %s ORDER BY position LIMIT %s"
_EVENTS_AT = _EVENT_COLUMNS + " WHERE position = ANY(%s::bigint[]) ORDER BY position"
# Section 7.10: newest first, by position descending and then by hub key id descending in byte order.
_CHECKPOINT_ORDER = ' ORDER BY position DESC, hub_key_id COLLATE "C" DESC LIMIT %s'
_CHECKPOINT_PAGE = _CHECKPOINT_COLUMNS + _CHECKPOINT_ORDER
_CHECKPOINT_PAGE_BEFORE = (
    _CHECKPOINT_COLUMNS
    + ' WHERE position < %s OR (position = %s AND hub_key_id COLLATE "C" < %s)'
    + _CHECKPOINT_ORDER
)


def read_events(connection: psycopg.Connection[Any], after: int, through: int, limit: int) -> tuple[StoredEvent, ...]:
    """The stored events with a position above `after` and at most `through`, ascending, at most
    `limit`. `StorageError` with step `read` when the database fails."""
    with database_step(STEP_READ):
        rows = connection.execute(_EVENT_PAGE, (after, through, limit)).fetchall()
    return tuple(_stored_event(row) for row in rows)


def read_events_at(connection: psycopg.Connection[Any], positions: Sequence[int]) -> tuple[StoredEvent, ...]:
    """The stored events at the given positions, ascending. `StorageError` with step `read`."""
    with database_step(STEP_READ):
        rows = connection.execute(_EVENTS_AT, (list(positions),)).fetchall()
    return tuple(_stored_event(row) for row in rows)


def read_checkpoints(
    connection: psycopg.Connection[Any], before: tuple[int, str] | None, limit: int
) -> tuple[Checkpoint, ...]:
    """A page of the checkpoints of every hub key id in the order of section 7.10: below the pair
    `before` of a position and a hub key id when it is given, at most `limit`. `StorageError` with
    step `read` when the database fails."""
    with database_step(STEP_READ):
        if before is None:
            rows = connection.execute(_CHECKPOINT_PAGE, (limit,)).fetchall()
        else:
            position, hub_key_id = before
            rows = connection.execute(_CHECKPOINT_PAGE_BEFORE, (position, position, hub_key_id, limit)).fetchall()
    return tuple(_stored_checkpoint(row) for row in rows)


# --- the connections of a hub process (section 11.6) ---------------------------------------------


def _open(writer: ConnectionString) -> psycopg.Connection[Any]:
    """A new connection of the writer role in autocommit mode, prepared as `writer_connection`
    prepares it; closed again when the preparation fails."""
    with database_step(STEP_CONNECT), ExitStack() as cleanup:
        connection = writer.connect(autocommit=True)
        cleanup.callback(connection.close)
        prepare_session(connection)
        cleanup.pop_all()
    return connection


class WriterPool:
    """At most `size` connections of the writer role, shared by the requests and the background
    work of one hub process (section 11.6, "Connections").

    `connection()` waits for a free slot, then hands out an idle connection or opens a new one
    (`StorageError` with step `connect` when that fails). A connection whose block ends with an
    exception is closed, so a connection that a failure left in an unknown state is never reused.
    `taken` counts the connections handed out; it is a test point that shows whether a request read
    the record.
    """

    def __init__(self, writer: ConnectionString, size: int) -> None:
        if size < 1:
            raise ValueError("a pool holds at least one connection")
        self._writer = writer
        self._slots = threading.BoundedSemaphore(size)
        self._idle: list[psycopg.Connection[Any]] = []
        self._lock = threading.Lock()
        self.taken = 0

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection[Any]]:
        with self._slots:
            with self._lock:
                connection = self._idle.pop() if self._idle else None
            if connection is None:
                connection = _open(self._writer)
            with self._lock:
                self.taken += 1
            try:
                yield connection
            except BaseException:
                connection.close()
                raise
            with self._lock:
                self._idle.append(connection)

    def close(self) -> None:
        """Close every idle connection."""
        with self._lock:
            idle, self._idle = self._idle, []
        for connection in idle:
            connection.close()


# --- the append transaction (section 11.5) -------------------------------------------------------


@dataclass(frozen=True)
class Appended:
    """Step 5: the event was committed at this position with this record hash, this event hash and
    this `received_at` (the answer `201` of section 7.2)."""

    position: int
    record_hash: str
    event_hash: str
    received_at: int


@dataclass(frozen=True)
class Refused:
    """Checks 3 to 18 of section 6.7 refused the event with this error value; nothing was written."""

    error: str


@dataclass(frozen=True)
class Unverified:
    """The incremental fold (or step 3 of a checkpoint attempt) failed; nothing was written and the
    process enters the state `record_unverified` (section 12.1)."""

    failure: Failure


@dataclass(frozen=True)
class EnteredUnverified:
    """The process entered the state `record_unverified` while the append waited for its
    connection or the advisory lock; nothing was read or written, and the request gets the `503`
    of a process that is in that state (section 12.1)."""


ENTERED_UNVERIFIED = EnteredUnverified()


def _always_verified() -> bool:
    """The default of `still_verified`: the caller holds no state `record_unverified`."""
    return True


class _Rollback(Exception):
    """Ends a transaction with ROLLBACK and gives its outcome to the caller."""

    def __init__(self, outcome: object) -> None:
        super().__init__("rollback")
        self.outcome = outcome


def append_event(
    connection: psycopg.Connection[Any],
    memory: MemoryState,
    keys: KeyRing,
    actor: KeyEntry,
    agent_key: str,
    endpoint: str,
    text: bytes,
    signature: bytes,
    clock: Callable[[], int],
    before_insert: Callable[[], None] = _no_pause,
    after_commit: Callable[[], None] = _no_pause,
    still_verified: Callable[[], bool] = _always_verified,
) -> Appended | Refused | Unverified | EnteredUnverified:
    """The append transaction of section 11.5 for one event, after checks 1 and 2 of section 6.7.

    Checks 3 to 6 run first, without a transaction and without a fold (`Refused` when one fails),
    so that an append refused there folds nothing (section 11.6). `actor` is the entry the request
    checks established and `agent_key` the request's `Agent-Key`, which check 4 compares (see
    `AppendContext`). Then:

    1. BEGIN at READ COMMITTED and the advisory lock 742180001.
    2. Under the state lock: `EnteredUnverified` and ROLLBACK, with nothing read, when
       `still_verified` is false, because the process entered the state `record_unverified` while
       the append waited for its connection or the advisory lock (section 12.1); otherwise the
       incremental fold (`Unverified` and ROLLBACK when it fails), then the one clock reading of
       the append, which fixes `received_at` (section 4.6).
    3. Checks 7 to 18 of section 6.7 by the reducer (`Refused` and ROLLBACK); the state lock is
       released.
    4. The INSERT of the row at the next position, with that `received_at`, the hashes and the three
       copied columns taken from the event text.
    5. COMMIT; then, under the state lock, the event is applied to the state when its position is
       the head of the state plus 1.

    `clock` is read exactly once. `before_insert` (between steps 3 and 4) and `after_commit`
    (between the commit and the apply of step 5) are test points. `still_verified` is read under the
    state lock; a process enters the state `record_unverified` only under that lock, so an append
    either ran its step 2 before the process entered it or reads and writes nothing. `StorageError` with step
    `append` for every failure of the database, unique constraints and triggers included: none can
    refuse a row that the checks under the lock accepted unless a role outside this document
    changes the rows, and section 11.5 answers every such failure with `500` `internal`.
    """
    try:
        check_signed_event(text, signature, actor, agent_key)
    except RecordRefusal as refusal:
        return Refused(refusal.error)
    try:
        with database_step(STEP_APPEND), _transaction(connection, BEGIN_READ_COMMITTED):
            connection.execute(_LOCK, (APPEND_LOCK,))
            with memory.lock:
                if not still_verified():
                    raise _Rollback(ENTERED_UNVERIFIED)
                failure = incremental_fold(connection, memory, keys)
                if failure is not None:
                    raise _Rollback(Unverified(failure))
                state = memory.state
                head = state.head
                received_at = next_received_at(clock(), None if head is None else head.received_at)
                try:
                    applied = check_append(
                        state, text, signature, AppendContext(keys, actor, endpoint, received_at, agent_key)
                    )
                except RecordRefusal as refusal:
                    raise _Rollback(Refused(refusal.error)) from None
            before_insert()
            event = applied.event
            connection.execute(
                _INSERT_EVENT,
                (
                    applied.head.position,
                    event.event_id,
                    event.actor_key_id,
                    event.prev_hash,
                    event.event_hash,
                    _timestamp(applied.head.received_at),
                    event.text,
                    signature,
                    applied.head.record_hash,
                ),
            )
    except _Rollback as rollback:
        return rollback.outcome  # type: ignore[return-value]
    after_commit()
    with memory.lock:
        current = memory.state.head
        if (0 if current is None else current.position) + 1 == applied.head.position:
            memory.state = applied_state(memory.state, applied)
    return Appended(
        position=applied.head.position,
        record_hash=applied.head.record_hash,
        event_hash=applied.event.event_hash,
        received_at=applied.head.received_at,
    )


# --- the checkpoint attempt (section 12.2) -------------------------------------------------------


@dataclass(frozen=True)
class CheckpointWritten:
    """Step 5 inserted the checkpoint of this head; the hub logs `record checkpoint <position>
    <record hash>`."""

    position: int
    record_hash: str


def _newest_checkpoint(connection: psycopg.Connection[Any], hub_key_id: str) -> Checkpoint | None:
    row = connection.execute(_NEWEST_CHECKPOINT, (hub_key_id,)).fetchone()
    if row is None:
        return None
    return _stored_checkpoint(row)


def _checkpoint_steps(
    connection: psycopg.Connection[Any],
    memory: MemoryState,
    keys: KeyRing,
    signer: HubSigner,
    hour: int,
    clock: Callable[[], int],
    before_insert: Callable[[], None],
    still_verified: Callable[[], bool],
) -> CheckpointWritten | None:
    """Steps 2 to 5 of section 12.2 under the state lock; `_Rollback` where the attempt writes
    nothing."""
    if not still_verified():
        # The process entered the state `record_unverified` while this attempt waited for its
        # connection or the advisory lock; it makes no attempt (section 12.2).
        raise _Rollback(None)
    failure = incremental_fold(connection, memory, keys)
    if failure is not None:
        raise _Rollback(Unverified(failure))
    head = memory.state.head
    position = 0 if head is None else head.position
    newest = _newest_checkpoint(connection, signer.key_id)
    public_key = b64url_decode(signer.public_key_text)
    if newest is not None and not checkpoint_valid(
        newest,
        position,
        connection.execute(_RECORD_HASH_AT, (newest.position,)).fetchone()[0],
        public_key,  # type: ignore[arg-type]
    ):
        raise _Rollback(Unverified(Failure(CHECKPOINT_INVALID, newest.position)))
    if head is None or not checkpoint_due(position, newest, hour):
        raise _Rollback(None)
    published_at = clock()
    text = checkpoint_text(signer.key_id, head.position, head.record_hash, published_at)
    before_insert()
    inserted = connection.execute(
        _INSERT_CHECKPOINT + " ON CONFLICT DO NOTHING",
        (signer.key_id, head.position, head.record_hash, _timestamp(published_at), b64url_decode(signer.sign(text))),
    ).rowcount
    connection.execute("COMMIT")
    if inserted != 1:
        return None
    return CheckpointWritten(position=head.position, record_hash=head.record_hash)


def checkpoint_attempt(
    connection: psycopg.Connection[Any],
    memory: MemoryState,
    keys: KeyRing,
    signer: HubSigner,
    hour: int,
    clock: Callable[[], int],
    before_insert: Callable[[], None] = _no_pause,
    still_verified: Callable[[], bool] = _always_verified,
) -> CheckpointWritten | Unverified | None:
    """One checkpoint attempt of section 12.2 for the full hour `hour`, as one transaction at READ
    COMMITTED.

    1. The advisory lock 742180001.
    2. Under the state lock, which is held until the commit: nothing is written (None) when
       `still_verified` is false, because the process entered the state `record_unverified` while
       the attempt waited for its connection or the advisory lock; otherwise the incremental fold
       (`Unverified` when it fails).
    3. The newest checkpoint of the hub's own key id, checked as the row `checkpoint_invalid` does
       against the head of the state and the stored record hash at its position (`Unverified` with
       `checkpoint_invalid` at the position it names).
    4. Nothing is written (None) for an empty record, a head that did not move since that
       checkpoint, or that checkpoint published at or after `hour`.
    5. Otherwise the checkpoint of the head of the state, with `published_at` read from `clock` at
       that moment and signed by `signer`, is inserted with ON CONFLICT DO NOTHING and committed;
       `CheckpointWritten` when a row was inserted, None when the conflict left it out.

    `before_insert` is a test point before the insert of step 5. `still_verified` is read under the
    state lock; a process enters the state `record_unverified` only under that lock, so an attempt
    either commits before the process enters it or writes nothing. `StorageError` with step
    `checkpoint` when the database fails.
    """
    try:
        with database_step(STEP_CHECKPOINT):
            connection.execute(BEGIN_READ_COMMITTED)
            try:
                connection.execute(_LOCK, (APPEND_LOCK,))
                with memory.lock:
                    return _checkpoint_steps(
                        connection, memory, keys, signer, hour, clock, before_insert, still_verified
                    )
            except BaseException:
                with suppress(psycopg.Error):
                    connection.execute("ROLLBACK")
                raise
    except _Rollback as rollback:
        return rollback.outcome  # type: ignore[return-value]


# --- the start steps `schema` and `writer` (section 11.4) ----------------------------------------


_SCHEMA_PARTS = (
    "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname = 'record'),"
    " EXISTS (SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace"
    " WHERE n.nspname = 'record' AND c.relname = 'schema_version' AND c.relkind = 'r')"
)


def check_schema(connection: psycopg.Connection[Any]) -> str | None:
    """Step `schema` of section 11.4 on a connection of the writer role: None when
    `record.schema_version` exists and holds exactly the versions this code expects (one row,
    version 1); SCHEMA_MISSING when the schema `record` does not exist; SCHEMA_VERSION for a missing
    table, a missing version row or an unknown version. Nothing is created or changed.
    `StorageError` with step `schema` when the database fails."""
    with database_step(STEP_SCHEMA):
        schema, table = connection.execute(_SCHEMA_PARTS).fetchone()  # type: ignore[misc]
        if not schema:
            return SCHEMA_MISSING
        if not table:
            return SCHEMA_VERSION
        rows = connection.execute("SELECT version FROM record.schema_version ORDER BY version").fetchall()
    versions = [row[0] for row in rows]
    if versions != sorted(KNOWN_VERSIONS):
        return SCHEMA_VERSION
    return None


_RECORD_TABLES = "('schema_version', 'event', 'checkpoint')"
_RECORD_RELATIONS = (
    "SELECT c.oid FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace"
    f" WHERE n.nspname = 'record' AND c.relname IN {_RECORD_TABLES}"
)
_ME = "(SELECT oid FROM pg_catalog.pg_roles WHERE rolname = %(role)s)"
_THIS_DATABASE = "(SELECT oid FROM pg_catalog.pg_database WHERE datname = pg_catalog.current_database())"

# Step `writer` of section 11.4, bullet by bullet. Each query names the writer role as the
# parameter `role` (a value, never composed into the text) and returns rows that must equal the
# expected rows exactly.
_WRITER_CHECKS: tuple[tuple[str, list[tuple[Any, ...]]], ...] = (
    # The server holds no database besides this one, `postgres`, `template0` and `template1`;
    # `current_user` is the role of the connection string and has none of the five attributes.
    (
        "SELECT NOT EXISTS (SELECT 1 FROM pg_catalog.pg_database"
        " WHERE datname NOT IN (pg_catalog.current_database(), 'postgres', 'template0', 'template1'))"
        " AND EXISTS (SELECT 1 FROM pg_catalog.pg_roles r WHERE r.rolname = %(role)s"
        " AND r.rolname = current_user AND NOT r.rolsuper AND NOT r.rolbypassrls"
        " AND NOT r.rolcreaterole AND NOT r.rolcreatedb AND NOT r.rolreplication)",
        [(True,)],
    ),
    # A member of no role, directly or indirectly; it owns no database, no tablespace and no other
    # object in any database of the server.
    (
        f"SELECT NOT EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members WHERE member = {_ME})"
        f" AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_database WHERE datdba = {_ME})"
        f" AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_tablespace WHERE spcowner = {_ME})"
        " AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_shdepend"
        " WHERE refclassid = 'pg_catalog.pg_authid'::pg_catalog.regclass"
        f" AND refobjid = {_ME} AND deptype = 'o')",
        [(True,)],
    ),
    # The rows of type `a` of `pg_shdepend` that name it are exactly five, each with `objsubid` 0:
    # this database, the schema `record` and its three tables.
    (
        "SELECT s.dbid = 0 AND s.classid = 'pg_catalog.pg_database'::pg_catalog.regclass"
        f" AND s.objid = {_THIS_DATABASE},"
        f" s.dbid = {_THIS_DATABASE}"
        " AND s.classid = 'pg_catalog.pg_namespace'::pg_catalog.regclass"
        " AND s.objid = (SELECT oid FROM pg_catalog.pg_namespace WHERE nspname = 'record'),"
        f" s.dbid = {_THIS_DATABASE}"
        " AND s.classid = 'pg_catalog.pg_class'::pg_catalog.regclass"
        f" AND s.objid IN ({_RECORD_RELATIONS}),"
        " s.objsubid"
        " FROM pg_catalog.pg_shdepend s"
        " WHERE s.refclassid = 'pg_catalog.pg_authid'::pg_catalog.regclass"
        f" AND s.refobjid = {_ME} AND s.deptype = 'a'"
        " ORDER BY 1, 2, 3, s.objid",
        [(False, False, True, 0)] * 3 + [(False, True, False, 0), (True, False, False, 0)],
    ),
    # `aclexplode` of those five objects gives the role exactly the rights of section 11.2, each
    # without grant option.
    (
        "SELECT o.name, a.privilege_type, a.is_grantable FROM ("
        " SELECT 'database' AS name, datacl AS acl FROM pg_catalog.pg_database"
        " WHERE datname = pg_catalog.current_database()"
        " UNION ALL SELECT 'schema', nspacl FROM pg_catalog.pg_namespace WHERE nspname = 'record'"
        " UNION ALL SELECT c.relname::text, c.relacl FROM pg_catalog.pg_class c"
        " JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace"
        f" WHERE n.nspname = 'record' AND c.relname IN {_RECORD_TABLES}"
        ") o CROSS JOIN LATERAL pg_catalog.aclexplode(o.acl) a"
        f" WHERE a.grantee = {_ME}"
        ' ORDER BY o.name COLLATE "C", a.privilege_type COLLATE "C", a.is_grantable',
        [
            ("checkpoint", "INSERT", False),
            ("checkpoint", "SELECT", False),
            ("database", "CONNECT", False),
            ("event", "INSERT", False),
            ("event", "SELECT", False),
            ("schema", "USAGE", False),
            ("schema_version", "SELECT", False),
        ],
    ),
    # Through PUBLIC: no CREATE or TEMPORARY on the database, no CREATE on any schema, no EXECUTE
    # on `record.refuse_change()`; no right on the schema `record`, its three tables or a column of
    # them. A NULL access list stands for the default rights of PostgreSQL (`acldefault`). No
    # SECURITY DEFINER function outside `pg_catalog` and `information_schema` that the role can
    # execute, directly, through PUBLIC or through a role.
    (
        "SELECT NOT EXISTS (SELECT 1 FROM pg_catalog.pg_database d CROSS JOIN LATERAL"
        " pg_catalog.aclexplode(COALESCE(d.datacl, pg_catalog.acldefault('d', d.datdba))) a"
        " WHERE d.datname = pg_catalog.current_database() AND a.grantee = 0"
        " AND a.privilege_type IN ('CREATE', 'TEMPORARY'))"
        " AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_namespace n CROSS JOIN LATERAL"
        " pg_catalog.aclexplode(COALESCE(n.nspacl, pg_catalog.acldefault('n', n.nspowner))) a"
        " WHERE a.grantee = 0 AND (a.privilege_type = 'CREATE' OR n.nspname = 'record'))"
        " AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_proc p"
        " JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace CROSS JOIN LATERAL"
        " pg_catalog.aclexplode(COALESCE(p.proacl, pg_catalog.acldefault('f', p.proowner))) a"
        " WHERE n.nspname = 'record' AND p.proname = 'refuse_change' AND a.grantee = 0"
        " AND a.privilege_type = 'EXECUTE')"
        " AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_class c"
        " JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace CROSS JOIN LATERAL"
        " pg_catalog.aclexplode(COALESCE(c.relacl, pg_catalog.acldefault('r', c.relowner))) a"
        f" WHERE n.nspname = 'record' AND c.relname IN {_RECORD_TABLES} AND a.grantee = 0)"
        " AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_attribute t"
        " JOIN pg_catalog.pg_class c ON c.oid = t.attrelid"
        " JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace CROSS JOIN LATERAL"
        " pg_catalog.aclexplode(t.attacl) a"
        f" WHERE n.nspname = 'record' AND c.relname IN {_RECORD_TABLES} AND a.grantee = 0)"
        " AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_proc p"
        " JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace"
        " WHERE p.prosecdef AND n.nspname NOT IN ('pg_catalog', 'information_schema')"
        " AND pg_catalog.has_function_privilege(%(role)s::name, p.oid, 'EXECUTE'))",
        [(True,)],
    ),
    # The four triggers of section 11.3, enabled ALWAYS, and no other row of `pg_trigger` for the
    # three tables, internal triggers included; no row-level security, enabled or forced, no
    # policy and no rule on them.
    (
        "SELECT u.relation, u.name, u.state FROM ("
        " SELECT c.relname::text AS relation, t.tgname::text AS name, t.tgenabled::text AS state"
        " FROM pg_catalog.pg_trigger t JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid"
        " JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace"
        f" WHERE n.nspname = 'record' AND c.relname IN {_RECORD_TABLES}"
        " UNION ALL SELECT c.relname::text, 'row level security', ''"
        " FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace"
        f" WHERE n.nspname = 'record' AND c.relname IN {_RECORD_TABLES}"
        " AND (c.relrowsecurity OR c.relforcerowsecurity"
        " OR EXISTS (SELECT 1 FROM pg_catalog.pg_policy p WHERE p.polrelid = c.oid)"
        " OR EXISTS (SELECT 1 FROM pg_catalog.pg_rewrite w WHERE w.ev_class = c.oid))"
        ') u ORDER BY u.relation COLLATE "C", u.name COLLATE "C", u.state COLLATE "C"',
        [
            ("checkpoint", "checkpoint_no_change", "A"),
            ("checkpoint", "checkpoint_no_truncate", "A"),
            ("event", "event_no_change", "A"),
            ("event", "event_no_truncate", "A"),
        ],
    ),
)


def writer_rights_hold(connection: psycopg.Connection[Any], role: str) -> bool:
    """Step `writer` of section 11.4 on a connection of the writer role `role` (the role name of
    the connection string, passed as a value): the server holds no other user database, the role
    has exactly the writer rights of section 11.2 and nothing else, and no function, row-level
    security, policy, rule or further trigger can widen them or cut its reads short.
    `StorageError` with step `writer` when the database fails."""
    params = {"role": role}
    with database_step(STEP_WRITER):
        return all(connection.execute(query, params).fetchall() == expected for query, expected in _WRITER_CHECKS)


# --- the insert of `restore` (section 12.3) ------------------------------------------------------


def restore_rows(
    connection: psycopg.Connection[Any], events: Sequence[StoredEvent], checkpoints: Sequence[Checkpoint]
) -> bool:
    """Steps 3 and 4 of `restore` (section 12.3), in one transaction at READ COMMITTED that takes
    the advisory lock 742180001: False, and nothing written, when `record.event` or
    `record.checkpoint` holds a row; otherwise every event and checkpoint is inserted with its
    stored position, times and hashes, and committed (True).

    The events and checkpoints have passed the verification of step 1, so every event text parses
    and every time is a time of section 2; the copied columns are taken from the event text.
    `StorageError` with step `restore` when the database fails.
    """
    try:
        with database_step(STEP_RESTORE), _transaction(connection, BEGIN_READ_COMMITTED):
            connection.execute(_LOCK, (APPEND_LOCK,))
            if connection.execute(_OCCUPIED).fetchone()[0]:  # type: ignore[index]
                raise _Rollback(False)
            with connection.cursor() as cursor:
                cursor.executemany(_INSERT_EVENT, [_event_row(stored) for stored in events])
                cursor.executemany(_INSERT_CHECKPOINT, [_checkpoint_row(item) for item in checkpoints])
    except _Rollback as rollback:
        return rollback.outcome  # type: ignore[return-value]
    return True


def _event_row(stored: StoredEvent) -> tuple[Any, ...]:
    event = parse_event(stored.text)
    return (
        stored.position,
        event.event_id,
        event.actor_key_id,
        event.prev_hash,
        stored.event_hash,
        _timestamp(stored.received_at),
        stored.text,
        stored.signature,
        stored.record_hash,
    )


def _checkpoint_row(checkpoint: Checkpoint) -> tuple[Any, ...]:
    return (
        checkpoint.hub_key_id,
        checkpoint.position,
        checkpoint.record_hash,
        _timestamp(checkpoint.published_at),
        checkpoint.signature,
    )
