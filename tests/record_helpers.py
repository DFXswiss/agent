"""Helpers of the storage and command tests of the record (RECORD-PROTOCOL.md sections 11, 12, 14).

The keys of these tests are loaded from a keys file by the loader of KEY-PROTOCOL.md section 3, as
offline `verify` and `restore` load them, so they hold only the scopes that loader accepts.
"""

from __future__ import annotations

import socket
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

import psycopg
import yaml

from agent_core.keys import KeyRing, load_keys
from agent_core.record.checkpoint import Checkpoint, checkpoint_text
from agent_core.record.connection import parse_connection_string
from agent_core.record.event import Change, build_event_text, parse_event, record_hash
from agent_core.record.export import write_export
from agent_core.record.migrate import run_migrate
from agent_core.record.reducer import AppendContext, reduce_append
from agent_core.record.state import EMPTY_STATE, RecordState
from agent_core.record.storage import (
    APPEND_LOCK,
    MemoryState,
    append_event,
    prepare_session,
)
from agent_core.record.verify import RowCopies, StoredEvent
from agent_core.signing import HubSigner, b64url_decode, b64url_encode, parse_time
from agent_core.teams import load_roster
from tests.conftest import RecordDatabase
from tests.test_record_reducer import private, public

T0 = parse_time("2026-11-17T10:00:00Z")
assert T0 is not None
STEP = 60
TEAM = "example"
ALICE = "alice-laptop-1"
BOB = "bob-laptop-1"
CARA = "cara-laptop-1"
SEEDS = {ALICE: 51, BOB: 52, CARA: 53}
SCOPES = ["record:read", "record:write", "device:controller"]

HUB = "hub-1"
HUB_SIGNER = HubSigner.from_seed_hex(HUB, "61" * 32)
HUB_PUBLIC = b64url_decode(HUB_SIGNER.public_key_text)
OTHER_HUB = "hub-9"
OTHER_HUB_SIGNER = HubSigner.from_seed_hex(OTHER_HUB, "62" * 32)
OTHER_HUB_PUBLIC = b64url_decode(OTHER_HUB_SIGNER.public_key_text)

Changes = Sequence[tuple[str, "str | None", "str | None"]]


def new_id() -> str:
    return str(uuid.uuid4())


def hub_key_argument(signer: HubSigner) -> str:
    """`ID=PUBLIC_KEY` of section 12.3."""
    return f"{signer.key_id}={signer.public_key_text}"


# --- the keys file and the teams file -------------------------------------------------------------


def key_entry(key_id: str, handle: str) -> dict[str, Any]:
    return {
        "id": key_id,
        "kind": "ed25519",
        "principal": "agent",
        "handle": handle,
        "team": TEAM,
        "public_key": b64url_encode(public(SEEDS[key_id])),
        "scopes": list(SCOPES),
        "device": "laptop-1",
    }


def write_key_files(directory: Path, key_ids: Sequence[str] = (ALICE, BOB)) -> tuple[Path, Path]:
    """A keys file with the given agent keys of the team `example`, and its teams file."""
    handles = {ALICE: "alice", BOB: "bob", CARA: "cara"}
    teams = directory / "record-teams.yaml"
    teams.write_text(
        yaml.safe_dump({"teams": {TEAM: {"members": ["alice", "bob", "cara"]}}}), encoding="utf-8"
    )
    keys = directory / "record-keys.yaml"
    keys.write_text(
        yaml.safe_dump({"keys": [key_entry(key_id, handles[key_id]) for key_id in key_ids]}), encoding="utf-8"
    )
    return keys, teams


def load_ring(keys: Path, teams: Path) -> KeyRing:
    return load_keys(keys, load_roster(teams), T0)


# --- events ---------------------------------------------------------------------------------------


def encode(value: str | None) -> bytes | None:
    return None if value is None else value.encode("utf-8")


