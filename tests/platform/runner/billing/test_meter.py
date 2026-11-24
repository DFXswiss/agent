"""U-RUN-billing (meter): VM runtime to the millisecond - a shared workgroup VM split between the sessions that hold it,
nothing for a VM nobody holds, pieces of at most one hour, a new VM starting at now, a stopped VM's gap free, the
extension point for further VMs, and the daemon task (KOR-31, ADR-062 section 3)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from agent_runner import registry
from agent_runner.billing import meter, tasks

from ..prompts.kit import run, setup
from ..reconcile.rig import Rig
from ..sessions.kit import ORG

ACTIVE = {"billing": {"api": 1, "active": True, "orgs": {ORG: {"exhausted": False}}}}
T0 = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


class Recorder:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    async def enqueue(self, _conn: Any, kind: str, body: dict[str, Any]) -> int:
        assert kind == "runtime"
        self.items.append(body)
        return len(self.items)


def test_split_and_pieces() -> None:
    assert meter.split(10, 3) == [4, 3, 3]
    assert meter.split(2, 3) == [1, 1, 0]
    assert meter.pieces(T0, T0) == []
    hour = timedelta(hours=1)
    assert meter.pieces(T0, T0 + hour + timedelta(seconds=1)) == [
        (T0, T0 + hour),
        (T0 + hour, T0 + hour + timedelta(seconds=1)),
    ]
    assert meter.whole_ms(1_700_000_000.1239) == datetime(2023, 11, 14, 22, 13, 20, 123000, tzinfo=UTC)


def test_a_shared_vm_is_split_and_nothing_is_charged_twice(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        first = await pk.add_session(1, state="working")
        second = await pk.add_session(2, state="idle")
        await pk.add_session(3, state="suspended")
        vm = (await pk.vm()).id
        recorder = Recorder()
        async with env.pool.connection() as conn, conn.transaction():
            assert await meter.measure(env.context, conn, recorder, T0) == 0  # first sight: from now on
        async with env.pool.connection() as conn, conn.transaction():
            assert await meter.measure(env.context, conn, recorder, T0 + timedelta(milliseconds=30_001)) == 2
        assert [(i["session_id"], i["runtime_ms"]) for i in recorder.items] == [(first, 15_001), (second, 15_000)]
        assert recorder.items[0] == {
            "session_id": first,
            "vm_id": vm,
            "vm_kind": "session",
            "size": "session",
            "from": "2026-11-21T12:00:00.000Z",
            "to": "2026-11-21T12:00:30.001Z",
            "runtime_ms": 15_001,
        }
        # a clock that went back: nothing, and measuring does not move back
        async with env.pool.connection() as conn, conn.transaction():
            assert await meter.measure(env.context, conn, recorder, T0 + timedelta(seconds=10)) == 0
        rows = await env.rows("SELECT measured_through FROM runner.billing_meter")
        assert rows[0][0] == T0 + timedelta(milliseconds=30_001)
        # the same time again: nothing; a long gap in pieces of one hour
        async with env.pool.connection() as conn, conn.transaction():
            assert await meter.measure(env.context, conn, recorder, T0 + timedelta(milliseconds=30_001)) == 0
            assert await meter.measure(env.context, conn, recorder, T0 + timedelta(hours=1, seconds=40)) == 4
        # nobody holds it: measured, not charged
        await pk.set_state(first, "suspended")
        await pk.set_state(second, "stopped")
        async with env.pool.connection() as conn, conn.transaction():
            assert await meter.measure(env.context, conn, recorder, T0 + timedelta(hours=2)) == 0
        # a stopped VM loses its row: the gap until it runs again is free
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.vm SET state = 'stopped' WHERE id = %s", (vm,))
        async with env.pool.connection() as conn, conn.transaction():
            await meter.measure(env.context, conn, recorder, T0 + timedelta(hours=3))
        assert await env.rows("SELECT vm_id FROM runner.billing_meter") == []

    run(rig, body)


def test_further_vms_come_from_the_extension_point(rig: Rig, monkeypatch: Any) -> None:
    async def work_vms(_context: Any, _conn: Any) -> list[meter.MeteredVm]:
        return [meter.MeteredVm("wvm-1", "work", "large", ("01930000-0000-7000-8000-000000000001",))]

    monkeypatch.setattr(registry, "discover", lambda module, attribute: [("workvm", work_vms), ("x", None)])

    async def body(env: Any) -> None:
        recorder = Recorder()
        async with env.pool.connection() as conn, conn.transaction():
            await meter.measure(env.context, conn, recorder, T0)
            await meter.measure(env.context, conn, recorder, T0 + timedelta(seconds=1))
        assert [(i["vm_id"], i["vm_kind"], i["size"], i["runtime_ms"]) for i in recorder.items] == [
            ("wvm-1", "work", "large", 1000)
        ]

    run(rig, body)


def test_the_task_meters_only_on_a_platform_device_of_a_billing_hub(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        await pk.add_session(1, state="working")
        session_env = pk.kit.session_env
        service = SimpleNamespace(env=session_env)
        times = iter([1_700_000_000.0, 1_700_000_010.0, 1_700_000_020.0])
        session_env.wall = lambda: next(times)
        assert await meter.tick(env.context, service) == 0  # no billing section
        session_env.desired = ACTIVE
        assert await meter.tick(env.context, service) == 0  # first sight
        assert await meter.tick(env.context, service) == 1
        assert [kind for kind, _ in await pk.kit.outbox()].count("runtime") == 1
        # billing switched off: the rows go, switched on again it starts from then
        session_env.desired = {"billing": {"api": 1, "active": False}}
        assert await meter.tick(env.context, service) == 0
        assert await env.rows("SELECT vm_id FROM runner.billing_meter") == []
        session_env.device_kind = "org"
        assert await meter.tick(env.context, service) == 0

    run(rig, body)


def test_the_daemon_loop() -> None:
    calls: list[str] = []
    stopping = asyncio.Event()

    async def sleep(_seconds: float) -> None:
        if len(calls) >= 2:
            stopping.set()

    class Broken:
        @property
        def env(self) -> Any:
            calls.append("env")
            raise RuntimeError("boom")

    lines: list[str] = []
    log = SimpleNamespace(write=lines.append, flush=lambda: None)
    context = SimpleNamespace(stopping=stopping, host=SimpleNamespace(sleep=sleep), log=log)
    found = iter([None, Broken(), Broken()])

    asyncio.run(meter.run(context, lambda _c: next(found)))
    assert lines == ["runner.billing meter_failed error=RuntimeError\n"] * 2
    assert [spec.name for spec in tasks.TASKS] == ["billing_meter"]


def test_a_share_of_zero_milliseconds_is_not_sent(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        for n in (1, 2, 3):
            await pk.add_session(n, state="working")
        recorder = Recorder()
        async with env.pool.connection() as conn, conn.transaction():
            await meter.measure(env.context, conn, recorder, T0)
            assert await meter.measure(env.context, conn, recorder, T0 + timedelta(milliseconds=2)) == 2
        assert [item["runtime_ms"] for item in recorder.items] == [1, 1]

    run(rig, body)
