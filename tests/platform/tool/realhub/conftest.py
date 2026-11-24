"""Fixtures of the real-hub suite of the agent tool (11-agent-access.md section 10): a fresh migrated database on the
test PostgreSQL of `AGENT_HUB_TEST_POSTGRES` (a missing variable fails the test, it never skips), the hub served on
it (`support.real_hub`) and fresh hub module state (no cached logs of another test)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from agent_hub import signals
from agent_hub.device import signals as device_signals
from agent_hub.device import state
from agent_hub.orgs import logs
from agent_hub.platform import log as platform_log
from agent_hub.scheduler import signals as scheduler_signals
from tests.platform.hub.conftest import HubDatabase, hub_db, migrated_db

from .support import RealHub, real_hub

__all__ = ["fresh_state", "hub_db", "migrated_db", "real"]


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
def real(migrated_db: HubDatabase, tmp_path: Path) -> Iterator[RealHub]:
    """The hub on the migrated test database, served on loopback."""
    with real_hub(migrated_db, tmp_path) as served:
        yield served
