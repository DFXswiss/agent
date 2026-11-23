"""Guest journal: SQLite, append-only records for the runner (02-data-model.md section 5, 03-api.md section 7.2).

Only the control process opens the database (root, 0600); the API process sends its records over ``api.sock``.
One connection, one lock: every write is a short ``BEGIN IMMEDIATE`` transaction; a commit that appended records
wakes the long polls of ``GET /v1/journal``.

Interface:

* ``Journal.open(path, clock, schemas=None)`` - creates the file 0600 (parent 0700), applies ``schema.sql`` when
  ``PRAGMA user_version`` is 0, refuses any other version than ``SCHEMA_VERSION`` (``journal_schema_mismatch``)
* ``Journal.transaction()`` - context manager yielding the connection inside ``BEGIN IMMEDIATE`` (other guest
  modules keep their writes and the matching journal record in one transaction)
* ``Journal.after_commit(callback)`` - inside a transaction: run ``callback`` once it committed (cache updates)
* ``Journal.insert(db, kind, body, session_id=None) -> int`` - append inside a transaction; ``append(...)`` is the
  same in its own transaction. Bodies are validated against ``guest/journal/<kind>.json`` when a ``SchemaSet`` is
  given (``body_invalid`` with pointers); kinds other than ``metrics``/``guest.error`` need a ``session_id``
* ``Journal.read(after, limit, wait) -> (records, head)`` - records with ``seq > after`` ascending; waits up to
  ``wait`` seconds for the first record (long poll), returns early on ``close()``
* ``Journal.ack(through)`` - marks records ``seq <= through`` acknowledged (``state_conflict`` if beyond the head)
* ``Journal.prune() -> int`` - deletes records acknowledged more than ``RETENTION_SECONDS`` ago
* ``Journal.prune_pending() -> list[str]`` - pending MCP calls (``pending``) older than ``PENDING_SECONDS``: an
  unanswered one is expired - removed, with ``guest.error pending_expired`` of its session in the same transaction (its
  MCP call returned long ago; a later answer of the runner is refused ``not_found``, which the runner counts as
  delivered and reports to the session as ``operation.finished``) - and an answered one is removed (KOR-02, AUD-01
  B-15); returns the expired IDs
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from agent_guest.clock import Clock, iso_ms
from agent_guest.schema import SchemaSet, pointers

SCHEMA_VERSION = 1
SCHEMA_FILE = Path(__file__).with_name("schema.sql")
RETENTION_SECONDS = 24 * 3600
PENDING_SECONDS = 3600
"""Age of a pending MCP call after which it is expired (unanswered) or removed (answered); every call waits at most
30 s (03-api.md section 12)."""
MAX_WAIT_SECONDS = 25
MAX_LIMIT = 1000
KINDS = (
    "session.state",
    "session.output",
    "session.tool",
    "session.hook",
    "session.file",
    "session.terminal",
    "usage",
    "operation.request",
    "platform.query",
    "workvm.call",
    "ping.delivered",
    "platform.read",
    "platform.refused",
    "metrics",
    "guest.error",
)
VM_LEVEL_KINDS = frozenset({"metrics", "guest.error"})


class JournalError(Exception):
    def __init__(self, code: str, detail: list[str] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail or []


def canonical(value: object) -> str:
    """Compact JSON text with sorted keys (the stored form of bodies and answers)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


