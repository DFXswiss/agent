"""Fixtures of the backup suites (U-HUB-backup, DR-01): the filled hub and an empty migrated target database."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from agent_hub.db.migrate import run_migrate
from agent_hub.orgs import logs
from agent_hub.platform import log as platform_log
from tests.platform.hub.backup.support import Estate, fresh_database
from tests.platform.hub.conftest import HubDatabase, HubHarness


@pytest.fixture(autouse=True)
def fresh_caches() -> Iterator[None]:
    """Every test starts without cached logs (they are keyed by head, not by database)."""
    for cache in (platform_log.CACHE, logs.ORG_LOGS, logs.PLATFORM_LOGS):
        cache.clear()
    yield
    for cache in (platform_log.CACHE, logs.ORG_LOGS, logs.PLATFORM_LOGS):
        cache.clear()


@pytest.fixture
def estate(hub: HubHarness) -> Estate:
    """The hub with a row in every exported table."""
    return Estate.build(hub)


@pytest.fixture
def target() -> Iterator[HubDatabase]:
    """An empty, migrated database (the target of a restore)."""
    with fresh_database() as database:
        line, code = run_migrate(database.migrate_env())
        assert code == 0, line
        yield database
