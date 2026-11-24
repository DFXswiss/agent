"""Fixtures of the context suites (KOR-33): a migrated store and the test rig of `reconcile/rig.py`."""

from __future__ import annotations

import pytest

from agent_runner.store.migrate import migrate

from ..reconcile.rig import Rig


@pytest.fixture
def store_dsn(runner_dsn: str) -> str:
    assert migrate(runner_dsn).status == "applied"
    return runner_dsn


@pytest.fixture
def rig(store_dsn: str) -> Rig:
    return Rig(store_dsn)