def sign_event(
    state: RecordState,
    key_id: str,
    object_type: str,
    object_id: str,
    changes: Changes,
    signed_at: int,
    *,
    event_id: str | None = None,
    prev_hash: str | None = None,
    certificate: bytes | None = None,
) -> tuple[bytes, bytes]:
    """A validly signed event of `key_id`, chained to the key's head in `state` unless `prev_hash`
    is given."""
    head = state.chains.get(key_id)
    if prev_hash is None:
        prev_hash = "-" if head is None else head.chain.event_hash
    text = build_event_text(
        event_id=event_id or new_id(),
        object_type=object_type,
        object_id=object_id,
        actor_key_id=key_id,
        signed_at=signed_at,
        prev_hash=prev_hash,
        changes=[Change(name, encode(old), encode(new)) for name, old, new in changes],
        key_certificate=certificate,
    )
    return text, private(SEEDS[key_id]).sign(text)


def task_create(title: str = "Fix login") -> list[tuple[str, str | None, str | None]]:
    return [("kind", None, "work"), ("team", None, TEAM), ("title", None, title)]


def stored_event(position: int, received_at: int, text: bytes, signature: bytes, previous: str | None) -> StoredEvent:
    """An event as a hub stores it at `position`, with its record hash after `previous`."""
    event = parse_event(text)
    return StoredEvent(
        position=position,
        received_at=received_at,
        event_hash=event.event_hash,
        record_hash=record_hash(position, received_at, event.event_hash, previous),
        text=text,
        signature=signature,
        copies=RowCopies(event.event_id, event.actor_key_id, event.prev_hash),
    )


def signed_checkpoint(signer: HubSigner, position: int, value: str, published_at: int) -> Checkpoint:
    signature = b64url_decode(signer.sign(checkpoint_text(signer.key_id, position, value, published_at)))
    assert signature is not None
    return Checkpoint(
        hub_key_id=signer.key_id, position=position, record_hash=value, published_at=published_at, signature=signature
    )


def rehash(events: Sequence[StoredEvent]) -> list[StoredEvent]:
    """The same events with every record hash computed again (section 4.6)."""
    out: list[StoredEvent] = []
    previous = None
    for stored in events:
        value = record_hash(stored.position, stored.received_at, stored.event_hash, previous)
        out.append(replace(stored, record_hash=value))
        previous = value
    return out


class PureRecord:
    """A record built by the library alone, as a hub stores it, with a clock that moves one
    minute per accepted event. No database."""

    def __init__(self, keys: KeyRing) -> None:
        self.keys = keys
        self.state: RecordState = EMPTY_STATE
        self.now = T0
        self.events: list[StoredEvent] = []
        self.checkpoints: list[Checkpoint] = []

    def write(self, key_id: str, object_type: str, object_id: str, changes: Changes) -> None:
        text, signature = sign_event(self.state, key_id, object_type, object_id, changes, self.now)
        accepted = reduce_append(
            self.state, text, signature, AppendContext(self.keys, self.keys.entries[key_id], "events", self.now)
        )
        head = accepted.applied.head
        self.events.append(stored_event(head.position, head.received_at, text, signature, None if not self.events else self.events[-1].record_hash))
        self.state = accepted.state
        self.now += STEP

    def checkpoint(self, signer: HubSigner) -> Checkpoint:
        head = self.events[-1]
        made = signed_checkpoint(signer, head.position, head.record_hash, self.now)
        self.checkpoints.append(made)
        return made

    def story(self) -> str:
        """alice creates a task, bob renames it, alice comments on it; hub-1 signs the head."""
        work = new_id()
        self.write(ALICE, "task", work, task_create())
        self.write(BOB, "task", work, [("title", "Fix login", "Fix the login")])
        self.write(ALICE, "comment", new_id(), [("task", None, work), ("body", None, "Seen")])
        self.checkpoint(HUB_SIGNER)
        return work

    def export(self) -> bytes:
        return write_export(self.events, self.checkpoints, self.now)


# --- the database ---------------------------------------------------------------------------------


def migrated(db: RecordDatabase) -> None:
    assert run_migrate(db.migrate_env()) == ("migrated 1", 0)


def writer(db: RecordDatabase) -> psycopg.Connection[Any]:
    """A connection of the writer role as the storage module opens it."""
    connection = parse_connection_string(db.writer_url).connect(autocommit=True)
    prepare_session(connection)
    return connection


