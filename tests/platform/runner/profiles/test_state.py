"""U-RUN-profiles (KOR-51, ADR-093 section 3): the section `agent_profiles` kept in the store, the effective profile of
a local session (the section's entry, a child's remembered answer, else its parent's), the names of a workgroup, the
reconcile step and the model."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from agent_runner.profiles import model, reconcile, state
from agent_runner.profiles.model import Profile
from agent_runner.reconcile import wire
from agent_runner.sessions import records as session_records

WG = "0193000b-0000-7000-8000-000000000001"
TASK = "0193000b-0000-7000-8000-000000000002"
ORG = "0193000b-0000-7000-8000-000000000003"
OWNER = "0193000b-0000-7000-8000-000000000004"
MAIN = "0193000b-0000-7000-8000-000000000010"
CHILD = "0193000b-0000-7000-8000-000000000011"
GRANDCHILD = "0193000b-0000-7000-8000-000000000012"
READ_ONLY = {"name": "read_only", "operations": ["github.get_issue", "subagent.request"], "reads": "all"}
NARROW = {"name": "narrow", "operations": ["github.get_issue"], "reads": "task"}


def section(sessions: dict[str, Any] | None = None, names: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"api": 1, "sessions": sessions or {}, "workgroups": {WG: names or {}}}


def with_conn(dsn: str, body: Callable[[AsyncConnection[Any]], Awaitable[None]]) -> None:
    async def main() -> None:
        async with await AsyncConnection.connect(dsn, autocommit=True) as conn:
            await body(conn)

    asyncio.run(main())


async def _session(conn: AsyncConnection[Any], sid: str, parent: str | None, depth: int) -> None:
    await session_records.insert(
        conn,
        session_id=sid,
        task_id=TASK,
        org_id=ORG,
        wg_id=WG,
        parent_id=parent,
        depth=depth,
        owner_id=OWNER,
        created_at=datetime(2026, 11, 25, tzinfo=UTC),
    )


def test_the_model() -> None:
    narrow = model.parse(NARROW)
    assert narrow == Profile("narrow", frozenset({"github.get_issue"}), "task")
    assert narrow is not None
    assert narrow.as_json() == NARROW
    assert model.parse(None) is None
    bad: object
    for bad in (
        [],
        {"name": "x"},
        {**NARROW, "name": "Bad"},
        {**NARROW, "reads": "some"},
        {**NARROW, "operations": "github.get_issue"},
        {**NARROW, "operations": ["Bad"]},
        {**NARROW, "operations": [1]},
    ):
        with pytest.raises(ValueError, match="profile"):
            model.parse(bad)
    assert model.allows(None, "github.push")
    assert not model.allows(narrow, "github.push")
    assert model.within(narrow, None)
    assert model.within(narrow, model.parse(READ_ONLY))
    read_only = model.parse(READ_ONLY)
    assert read_only is not None
    assert not model.within(read_only, narrow)
    assert model.within(Profile("x", frozenset(), "all"), narrow) is False
    assert model.tool_type("workvm_exec") == "workvm.request"
    assert model.tool_type("browser_click") == "browser.open"
    assert model.tool_type("get_task") is None
    assert (model.reach(None), model.reach(narrow)) == ("all", "task")


def test_store_keeps_and_clears_the_section(store_dsn: str) -> None:
    async def body(conn: AsyncConnection[Any]) -> None:
        await state.remember_child(conn, CHILD, model.parse(NARROW))
        await state.store(conn, {"agent_profiles": section({MAIN: READ_ONLY, CHILD: None}, {"narrow": NARROW})})
        cursor = await conn.execute("SELECT count(*) FROM runner.session_profile")
        assert await cursor.fetchone() == (0,)  # the section lists the child now
        assert await state.named(conn, WG, "narrow") == model.parse(NARROW)
        assert await state.named(conn, WG, "unknown") is None
        assert await state.named(conn, ORG, "narrow") is None
        await state.store(conn, {"sessions": []})  # an older hub: no profiles
        assert await state.named(conn, WG, "narrow") is None
        assert await state.effective(conn, MAIN, None) is None

    with_conn(store_dsn, body)


@pytest.mark.parametrize(
    "bad",
    [
        [],
        {"api": 0, "sessions": {}, "workgroups": {}},
        {"api": True, "sessions": {}, "workgroups": {}},
        {"api": 1, "sessions": [], "workgroups": {}},
        {"api": 1, "sessions": {"x": None}, "workgroups": {}},
        {"api": 1, "sessions": {MAIN: {"name": "x"}}, "workgroups": {}},
        {"api": 1, "sessions": {}, "workgroups": {WG: {"other": NARROW}}},
        {"api": 1, "sessions": {}, "workgroups": {WG: {"narrow": None}}},
        {"api": 1, "sessions": {}, "workgroups": {WG: []}},
    ],
)
def test_a_malformed_section_fails_the_step(store_dsn: str, bad: object) -> None:
    async def body(conn: AsyncConnection[Any]) -> None:
        with pytest.raises(wire.ResponseInvalid):
            await state.store(conn, {"agent_profiles": bad})

    with_conn(store_dsn, body)


def test_effective_profiles_follow_the_chain(store_dsn: str) -> None:
    async def body(conn: AsyncConnection[Any]) -> None:
        await _session(conn, MAIN, None, 0)
        await _session(conn, CHILD, MAIN, 1)
        await _session(conn, GRANDCHILD, CHILD, 2)
        assert await state.effective(conn, GRANDCHILD, CHILD) is None  # no section: no profiles at this hub
        await state.store(conn, {"agent_profiles": section({MAIN: READ_ONLY})})
        # children the section does not list yet have their parent's rights
        assert await state.effective(conn, GRANDCHILD, CHILD) == model.parse(READ_ONLY)
        await state.remember_child(conn, CHILD, model.parse(NARROW))
        assert await state.effective(conn, GRANDCHILD, CHILD) == model.parse(NARROW)
        await state.remember_child(conn, CHILD, None)
        assert await state.effective(conn, CHILD, MAIN) is None
        assert await state.effective(conn, "0193000b-0000-7000-8000-0000000000ff", None) is None
        # a parent unknown to the store ends the walk; a loop of parents ends too
        assert await state.effective(conn, GRANDCHILD, "0193000b-0000-7000-8000-0000000000fe") is None
        await conn.execute("DELETE FROM runner.session_profile")
        await conn.execute("UPDATE runner.session SET parent_id = %s WHERE id = %s", (GRANDCHILD, MAIN))
        await state.store(conn, {"agent_profiles": section()})
        assert await state.effective(conn, GRANDCHILD, CHILD) is None

    with_conn(store_dsn, body)


def test_the_reconcile_step(store_dsn: str) -> None:
    async def main() -> None:
        async with AsyncConnectionPool(store_dsn, open=False) as pool:
            await pool.open()
            await reconcile.step(SimpleNamespace(desired=None, pool=pool))  # type: ignore[arg-type]
            await reconcile.step(SimpleNamespace(desired={"agent_profiles": section({MAIN: NARROW})}, pool=pool))  # type: ignore[arg-type]
            async with pool.connection() as conn:
                assert await state.effective(conn, MAIN, None) == model.parse(NARROW)
        assert [step.name for step in reconcile.STEPS] == ["agent_profiles"]

    asyncio.run(main())
