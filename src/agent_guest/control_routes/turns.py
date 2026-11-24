"""The api.sock ops of the fail-closed hook path (ADR-064 sections 1 and 2).

* op ``turn.check {session_id} -> {confirmed: bool}`` - the gateway asks before it forwards a request that can make
  the agent act: the session's turn is confirmed while its state is ``working`` (the ``UserPromptSubmit`` hook accepted
  a registered prompt; ``Stop``, ``StopFailure`` and an interrupt end the turn). An unknown session is not confirmed.
* op ``session.hook_failed {session_id, event} -> {ended: bool}`` - the API process saw a decision hook
  (``DECISION_EVENTS``) fail: the session ends ``failed`` with reason ``hook_unavailable`` (unit stopped, pane
  closed; ``sessions.SessionManager.fail_running``). ``ended`` is false for a session that has ended already.

With the fault switch ``open`` of a TEST_MODE golden (``fault.guard_suppressed``) every turn counts as confirmed and no
session is ended: the attack suite proves the sandbox alone.
"""

from __future__ import annotations

from typing import Any

from agent_guest import sessions
from agent_guest.control import ControlContext
from agent_guest.fault import guard_suppressed
from agent_guest.state import StateError

DECISION_EVENTS = frozenset({"UserPromptSubmit", "PreToolUse", "Stop", "StopFailure"})
REASON = "hook_unavailable"


def _session_id(args: dict[str, Any], names: set[str]) -> str:
    session_id = args.get("session_id")
    if set(args) != names or not isinstance(session_id, str):
        raise StateError("request_invalid")
    return session_id


def check(context: ControlContext, args: dict[str, Any]) -> dict[str, Any]:
    session_id = _session_id(args, {"session_id"})
    if guard_suppressed(context.config):
        return {"confirmed": True}
    session = context.state.session(session_id)
    return {"confirmed": session is not None and session.state == "working"}


def hook_failed(context: ControlContext, args: dict[str, Any]) -> dict[str, Any]:
    session_id = _session_id(args, {"session_id", "event"})
    if args["event"] not in DECISION_EVENTS:
        raise StateError("request_invalid")
    if guard_suppressed(context.config):
        return {"ended": False}
    return {"ended": sessions.manager(context).fail_running(session_id, REASON)}


API_OPS = {"turn.check": check, "session.hook_failed": hook_failed}
