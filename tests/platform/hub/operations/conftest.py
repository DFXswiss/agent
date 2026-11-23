"""Fixtures of the operations suites: those of the outbox suites plus the operations helper, a recorder of the
operation signals and the discovered budget check before and after every test."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

import pytest

from agent_hub import signals
from agent_hub.operations import limits
from tests.platform.hub.device.support import Fleet
from tests.platform.hub.operations.support import Ops
from tests.platform.hub.outbox.conftest import desk, fleet, fresh_chains, fresh_state, outbox, published, world
from tests.platform.hub.outbox.support import Outbox

__all__ = ["desk", "fleet", "fresh_chains", "fresh_state", "op_signals", "ops", "outbox", "published", "world"]
SIGNALS = ("operation.finished", "subagent.finished")


@pytest.fixture(autouse=True)
def fresh_budget() -> Iterator[None]:
    limits.configure()
    yield
    limits.configure()


@pytest.fixture
def ops(outbox: Outbox, fleet: Fleet) -> Ops:
    return Ops(outbox, fleet)


@pytest.fixture
def op_signals() -> Iterator[list[tuple[str, dict[str, Any]]]]:
    """Every `operation.finished` and `subagent.finished` signal (the discovered subscribers stay)."""
    seen: list[tuple[str, dict[str, Any]]] = []

    def record(name: str, ids: Mapping[str, Any]) -> None:
        seen.append((name, {key: str(value) for key, value in ids.items()}))

    found = {name: list(items) for name, items in signals.configure().items()}
    for name in SIGNALS:
        found.setdefault(name, []).append(record)
    signals.configure(found)
    yield seen
    signals.configure()
