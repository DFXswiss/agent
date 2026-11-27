"""Fixtures of the attachments suite: the scene of the outbox suites (an org with a writer, a reader, a stranger and two
assigned devices) and the `limits` switch of the contexts suites."""

from __future__ import annotations

from tests.platform.hub.contexts.conftest import (
    desk,
    fleet,
    fresh_chains,
    fresh_state,
    limits,
    outbox,
    published,
    task_signals,
    world,
)

__all__ = [
    "desk",
    "fleet",
    "fresh_chains",
    "fresh_state",
    "limits",
    "outbox",
    "published",
    "task_signals",
    "world",
]
