"""Shared fixtures of the double suites (ADR-027 section 6; owner DBL-01).

Doubles are tested against their real HTTP servers on loopback with ephemeral ports. `serve` starts a double and
stops it at teardown; `client_for` builds an `agent_doubles.common.client.DoubleClient` for a started double. Area
suites (model, github, mail, login) add their own conftest in their sub-directory.

Tests marked ``live`` under this directory (a real Claude Code run against the model double) are deselected unless the
marker expression names ``live`` (``-m live``), so a plain pytest run of the platform suites never starts them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Protocol, TypeVar

import pytest

from agent_doubles.common.client import DoubleClient


class Startable(Protocol):
    """A double that can be started and stopped and has a base URL."""

    @property
    def url(self) -> str: ...

    def start(self) -> Any: ...

    def stop(self) -> None: ...


D = TypeVar("D", bound=Startable)
HERE = Path(__file__).resolve().parent


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Deselect the ``live`` tests of the double suites unless ``-m`` selects them explicitly."""
    if "live" in (config.option.markexpr or ""):
        return
    live = [item for item in items if item.get_closest_marker("live") and HERE in item.path.parents]
    if live:
        config.hook.pytest_deselected(items=live)
        items[:] = [item for item in items if item not in live]


@pytest.fixture
def serve() -> Iterator[Callable[[D], D]]:
    """Start a double; every double started through this fixture is stopped at teardown."""
    started: list[Startable] = []

    def start(double: D) -> D:
        double.start()
        started.append(double)
        return double

    yield start
    for double in reversed(started):
        double.stop()


@pytest.fixture
def client_for() -> Callable[[Startable], DoubleClient]:
    """A client for the base URL of a started double."""

    def make(double: Startable) -> DoubleClient:
        return DoubleClient(double.url, timeout=10)

    return make
