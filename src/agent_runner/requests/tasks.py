"""Daemon task `requests` (05-runner.md sections 1.1 and 11.1): processes the offered device requests until the daemon
stops, then ends the running flows (`device_shutdown`). The task ends at once on a device that is not enrolled."""

from __future__ import annotations

from typing import Any

from agent_runner.registry import TaskSpec
from agent_runner.requests.service import service_for


async def run_requests(context: Any) -> None:
    service = service_for(context)
    if service is None:
        context.log.write("runner.requests not_enrolled\n")
        context.log.flush()
        return
    await service.loop(context.stopping)


TASKS = [TaskSpec("requests", run_requests)]
