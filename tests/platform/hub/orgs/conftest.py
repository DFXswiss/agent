"""Fixtures of the orgs suites (U-HUB-log at the hub, U-HUB-rights)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from agent_hub.stream import publish
from tests.platform.hub.conftest import HubHarness
from tests.platform.hub.orgs.support import World


@pytest.fixture
def world(hub: HubHarness) -> World:
    """Actors and orgs on the running hub."""
    return World(hub)


@pytest.fixture
def published() -> Iterator[publish.MemoryPublisher]:
    """A fresh in-process publisher that records every live frame."""
    recorder = publish.MemoryPublisher()
    previous = publish.set_publisher(recorder)
    yield recorder
    publish.set_publisher(previous)
