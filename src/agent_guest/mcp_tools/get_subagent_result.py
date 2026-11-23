"""MCP tool ``get_subagent_result`` (03-api.md §8.1; 05-runner.md §13; ADR-017).

Only child sessions of the calling session can be read. The calling session is the one of the bearer token; the
guest refuses its own ID at once (``not_found``, a session is never its own child; journaled as ``platform.refused``
and audited by the runner like every refused read, KOR-10) and forwards every other ID as
``platform.query``: the runner (RUN-11) holds the session tree and answers ``not_found`` for anything that is not a
child of the caller, else the child's state, waiting up to ``wait_seconds`` (≤ 30, default 0) for it to finish
(``_forward.waiting_read``). An answer about another session than the one asked for is ``unavailable``.
"""

from __future__ import annotations

from typing import Any

from agent_guest.mcp import Tool, ToolCall, ToolRefused
from agent_guest.mcp_tools import _forward


def get_subagent_result(call: ToolCall) -> Any:
    child = call.arguments["session_id"]
    if child == call.session_id:
        raise ToolRefused("not_found", "Only child sessions of this session can be read.")
    return _forward.waiting_read(call, session_id=child)


TOOL = Tool(
    name="get_subagent_result",
    description="Return the state of a subagent session started by this session and, once it stopped, its final "
    "text; wait_seconds (at most 30) waits for it to finish.",
    handler=get_subagent_result,
)
