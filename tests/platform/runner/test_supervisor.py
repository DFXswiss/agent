"""U-RUN supervisor (05-runner.md section 1.1): restart backoff and limits with a fake clock, SIGTERM stop."""

from __future__ import annotations

import asyncio
import io
import os
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from agent_runner.errors import RunnerError
from agent_runner.registry import ShutdownHook, TaskSpec
from agent_runner.supervisor import (
    BACKOFF_BASE_S,
    BACKOFF_CAP_S,
    RESTART_LIMIT,
    RESTART_WINDOW_S,
    STOP_TIMEOUT_S,
    Supervisor,
    SupervisorPolicy,
)


class Boom(Exception):
    """A task failure whose message must never be logged."""


@dataclass
class Ctx:
    stopping: asyncio.Event = field(default_factory=asyncio.Event)
    events: list[str] = field(default_factory=list)


@dataclass
class Clock:
    now: float = 0.0
    sleeps: list[float] = field(default_factory=list)
    on_sleep: Callable[[], None] | None = None

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep()
        await asyncio.sleep(0)


def make(
    specs: list[TaskSpec], clock: Clock, *, hooks: list[ShutdownHook] | None = None, **policy: float
) -> tuple[Supervisor, io.StringIO]:
    log = io.StringIO()
    sup = Supervisor(
        specs,
        hooks=hooks or [],
        policy=SupervisorPolicy(**policy),  # type: ignore[arg-type]
        clock=clock,
        sleep=clock.sleep,
        log=log,
        install_signals=False,
    )
    return sup, log


def test_defaults() -> None:
    assert (BACKOFF_BASE_S, BACKOFF_CAP_S, RESTART_LIMIT, RESTART_WINDOW_S, STOP_TIMEOUT_S) == (
        1.0,
        30.0,
        10,
        60.0,
        10.0,
    )
    policy = SupervisorPolicy()
    assert [policy.backoff(n) for n in range(1, 8)] == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


def test_default_policy_and_log() -> None:
    sup = Supervisor([], clock=time.monotonic, sleep=asyncio.sleep)
    assert sup.policy == SupervisorPolicy()
    assert sup.running == []


def test_duplicate_specs_refused() -> None:
    async def run(_ctx: object) -> None:
        return None

    with pytest.raises(RunnerError) as caught:
        Supervisor([TaskSpec("a", run), TaskSpec("a", run)], clock=time.monotonic, sleep=asyncio.sleep)
    assert (caught.value.code, caught.value.name) == ("registry_duplicate", "a")


def test_failing_task_hits_restart_limit_with_backoff() -> None:
    """Default policy, a task failing immediately: backoff 1, 2, 4, 8, 16, 30 ...; the 11th restart within 60 s of
    task run time ends the daemon with exit 1 (backoff waits do not count towards the window)."""
    clock = Clock()
    calls: list[float] = []

    async def failing(_ctx: object) -> None:
        calls.append(clock.now)
        raise Boom("secret detail")

    sup, log = make([TaskSpec("sync", failing)], clock)
    assert asyncio.run(sup.run(Ctx())) == 1
    assert sup.fatal_task == "sync"
    assert clock.sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0, 30.0, 30.0]
    assert len(calls) == 11
    text = log.getvalue()
    assert "task_failed name=sync error=Boom restarts=11" in text
    assert "restart_limit name=sync" in text
    assert "secret detail" not in text


def test_more_than_ten_restarts_in_sixty_seconds_is_fatal() -> None:
    """Default policy: a task that fails after 5 s of work eleven times (55 s of run time) is fatal."""
    clock = Clock()

    async def flapping(_ctx: object) -> None:
        clock.now += 5.0
        raise Boom

    sup, log = make([TaskSpec("ws", flapping)], clock)
    assert asyncio.run(sup.run(Ctx())) == 1
    assert len(clock.sleeps) == 10
    assert "restarts=11" in log.getvalue()


def test_ten_restarts_in_sixty_seconds_are_tolerated() -> None:
    """Default policy: failures every 6.5 s of run time keep at most 10 restarts in the window; never fatal."""
    clock = Clock()
    state = {"n": 0}
    stop: list[Callable[[], None]] = []

    async def task(_ctx: object) -> None:
        state["n"] += 1
        if state["n"] <= 15:
            clock.now += 6.5
            raise Boom
        stop[0]()
        await asyncio.Event().wait()

    sup, log = make([TaskSpec("t", task)], clock)
    stop.append(sup.request_stop)
    assert asyncio.run(sup.run(Ctx())) == 0
    text = log.getvalue()
    counts = [int(line.rsplit("=", 1)[1]) for line in text.splitlines() if "task_failed" in line]
    assert counts == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 10, 10, 10, 10, 10]
    assert "restart_limit" not in text
    assert sup.fatal_task is None


