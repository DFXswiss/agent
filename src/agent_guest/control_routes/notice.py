"""The session notice on ``api.sock`` (KOR-33, ADR-072 section 7; ``sessions.py``, ``hook_addons/notice.py``).

* op ``session.notice {session_id} -> {notice: string | null}``: the pending notice of a resumed or restored
  conversation - what changed and the session facts it does not know yet -, handed out once
  (``SessionManager.take_notice``); ``null`` when nothing is pending.
"""

from __future__ import annotations

from typing import Any

from agent_guest import sessions
from agent_guest.control import ControlContext
from agent_guest.state import StateError


def notice(context: ControlContext, args: dict[str, Any]) -> dict[str, Any]:
    session_id = args.get("session_id")
    if set(args) != {"session_id"} or not isinstance(session_id, str):
        raise StateError("request_invalid")
    return {"notice": sessions.take_notice(context, session_id)}


API_OPS = {"session.notice": notice}