class Journal:
    def __init__(self, connection: sqlite3.Connection, clock: Clock, schemas: SchemaSet | None) -> None:
        self._db = connection
        self._clock = clock
        self._schemas = schemas
        self._cond = threading.Condition(threading.RLock())
        self._closed = False
        self._appended = False
        self._after_commit: list[Callable[[], None]] = []
        row = self._db.execute("SELECT seq FROM sqlite_sequence WHERE name = 'journal'").fetchone()
        self._head = int(row[0]) if row else 0

    @classmethod
    def open(cls, path: str, clock: Clock, schemas: SchemaSet | None = None) -> Journal:
        parent = os.path.dirname(path)
        os.makedirs(parent, mode=0o700, exist_ok=True)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        os.chmod(path, 0o600)
        connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        try:
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                connection.executescript("BEGIN IMMEDIATE;\n" + SCHEMA_FILE.read_text(encoding="utf-8") + "\nCOMMIT;")
            elif version != SCHEMA_VERSION:
                raise JournalError("journal_schema_mismatch")
        except BaseException:
            connection.close()
            raise
        return cls(connection, clock, schemas)

    @property
    def head(self) -> int:
        with self._cond:
            return self._head

    @property
    def clock(self) -> Clock:
        return self._clock

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._cond:
            if self._closed:
                raise JournalError("unavailable")
            self._db.execute("BEGIN IMMEDIATE")
            self._appended = False
            self._after_commit = []
            head = self._head
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                self._head = head
                raise
            try:
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                self._head = head
                raise
            if self._appended:
                self._cond.notify_all()
            callbacks, self._after_commit = self._after_commit, []
        # Outside the journal lock: callbacks take other locks (session cache, pending waiters).
        for callback in callbacks:
            callback()

    def after_commit(self, callback: Callable[[], None]) -> None:
        """Run ``callback`` after the current transaction committed (never after a rollback)."""
        self._after_commit.append(callback)

    def insert(self, db: sqlite3.Connection, kind: str, body: dict[str, Any], session_id: str | None = None) -> int:
        """Append one record inside ``transaction()``; returns its ``seq``."""
        if kind not in KINDS:
            raise JournalError("kind_invalid")
        if session_id is None and kind not in VM_LEVEL_KINDS:
            raise JournalError("session_required")
        if self._schemas is not None:
            found = self._schemas.errors(f"guest/journal/{kind}.json", body)
            if found:
                raise JournalError("body_invalid", pointers(found))
        cursor = db.execute(
            "INSERT INTO journal (ts, session_id, kind, body) VALUES (?, ?, ?, ?)",
            (iso_ms(self._clock.now()), session_id, kind, canonical(body)),
        )
        seq = int(cursor.lastrowid or 0)
        self._head = seq
        self._appended = True
        return seq

    def append(self, kind: str, body: dict[str, Any], session_id: str | None = None) -> int:
        with self.transaction() as db:
            return self.insert(db, kind, body, session_id)

    def read(self, after: int, limit: int, wait: float = 0) -> tuple[list[dict[str, Any]], int]:
        limit = max(1, min(limit, MAX_LIMIT))
        deadline = time.monotonic() + max(0.0, min(wait, MAX_WAIT_SECONDS))
        with self._cond:
            while True:
                if self._closed:
                    rows = []
                    break
                rows = self._db.execute(
                    "SELECT seq, ts, session_id, kind, body FROM journal WHERE seq > ? ORDER BY seq LIMIT ?",
                    (after, limit),
                ).fetchall()
                remaining = deadline - time.monotonic()
                if rows or remaining <= 0:
                    break
                self._cond.wait(remaining)
            records = [
                {"seq": seq, "ts": ts, "session_id": session, "kind": kind, "body": json.loads(body)}
                for seq, ts, session, kind, body in rows
            ]
            return records, self._head

    def ack(self, through: int) -> None:
        with self.transaction() as db:
            if through > self._head:
                raise JournalError("state_conflict")
            db.execute(
                "UPDATE journal SET acked_at = ? WHERE seq <= ? AND acked_at IS NULL", (self._clock.now(), through)
            )

    def prune(self) -> int:
        with self.transaction() as db:
            cursor = db.execute(
                "DELETE FROM journal WHERE acked_at IS NOT NULL AND acked_at <= ?",
                (self._clock.now() - RETENTION_SECONDS,),
            )
            return cursor.rowcount

    def prune_pending(self) -> list[str]:
        cutoff = iso_ms(self._clock.now() - PENDING_SECONDS)
        with self.transaction() as db:
            expired = db.execute(
                "SELECT id, session_id FROM pending WHERE state = 'waiting' AND created_at <= ? ORDER BY id", (cutoff,)
            ).fetchall()
            for pending_id, session_id in expired:
                db.execute("DELETE FROM pending WHERE id = ?", (pending_id,))
                self.insert(db, "guest.error", {"code": "pending_expired"}, session_id)
            db.execute("DELETE FROM pending WHERE state = 'answered' AND answered_at <= ?", (cutoff,))
        return [pending_id for pending_id, _ in expired]

    def close(self) -> None:
        with self._cond:
            if self._closed:
                return
            self._closed = True
            self._cond.notify_all()
            self._db.close()
