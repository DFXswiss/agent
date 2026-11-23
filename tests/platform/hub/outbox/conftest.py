"""Fixtures of the outbox suites: those of the tasks suites (`tests/platform/hub/tasks/conftest.py`) plus the outbox
helper and fresh handler chains."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from agent_hub.outbox import chains
from tests.platform.hub.device.support import Fleet
from tests.platform.hub.outbox.support import Outbox
from tests.platform.hub.tasks.conftest import desk, fleet, fresh_state, published, task_signals, world
from tests.platform.hub.tasks.support import Desk

__all__ = ["desk", "fleet", "fresh_chains", "fresh_state", "outbox", "published", "task_signals", "world"]


@pytest.fixture(autouse=True)
def fresh_chains() -> Iterator[None]:
    """The discovered handler chains before and after every test (a test may configure others)."""
    chains.configure()
    yield
    chains.configure()


@pytest.fixture
def outbox(desk: Desk, fleet: Fleet) -> Outbox:
    return Outbox(desk, fleet)
