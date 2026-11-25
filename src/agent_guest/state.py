"""In-memory state of the control process: session cache, assignment, pending calls (05-runner.md §4.4, §4.6 no. 1).

The ``session`` table is read once at start and afterwards changed only through ``State.save_session`` (called by
control routes such as ``POST /v1/sessions``); the cache is updated after the commit, never by re-reading the
database per request. Token lookup hashes nothing itself: the caller passes the SHA-256 of the bearer token and the
cache compares it with ``hmac.compare_digest`` against every stored hash (no early exit), so the time does not depend
on which session matches.

Interface:

* ``Session`` - row of the ``session`` table
* ``State(journal)`` - loads sessions and the persisted assignment/broken flags
* ``State.sessions()``, ``State.session(id)``, ``State.save_session(session, db=None)``
* ``State.resolve(token_sha256_hex) -> Session | None`` - only sessions that are not ``stopped``/``failed``
* ``State.assignment``, ``State.assign(vm_id, org_id, wg_id)`` - once per VM (``already_assigned``; ``state_conflict``
  when broken); repeating the same assignment is accepted
* ``State.broken``, ``State.mark_broken()``
* ``Pending(journal, state)`` - MCP calls waiting for the runner (02 section 5 ``pending``):
  ``create(session_id, kind, body) -> pending_id`` writes the pending row and its journal record
  (``operation.request``, ``platform.query`` or ``workvm.call`` with ``pending_id`` added) in one transaction;
  ``answer(pending_id, answer)`` stores the runner's answer once (an identical repeat is accepted, a different one is
  ``state_conflict``); ``wait(pending_id, timeout, session_id) -> answer | None`` blocks until answered, the time
  limit or ``close()``
"""

from __future__ import annotations

import hmac
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Literal

from agent_guest.clock import iso_ms, uuid7
from agent_guest.journal import Journal, canonical

INACTIVE_STATES = frozenset({"stopped", "failed"})
PENDING_KINDS = {
    "operation": "operation.request",
    "query": "platform.query",
    "workvm": "workvm.call",
    "relay": "hub.relay",
}


class StateError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Session:
    id: str
    uid: int
    token_sha256: str
    state: str
    workspace: str
    claude_session_id: str | None
    created_at: str


class State:
    def __init__(self, journal: Journal) -> None:
        self.journal = journal
        self._lock = threading.Lock()
        with journal.transaction() as db:
            rows = db.execute(
                "SELECT id, uid, token_sha256, state, workspace, claude_session_id, created_at FROM session"
            ).fetchall()
            meta = dict(db.execute("SELECT key, value FROM guest_meta").fetchall())
        self._sessions = {row[0]: Session(*row) for row in rows}
        self._assignment: dict[str, str] | None = json.loads(meta["assignment"]) if "assignment" in meta else None
        self._broken = meta.get("broken") == "1"

    def sessions(self) -> list[Session]:
        with self._lock:
            return sorted(self._sessions.values(), key=lambda session: session.id)

    def session(self, session_id: str) -> Session | None:
        with self._lock:
            return self._sessions.get(session_id)

    def save_session(self, session: Session, db: sqlite3.Connection | None = None) -> None:
        """Insert or replace the row; the cache follows after the commit of the surrounding transaction."""
        if db is None:
            with self.journal.transaction() as own:
                self.save_session(session, own)
            return
        db.execute(
            "INSERT OR REPLACE INTO session (id, uid, token_sha256, state, workspace, claude_session_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                session.id,
                session.uid,
                session.token_sha256,
                session.state,
                session.workspace,
                session.claude_session_id,
                session.created_at,
            ),
        )
        self.journal.after_commit(lambda: self._cache(session))

    def _cache(self, session: Session) -> None:
        with self._lock:
            self._sessions[session.id] = session

    def resolve(self, token_hash: str) -> Session | None:
        wanted = token_hash.encode("ascii", "replace")
        found: Session | None = None
        with self._lock:
            candidates = list(self._sessions.values())
        for session in candidates:
            if hmac.compare_digest(session.token_sha256.encode("ascii"), wanted):
                found = session
        if found is None or found.state in INACTIVE_STATES:
            return None
        return found

    @property
    def assignment(self) -> dict[str, str] | None:
        with self._lock:
            return None if self._assignment is None else dict(self._assignment)

    @property
    def broken(self) -> bool:
        with self._lock:
            return self._broken

    def assign(self, vm_id: str, org_id: str, wg_id: str) -> None:
        wanted = {"vm_id": vm_id, "org_id": org_id, "wg_id": wg_id}
        with self.journal.transaction() as db:
            with self._lock:
                current, broken = self._assignment, self._broken
            if current is not None:
                if current != wanted:
                    raise StateError("already_assigned")
                return
            if broken:
                raise StateError("state_conflict")
            db.execute("INSERT INTO guest_meta (key, value) VALUES ('assignment', ?)", (canonical(wanted),))
            self.journal.after_commit(lambda: self._set_assignment(wanted))

    def _set_assignment(self, value: dict[str, str]) -> None:
        with self._lock:
            self._assignment = value

    def mark_broken(self) -> None:
        with self.journal.transaction() as db:
            db.execute("INSERT OR REPLACE INTO guest_meta (key, value) VALUES ('broken', '1')")
            self.journal.after_commit(self._set_broken)

    def _set_broken(self) -> None:
        with self._lock:
            self._broken = True


