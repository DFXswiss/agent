"""The record in one hub process (RECORD-PROTOCOL.md sections 7, 11.4, 11.6, 12.1, 12.2 and 13).

`start_record` runs the start of section 11.4 and returns the `RecordRuntime` of the process: its
state in memory, its pool of writer connections, the state `record_unverified`, the limit of the
verification on request, the periodic verification and the checkpoint attempts. Every rule of the
record is decided by the library and every statement runs in `storage`; this module only orders
them and writes the lines of section 13 that do not belong to a request under `/api/key/record/`.

A log line of this module holds only fixed words, positions, record hashes, failure values, steps
and SQLSTATEs (section 13).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable, Generic, Mapping, TypeVar

import psycopg

from ..keys import KeyEntry, KeyRing
from ..signing import LOG, HubSigner
from .checkpoint import full_hour
from .connection import (
    OWNER_VARIABLE,
    WRITER_VARIABLE,
    ConnectionString,
    ConnectionStringError,
    libpq_supported,
    read_connection_string,
)
from .event import RecordRefusal
from .reducer import check_signed_event
from .state import Head, RecordState
from .storage import (
    STEP_CHECKPOINT,
    STEP_READ,
    Appended,
    CheckpointWritten,
    EnteredUnverified,
    MemoryState,
    Refused,
    StorageError,
    Unverified,
    WriterPool,
    append_event,
    check_schema,
    checkpoint_attempt,
    database_step,
    incremental_fold,
    verify_stored,
    writer_connection,
    writer_rights_hold,
)
from .verify import FoldResult

# Section 12.1: a periodic verification runs 10 minutes after the previous run started, measured on
# a monotonic clock.
PERIOD_SECONDS = 600
# Section 7.9: at most one verification on request within any 60 seconds per process.
VERIFY_LIMIT_SECONDS = 60

# The steps of the line `record start failed <step>` (section 11.4).
START_CONFIG = "config"
START_SCHEMA = "schema"
START_WRITER = "writer"
START_VERIFY = "verify"
START_VERIFY_READ = "verify_read"
# Section 11.4: a database failure fails the start step in which it occurs. The connection is
# opened in step `schema`; a failure of the read of step `verify` is `verify_read`.
_START_STEP_OF_STORAGE_STEP = {
    "connect": START_SCHEMA,
    "schema": START_SCHEMA,
    "writer": START_WRITER,
    STEP_READ: START_VERIFY_READ,
}

Clock = Callable[[], int]
Monotonic = Callable[[], float]
T = TypeVar("T")


class RecordStartFailed(SystemExit):
    """The start of section 11.4 failed; its line was logged. The hub does not start (exit 1)."""

    def __init__(self, step: str) -> None:
        super().__init__(1)
        self.step = step


def _nothing() -> None:
    """A test point that does nothing."""


@dataclass(frozen=True)
class Hooks:
    """Test points of section 14, each called at its point. `NO_HOOKS` does nothing at any of them.

    - `in_read_fold`: under the state lock, after the incremental fold of a `GET` endpoint or of the
      release query, before the answer is read from the state
    - `append_before_insert` and `append_after_commit`: the points of `storage.append_event`
    - `snapshot_between_reads`: between the read of the events and of the checkpoints of a snapshot
    - `checkpoint_before_insert`: the point of `storage.checkpoint_attempt`
    """

    in_read_fold: Callable[[], None]
    append_before_insert: Callable[[], None]
    append_after_commit: Callable[[], None]
    snapshot_between_reads: Callable[[], None]
    checkpoint_before_insert: Callable[[], None]


NO_HOOKS = Hooks(_nothing, _nothing, _nothing, _nothing, _nothing)


@dataclass(frozen=True)
class AlreadyUnverified:
    """The process was already in the state `record_unverified` when the request arrived."""


ALREADY_UNVERIFIED = AlreadyUnverified()


@dataclass(frozen=True)
class DatabaseFailed:
    """A database operation of a request failed: `step` is `connect`, `read` or `append`, and
    `sqlstate` is the SQLSTATE or `-` (section 13)."""

    step: str
    sqlstate: str


@dataclass(frozen=True)
class Read(Generic[T]):
    """What a reading request read from the state after its incremental fold."""

    value: T


@dataclass(frozen=True)
class VerifyBusy:
    """Section 7.9: a verification on request runs, or one started less than 60 seconds ago."""


VERIFY_BUSY = VerifyBusy()


@dataclass(frozen=True)
class Verified:
    """A full verification on request ran; `result.failure` is None when the record is sound."""

    result: FoldResult


def _database_failed(error: StorageError) -> DatabaseFailed:
    return DatabaseFailed(error.step, error.sqlstate_text)


def head_words(head: Head | None) -> str:
    """`<position> <record hash or ->`, `0 -` for an empty record."""
    return "0 -" if head is None else f"{head.position} {head.record_hash}"


def log_verify_failed(position: int, failure: str) -> None:
    LOG.error("record verify failed %d %s", position, failure)


def log_database_error(step: str, sqlstate: str) -> None:
    LOG.error("record database error %s %s", step, sqlstate)


class RecordRuntime:
    """The record part of one hub process after its start (sections 11.6 and 12.1).

    The state `record_unverified` belongs to this process only; once entered it lasts until the
    process ends. `poll_seconds` is the interval at which the background thread looks whether a
    periodic verification or a checkpoint attempt is due; None runs no background thread, and the
    caller runs `tick` itself.
    """

    def __init__(
        self,
        *,
        writer: ConnectionString,
        keys: KeyRing,
        signer: HubSigner,
        clock: Clock,
        monotonic: Monotonic,
        memory: MemoryState,
        pool_size: int,
        poll_seconds: float | None,
        hooks: Hooks,
        last_verification: float,
    ) -> None:
        self.keys = keys
        self.signer = signer
        self.hub_keys: Mapping[str, bytes] = {signer.key_id: signer.public_key}
        self.clock = clock
        self.monotonic = monotonic
        self.memory = memory
        self.pool = WriterPool(writer, pool_size)
        self.hooks = hooks
        self._poll_seconds = poll_seconds
        self._unverified = threading.Event()
        self._limit = threading.Lock()
        self._request_running = False
        self._request_started: float | None = None
        self._periodic_started = last_verification
        self._last_hour: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- the state `record_unverified` (section 12.1) --------------------------------------------

    @property
    def unverified(self) -> bool:
        return self._unverified.is_set()

    def _enter_unverified(self) -> None:
        """Enter the state under the state lock. A checkpoint attempt holds that lock from its
        check of the state until its commit (section 12.2), so it either commits before the
        process enters the state or sees the state and writes nothing; a reading request and an
        append test the state again once they hold that lock, so neither starts its fold after the
        process entered it."""
        with self.memory.lock:
            self._unverified.set()

    def _still_verified(self) -> bool:
        return not self._unverified.is_set()

    # --- requests ---------------------------------------------------------------------------------

    def read(
        self, reader: Callable[[RecordState, psycopg.Connection[Any]], T]
    ) -> Read[T] | AlreadyUnverified | Unverified | DatabaseFailed:
        """A `GET` endpoint or the release query (sections 7.1, 9 and 11.6).

        In the state `record_unverified` nothing is read. Otherwise the connection is taken from
        the pool before the state lock. Under the state lock the state is tested again, because the
        process can have entered it while this request waited for its connection or the lock (section
        12.1); then the incremental fold runs, and `reader` reads the answer from the state, through
        that connection where it needs stored rows. A failed fold enters the state
        `record_unverified`.
        """
        if self.unverified:
            return ALREADY_UNVERIFIED
        try:
            with self.pool.connection() as connection, self.memory.lock:
                if self._unverified.is_set():
                    return ALREADY_UNVERIFIED
                with database_step(STEP_READ):
                    failure = incremental_fold(connection, self.memory, self.keys)
                self.hooks.in_read_fold()
                if failure is not None:
                    # The state lock is held here, as `_enter_unverified` requires.
                    self._unverified.set()
                    return Unverified(failure)
                return Read(reader(self.memory.state, connection))
        except StorageError as error:
            return _database_failed(error)

    def append(
        self, actor: KeyEntry, endpoint: str, text: bytes, signature: bytes
    ) -> Appended | Refused | AlreadyUnverified | Unverified | DatabaseFailed:
        """An append endpoint after checks 1 and 2 of section 6.7.

        The `503` of a process already in the state `record_unverified` comes before check 3;
        checks 3 to 6 need no connection; then the append transaction of section 11.5. Its step 2
        tests the state again under the state lock and answers that same `503` when the process
        entered it while the append waited. A failed fold in its step 2 enters the state
        `record_unverified`.
        """
        if self.unverified:
            return ALREADY_UNVERIFIED
        try:
            check_signed_event(text, signature, actor)
        except RecordRefusal as refusal:
            return Refused(refusal.error)
        try:
            with self.pool.connection() as connection:
                outcome = append_event(
                    connection,
                    self.memory,
                    self.keys,
                    actor,
                    endpoint,
                    text,
                    signature,
                    self.clock,
                    self.hooks.append_before_insert,
                    self.hooks.append_after_commit,
                    self._still_verified,
                )
        except StorageError as error:
            return _database_failed(error)
        if isinstance(outcome, EnteredUnverified):
            return ALREADY_UNVERIFIED
        if isinstance(outcome, Unverified):
            self._enter_unverified()
        return outcome

    def verify(self) -> Verified | VerifyBusy | AlreadyUnverified | DatabaseFailed:
        """`GET /api/key/record/verify` after its query validation (section 7.9): the `503`, then
        the limit, then a full verification with the comparison with the head of the state."""
        if self.unverified:
            return ALREADY_UNVERIFIED
        with self._limit:
            now = self.monotonic()
            started = self._request_started
            if self._request_running or (started is not None and now - started < VERIFY_LIMIT_SECONDS):
                return VERIFY_BUSY
            self._request_running = True
            self._request_started = now
        try:
            return self._full_verification()
        finally:
            with self._limit:
                self._request_running = False

    def _full_verification(self) -> Verified | AlreadyUnverified | DatabaseFailed:
        """A full verification of a running process (section 12.1): the head of the state, read
        under the state lock, then the snapshot. Under that lock the state `record_unverified` is
        tested again, because the process can have entered it while this run waited for its
        connection or the lock; then nothing is read. A failure enters the state
        `record_unverified`."""
        try:
            with self.pool.connection() as connection:
                with self.memory.lock:
                    if self._unverified.is_set():
                        return ALREADY_UNVERIFIED
                    memory_head = self.memory.state.head
                result = verify_stored(
                    connection, self.keys, self.hub_keys, memory_head, self.hooks.snapshot_between_reads
                )
        except StorageError as error:
            return _database_failed(error)
        if result.failure is not None:
            self._enter_unverified()
        return Verified(result)

    # --- background work (sections 12.1 and 12.2) -------------------------------------------------

    def periodic(self) -> None:
        """The periodic verification when it is due: 10 minutes after the previous run started, or
        at once when that run ended later. A failure logs `record verify failed`; a database
        failure logs `record database error` and does not enter the state `record_unverified`."""
        if self.unverified:
            return
        now = self.monotonic()
        if now - self._periodic_started < PERIOD_SECONDS:
            return
        self._periodic_started = now
        outcome = self._full_verification()
        if isinstance(outcome, DatabaseFailed):
            log_database_error(outcome.step, outcome.sqlstate)
            return
        # A run that finds the process already in the state `record_unverified` reads nothing and
        # logs nothing (section 12.1).
        failure = outcome.result.failure if isinstance(outcome, Verified) else None
        if failure is not None:
            log_verify_failed(failure.position, failure.failure)

    def checkpoint(self) -> None:
        """One checkpoint attempt for the latest full hour of the clock, once per full hour reached
        (section 12.2); none in the state `record_unverified`."""
        if self.unverified:
            return
        hour = full_hour(self.clock())
        if hour == self._last_hour:
            return
        self._last_hour = hour
        try:
            with self.pool.connection() as connection:
                outcome = checkpoint_attempt(
                    connection,
                    self.memory,
                    self.keys,
                    self.signer,
                    hour,
                    self.clock,
                    self.hooks.checkpoint_before_insert,
                    self._still_verified,
                )
        except StorageError as error:
            # Section 15: also a failed connection is `record database error checkpoint -`.
            log_database_error(STEP_CHECKPOINT, error.sqlstate_text)
            return
        if isinstance(outcome, CheckpointWritten):
            LOG.info("record checkpoint %d %s", outcome.position, outcome.record_hash)
        elif isinstance(outcome, Unverified):
            self._enter_unverified()
            log_verify_failed(outcome.failure.position, outcome.failure.failure)

    def tick(self) -> None:
        """Whatever background work is due now."""
        self.periodic()
        self.checkpoint()

    def start_background(self) -> None:
        """Start the background thread, unless `poll_seconds` is None."""
        if self._poll_seconds is None:
            return
        self._thread = threading.Thread(target=self._serve, args=(self._poll_seconds,), name="record", daemon=True)
        self._thread.start()

    def _serve(self, poll_seconds: float) -> None:
        while not self._stop.wait(poll_seconds):
            self.tick()

    def stop(self) -> None:
        """Stop the background thread, if one runs, and close the idle connections."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        self.pool.close()


