"""Fixtures of the retention area tests (ADR-027 section 6)."""

from __future__ import annotations

import pytest

from agent_runner.store.migrate import migrate


@pytest.fixture
def store_dsn(runner_dsn: str) -> str:
    """A migrated runner store."""
    assert migrate(runner_dsn).status == "applied"
    return runner_dsn
