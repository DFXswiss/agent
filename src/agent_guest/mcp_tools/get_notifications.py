"""MCP tool ``get_notifications`` (03-api.md §8.1, 05-runner.md §9, ADR-020).

Answered by the guest itself, not forwarded: the control process returns the pings of the calling session that were
not fetched yet (``{ping_id, kind, level?, ref, at}``, content-free, oldest first) and marks them delivered with the
journal record ``ping.delivered {via:"tool"}`` (api.sock op ``ping.take``, ``control_routes/pings.py``). The session
reads the content itself, for example with ``get_whiteboard``. An unreachable control process is ``unavailable``.
"""

from __future__ import annotations

from typing import Any

from agent_guest.apisock import ApiSockError
from agent_guest.mcp import Tool, ToolCall, ToolError


def get_notifications(call: ToolCall) -> Any:
    try:
        result = call.control.call("ping.take", {"session_id": call.session_id})
    except ApiSockError:
        raise ToolError("unavailable", "The platform is not reachable right now. Try again later.") from None
    return {"notifications": result["notifications"]}


TOOL = Tool(
    name="get_notifications",
    description="Return the new notifications of this session (kind and IDs only, for example a new whiteboard "
    "entry, a finished subagent or a finished operation) and mark them as seen.",
    handler=get_notifications,
)
