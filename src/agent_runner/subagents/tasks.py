"""Daemon task `subagents` (05-runner.md sections 1.1 and 13): the controller's loop - end of a child after its first
turn, runtime limit, settling of the final text, `subagent.finished` ping (`controller.Subagents.loop`). The task ends
at once on a device that is not enrolled."""

from __future__ import annotations

from typing import Any

from agent_runner.registry import TaskSpec
from agent_runner.subagents.controller import controller_for


async def run_subagents(context: Any) -> None:
    controller = controller_for(context)
    if controller is None:
        context.log.write("runner.subagents not_enrolled\n")
        context.log.flush()
        return
    await controller.loop(context.stopping)


TASKS = [TaskSpec("subagents", run_subagents)]
