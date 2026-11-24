"""Op ``session.tools_listed`` of ``api.sock`` (HOT-09): the API process reports that a session's agent listed the
platform tools on ``/mcp`` (``mcp.py``).

* op ``session.tools_listed {session_id, tools, token_sha256} -> {first}`` - ``toolsready.mark``: ``first`` is
  ``true`` for the first listing of the agent's current run (journaled ``session.tools_ready``); a listing with the
  token of an earlier run marks nothing (``false``); an unknown or inactive session is ``not_found``, any other shape
  ``request_invalid``. The delivery holds the session's first prompt until then.
"""

from __future__ import annotations

import re
from typing import Any

from agent_guest import toolsready
from agent_guest.control import ControlContext
from agent_guest.state import StateError

MAX_TOOLS = 1000
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def tools_listed(context: ControlContext, args: dict[str, Any]) -> dict[str, Any]:
    session_id, tools, token = args.get("session_id"), args.get("tools"), args.get("token_sha256")
    if set(args) != {"session_id", "tools", "token_sha256"} or not isinstance(session_id, str):
        raise StateError("request_invalid")
    if not isinstance(token, str) or not _SHA256_RE.fullmatch(token):
        raise StateError("request_invalid")
    if not isinstance(tools, int) or isinstance(tools, bool) or not 0 <= tools <= MAX_TOOLS:
        raise StateError("request_invalid")
    return {"first": toolsready.mark(context, session_id, tools, token)}


API_OPS = {"session.tools_listed": tools_listed}
