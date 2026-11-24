"""Fixtures of the KOR-50 DFX key suites: a migrated store of the issuing platform device and one of the org device
(two databases: the devices never share a store)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from agent_cli.pg import create_database, drop_database
from agent_runner.store.migrate import migrate


@pytest.fixture
def store_dsn(runner_dsn: str) -> str:
    assert migrate(runner_dsn).status == "applied"
    return runner_dsn


@pytest.fixture
def org_dsn(runner_pg_admin: str) -> Iterator[str]:
    name = "t_run_" + uuid.uuid4().hex[:12]
    dsn = create_database(runner_pg_admin, name)
    assert migrate(dsn).status == "applied"
    yield dsn
    drop_database(runner_pg_admin, name)