class Clock:
    """A fixed clock that counts its readings."""

    def __init__(self, now: int) -> None:
        self.now = now
        self.reads = 0

    def __call__(self) -> int:
        self.reads += 1
        return self.now


_LAST_EVENT_OF_KEY = "SELECT event_hash FROM record.event WHERE actor_key_id = %s ORDER BY position DESC LIMIT 1"


class Process:
    """One hub process on a test database: its writer connection, its state in memory and its clock."""

    def __init__(self, db: RecordDatabase, keys: KeyRing) -> None:
        self.db = db
        self.keys = keys
        self.memory = MemoryState()
        self.clock = Clock(T0)
        self.connection = writer(db)

    def close(self) -> None:
        self.connection.close()

    def accepted_prev_hash(self, key_id: str) -> str:
        """The `prev_hash` a writer signs against (section 4.5): the event hash of the last stored
        event of `key_id`, as `GET /api/key/record/head` tells it (section 7.8), or `-` when the
        key has no stored event.

        It is read from the database, not from the state in memory of this process: the writer
        knows the record of the hub, not the state of the one process that happens to take its
        request, and that state may not yet hold the events another process committed. Reading
        it does not change the state in memory, so an append still has to fold those events
        itself (section 11.5, step 2) before its checks accept the event.
        """
        row = self.connection.execute(_LAST_EVENT_OF_KEY, (key_id,)).fetchone()
        return "-" if row is None else row[0]

    def sign(self, key_id: str, object_type: str, object_id: str, changes: Changes, **options: Any) -> tuple[bytes, bytes]:
        """A validly signed event of `key_id`, chained to the last stored event of that key unless
        `prev_hash` is given."""
        if "prev_hash" not in options:
            options["prev_hash"] = self.accepted_prev_hash(key_id)
        return sign_event(self.memory.state, key_id, object_type, object_id, changes, self.clock.now, **options)

    def append_text(self, key_id: str, text: bytes, signature: bytes, **hooks: Callable[[], None]) -> Any:
        outcome = append_event(
            self.connection,
            self.memory,
            self.keys,
            self.keys.entries[key_id],
            "events",
            text,
            signature,
            self.clock,
            **hooks,
        )
        self.clock.now += STEP
        return outcome

    def append(self, key_id: str, object_type: str, object_id: str, changes: Changes, **hooks: Callable[[], None]) -> Any:
        text, signature = self.sign(key_id, object_type, object_id, changes)
        return self.append_text(key_id, text, signature, **hooks)

    def story(self) -> str:
        """alice creates a task, bob renames it, alice comments on it: positions 1 to 3."""
        work = new_id()
        self.append(ALICE, "task", work, task_create())
        self.append(BOB, "task", work, [("title", "Fix login", "Fix the login")])
        self.append(ALICE, "comment", new_id(), [("task", None, work), ("body", None, "Seen")])
        return work


@contextmanager
def process(db: RecordDatabase, keys: KeyRing) -> Iterator[Process]:
    made = Process(db, keys)
    try:
        yield made
    finally:
        made.close()


def timestamp(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, timezone.utc)


TRIGGERS_OF = {"record.event": "event_no_change", "record.checkpoint": "checkpoint_no_change"}


