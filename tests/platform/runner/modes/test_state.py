"""U-RUN-modes (KOR-75, ADR-098 sections 3 and 4): the section `task_modes` kept in the store, the reconcile steps and
the brief of a session start."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from agent_runner.modes import drivers, reconcile, session_request, state
from agent_runner.reconcile import wire
from agent_runner.sessions import records
from tests.platform.runner.modes.support import CHOOSER, MAIN, OTHER_TASK, TASK, entry, section, session, with_conn


def test_store_keeps_replaces_and_clears_the_section(store_dsn: str) -> None:
    async def body(conn: AsyncConnection[Any]) -> None:
        driver = {"step": "woken", "counters": {"runs": 1}, "next_at": None, "at": "2026-11-26T10:00:01.000Z"}
        await state.store(conn, section({TASK: [entry(), entry("other", driver)], OTHER_TASK: []}))
        assert await state.active(conn, TASK) == ["other", "test"]
        assert await state.active(conn, OTHER_TASK) == []
        listed = await state.listed(conn)
        assert [(item.task_id, item.mode, item.chosen_by) for item in listed] == [
            (TASK, "other", CHOOSER),
            (TASK, "test", CHOOSER),
        ]
        assert listed[0].driver == driver
        assert listed[1].driver is None
        await state.store(conn, section({TASK: [entry()]}))
        assert await state.active(conn, TASK) == ["test"]
        await state.store(conn, {"sessions": []})  # an older hub: no modes
        assert await state.listed(conn) == []

    with_conn(store_dsn, body)


@pytest.mark.parametrize(
    "bad",
    [
        [],
        {"api": 0, "tasks": {}},
        {"api": True, "tasks": {}},
        {"api": 1, "tasks": []},
        {"api": 1, "tasks": {"x": []}},
        {"api": 1, "tasks": {TASK: {}}},
        {"api": 1, "tasks": {TASK: [[]]}},
        {"api": 1, "tasks": {TASK: [entry(mode="Bad")]}},
        {"api": 1, "tasks": {TASK: [entry(mode=1)]}},
        {"api": 1, "tasks": {TASK: [entry(way="default")]}},
        {"api": 1, "tasks": {TASK: [entry(version=0)]}},
        {"api": 1, "tasks": {TASK: [entry(chosen_by="x")]}},
        {"api": 1, "tasks": {TASK: [entry(changed_at="yesterday")]}},
        {"api": 1, "tasks": {TASK: [entry(changed_at=1)]}},
        {"api": 1, "tasks": {TASK: [entry(driver=[])]}},
    ],
)
def test_a_malformed_section_fails_the_step(store_dsn: str, bad: object) -> None:
    async def body(conn: AsyncConnection[Any]) -> None:
        with pytest.raises(wire.ResponseInvalid):
            await state.store(conn, {"task_modes": bad})

    with_conn(store_dsn, body)


def test_the_reconcile_steps(store_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[Any] = []

    async def run_once(context: Any) -> int:
        ran.append(context)
        return 0

    monkeypatch.setattr(drivers, "run_once", run_once)

    async def main() -> None:
        async with AsyncConnectionPool(store_dsn, open=False) as pool:
            await pool.open()
            await reconcile.modes_step(SimpleNamespace(desired=None, pool=pool))  # type: ignore[arg-type]
            await reconcile.modes_step(SimpleNamespace(desired=section(), pool=pool))  # type: ignore[arg-type]
            async with pool.connection() as conn:
                assert await state.active(conn, TASK) == ["test"]
            await reconcile.drivers_step(SimpleNamespace(context="ctx"))  # type: ignore[arg-type]
        assert ran == ["ctx"]
        assert [step.name for step in reconcile.STEPS] == ["task_modes", "mode_drivers"]

    asyncio.run(main())


def test_the_brief_names_the_modes_of_the_task(store_dsn: str) -> None:
    async def main() -> None:
        async with AsyncConnectionPool(store_dsn, open=False) as pool:
            await pool.open()
            context = SimpleNamespace(pool=pool)
            async with pool.connection() as conn:
                await session(conn)
                found = await _local(conn)
            request: dict[str, Any] = {"harness": {"context_markdown": "- Task: x\n"}}
            await session_request.amend(context, found, request)
            assert request["harness"]["context_markdown"] == "- Task: x\n"  # no mode: nothing added
            async with pool.connection() as conn:
                await state.store(conn, section())
            await session_request.amend(context, found, request)
            assert request["harness"]["context_markdown"] == "- Task: x\n" + session_request.harness_line(["test"])
        assert session_request.harness_line(["a", "b"]).startswith(
            'Working modes of this task: a, b. Follow the sections "Working mode: a", "Working mode: b"'
        )
        assert [amender.name for amender in session_request.AMENDERS] == ["modes.brief"]

    asyncio.run(main())


async def _local(conn: AsyncConnection[Any]) -> Any:
    found = await records.get(conn, MAIN)
    assert found is not None
    return found
