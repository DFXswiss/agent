"""Control actions ``interrupt`` and ``suspend``, and the end of a turn (05-runner.md §5.5, §6.3, §13; 03-api.md §7.1).

``POST /v1/sessions/{id}/control`` is routed by ``control_routes/sessions.py`` (GST-02, which handles ``stop``); the
other actions are this module's ``CONTROL_ACTIONS``:

* ``interrupt`` - ``Escape`` into the pane (``delivery.Delivery.interrupt``, the only key besides prompt delivery,
  ADR-016). Only for a session whose agent runs (``agent_starting``, ``prompt_delivering``, ``working``, ``idle``),
  otherwise ``409 state_conflict``. Claude Code sends no Stop hook for an interrupted turn, so a ``working`` session
  becomes ``idle`` here. A second interrupt within ``INTERRUPT_GAP_SECONDS`` is answered without a key: two quick
  ``Escape`` presses open Claude Code's rewind dialog instead of interrupting.
* ``suspend`` - only from ``idle`` (``suspended`` again answers ``{}``; every other state ``409 state_conflict``):
  stop the unit and close the window, keep directories and transcript (``sessions.stop_session(..., "suspended")``).
  Resume is a new ``POST /v1/sessions`` with ``resume_claude_session_id`` (GST-02, ``--resume``).

api.sock op ``session.turn_ended {session_id, claude_session_id: string | null, failed: bool} -> {state}`` (called by
the ``hook_addons/idle.py`` addon for ``Stop`` and, with ``failed`` true, ``StopFailure``): a ``working`` session
becomes ``idle`` (``session.state idle`` with the Claude session ID of the hook, kept in the session cache for a later
resume); a failed turn also returns the TUI to its input, its error is journaled by the hook itself
(``session.hook``). For a subagent session (``depth`` > 0) the transition after a successful ``Stop`` also calls every
``SUBAGENT_STOP`` callback of the ``control_routes`` modules (``callback(context, session_id)``, module order, checked
when called; a failing callback is logged and changes nothing): the completion hook of RUN-11 (05 §13: the first Stop
ends a subagent). The op answers the session state after the call.
"""

from __future__ import annotations

import re
import subprocess
import sys
import threading
import traceback
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any

from agent_guest import delivery, registry, sessions, tmux
from agent_guest.clock import iso_ms
from agent_guest.control import ControlContext
from agent_guest.state import StateError

INTERRUPT_GAP_SECONDS = 2.0
LIVE_STATES = frozenset({"agent_starting", "prompt_delivering", "working", "idle"})
_CLAUDE_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_INTERRUPTS = "_interrupts"
_interrupt_lock = threading.Lock()

SubagentStop = Callable[[ControlContext, str], None]


def _live(context: ControlContext, session_id: str) -> str:
    session = context.state.session(session_id)
    if session is None:
        raise StateError("not_found")
    return session.state


def _first_interrupt(context: ControlContext, session_id: str) -> bool:
    now = context.journal.clock.now()
    with _interrupt_lock:
        last: dict[str, float] = vars(context).setdefault(_INTERRUPTS, {})
        previous = last.get(session_id)
        if previous is not None and 0 <= now - previous < INTERRUPT_GAP_SECONDS:
            return False
        last[session_id] = now
        return True


def interrupt(context: ControlContext, session_id: str) -> None:
    if _live(context, session_id) not in LIVE_STATES:
        raise StateError("state_conflict")
    if not _first_interrupt(context, session_id):
        return
    try:
        delivery.manager(context).interrupt(session_id)
    except (tmux.TmuxError, OSError, subprocess.SubprocessError):
        raise StateError("state_conflict") from None
    mark_idle(context, session_id, None)


def suspend(context: ControlContext, session_id: str) -> None:
    state = _live(context, session_id)
    if state == "suspended":
        return
    if state != "idle":
        raise StateError("state_conflict")
    # Under the pane lock: a delivery never types into a session that is being suspended.
    with delivery.manager(context).pane_lock(session_id):
        if _live(context, session_id) != "idle":
            raise StateError("state_conflict")
        sessions.stop_session(context, session_id, "suspended")


CONTROL_ACTIONS = {"interrupt": interrupt, "suspend": suspend}


def mark_idle(context: ControlContext, session_id: str, claude_session_id: str | None) -> bool:
    """``working`` -> ``idle`` (journal and cache in one transaction); ``False`` when the session was not working."""
    at = iso_ms(context.journal.clock.now())
    with context.journal.transaction() as db:
        session = context.state.session(session_id)
        if session is None:
            raise StateError("not_found")
        if session.state != "working":
            return False
        if claude_session_id is not None:
            session = replace(session, claude_session_id=claude_session_id)
        context.state.save_session(replace(session, state="idle"), db)
        body: dict[str, str] = {"state": "idle", "at": at}
        if session.claude_session_id is not None:
            body["claude_session_id"] = session.claude_session_id
        context.journal.insert(db, "session.state", body, session_id)
    return True


def subagent_stop_handlers() -> list[SubagentStop]:
    """``SUBAGENT_STOP`` of every ``control_routes`` module (a sequence of callables), in module order."""
    found: list[SubagentStop] = []
    for module in registry.modules(registry.CONTROL_ROUTES):
        value = getattr(module, "SUBAGENT_STOP", ())
        if not isinstance(value, list | tuple) or not all(callable(item) for item in value):
            raise registry.RegistryError("registry_invalid", f"{module.__name__}.SUBAGENT_STOP")
        found.extend(value)
    return found


def _subagent(context: ControlContext, session_id: str) -> bool:
    identity = sessions.manager(context)._meta(session_id).get("identity")
    depth = identity.get("depth") if isinstance(identity, dict) else None
    return type(depth) is int and depth > 0


def turn_ended(context: ControlContext, args: dict[str, Any]) -> dict[str, Any]:
    session_id, claude_session_id = args.get("session_id"), args.get("claude_session_id")
    failed = args.get("failed")
    if set(args) != {"session_id", "claude_session_id", "failed"} or not isinstance(session_id, str):
        raise StateError("request_invalid")
    if not isinstance(failed, bool):
        raise StateError("request_invalid")
    if claude_session_id is not None and not (
        isinstance(claude_session_id, str) and _CLAUDE_ID_RE.fullmatch(claude_session_id)
    ):
        raise StateError("request_invalid")
    if mark_idle(context, session_id, claude_session_id) and not failed and _subagent(context, session_id):
        _subagent_stopped(context, session_id, subagent_stop_handlers())
    return {"state": _live(context, session_id)}


def _subagent_stopped(context: ControlContext, session_id: str, handlers: Sequence[SubagentStop]) -> None:
    for handler in handlers:
        try:
            handler(context, session_id)
        except Exception:
            sys.stderr.write("agent-guest-control: subagent stop handler failed\n")
            traceback.print_exc(file=sys.stderr)


API_OPS = {"session.turn_ended": turn_ended}
