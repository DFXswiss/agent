"""U-RUN-inbox / reconcile: the engine (order, halting, failures, triggers, period) and step 1 (pending polling)."""

from __future__ import annotations

import asyncio
from typing import Any

from agent_runner.reconcile import connect, hints, tasks
from agent_runner.reconcile.engine import PERIOD_S, Engine, Run, describe, engine_for
from agent_runner.reconcile.reconcile import STEPS
from agent_runner.registry import ReconcileStep
from agent_runner.sync.http import HubRefusal, HubTransportError

from .rig import Env, Rig, World, standard_world


def recorder(calls: list[str], name: str, *, halt: str | None = None, error: Exception | None = None) -> Any:
    async def step(run: Run) -> None:
        calls.append(name)
        if halt is not None:
            run.halt(halt)
            run.halt("ignored: the first reason stays")
        if error is not None:
            raise error

    return step


def test_steps_run_in_order_and_a_halt_skips_the_rest(rig: Rig) -> None:
    async def body() -> None:
        async with rig.env() as env:
            calls: list[str] = []
            engine = Engine(
                env.context,
                [
                    ReconcileStep(10, "one", recorder(calls, "one")),
                    ReconcileStep(20, "two", recorder(calls, "two", halt="pending_certification")),
                    ReconcileStep(30, "three", recorder(calls, "three")),
                ],
            )
            run = await engine.run_once()
            assert run is not None
            assert calls == ["one", "two"]
            assert run.halted == "pending_certification"
            assert engine.last is run
            assert engine.runs == 1
            assert run.pool is env.pool
            await engine.run_once()
            assert env.log().count("runner.reconcile state=pending_certification") == 1

    asyncio.run(body())


def test_a_failing_step_is_logged_by_type_and_halts_the_run(rig: Rig) -> None:
    async def body() -> None:
        async with rig.env() as env:
            for error, text in (
                (HubRefusal(401, "key_revoked"), "error=HubRefusal code=key_revoked"),
                (HubTransportError("response_unsigned"), "error=HubTransportError reason=response_unsigned"),
                (RuntimeError("secret detail"), "error=RuntimeError"),
            ):
                calls: list[str] = []
                engine = Engine(
                    env.context,
                    [
                        ReconcileStep(10, "bad", recorder(calls, "bad", error=error)),
                        ReconcileStep(20, "never", recorder(calls, "never")),
                    ],
                )
                run = await engine.run_once()
                assert run is not None
                assert run.halted == "failed"
                assert calls == ["bad"]
                assert f"runner.reconcile step_failed step=bad {text}" in env.log()
            assert "secret detail" not in env.log()
            assert describe(ValueError("x")) == "ValueError"

    asyncio.run(body())


def test_runs_never_overlap(rig: Rig) -> None:
    async def body() -> None:
        async with rig.env() as env:
            active: list[int] = []
            peak: list[int] = []

            async def slow(run: Run) -> None:
                active.append(1)
                peak.append(len(active))
                await asyncio.sleep(0.01)
                active.pop()

            engine = Engine(env.context, [ReconcileStep(10, "slow", slow)])
            await asyncio.gather(engine.run_once(), engine.run_once(), engine.run_once())
            assert max(peak) == 1
            assert engine.runs == 3

    asyncio.run(body())


def test_loop_runs_every_period_and_on_request_until_stopping(rig: Rig) -> None:
    async def body() -> None:
        async with rig.env() as env:
            calls: list[str] = []
            engine = Engine(env.context, [ReconcileStep(10, "count", recorder(calls, "count"))])
            env.clock.on_sleep = lambda seconds: (
                env.context.stopping.set() if len(env.clock.sleeps) == 3 else engine.request()
            )
            await engine.loop(env.context.stopping)
            assert env.clock.sleeps == [PERIOD_S] * 3
            assert len(calls) == 3
            stopping = asyncio.Event()
            task = asyncio.ensure_future(engine.wait(3600.0, stopping))
            await asyncio.sleep(0)
            engine.request()
            await asyncio.wait_for(task, 1)

    asyncio.run(body())


