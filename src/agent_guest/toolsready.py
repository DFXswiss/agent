"""Whether a session's agent has the platform tools (HOT-09; 05-runner.md section 6.1 step 6, ADR-068).

Claude Code connects its MCP servers in the background and starts a turn without waiting for them, so a prompt that
arrives before the agent listed the platform tools runs a turn without them. The proof that the tools reached the
agent is its own ``tools/list`` on ``/mcp``: the API process reports it over ``api.sock`` (op
``session.tools_listed {session_id, tools}``, ``control_routes/tools.py``), authenticated by the token of the run
that asked. ``mark`` stores the session's current token hash in ``guest_meta`` (key ``tools_ready:<session>``), so

* the mark belongs to one run of the agent: a restart, resume or retry gives the session a new token and the mark no
  longer matches (``ready`` is ``False`` until that run listed the tools);
* it survives a restart of the control process (the agent keeps its MCP connection and does not list again);
* the first listing of a run journals ``session.tools_ready {tools, at}`` (the runner's start step ``tools_ready``);
  later listings of the same run change nothing.

The watcher of ``delivery.py`` asks ``ready`` every 50 ms: the marks are kept in memory per control process as well
(``_marks``, loaded from the database once, written through by ``mark``), so a poll opens no journal transaction.

Interface: ``mark(context, session_id, tools) -> bool`` (``True`` for the first listing of the run; ``StateError
not_found`` for an unknown or inactive session), ``ready(context, session_id) -> bool``, ``forget(context,
session_id)``.
"""

from __future__ import annotations

import threading

from agent_guest.clock import iso_ms
from agent_guest.control import ControlContext
from agent_guest.state import INACTIVE_STATES, StateError

KIND = "session.tools_ready"
_MARKS = "_tools_marks"
_marks_lock = threading.Lock()


def _marks(context: ControlContext) -> dict[str, str]:
    """Session -> token hash of the run that listed the tools (memory of this control process, loaded once)."""
    with _marks_lock:
        found = vars(context).get(_MARKS)
        if not isinstance(found, dict):
            with context.journal.transaction() as db:
                rows = db.execute("SELECT key, value FROM guest_meta WHERE key LIKE 'tools_ready:%'").fetchall()
            found = vars(context)[_MARKS] = {str(key).removeprefix("tools_ready:"): str(value) for key, value in rows}
        return found


def _key(session_id: str) -> str:
    return f"tools_ready:{session_id}"


def mark(context: ControlContext, session_id: str, tools: int) -> bool:
    """Record that the current run of the session listed the tools; ``True`` when it is the run's first listing."""
    with context.journal.transaction() as db:
        session = context.state.session(session_id)
        if session is None or session.state in INACTIVE_STATES:
            raise StateError("not_found")
        row = db.execute("SELECT value FROM guest_meta WHERE key = ?", (_key(session_id),)).fetchone()
        if row is not None and row[0] == session.token_sha256:
            return False
        db.execute(
            "INSERT OR REPLACE INTO guest_meta (key, value) VALUES (?, ?)", (_key(session_id), session.token_sha256)
        )
        body = {"tools": tools, "at": iso_ms(context.journal.clock.now())}
        context.journal.insert(db, KIND, body, session_id)
    _marks(context)[session_id] = session.token_sha256
    return True


def ready(context: ControlContext, session_id: str) -> bool:
    """``True`` when the current run of the session listed the tools."""
    session = context.state.session(session_id)
    if session is None:
        return False
    return _marks(context).get(session_id) == session.token_sha256


def forget(context: ControlContext, session_id: str) -> None:
    """Drop the mark of a session that is removed (retention cleanup)."""
    _marks(context).pop(session_id, None)
    with context.journal.transaction() as db:
        db.execute("DELETE FROM guest_meta WHERE key = ?", (_key(session_id),))
