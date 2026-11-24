"""Prompt origin: registry of platform prompts and the UserPromptSubmit check (05-runner.md section 5.4, 4.6 no. 7).

Only prompts the runner registered through ``POST /v1/sessions/{id}/prompts`` (``control_routes/prompts.py``) are
accepted, each exactly once and only in its own session. Text typed into the pane by anybody else, a second
submission of the same prompt and a prompt of another session are blocked.

* ``normalise(text)`` - Claude Code's escaping of wrapper tags inside a prompt (``<\\pasted_content``,
  ``<\\/pasted_content``, seen with 2.1.330 before the UserPromptSubmit hook, ATS-02) undone; then, if the whole prompt
  (or the prompt without surrounding whitespace) is one ``<pasted_content id="...">`` wrapper of the TUI, its inner
  text; then surrounding whitespace removed; then NFC. Registered and submitted text go through the same steps, so a
  prompt whose text contains such a tag is accepted as registered and a forged wrapper hides nothing new
* ``prompt_sha256(text)`` - hex SHA-256 of the UTF-8 bytes of ``normalise(text)``; registration and check use it
* ``PromptRegistry(journal, state)`` (control process, owner of ``prompt_registry``):

  - ``register(session_id, prompt_id, text, speaker=None)`` - stores the hash for an active session (``not_found`` /
    ``state_conflict``) with the sender line the runner composed (``speaker``, ADR-083 section 1; checked by the
    request schema). Repeating the same ``prompt_id`` with the same text is accepted (runner retry); the same
    ``prompt_id`` with another text is ``id_conflict``, an already accepted ``prompt_id`` ``state_conflict``. Rows are
    keyed by ``prompt_id`` and never replaced, so the same text may be registered under several IDs (a repeated
    prompt); each registration is accepted once, the oldest unused one first.
  - ``decide(session_id, prompt)`` -> ``(accepted, speaker)`` - in one transaction: an unused row with the hash ->
    mark it used, journal ``session.hook`` (allow) and ``session.state prompt_accepted {prompt_id}``, set the session
    state ``working``, and return the sender line of exactly that row (``None`` when it has none);
    otherwise (also ``prompt`` ``None`` for an unreadable hook body) journal ``session.hook`` (block) and
    ``session.state prompt_refused {prompt_id, reason: "unregistered"}`` with a fresh UUIDv7 as ``prompt_id`` (the
    refused text has no platform ID). The runner turns ``prompt_refused`` into the audit row
    ``session.prompt_refused``. ``check`` is the decision alone.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import replace

from agent_guest.clock import iso_ms, uuid7
from agent_guest.journal import Journal
from agent_guest.policy import BLOCK_REASON
from agent_guest.state import INACTIVE_STATES, Session, State, StateError

_PASTED_RE = re.compile(r'<pasted_content id="[^"]*">\n(.*)\n</pasted_content id="[^"]*">', re.DOTALL)


_ESCAPED = (("<\\/pasted_content", "</pasted_content"), ("<\\pasted_content", "<pasted_content"))


def normalise(text: str) -> str:
    for escaped, plain in _ESCAPED:
        text = text.replace(escaped, plain)
    found = _PASTED_RE.fullmatch(text) or _PASTED_RE.fullmatch(text.strip())
    inner = found.group(1) if found else text
    return unicodedata.normalize("NFC", inner.strip())


def prompt_sha256(text: str) -> str:
    return hashlib.sha256(normalise(text).encode("utf-8", "surrogatepass")).hexdigest()


class PromptRegistry:
    def __init__(self, journal: Journal, state: State) -> None:
        self.journal = journal
        self.state = state

    def _active(self, session_id: str) -> Session:
        session = self.state.session(session_id)
        if session is None:
            raise StateError("not_found")
        if session.state in INACTIVE_STATES:
            raise StateError("state_conflict")
        return session

    def register(self, session_id: str, prompt_id: str, text: str, speaker: str | None = None) -> None:
        digest = prompt_sha256(text)
        with self.journal.transaction() as db:
            self._active(session_id)
            row = db.execute(
                "SELECT sha256, used_at FROM prompt_registry WHERE session_id = ? AND prompt_id = ?",
                (session_id, prompt_id),
            ).fetchone()
            if row is not None:
                if row[0] != digest:
                    raise StateError("id_conflict")
                if row[1] is not None:
                    raise StateError("state_conflict")
                return
            db.execute(
                "INSERT INTO prompt_registry (session_id, sha256, prompt_id, registered_at, used_at, speaker)"
                " VALUES (?, ?, ?, ?, NULL, ?)",
                (session_id, digest, prompt_id, iso_ms(self.journal.clock.now()), speaker),
            )

    def check(self, session_id: str, prompt: str | None) -> bool:
        return self.decide(session_id, prompt)[0]

    def decide(self, session_id: str, prompt: str | None) -> tuple[bool, str | None]:
        now = self.journal.clock.now()
        at = iso_ms(now)
        with self.journal.transaction() as db:
            session = self._active(session_id)
            row = None
            if prompt is not None:
                row = db.execute(
                    "SELECT prompt_id, speaker FROM prompt_registry WHERE session_id = ? AND sha256 = ?"
                    " AND used_at IS NULL"
                    " ORDER BY registered_at, rowid LIMIT 1",
                    (session_id, prompt_sha256(prompt)),
                ).fetchone()
            if row is None:
                self.journal.insert(
                    db,
                    "session.hook",
                    {"event": "UserPromptSubmit", "decision": "block", "reason": BLOCK_REASON},
                    session_id,
                )
                self.journal.insert(
                    db,
                    "session.state",
                    {"state": "prompt_refused", "prompt_id": uuid7(now), "reason": "unregistered", "at": at},
                    session_id,
                )
                return False, None
            db.execute(
                "UPDATE prompt_registry SET used_at = ? WHERE session_id = ? AND prompt_id = ?",
                (at, session_id, row[0]),
            )
            self.journal.insert(db, "session.hook", {"event": "UserPromptSubmit", "decision": "allow"}, session_id)
            self.journal.insert(
                db, "session.state", {"state": "prompt_accepted", "prompt_id": row[0], "at": at}, session_id
            )
            self.state.save_session(replace(session, state="working"), db)
            return True, row[1]
