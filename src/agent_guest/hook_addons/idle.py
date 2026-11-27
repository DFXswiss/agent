"""Hook addon: the end of a turn (``Stop``, ``StopFailure``) makes the session ``idle`` (05-runner.md §5.5, §6.3, §13).

Calls the api.sock op ``session.turn_ended`` (``control_routes/control.py``) with the Claude session ID of the hook
body (only a UUID-shaped ID; ``null`` otherwise or for an unreadable body), ``failed`` for ``StopFailure`` and, for
``StopFailure``, the error class Claude Code names in the body's ``error`` (``stop_error``: one of
``policy.STOP_ERRORS``, anything else ``unknown``; FIX-02), so the runner can continue a turn a model rate limit ended.
The control process moves a ``working`` session to ``idle`` and, for a subagent session after a successful ``Stop``,
runs the ``SUBAGENT_STOP`` callbacks (RUN-11). The answer to Claude Code stays as decided: the addon returns no text.
A failure of the call is counted by ``hooks.py`` as ``guest.error hook_handler_failed``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from agent_guest.hooks import HookAddon, HookCall
from agent_guest.policy import STOP_ERRORS
from agent_guest.server import ApiContext

_CLAUDE_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
UNKNOWN = "unknown"


def stop_error(body: Mapping[str, Any] | None) -> str:
    """The error class of a ``StopFailure`` body: one of ``STOP_ERRORS``, else ``unknown``."""
    error = None if body is None else body.get("error")
    return error if isinstance(error, str) and error in STOP_ERRORS else UNKNOWN


def handle(context: ApiContext, call: HookCall) -> str | None:
    claude_session_id = call.body.get("session_id") if call.body is not None else None
    if not isinstance(claude_session_id, str) or not _CLAUDE_ID_RE.fullmatch(claude_session_id):
        claude_session_id = None
    failed = call.event == "StopFailure"
    args: dict[str, object] = {"session_id": call.session_id, "claude_session_id": claude_session_id, "failed": failed}
    if failed:
        args["stop_error"] = stop_error(call.body)
    context.control.call("session.turn_ended", args)
    return None


ADDON = HookAddon(events=frozenset({"Stop", "StopFailure"}), handle=handle)
