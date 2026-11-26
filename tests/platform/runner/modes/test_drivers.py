"""U-RUN-modes (KOR-75, ADR-098 section 4): the frame of the drivers - state and take-over, the rate budget per
GitHub identity, steps as operations of the task's main session, the wake-up of the session and the report."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from agent_runner.errors import RunnerError
from agent_runner.modes import drivers, state
from agent_runner.modes.drivers import Budget, Current, Step, TestDriver
from agent_runner.prompts.resume import RESUMED
from agent_runner.sessions import records
from agent_runner.sync.http import HubRefusal, HubTransportError
from tests.platform.runner.modes.support import CHOOSER, MAIN, TASK, entry, section, session

NOW = datetime(2026, 11, 26, 10, 0, tzinfo=UTC)


class Hub:
    def __init__(self, answer: Any = None, failure: Exception | None = None) -> None:
        self.answer = {"on": True} if answer is None else answer
        self.failure = failure
        self.calls: list[tuple[str, str, Any]] = []

    async def request(self, method: str, target: str, body: Any = None) -> dict[str, object]:
        self.calls.append((method, target, body))
        if self.failure is not None:
            raise self.failure
        return dict(self.answer)


@dataclass
class Rig:
    pool: AsyncConnectionPool[Any]
    hub: Hub
    woken: list[tuple[str, str]]

    @property
    def context(self) -> Any:
        return SimpleNamespace(pool=self.pool)

    async def rows(self, query: str, *params: Any) -> list[tuple[Any, ...]]:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(query, params)
            return list(await cursor.fetchall())

    async def store(self, desired: dict[str, Any]) -> None:
        async with self.pool.connection() as conn:
            await state.store(conn, desired)


@pytest.fixture
def rig_factory(store_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    def build(hub: Hub | None = None, sync: bool = True) -> Any:
        found = hub or Hub()
        woken: list[tuple[str, str]] = []

        async def wake(_context: Any, session_id: str, ref: str) -> bool:
            woken.append((session_id, ref))
            return True

        monkeypatch.setattr(drivers, "wake", wake)
        monkeypatch.setattr(drivers, "runtime", lambda _c: SimpleNamespace(client=found) if sync else None)
        return found, woken

    return build


def _run(store_dsn: str, body: Any) -> None:
    async def main() -> None:
        async with AsyncConnectionPool(store_dsn, open=False, kwargs={"autocommit": True}) as pool:
            await pool.open()
            await body(pool)

    asyncio.run(main())


def test_the_test_driver() -> None:
    driver = TestDriver()
    first = asyncio.run(driver.step(Current(TASK, "test", CHOOSER, drivers.START, {})))
    assert first == Step("woken", {"runs": 1}, None, (("github.list_identities", {}),), wake=True)
    again = asyncio.run(driver.step(Current(TASK, "test", CHOOSER, "woken", {"runs": 1})))
    assert again == Step("woken", {"runs": 1}, None)
    assert driver.reads_github is False
    assert set(drivers.DRIVERS) == {"test"}


def test_a_step_becomes_an_operation_of_the_main_session(store_dsn: str, rig_factory: Any) -> None:
    hub, woken = rig_factory()

    async def body(pool: AsyncConnectionPool[Any]) -> None:
        rig = Rig(pool, hub, woken)
        async with pool.connection() as conn:
            await session(conn)
            await session(conn, "0193000c-0000-7000-8000-000000000011", depth=1)
        await rig.store(section())
        assert await drivers.run_once(rig.context, now=NOW) == 1
        [(op_id, session_id, op_type, actor, params, op_state)] = await rig.rows(
            "SELECT id::text, session_id::text, type, actor_id::text, params, state FROM runner.operation"
        )
        assert (session_id, op_type, actor, params, op_state) == (
            MAIN,
            "github.list_identities",
            CHOOSER,
            {},
            "received",
        )
        assert woken == [(MAIN, op_id)]
        assert await rig.rows("SELECT step, counters, next_at FROM runner.mode_driver") == [
            ("woken", {"runs": 1}, None)
        ]
        [(method, target, report)] = hub.calls
        assert (method, target) == ("PUT", f"/v1/device/tasks/{TASK}/modes/test/driver")
        assert report == {
            "driver": {"step": "woken", "counters": {"runs": 1}, "next_at": None, "at": "2026-11-26T10:00:00.000Z"}
        }
        assert await drivers.run_once(rig.context, now=NOW) == 0  # nothing new: no operation, no report
        assert len(hub.calls) == 1
        await rig.store(section({}))  # the mode is off: the driver stops at its step boundary
        assert await drivers.run_once(rig.context, now=NOW) == 0
        assert await rig.rows("SELECT count(*) FROM runner.mode_driver") == [(0,)]

    _run(store_dsn, body)


def test_a_device_takes_over_the_reported_state(store_dsn: str, rig_factory: Any) -> None:
    hub, woken = rig_factory()

    async def body(pool: AsyncConnectionPool[Any]) -> None:
        rig = Rig(pool, hub, woken)
        async with pool.connection() as conn:
            await session(conn)
        reported = {"step": "woken", "counters": {"runs": 3}, "next_at": None, "at": "2026-11-26T09:00:00.000Z"}
        await rig.store(section({TASK: [entry(driver=reported)]}))
        assert await drivers.run_once(rig.context, now=NOW) == 0  # nothing left to do in that step
        assert await rig.rows("SELECT count(*) FROM runner.operation") == [(0,)]
        later = {**reported, "step": drivers.START, "next_at": "2026-11-26T11:00:00.000Z"}
        await rig.store(section({TASK: [entry(driver=later)]}))
        assert await drivers.run_once(rig.context, now=NOW) == 0  # not due yet
        assert await drivers.run_once(rig.context, now=NOW + timedelta(hours=2)) == 1
        odd = {"step": 1, "counters": [], "next_at": 2}
        await rig.store(section({TASK: [entry("other", driver=odd)]}))
        assert await drivers.run_once(rig.context, now=NOW) == 0  # a mode without a driver on this device

    _run(store_dsn, body)


def test_without_a_main_session_with_a_vm_nothing_runs(store_dsn: str, rig_factory: Any) -> None:
    hub, woken = rig_factory()

    async def body(pool: AsyncConnectionPool[Any]) -> None:
        rig = Rig(pool, hub, woken)
        async with pool.connection() as conn:
            await session(conn, vm=None)
        await rig.store(section())
        assert await drivers.run_once(rig.context, now=NOW) == 0
        assert hub.calls == []

    _run(store_dsn, body)


def test_switched_off_meanwhile_and_unreachable_reports(store_dsn: str, rig_factory: Any) -> None:
    async def body(pool: AsyncConnectionPool[Any]) -> None:
        async with pool.connection() as conn:
            await session(conn)
            await state.store(conn, section())
        rig_factory(Hub(answer={"on": False}))
        assert await drivers.run_once(SimpleNamespace(pool=pool), now=NOW) == 1
        async with pool.connection() as conn:
            cursor = await conn.execute("SELECT count(*) FROM runner.mode_driver")
            assert await cursor.fetchone() == (0,)  # the hub said off: the driver forgets its state

    _run(store_dsn, body)


def test_the_report_without_a_sync_runtime_or_a_reachable_hub(monkeypatch: pytest.MonkeyPatch) -> None:
    mode = state.TaskMode(TASK, "test", CHOOSER, None)
    monkeypatch.setattr(drivers, "runtime", lambda _c: None)
    assert asyncio.run(drivers._report(None, mode, Step("s", {}, None), NOW))
    for failure in (HubRefusal(404, "not_found"), HubTransportError("unreachable")):
        hub = Hub(failure=failure)
        monkeypatch.setattr(drivers, "runtime", lambda _c, hub=hub: SimpleNamespace(client=hub))
        assert asyncio.run(drivers._report(None, mode, Step("s", {}, 5.0), NOW))
        assert hub.calls[0][2]["driver"]["next_at"] == "2026-11-26T10:00:05.000Z"


def test_the_rate_budget_per_identity(store_dsn: str, rig_factory: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    hub, woken = rig_factory()

    @dataclass(frozen=True)
    class Reader:
        reads_github: bool = True

        async def step(self, current: Current) -> Step:
            return Step("read", {"reads": int(current.counters.get("reads", 0)) + 1}, 0.0)

    monkeypatch.setattr(drivers, "DRIVERS", {"test": Reader()})

    async def body(pool: AsyncConnectionPool[Any]) -> None:
        rig = Rig(pool, hub, woken)
        async with pool.connection() as conn:
            await session(conn)
        await rig.store(section())
        budget = Budget(per_hour=2)
        assert await drivers.run_once(rig.context, now=NOW, budget=budget) == 1
        assert await drivers.run_once(rig.context, now=NOW, budget=budget) == 1
        assert await drivers.run_once(rig.context, now=NOW, budget=budget) == 0  # used up: waits for the window
        assert await rig.rows("SELECT next_at FROM runner.mode_driver") == [(NOW + timedelta(hours=1),)]
        assert await rig.rows("SELECT identity, used FROM runner.mode_rate") == [(f"github:{CHOOSER}", 2)]
        assert await drivers.run_once(rig.context, now=NOW + timedelta(hours=1), budget=budget) == 1
        assert await rig.rows("SELECT used FROM runner.mode_rate") == [(1,)]
        assert woken == []

    _run(store_dsn, body)


class Env:
    def __init__(self, pool: AsyncConnectionPool[Any]) -> None:
        self.pool = pool
        self.prompts: list[tuple[str, str, str]] = []
        self.failure: Exception | None = None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncConnection[Any]]:
        async with self.pool.connection() as conn:
            yield conn

    async def client(self, _row: Any) -> Any:
        env = self

        class Guest:
            async def __aenter__(self) -> Guest:
                return self

            async def __aexit__(self, *_exc: object) -> None:
                return None

            async def register_prompt(
                self, session_id: str, prompt_id: str, text: str, line: str | None = None
            ) -> None:
                if env.failure is not None:
                    raise env.failure
                env.prompts.append((session_id, prompt_id, text))

        return Guest()


class Deliverer:
    def __init__(self, env: Env, outcome: str = RESUMED) -> None:
        self.env = env
        self.outcome = outcome
        self.resumed: list[str] = []
        self.resumer = self

    def lock(self, _session_id: str) -> Any:
        return asyncio.Lock()

    async def resume(self, session: Any) -> str:
        self.resumed.append(session.id)
        return self.outcome


def test_the_wake_up(store_dsn: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ref = "0193000c-0000-7000-8000-000000000030"

    async def body(pool: AsyncConnectionPool[Any]) -> None:
        env = Env(pool)
        deliverer = Deliverer(env)
        running: dict[str, Any] = {"row": SimpleNamespace(id="vm-00000001")}

        async def vm_running(_env: Any, _session: Any) -> Any:
            return running["row"]

        monkeypatch.setattr(drivers, "vm_running", vm_running)
        monkeypatch.setattr(drivers, "deliverer_for", lambda _c: None)
        assert not await drivers.wake(None, MAIN, ref)
        monkeypatch.setattr(drivers, "deliverer_for", lambda _c: deliverer)
        assert not await drivers.wake(None, MAIN, ref)  # no such session
        async with pool.connection() as conn:
            await session(conn)
        assert await drivers.wake(None, MAIN, ref)
        [(session_id, prompt_id, text)] = env.prompts
        assert (session_id, text) == (MAIN, drivers.WAKE.format(ref=ref))
        assert prompt_id == drivers.stable_ping_id("mode.wake", ref)
        async with pool.connection() as conn:
            await records.update(conn, MAIN, state="suspended")
        assert await drivers.wake(None, MAIN, ref)
        assert deliverer.resumed == [MAIN]
        deliverer.outcome = "retry"
        assert not await drivers.wake(None, MAIN, ref)
        async with pool.connection() as conn:
            await records.update(conn, MAIN, state="idle")
        running["row"] = None
        assert not await drivers.wake(None, MAIN, ref)
        running["row"] = SimpleNamespace(id="vm-00000001")
        env.failure = RunnerError("guest_unreachable", "x")
        assert not await drivers.wake(None, MAIN, ref)
        async with pool.connection() as conn:
            await records.update(conn, MAIN, state="stopped")
        assert not await drivers.wake(None, MAIN, ref)

    _run(store_dsn, body)
