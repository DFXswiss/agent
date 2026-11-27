"""Fixtures of the modes suites: those of the operations suites, and a hub that offers the test mode."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

import pytest

from tests.platform.hub.operations.conftest import (
    desk,
    fleet,
    fresh_budget,
    fresh_chains,
    fresh_state,
    op_signals,
    ops,
    outbox,
    published,
    world,
)
from tests.platform.hub.operations.support import Ops

__all__ = [
    "desk",
    "fleet",
    "fresh_budget",
    "fresh_chains",
    "fresh_state",
    "modes_test",
    "op_signals",
    "ops",
    "outbox",
    "published",
    "world",
]


@pytest.fixture(autouse=True)
def modes_test(ops: Ops) -> Iterator[None]:
    """The hub of the test is configured with `AGENT_HUB_MODES_TEST=1` (the catalogue's test mode is offered)."""
    state = ops.hub.app.state
    original = state.hub
    state.hub = replace(original, config=replace(original.config, modes_test=True))
    yield
    state.hub = original
