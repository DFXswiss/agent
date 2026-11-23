"""Executor of `subagent.request` (`<area>/executors.py:EXECUTORS`, ADR-027 section 4; 05-runner.md sections 12.1 and
13; 03-api.md section 9).

Runs only after the hub accepted the operation (`accepted` with `child_session_id`, check H4 decided the limits): the
controller starts the child session (`controller.Subagents.start_child`); the result is `{session_id}`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_runner.operations.extensions import OperationContext, OperationFailed
from agent_runner.subagents.controller import controller_for


async def request_subagent(ctx: OperationContext) -> dict[str, Any]:
    controller = controller_for(ctx.context)
    if controller is None:
        raise OperationFailed("upstream_error", message="Subagents cannot be started on this device.")
    return await controller.start_child(ctx)


EXECUTORS: dict[str, Callable[..., Any]] = {"subagent.request": request_subagent}
