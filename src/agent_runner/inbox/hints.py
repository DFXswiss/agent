"""WebSocket hint `events {scope_id, scope_seq}` (03-api.md section 6.2): wakes the pull loop. The hint carries no
content; the events are fetched and verified by signed HTTP."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from agent_runner.inbox.pull import inbox_for


async def events(context: Any, _hint: Mapping[str, object]) -> None:
    inbox = inbox_for(context)
    if inbox is not None:
        inbox.request()


HANDLERS: dict[str, Callable[..., Any]] = {"events": events}