# --- the start (section 11.4) ---------------------------------------------------------------------


def _fail(step: str) -> RecordStartFailed:
    LOG.error("record start failed %s", step)
    return RecordStartFailed(step)


def start_record(
    environ: Mapping[str, str],
    keys: KeyRing,
    signer: HubSigner,
    clock: Clock,
    *,
    monotonic: Monotonic,
    pool_size: int,
    poll_seconds: float | None,
    hooks: Hooks,
) -> RecordRuntime:
    """The start of the hub's record part (section 11.4), in the order `config`, `schema`,
    `writer` and `verify`; `RecordStartFailed` after the line `record start failed <step>`.

    1. `config`: AGENT_CORE_RECORD_DATABASE has the form of section 15, the owner's variable is not
       set (also not empty), and libpq is version 16 or later. Nothing connects before this passed.
    2. `schema`: the connection of the writer role, and exactly the schema version this code knows.
    3. `writer`: exactly the writer rights of section 11.2.
    4. `verify`: the whole record in one snapshot; a failure logs `record verify failed <position>
       <failure>` and then `record start failed verify`, a database failure `record start failed
       verify_read` alone. On success `record verified <position> <record hash or ->`, and the
       state in memory is the state of that fold. This run is the first periodic verification.
    """
    try:
        writer = read_connection_string(environ, WRITER_VARIABLE)
    except ConnectionStringError:
        raise _fail(START_CONFIG) from None
    if OWNER_VARIABLE in environ or not libpq_supported():
        raise _fail(START_CONFIG)
    try:
        with writer_connection(writer) as connection:
            if check_schema(connection) is not None:
                raise _fail(START_SCHEMA)
            if not writer_rights_hold(connection, writer.role):
                raise _fail(START_WRITER)
            started = monotonic()
            result = verify_stored(
                connection, keys, {signer.key_id: signer.public_key}, None, hooks.snapshot_between_reads
            )
    except StorageError as error:
        raise _fail(_START_STEP_OF_STORAGE_STEP[error.step]) from None
    failure = result.failure
    if failure is not None:
        log_verify_failed(failure.position, failure.failure)
        raise _fail(START_VERIFY)
    LOG.info("record verified %s", head_words(result.state.head))
    return RecordRuntime(
        writer=writer,
        keys=keys,
        signer=signer,
        clock=clock,
        monotonic=monotonic,
        memory=MemoryState(result.state),
        pool_size=pool_size,
        poll_seconds=poll_seconds,
        hooks=hooks,
        last_verification=started,
    )
