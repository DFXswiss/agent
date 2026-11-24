"""Op ``session.tools_listed`` of ``api.sock`` (HOT-09): the API process reports that a session's agent listed the
platform tools on ``/mcp`` (``mcp.py``).

* op ``session.tools_listed {session_id, tools} -> {first}`` - ``toolsready.mark``: ``first`` is ``true`` for the
  first listing of the agent's current run (journaled ``session.tools_ready``); an unknown or inactive session is
  ``not_found``, any other shape ``request_invalid``. The delivery holds the session's first prompt until then.
"""

from __future__ import annotations

from typing import Any

from agent_guest import toolsready
from agent_guest.control import ControlContext
from agent_guest.state import StateError

MAX_TOOLS = 1000


def tools_listed(context: ControlContext, args: dict[str, Any]) -> dict[str, Any]:
    session_id, tools = args.get("session_id"), args.get("tools")
    if set(args) != {"session_id", "tools"} or not isinstance(session_id, str):
        raise StateError("request_invalid")
    if not isinstance(tools, int) or isinstance(tools, bool) or not 0 <= tools <= MAX_TOOLS:
        raise StateError("request_invalid")
    return {"first": toolsready.mark(context, session_id, tools)}


API_OPS = {"session.tools_listed": tools_listed}