def tamper(db: RecordDatabase, table: str, statement: str, params: Sequence[Any] = ()) -> None:
    """Change rows as database superuser with the triggers bypassed (section 14): the trigger is
    disabled for the statement and enabled ALWAYS again afterwards."""
    trigger = TRIGGERS_OF[table]
    with db.admin() as admin:
        admin.execute(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}")
        admin.execute(statement, params)
        admin.execute(f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {trigger}")


def insert_as_superuser(db: RecordDatabase, stored: StoredEvent, **columns: str) -> None:
    """Insert a stored event as database superuser; `columns` overrides copied columns."""
    event = parse_event(stored.text)
    with db.admin() as admin:
        admin.execute(
            "INSERT INTO record.event (position, event_id, actor_key_id, prev_hash, event_hash, received_at,"
            " event_text, signature, record_hash) VALUES (%s, %s::uuid, %s, %s, %s, %s, %s, %s, %s)",
            (
                stored.position,
                columns.get("event_id", event.event_id),
                columns.get("actor_key_id", event.actor_key_id),
                columns.get("prev_hash", event.prev_hash),
                stored.event_hash,
                timestamp(stored.received_at),
                stored.text,
                stored.signature,
                stored.record_hash,
            ),
        )


def insert_checkpoint(db: RecordDatabase, checkpoint: Checkpoint, published_at: str | None = None) -> None:
    """Insert a checkpoint as the writer role; `published_at` overrides the stored time as text."""
    with writer(db) as connection:
        connection.execute(
            "INSERT INTO record.checkpoint (hub_key_id, position, record_hash, published_at, signature)"
            " VALUES (%s, %s, %s, %s::timestamptz, %s)",
            (
                checkpoint.hub_key_id,
                checkpoint.position,
                checkpoint.record_hash,
                published_at or timestamp(checkpoint.published_at).isoformat(),
                checkpoint.signature,
            ),
        )


def counts(db: RecordDatabase) -> tuple[int, int]:
    """The number of rows of `record.event` and `record.checkpoint`, as the superuser sees them."""
    with db.admin() as admin:
        row = admin.execute(
            "SELECT (SELECT count(*) FROM record.event), (SELECT count(*) FROM record.checkpoint)"
        ).fetchone()
    assert row is not None
    return row[0], row[1]


def backend_of(connection: psycopg.Connection[Any]) -> int:
    return connection.info.backend_pid


def terminate(db: RecordDatabase, pid: int) -> None:
    """End the server process of another connection, as a lost connection does."""
    with db.admin() as admin:
        admin.execute("SELECT pg_terminate_backend(%s)", (pid,))


def wait_for_append_lock(db: RecordDatabase, waiting: int) -> None:
    """Wait until `waiting` sessions wait for the advisory lock 742180001 of section 11.5."""
    deadline = time.monotonic() + 60
    with db.admin() as watcher:
        while time.monotonic() < deadline:
            row = watcher.execute(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid::bigint = %s AND NOT granted"
                " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())",
                (APPEND_LOCK,),
            ).fetchone()
            assert row is not None
            if row[0] >= waiting:
                return
            time.sleep(0.05)
    raise AssertionError("no session waits for the advisory lock")


@contextmanager
def append_lock_held(db: RecordDatabase) -> Iterator[psycopg.Connection[Any]]:
    """The advisory lock 742180001 held by the superuser in an open transaction for the block."""
    with db.admin(autocommit=False) as holder:
        holder.execute("SELECT pg_advisory_xact_lock(%s)", (APPEND_LOCK,))
        yield holder
        holder.rollback()


class Parallel:
    """Calls started each in a thread of its own; `results` joins them."""

    def __init__(self, *calls: Callable[[], Any]) -> None:
        self._results: list[Any] = [None] * len(calls)
        self._threads = [threading.Thread(target=self._run, args=(index, call)) for index, call in enumerate(calls)]
        for thread in self._threads:
            thread.start()

    def _run(self, index: int, call: Callable[[], Any]) -> None:
        self._results[index] = call()

    def results(self) -> list[Any]:
        for thread in self._threads:
            thread.join(timeout=60)
        assert not any(thread.is_alive() for thread in self._threads)
        return self._results


def closed_port() -> int:
    """A port of 127.0.0.1 that nothing listens on."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@contextmanager
def silent_listener() -> Iterator[int]:
    """A listener on 127.0.0.1 that accepts TCP connections and never answers (section 14)."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    accepted: list[socket.socket] = []
    stop = threading.Event()

    def serve() -> None:
        server.settimeout(0.1)
        while not stop.is_set():
            try:
                connection, _ = server.accept()
            except OSError:
                continue
            accepted.append(connection)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield server.getsockname()[1]
    finally:
        stop.set()
        thread.join(timeout=5)
        for connection in accepted:
            connection.close()
        server.close()


def unreachable_url(db: RecordDatabase, port: int) -> str:
    return db.url(db.writer_role, db.writer_password, host="127.0.0.1", port=port, sslmode="disable")
