"""Fixtures of the requests suite (U-HUB-requests): the desk, recorded frames and fresh caches."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from agent_hub.device import signals as device_signals
from agent_hub.device import state
from agent_hub.orgs import logs
from agent_hub.platform import log as platform_log
from agent_hub.stream import publish
from tests.platform.hub.conftest import HubHarness
from tests.platform.hub.credentials.support import Desk
from tests.platform.hub.orgs.support import World


@pytest.fixture(autouse=True)
def fresh_state() -> Iterator[None]:
    """No cached logs, discovered sections and no bound hub context from another test."""
    for cache in (logs.ORG_LOGS, logs.PLATFORM_LOGS, platform_log.CACHE):
        cache.clear()
    device_signals._context.clear()
    state.configure()
    yield
    device_signals.wait()
    device_signals._context.clear()
    state.configure()


@pytest.fixture
def published() -> Iterator[publish.MemoryPublisher]:
    """A fresh in-process publisher that records every live frame."""
    recorder = publish.MemoryPublisher()
    previous = publish.set_publisher(recorder)
    yield recorder
    publish.set_publisher(previous)


@pytest.fixture
def desk(hub: HubHarness, published: publish.MemoryPublisher) -> Desk:
    """The desk; the frames of building its scene are dropped."""
    built = Desk.build(World(hub))
    published.clear()
    return built
