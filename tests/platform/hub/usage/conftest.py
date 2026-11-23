"""Fixtures of the usage suites: those of the operations suites (outbox, desk, operations helper, fresh budget hook)
plus the usage helper, a hub configured with a platform budget and a fresh configuration binding."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from agent_hub.config import HubConfig, load_config
from agent_hub.usage import binding
from tests.platform.hub.conftest import HubDatabase, hub_environ
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
from tests.platform.hub.usage.support import BUDGET, Meter

__all__ = [
    "budget_value",
    "desk",
    "fleet",
    "fresh_binding",
    "fresh_budget",
    "fresh_chains",
    "fresh_state",
    "hub_config",
    "meter",
    "op_signals",
    "ops",
    "outbox",
    "published",
    "world",
]


@pytest.fixture
def budget_value() -> str:
    """`AGENT_HUB_PLATFORM_BUDGET_USD_PER_ORG_DAY` of the test hub (a test parametrizes it to change it)."""
    return BUDGET


@pytest.fixture
def hub_config(migrated_db: HubDatabase, budget_value: str) -> HubConfig:
    """The hub configuration with the platform budget of `budget_value`."""
    return load_config(hub_environ(migrated_db, AGENT_HUB_PLATFORM_BUDGET_USD_PER_ORG_DAY=budget_value))


@pytest.fixture(autouse=True)
def fresh_binding() -> Iterator[None]:
    """No bound configuration before and after every test (the binding is process state)."""
    binding.unbind()
    yield
    binding.unbind()


@pytest.fixture
def meter(ops: Ops) -> Meter:
    return Meter(ops)
