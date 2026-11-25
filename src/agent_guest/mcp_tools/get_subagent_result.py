"""MCP tool ``get_subagent_result`` (03-api.md §8.1; 05-runner.md §13; ADR-088 §1).

Only child sessions of the calling session can be read. The calling session is the one of the bearer token; the
guest refuses its own ID at once (``not_found``, a session is never its own child; journaled as ``platform.refused``
and audited by the runner like every refused read, KOR-10) and forwards every other ID as
``platform.query``: the runner (RUN-11) holds the session tree and answers ``not_found`` for anything that is not a
child of the caller, else the answer about the child's newest instruction (working, or idle/suspended with the final
text of that turn; stopped or failed at the child's end), waiting up to ``wait_seconds`` (≤ 30, default 0) for that
turn or the child to end (``_forward.waiting_read``). An answer about another session than the one asked for is
``unavailable``.
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
    description="Return the answer of a subagent session started by this session to its newest instruction: working "
    "while that turn runs, then its final text; wait_seconds (at most 30) waits for the turn or the subagent to end.",
    handler=get_subagent_result,
)
