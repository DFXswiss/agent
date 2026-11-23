"""Daemon task `prompts` (05-runner.md sections 1.1, 6.2, 6.3): the prompt delivery and the idle -> suspend rule
(`Deliverer.loop`). The task ends at once on a device that is not enrolled."""

from __future__ import annotations

from typing import Any

from agent_runner.prompts.deliver import deliverer_for
from agent_runner.registry import TaskSpec


async def run_prompts(context: Any) -> None:
    deliverer = deliverer_for(context)
    if deliverer is None:
        context.log.write("runner.prompts not_enrolled\n")
        context.log.flush()
        return
    await deliverer.loop(context.stopping, context.host.sleep)


TASKS = [TaskSpec("prompts", run_prompts)]
