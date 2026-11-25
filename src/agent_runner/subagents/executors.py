"""Executors of `subagent.request`, `subagent.instruct` and `subagent.stop` (`<area>/executors.py:EXECUTORS`, ADR-027
section 4; 05-runner.md sections 12.1 and 13; 03-api.md section 9; ADR-088 section 1).

They run only after the hub accepted the operation:

- `subagent.request` (`accepted` with `child_session_id`, check H4 decided the limits): the controller starts the
  child session (`controller.Subagents.start_child`); the result is `{session_id}`.
- `subagent.instruct`: the instruction waits for its turn and is handed to the child (`turns.Turns.instruct`); the
  result is `{session_id, instruction_id}` once the child's guest registered it.
- `subagent.stop`: the child is stopped (`turns.Turns.stop`); the result is `{session_id, state}`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_runner.operations.extensions import OperationContext, OperationFailed
from agent_runner.subagents.controller import Subagents, controller_for

UNAVAILABLE = "Subagents are not available on this device."


def _controller(ctx: OperationContext, message: str = UNAVAILABLE) -> Subagents:
    controller = controller_for(ctx.context)
    if controller is None:
        raise OperationFailed("upstream_error", message=message)
    return controller


async def request_subagent(ctx: OperationContext) -> dict[str, Any]:
    return await _controller(ctx, "Subagents cannot be started on this device.").start_child(ctx)


async def instruct_subagent(ctx: OperationContext) -> dict[str, Any]:
    return await _controller(ctx).turns.instruct(ctx)


async def stop_subagent(ctx: OperationContext) -> dict[str, Any]:
    return await _controller(ctx).turns.stop(ctx)


EXECUTORS: dict[str, Callable[..., Any]] = {
    "subagent.request": request_subagent,
    "subagent.instruct": instruct_subagent,
    "subagent.stop": stop_subagent,
}
