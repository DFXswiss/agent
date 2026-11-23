"""Content-free pings of a session (05-runner.md section 9, 03-api.md sections 7.1, 8.1 and 8.2, ADR-020).

The control process owns the ``ping`` table. A ping names only its kind, the whiteboard level and the ID of the
thing that is new (entry, child session or operation); it never carries content and is never typed into the TUI.

* ``store(session_id, ping)`` - ``POST /v1/sessions/{id}/pings`` (``control_routes/pings.py``): keeps the ping for an
  active session (``not_found`` / ``state_conflict``). The ``ping_id`` is idempotent: the same ping again changes
  nothing, the same ID with other fields is ``id_conflict``. A repeated ping that was already delivered journals its
  ``ping.delivered`` again (a runner that lost its record of the delivery learns it once more and can mark the hub
  row; decisions/RUN-14.md).
* ``notice(session_id)`` - the hook addon (``hook_addons/pings.py``): the pings that were neither shown nor fetched
  form the pending batch; they are marked noticed, each journaled as ``ping.delivered {ping_id, via:"hook"}``, and the
  fixed notice of 03-api.md section 8.2 is returned (``None`` without a new ping). A batch is noticed once.
* ``take(session_id)`` - the tool ``get_notifications``: every ping not fetched yet (oldest first, at most
  ``MAX_LIST``) as ``{ping_id, kind, level?, ref, at}``, marked delivered and journaled as ``ping.delivered {ping_id,
  via:"tool"}``. A ping that was noticed by a hook is still listed once by the tool. The read itself is journaled in
  the same transaction as ``platform.read {tool:"get_notifications", result_count}`` (03-api.md section 8: every read
  is audited; the runner writes the ``platform.read`` event and ``read.session.get_notifications``, KOR-07).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from agent_guest.clock import iso_ms
from agent_guest.journal import Journal
from agent_guest.state import INACTIVE_STATES, State, StateError

READ = "platform.read"
KINDS: tuple[str, ...] = ("whiteboard.entry", "subagent.finished", "operation.finished")
"""The ping kinds in the order of the notice (``contracts/mcp/objects.json#/$defs/notice``)."""
MAX_LIST = 1000
NOTICE = (
    "agent.dfx.swiss notice: {count} new item(s) available ({kinds}). Use mcp__platform__get_notifications to see them."
)


def notice_text(count: int, kinds: Sequence[str]) -> str:
    """The exact ``additionalContext`` of 03-api.md section 8.2 (kinds distinct, in the order of ``KINDS``)."""
    shown = [kind for kind in KINDS if kind in kinds]
    return NOTICE.format(count=count, kinds=", ".join(shown))


class PingStore:
    def __init__(self, journal: Journal, state: State) -> None:
        self.journal = journal
        self.state = state

    def _known(self, session_id: str) -> None:
        if self.state.session(session_id) is None:
            raise StateError("not_found")

    def _journal(self, db: sqlite3.Connection, session_id: str, ping_ids: Sequence[str], via: str) -> None:
        for ping_id in ping_ids:
            self.journal.insert(db, "ping.delivered", {"ping_id": ping_id, "via": via}, session_id)

    def store(self, session_id: str, ping: Mapping[str, Any]) -> None:
        session = self.state.session(session_id)
        if session is None:
            raise StateError("not_found")
        if session.state in INACTIVE_STATES:
            raise StateError("state_conflict")
        fields = (session_id, ping["kind"], ping.get("level"), ping["ref"])
        with self.journal.transaction() as db:
            row = db.execute(
                "SELECT session_id, kind, level, ref, noticed_at, delivered_at FROM ping WHERE id = ?",
                (ping["ping_id"],),
            ).fetchone()
            if row is not None:
                if tuple(row[:4]) != fields:
                    raise StateError("id_conflict")
                if row[5] is not None or row[4] is not None:
                    self._journal(db, session_id, [ping["ping_id"]], "tool" if row[5] is not None else "hook")
                return
            db.execute(
                "INSERT INTO ping (id, session_id, kind, level, ref, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (ping["ping_id"], *fields, iso_ms(self.journal.clock.now())),
            )

    def notice(self, session_id: str) -> str | None:
        self._known(session_id)
        with self.journal.transaction() as db:
            rows = db.execute(
                "SELECT id, kind FROM ping WHERE session_id = ? AND noticed_at IS NULL AND delivered_at IS NULL"
                " ORDER BY created_at, id",
                (session_id,),
            ).fetchall()
            if not rows:
                return None
            ids = [row[0] for row in rows]
            db.executemany(
                "UPDATE ping SET noticed_at = ? WHERE id = ?",
                [(iso_ms(self.journal.clock.now()), ping_id) for ping_id in ids],
            )
            self._journal(db, session_id, ids, "hook")
        return notice_text(len(rows), [row[1] for row in rows])

    def take(self, session_id: str) -> list[dict[str, Any]]:
        self._known(session_id)
        with self.journal.transaction() as db:
            rows = db.execute(
                "SELECT id, kind, level, ref, created_at FROM ping WHERE session_id = ? AND delivered_at IS NULL"
                " ORDER BY created_at, id LIMIT ?",
                (session_id, MAX_LIST),
            ).fetchall()
            now = iso_ms(self.journal.clock.now())
            db.executemany("UPDATE ping SET delivered_at = ? WHERE id = ?", [(now, row[0]) for row in rows])
            self._journal(db, session_id, [row[0] for row in rows], "tool")
            self.journal.insert(db, READ, {"tool": "get_notifications", "result_count": len(rows)}, session_id)
        found: list[dict[str, Any]] = []
        for ping_id, kind, level, ref, created_at in rows:
            item: dict[str, Any] = {"ping_id": ping_id, "kind": kind, "ref": ref, "at": created_at}
            if level is not None:
                item["level"] = level
            found.append(item)
        return found
