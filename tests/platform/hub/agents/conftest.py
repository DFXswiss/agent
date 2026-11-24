"""Fixtures of the agent suites (KOR-24): the fake browser of the authentication tests, recorded live frames and fresh
module state (no cached logs, no bound signal context of another test)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from agent_hub import signals
from agent_hub.device import signals as device_signals
from agent_hub.device import state
from agent_hub.orgs import logs
from agent_hub.platform import log as platform_log
from agent_hub.scheduler import signals as scheduler_signals
from agent_hub.stream import publish
from tests.platform.hub.auth.authenticator import FakeBrowser
from tests.platform.hub.conftest import HubHarness


@pytest.fixture(autouse=True)
def fresh_state() -> Iterator[None]:
    for cache in (logs.ORG_LOGS, logs.PLATFORM_LOGS, platform_log.CACHE):
        cache.clear()
    scheduler_signals._context.clear()
    device_signals._context.clear()
    state.configure()
    signals.configure()
    yield
    scheduler_signals.wait()
    device_signals.wait()
    scheduler_signals._context.clear()
    device_signals._context.clear()
    signals.configure()


@pytest.fixture
def published() -> Iterator[publish.MemoryPublisher]:
    """A fresh in-process publisher that records every live frame."""
    recorder = publish.MemoryPublisher()
    previous = publish.set_publisher(recorder)
    yield recorder
    publish.set_publisher(previous)


@pytest.fixture
def browser(hub: HubHarness) -> FakeBrowser:
    """A fake browser (with a fake authenticator for the hub's origin and RP ID) on the running hub."""
    return FakeBrowser(hub)
