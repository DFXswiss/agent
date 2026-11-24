"""Fixtures of the attention suites (U-HUB-attention, KOR-39): those of the operations suites (the tasks desk with
devices, the outbox and registrations signed by the device) plus the situation helper and bound notifications."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from agent_hub.notifications import signals as notification_signals
from tests.platform.hub.attention.support import Situation
from tests.platform.hub.conftest import HubHarness
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
    "bound",
    "desk",
    "fleet",
    "fresh_budget",
    "fresh_chains",
    "fresh_state",
    "op_signals",
    "ops",
    "outbox",
    "published",
    "situation",
    "world",
]


@pytest.fixture
def situation(ops: Ops) -> Situation:
    """The helper with only the scene's `device` online, so every task is placed on it."""
    ops.desk.offline(ops.scene.other_device)
    return Situation(ops)


@pytest.fixture
def bound(hub: HubHarness) -> Iterator[None]:
    """The notification subscribers work with the running hub's context (unbound again afterwards)."""
    notification_signals.bind(hub.app.state.hub)
    yield
    notification_signals.wait()
    notification_signals.bind(None)
