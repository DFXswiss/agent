from __future__ import annotations

import os
import uuid

import pytest

from agent_cli.pg import create_database, drop_database, ensure_cluster, stop_cluster


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
    name = "t" + uuid.uuid4().hex[:16]
    dsn = create_database(pg_admin_dsn, name)
    yield dsn
    drop_database(pg_admin_dsn, name)


@pytest.fixture(autouse=True)
def _agent_pg(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str | None:
    if request.node.get_closest_marker("no_pg") is not None:
        return None
    dsn = request.getfixturevalue("pg_dsn")
    monkeypatch.setenv("AGENT_PG_DSN", dsn)
    return dsn