class Pending:
    def __init__(self, journal: Journal, state: State) -> None:
        self.journal = journal
        self.state = state
        self._cond = threading.Condition()
        self._closed = False

    def create(
        self, session_id: str, kind: Literal["operation", "query", "workvm", "relay"], body: dict[str, Any]
    ) -> str:
        session = self.state.session(session_id)
        if session is None or session.state in INACTIVE_STATES:
            raise StateError("not_found")
        if kind not in PENDING_KINDS:
            raise StateError("request_invalid")
        now = self.journal.clock.now()
        pending_id = uuid7(now)
        record = {**body, "pending_id": pending_id}
        with self.journal.transaction() as db:
            db.execute(
                "INSERT INTO pending (id, session_id, kind, request, state, created_at)"
                " VALUES (?, ?, ?, ?, 'waiting', ?)",
                (pending_id, session_id, kind, canonical(record), iso_ms(now)),
            )
            self.journal.insert(db, PENDING_KINDS[kind], record, session_id)
        return pending_id

    def answer(self, pending_id: str, answer: dict[str, Any]) -> None:
        text = canonical(answer)
        with self.journal.transaction() as db:
            row = db.execute("SELECT state, answer FROM pending WHERE id = ?", (pending_id,)).fetchone()
            if row is None:
                raise StateError("not_found")
            if row[0] == "answered":
                if row[1] != text:
                    raise StateError("state_conflict")
                return
            db.execute(
                "UPDATE pending SET state = 'answered', answer = ?, answered_at = ? WHERE id = ?",
                (text, iso_ms(self.journal.clock.now()), pending_id),
            )
            self.journal.after_commit(self._wake)

    def _wake(self) -> None:
        with self._cond:
            self._cond.notify_all()

    def _lookup(self, pending_id: str, session_id: str) -> dict[str, Any] | None:
        with self.journal.transaction() as db:
            row = db.execute("SELECT session_id, answer FROM pending WHERE id = ?", (pending_id,)).fetchone()
        if row is None or row[0] != session_id:
            raise StateError("not_found")
        return None if row[1] is None else dict(json.loads(row[1]))

    def wait(self, pending_id: str, timeout: float, session_id: str) -> dict[str, Any] | None:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cond:
            while True:
                answer = self._lookup(pending_id, session_id)
                remaining = deadline - time.monotonic()
                if answer is not None or self._closed or remaining <= 0:
                    return answer
                self._cond.wait(remaining)

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()
