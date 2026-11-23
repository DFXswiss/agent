"""Read tools of this area (`<area>/query_tools.py:TOOLS`, ADR-038): `get_subagent_result` (03-api.md section 8.1,
05-runner.md section 13) through `controller.Subagents.result`."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_runner.subagents.controller import controller_for
from agent_runner.subagents.extensions import QueryCall, QueryOutcome, failure


async def get_subagent_result(call: QueryCall) -> QueryOutcome:
    controller = controller_for(call.context)
    if controller is None:
        return failure("unavailable", "Subagents are not available on this device.")
    return await controller.result(call)


TOOLS: dict[str, Callable[..., Any]] = {"get_subagent_result": get_subagent_result}
