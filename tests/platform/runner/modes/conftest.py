"""Fixtures of the modes suites (KOR-75): a migrated store."""

from __future__ import annotations

import pytest

from agent_runner.store.migrate import migrate


@pytest.fixture
def store_dsn(runner_dsn: str) -> str:
    assert migrate(runner_dsn).status == "applied"
    return runner_dsn
