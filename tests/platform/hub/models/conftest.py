"""Fixtures of the models suites (U-HUB-models, KOR-50): those of the billing suites (the outbox scene with prices,
credit and the start credit of `start_credit`) plus the model-access helper `keys`."""

from __future__ import annotations

import pytest

from tests.platform.hub.billing.conftest import (
    bill,
    desk,
    fleet,
    fresh_billing,
    fresh_binding,
    fresh_budget,
    fresh_chains,
    fresh_state,
    hub_config,
    meter,
    op_signals,
    ops,
    outbox,
    published,
    start_credit,
    world,
)
from tests.platform.hub.billing.support import Bill
from tests.platform.hub.models.support import Keys

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
    "keys",
    "meter",
    "op_signals",
    "ops",
    "outbox",
    "published",
    "start_credit",
    "world",
]


@pytest.fixture
def keys(bill: Bill) -> Keys:
    return Keys(bill)
