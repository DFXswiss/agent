"""Daemon task `inbox` (05-runner.md section 2.3): pulls every 30 s and on request (`events` hint).

The task ends at once on a device that is not enrolled.
"""

from __future__ import annotations

from typing import Any

from agent_runner.inbox.pull import inbox_for
from agent_runner.registry import TaskSpec


async def run_inbox(context: Any) -> None:
    inbox = inbox_for(context)
    if inbox is None:
        context.log.write("runner.inbox not_enrolled\n")
        context.log.flush()
        return
    await inbox.loop(context.stopping)


TASKS = [TaskSpec("inbox", run_inbox)]
