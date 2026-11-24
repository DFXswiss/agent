"""Fixtures of the billing suites (U-HUB-billing): those of the usage suites, a hub with the start credit of
`start_credit` (a test parametrizes it), and fresh billing bindings and job states."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from agent_hub.billing import binding, jobs
from agent_hub.config import HubConfig, load_config
from tests.platform.hub.billing.support import Bill
from tests.platform.hub.conftest import HubDatabase, hub_environ
from tests.platform.hub.usage.conftest import (
    desk,
    fleet,
    fresh_binding,
    fresh_budget,
    fresh_chains,
    fresh_state,
    meter,
    op_signals,
    ops,
    outbox,
    published,
    world,
)

__all__ = [
    "bill",
    "desk",
    "fleet",
    "fresh_billing",
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
    "start_credit",
    "world",
]


@pytest.fixture
def start_credit() -> str:
    """`AGENT_HUB_BILLING_START_CREDIT_USD` of the test hub."""
    return "5"


@pytest.fixture
def hub_config(migrated_db: HubDatabase, start_credit: str) -> HubConfig:
    return load_config(
        hub_environ(
            migrated_db,
            AGENT_HUB_BILLING_START_CREDIT_USD=start_credit,
            AGENT_HUB_PLATFORM_BUDGET_USD_PER_ORG_DAY="",
        )
    )


@pytest.fixture(autouse=True)
def fresh_billing() -> Iterator[None]:
    binding.unbind()
    jobs.forget()
    yield
    binding.unbind()
    jobs.forget()


@pytest.fixture
def bill(meter: object) -> Bill:
    return Bill(meter)  # type: ignore[arg-type]
