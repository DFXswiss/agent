"""Fixtures of the contexts suites: those of the outbox suites (devices with real keys, the tasks desk, recorded frames
and fresh handler chains) plus the `Checkpoints` helper."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

import pytest

from tests.platform.hub.contexts.support import Checkpoints, Limits
from tests.platform.hub.outbox.conftest import (
    desk,
    fleet,
    fresh_chains,
    fresh_state,
    outbox,
    published,
    task_signals,
    world,
)
from tests.platform.hub.outbox.support import Outbox

__all__ = [
    "checkpoints",
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


@pytest.fixture
def checkpoints(outbox: Outbox) -> Checkpoints:
    return Checkpoints(outbox)


@pytest.fixture
def limits(outbox: Outbox) -> Iterator[Limits]:
    state = outbox.hub.app.state
    original = state.hub

    def change(**settings: int) -> None:
        state.hub = replace(original, config=replace(original.config, **settings))

    yield change
    state.hub = original
