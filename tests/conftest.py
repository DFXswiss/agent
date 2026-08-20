from __future__ import annotations

import os
import uuid

import pytest

from agent_cli.pg import create_database, ensure_cluster, stop_cluster


@pytest.fixture(scope="session")
def pg_admin_dsn(tmp_path_factory: pytest.TempPathFactory) -> str:
    env = os.environ.get("AGENT_TEST_PG")
    if env:
        yield env
        return
    data = tmp_path_factory.mktemp("pg-cluster")
    dsn = ensure_cluster(data)
    yield dsn
    stop_cluster(data)


@pytest.fixture
def pg_dsn(pg_admin_dsn: str) -> str:
    return create_database(pg_admin_dsn, "t" + uuid.uuid4().hex[:16])


@pytest.fixture(autouse=True)
def _agent_pg(monkeypatch: pytest.MonkeyPatch, pg_dsn: str) -> str:
    monkeypatch.setenv("AGENT_PG_DSN", pg_dsn)
    return pg_dsn