def test_backoff_resets_after_a_stable_run() -> None:
    clock = Clock()
    runs = {"n": 0}
    stop: list[Callable[[], None]] = []

    async def task(_ctx: object) -> None:
        runs["n"] += 1
        if runs["n"] <= 3:
            raise Boom
        if runs["n"] == 4:
            clock.now += 120.0
            raise Boom
        stop[0]()
        await asyncio.Event().wait()

    sup, _ = make([TaskSpec("t", task)], clock)
    stop.append(sup.request_stop)
    assert asyncio.run(sup.run(Ctx())) == 0
    assert clock.sleeps == [1.0, 2.0, 4.0, 1.0]


def test_finished_task_is_not_restarted() -> None:
    clock = Clock()
    ctx = Ctx()

    async def once(c: Ctx) -> None:
        c.events.append("ran")

    async def stopper(_c: Ctx) -> None:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        sup.request_stop()
        await asyncio.Event().wait()

    sup, log = make([TaskSpec("once", once), TaskSpec("stopper", stopper)], clock)
    assert asyncio.run(sup.run(ctx)) == 0
    assert ctx.events == ["ran"]
    assert "task_finished name=once" in log.getvalue()


def test_graceful_stop_runs_hooks_then_cancels() -> None:
    clock = Clock()
    ctx = Ctx()
    order: list[str] = []

    async def worker(c: Ctx) -> None:
        try:
            while not c.stopping.is_set():
                await asyncio.sleep(0)
            order.append("worker saw stopping")
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            order.append("worker cancelled")
            raise

    async def flush(c: Ctx) -> None:
        await asyncio.sleep(0.01)
        order.append(f"flush stopping={c.stopping.is_set()}")

    async def broken(_c: Ctx) -> None:
        raise Boom("hook secret")

    async def requester(_c: Ctx) -> None:
        await asyncio.sleep(0.01)
        sup.request_stop()
        sup.request_stop()
        await asyncio.Event().wait()

    sup, log = make(
        [TaskSpec("worker", worker), TaskSpec("requester", requester)],
        clock,
        hooks=[ShutdownHook("outbox-flush", flush), ShutdownHook("broken", broken)],
    )
    started = time.monotonic()
    assert asyncio.run(sup.run(ctx)) == 0
    assert time.monotonic() - started < STOP_TIMEOUT_S
    assert order == ["worker saw stopping", "flush stopping=True", "worker cancelled"]
    text = log.getvalue()
    assert text.count("stop_requested") == 1
    assert "hook_failed name=broken error=Boom" in text
    assert "hook secret" not in text
    assert text.rstrip().endswith("runner.supervisor stopped")


def test_hanging_hook_and_stubborn_task_respect_the_stop_budget() -> None:
    clock = Clock()

    async def hang(_c: Ctx) -> None:
        await asyncio.Event().wait()

    async def stubborn(_c: Ctx) -> None:
        while True:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                await asyncio.sleep(0.3)
                return

    async def requester(_c: Ctx) -> None:
        sup.request_stop()
        await asyncio.Event().wait()

    sup, log = make(
        [TaskSpec("stubborn", stubborn), TaskSpec("requester", requester)],
        clock,
        hooks=[ShutdownHook("hang", hang), ShutdownHook("late", hang)],
        stop_timeout=0.2,
    )
    started = time.monotonic()
    assert asyncio.run(sup.run(Ctx())) == 0
    assert time.monotonic() - started < 5  # the hooks hang for ever: the 0.2 s budget ends them, with room for load
    text = log.getvalue()
    assert "hook_timeout name=hang" in text
    assert "hook_timeout name=late" in text
    assert "task_unresponsive name=stubborn" in text


def test_failure_during_stop_is_not_restarted() -> None:
    clock = Clock()

    async def fails_when_stopping(c: Ctx) -> None:
        await c.stopping.wait()
        raise Boom

    async def slow_hook(_c: Ctx) -> None:
        await asyncio.sleep(0.02)

    async def requester(_c: Ctx) -> None:
        sup.request_stop()
        await asyncio.Event().wait()

    sup, log = make(
        [TaskSpec("f", fails_when_stopping), TaskSpec("requester", requester)],
        clock,
        hooks=[ShutdownHook("slow", slow_hook)],
    )
    assert asyncio.run(sup.run(Ctx())) == 0
    assert "task_failed" not in log.getvalue()


def test_stop_during_backoff_ends_the_restart_loop() -> None:
    clock = Clock()
    runs: list[int] = []

    async def failing(_c: Ctx) -> None:
        runs.append(1)
        raise Boom

    async def slow_hook(_c: Ctx) -> None:
        # Gives the sleeping task the chance to wake up and see the stop flag before it is cancelled.
        await asyncio.sleep(0.01)

    sup, _ = make([TaskSpec("f", failing)], clock, hooks=[ShutdownHook("slow", slow_hook)])
    clock.on_sleep = sup.request_stop
    assert asyncio.run(sup.run(Ctx())) == 0
    assert len(runs) == 1


