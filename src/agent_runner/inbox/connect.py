"""Connect hook `inbox.pull` (ADR-028, 03-api.md section 6.2): after every (re)connect, once the reconcile run and
the outbox push are done, every cursor is pulled."""

from __future__ import annotations

from typing import Any

from agent_runner.inbox.pull import inbox_for
from agent_runner.sync.extensions import ConnectHook


async def pull_now(context: Any) -> None:
    inbox = inbox_for(context)
    if inbox is not None:
        await inbox.pull_all()


HOOKS = [ConnectHook("pull", "inbox.pull", pull_now)]
