"""Journal and failure list of the mail double, shared by its SMTP and IMAP connections.

Mail commands use the common journal format (`agent_doubles.common.journal`): ``method`` is the protocol (``SMTP``,
``IMAP`` or ``IMAPS``), ``path`` the command verb, ``query`` empty, ``headers`` empty, ``body`` the arguments as the
double understood them (passwords reduced to their length), ``status`` the SMTP reply code (0 for IMAP), ``note``
a short explanation of a refusal and ``result`` the IMAP completion (``OK``, ``NO``, ``BAD``) or the SMTP reply class.
"""

from __future__ import annotations

import threading
from typing import Any

from agent_doubles.common.journal import Journal


def redacted(secret: str) -> str:
    """How the journal shows a password."""
    return f"<redacted {len(secret)} chars>"


class Recorder:
    """The journal and the failures (unexpected commands and protocol errors) of one mail double."""

    def __init__(self, journal: Journal) -> None:
        self.journal = journal
        self._lock = threading.Lock()
        self._failures: list[str] = []

    def record(
        self, protocol: str, command: str, body: Any, *, result: str, status: int = 0, note: str | None = None
    ) -> None:
        """Journal one command and how it was answered."""
        entry = self.journal.open(protocol, command, "", {}, body)
        entry.status = status
        entry.note = note
        entry.extra["result"] = result
        self.journal.close(entry)

    def fail(self, text: str) -> None:
        """Record an unexpected command or protocol error."""
        with self._lock:
            self._failures.append(text)

    def failures(self) -> list[str]:
        with self._lock:
            return list(self._failures)

    def clear(self) -> None:
        with self._lock:
            self._failures.clear()
        self.journal.clear()
