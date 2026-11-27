"""Fixtures of the DBG-01 runner suites (`dfx.debug`): a migrated store."""

from __future__ import annotations

import pytest

from agent_runner.store.migrate import migrate


@pytest.fixture
def store_dsn(runner_dsn: str) -> str:
    assert migrate(runner_dsn).status == "applied"
    return runner_dsn
