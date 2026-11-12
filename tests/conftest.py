from __future__ import annotations

import os
import uuid

import pytest

from agent_cli.pg import create_database, drop_database, ensure_cluster, stop_cluster


_STRING_CONTROL_INTRODUCERS = {
    "osc": ("\x1b]", "\x9d"),
    "dcs": ("\x1bP", "\x90"),
    "pm": ("\x1b^", "\x9e"),
    "apc": ("\x1b_", "\x9f"),
    "sos": ("\x1bX", "\x98"),
}


@pytest.fixture(
    params=[
        (kind, eight_bit)
        for kind in _STRING_CONTROL_INTRODUCERS
        for eight_bit in (False, True)
    ],
    ids=[
        f"{kind}-{'8bit' if eight_bit else '7bit'}"
        for kind in _STRING_CONTROL_INTRODUCERS
        for eight_bit in (False, True)
    ],
)
def nested_terminal_string_control(request: pytest.FixtureRequest) -> str:
    """A payload-bearing string control with the opposite-width nested DCS."""
    kind, eight_bit = request.param
    introducer = _STRING_CONTROL_INTRODUCERS[kind][int(eight_bit)]
    nested = (
        "\x1bPnested payload\x1b\\"
        if eight_bit
        else "\x90nested payload\x9c"
    )
    terminator = (
        "\x07"
        if kind == "osc"
        else ("\x9c" if eight_bit else "\x1b\\")
    )
    return f"{introducer}outer {nested} payload{terminator}"


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