def test_not_enrolled_devices_have_no_engine(rig: Rig) -> None:
    async def body() -> None:
        async with rig.env(enrolled=False) as env:
            assert engine_for(env.context) is None
            assert await Engine(env.context, STEPS).run_once() is None
            await tasks.run_reconcile(env.context)
            await connect.reconcile_now(env.context)
            await hints.wake(env.context, {"type": "reconcile"})
            assert "runner.reconcile not_enrolled" in env.log()

    asyncio.run(body())


def test_engine_for_discovers_the_steps_and_is_kept_on_the_context(rig: Rig) -> None:
    async def body() -> None:
        async with rig.env() as env:
            engine = engine_for(env.context)
            assert engine is not None
            assert engine is engine_for(env.context)
            names = [step.name for step in engine.steps]
            assert [name for name in names if name in ("state", "logs", "wraps")] == ["state", "logs", "wraps"]
            assert [(s.order, s.name) for s in STEPS] == [(10, "state"), (20, "logs"), (30, "wraps")]

    asyncio.run(body())


def use_engine(env: Env) -> Engine:
    """The engine with exactly this package's steps (later packages add their own steps to the discovery)."""
    engine = Engine(env.context, STEPS)
    env.context.reconcile_engine = engine
    return engine


def test_pending_device_polls_the_state_every_30_seconds_until_active(rig: Rig) -> None:
    async def body() -> None:
        world = standard_world()
        world.state = "pending_certification"
        async with rig.env(world) as env:
            engine = use_engine(env)

            def tick(_seconds: float) -> None:
                if len(env.clock.sleeps) == 2:
                    world.state = "active"
                if len(env.clock.sleeps) == 3:
                    env.context.stopping.set()

            env.clock.on_sleep = tick
            await tasks.run_reconcile(env.context)
            assert env.clock.sleeps == [30.0, 30.0, 30.0]
            assert len(env.hub.targets("/v1/device/state")) == 3
            # nothing else is fetched while pending: the platform log only in the one active run
            assert len(env.hub.targets("/v1/device/platform-log")) == 1
            log = env.log()
            assert log.count("state=pending_certification") == 1
            assert log.count("state=active") == 1
            assert engine.last is not None
            assert engine.last.halted is None
            assert engine.last.book.acting_orgs() == list(world.orgs)

    asyncio.run(body())


def test_hints_and_connect_hook_drive_the_engine(rig: Rig) -> None:
    async def body() -> None:
        async with rig.env(standard_world()) as env:
            engine = use_engine(env)
            for kind in ("reconcile", "log", "wraps", "session.start"):
                engine.trigger.clear()
                await hints.HANDLERS[kind](env.context, {"type": kind})
                assert engine.trigger.is_set()
            engine.trigger.clear()
            await hints.HANDLERS["decertified"](env.context, {"type": "decertified"})
            assert engine.trigger.is_set()
            assert "runner.reconcile hint=decertified" in env.log()
            await connect.HOOKS[0].run(env.context)
            assert engine.runs == 1
            assert (connect.HOOKS[0].phase, connect.HOOKS[0].name) == ("reconcile", "reconcile")
            assert [spec.name for spec in tasks.TASKS] == ["reconcile"]

    asyncio.run(body())


def test_the_hub_position_is_adopted_from_the_state(rig: Rig) -> None:
    async def body() -> None:
        async with rig.env(World()) as env:
            env.hub.stored = {1: b"{}", 2: b"{}", 3: b"{}"}
            run = await use_engine(env).run_once()
            assert run is not None
            assert run.desired is not None
            assert run.desired["last_origin_seq"] == 3
            rows = await env.rows("SELECT value FROM runner.sync_state WHERE key = 'pushed_origin_seq'")
            assert rows == [("3",)]

    asyncio.run(body())