def test_context_without_stopping_event() -> None:
    clock = Clock()
    seen: list[bool] = []

    async def task(ctx: object) -> None:
        seen.append(hasattr(ctx, "stopping"))
        sup.request_stop()
        await asyncio.Event().wait()

    sup, _ = make([TaskSpec("t", task)], clock)
    assert asyncio.run(sup.run(object())) == 0
    assert seen == [False]


def test_spawn_and_cancel_dynamic_tasks() -> None:
    clock = Clock()
    ctx = Ctx()

    async def guestlink(c: Ctx) -> None:
        c.events.append("guestlink")
        await asyncio.Event().wait()

    async def controller(c: Ctx) -> None:
        sup.spawn(TaskSpec("guestlink-vm-1", guestlink))
        await asyncio.sleep(0)
        assert sup.running == ["controller", "guestlink-vm-1"]
        with pytest.raises(RunnerError) as caught:
            sup.spawn(TaskSpec("guestlink-vm-1", guestlink))
        assert caught.value.code == "registry_duplicate"
        await sup.cancel("guestlink-vm-1")
        await sup.cancel("guestlink-vm-1")
        assert sup.running == ["controller"]
        sup.spawn(TaskSpec("guestlink-vm-1", guestlink))
        await asyncio.sleep(0)
        c.events.append("done")
        sup.request_stop()
        await asyncio.Event().wait()

    sup, _ = make([TaskSpec("controller", controller)], clock)
    assert asyncio.run(sup.run(ctx)) == 0
    assert ctx.events == ["guestlink", "guestlink", "done"]


def test_spawn_outside_run_is_refused() -> None:
    async def run(_c: object) -> None:
        return None

    sup, _ = make([], Clock())
    with pytest.raises(RunnerError) as caught:
        sup.spawn(TaskSpec("x", run))
    assert caught.value.code == "supervisor_not_running"
    sup.request_stop()


def test_spawn_after_finish_reuses_the_name() -> None:
    clock = Clock()
    ctx = Ctx()

    async def once(c: Ctx) -> None:
        c.events.append("once")

    async def controller(c: Ctx) -> None:
        sup.spawn(TaskSpec("once", once))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        sup.spawn(TaskSpec("once", once))
        await asyncio.sleep(0)
        sup.request_stop()
        await asyncio.Event().wait()

    sup, _ = make([TaskSpec("controller", controller)], clock)
    assert asyncio.run(sup.run(ctx)) == 0
    assert ctx.events == ["once", "once"]


def test_fatal_stops_other_tasks() -> None:
    clock = Clock()
    cancelled: list[str] = []

    async def failing(_c: Ctx) -> None:
        raise Boom

    async def other(_c: Ctx) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append("other")
            raise

    sup, _ = make([TaskSpec("failing", failing), TaskSpec("other", other)], clock, restart_limit=0)
    ctx = Ctx()
    assert asyncio.run(sup.run(ctx)) == 1
    assert cancelled == ["other"]
    assert ctx.stopping.is_set()


def test_sigterm_stops_with_exit_zero() -> None:
    """Real signal delivery: the installed handler starts the graceful stop."""

    async def sender(_c: Ctx) -> None:
        await asyncio.sleep(0.01)
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.Event().wait()

    async def scenario() -> tuple[int, bool]:
        log = io.StringIO()
        sup = Supervisor([TaskSpec("sender", sender)], clock=time.monotonic, sleep=asyncio.sleep, log=log)
        code = await sup.run(Ctx())
        loop = asyncio.get_running_loop()
        # The handler is removed again after the run.
        removed = not loop.remove_signal_handler(signal.SIGTERM)
        return code, removed and "stop_requested" in log.getvalue()

    previous = signal.getsignal(signal.SIGTERM)
    try:
        assert asyncio.run(scenario()) == (0, True)
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_sigint_also_stops() -> None:
    async def sender(_c: Ctx) -> None:
        await asyncio.sleep(0.01)
        os.kill(os.getpid(), signal.SIGINT)
        await asyncio.Event().wait()

    async def scenario() -> int:
        sup = Supervisor([TaskSpec("sender", sender)], clock=time.monotonic, sleep=asyncio.sleep, log=io.StringIO())
        return await sup.run(Ctx())

    assert asyncio.run(scenario()) == 0


def test_default_log_is_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    async def task(_c: Ctx) -> None:
        sup.request_stop()
        await asyncio.Event().wait()

    sup = Supervisor([TaskSpec("t", task)], clock=time.monotonic, sleep=asyncio.sleep, install_signals=False)
    assert asyncio.run(sup.run(Ctx())) == 0
    assert "runner.supervisor started tasks=1" in capsys.readouterr().err
