"""Internal supervisor of the daemon (docs/platform/spec/05-runner.md section 1.1).

Runs the named tasks discovered from `agent_runner/<area>/tasks.py:TASKS` in one asyncio process. A task that
raises is restarted with exponential backoff 1 s -> 30 s (the backoff resets after a run of at least one restart
window); more than 10 restarts of the same task within 60 s end the daemon with exit 1, so launchd restarts it
(pattern of the legacy `agent_cli.daemon` restart limit). The 60 s window is measured on the task's own clock,
which stands still while the task waits for its restart: with a 1 s -> 30 s backoff at most six wall-clock restarts
fit into 60 s, so a wall-clock window could never reach the limit, while a task that keeps failing is caught after
eleven failures (decisions/RUN-01.md). A task that returns is finished and not restarted.

SIGTERM (and SIGINT): stop accepting work (`context.stopping` is set), await the shutdown hooks of
`<area>/shutdown.py:HOOKS` (outbox flush), cancel the tasks and return 0, all within 10 s. Running sessions are
left as they are; VMs keep running.

Only the type name of an exception is logged, never its message (it may carry content or secrets).
"""

from __future__ import annotations

import asyncio
import signal
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, TextIO

from agent_runner.errors import RunnerError
from agent_runner.registry import ShutdownHook, TaskSpec

BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 30.0
RESTART_LIMIT = 10
RESTART_WINDOW_S = 60.0
STOP_TIMEOUT_S = 10.0
# Share of the stop budget given to the shutdown hooks; the rest is for cancelling the tasks.
HOOK_SHARE = 0.8
EXIT_OK = 0
EXIT_FAILURE = 1


@dataclass
class SupervisorPolicy:
    backoff_base: float = BACKOFF_BASE_S
    backoff_cap: float = BACKOFF_CAP_S
    restart_limit: int = RESTART_LIMIT
    restart_window: float = RESTART_WINDOW_S
    stop_timeout: float = STOP_TIMEOUT_S

    def backoff(self, failures: int) -> float:
        """Delay before restart number `failures` (1-based) of an uninterrupted failure series."""
        return float(min(self.backoff_cap, self.backoff_base * 2 ** (failures - 1)))


@dataclass
class _TaskState:
    spec: TaskSpec
    # Restart times on the task's active clock (sum of its run durations; backoff waits do not count).
    restarts: list[float] = field(default_factory=list)
    active: float = 0.0
    failures: int = 0
    task: asyncio.Task[None] | None = None


class Supervisor:
    """Keeps the daemon tasks alive; `run(context)` returns the exit code."""

    def __init__(
        self,
        specs: Sequence[TaskSpec],
        *,
        hooks: Sequence[ShutdownHook] = (),
        policy: SupervisorPolicy | None = None,
        clock: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]],
        log: TextIO | None = None,
        install_signals: bool = True,
    ) -> None:
        names = [spec.name for spec in specs]
        duplicate = next((name for name in names if names.count(name) > 1), None)
        if duplicate is not None:
            raise RunnerError("registry_duplicate", duplicate)
        self._initial = list(specs)
        self._hooks = list(hooks)
        self.policy = policy or SupervisorPolicy()
        self._clock = clock
        self._sleep = sleep
        self._log = log if log is not None else sys.stderr
        self._install_signals = install_signals
        self._states: dict[str, _TaskState] = {}
        self._wake: asyncio.Event | None = None
        self._stopping: asyncio.Event | None = None
        self._context: Any = None
        self.fatal_task: str | None = None

    def _line(self, text: str) -> None:
        self._log.write(f"runner.supervisor {text}\n")
        self._log.flush()

    @property
    def running(self) -> list[str]:
        """Names of the tasks that currently run (sorted)."""
        return sorted(name for name, state in self._states.items() if state.task is not None and not state.task.done())

    def request_stop(self) -> None:
        """Begin the graceful stop (signal handler; idempotent)."""
        if self._stopping is not None and self._wake is not None and not self._stopping.is_set():
            self._line("stop_requested")
            self._stopping.set()
            self._wake.set()

    def spawn(self, spec: TaskSpec) -> None:
        """Start an additional task while running (for example `guestlink` per VM)."""
        if self._stopping is None or self._stopping.is_set():
            raise RunnerError("supervisor_not_running")
        current = self._states.get(spec.name)
        if current is not None and current.task is not None and not current.task.done():
            raise RunnerError("registry_duplicate", spec.name)
        state = _TaskState(spec)
        self._states[spec.name] = state
        state.task = asyncio.create_task(self._keep(state), name=spec.name)

    async def cancel(self, name: str) -> None:
        """Stop the task `name` (no restart); unknown or finished names are ignored."""
        state = self._states.pop(name, None)
        if state is None or state.task is None:
            return
        state.task.cancel()
        await asyncio.gather(state.task, return_exceptions=True)

    async def _keep(self, state: _TaskState) -> None:
        assert self._stopping is not None
        while True:
            started = self._clock()
            try:
                await state.spec.run(self._context)
            except Exception as exc:
                if self._stopping.is_set():
                    return
                ran = self._clock() - started
                if ran >= self.policy.restart_window:
                    state.failures = 0
                state.failures += 1
                state.active += ran
                now = state.active
                state.restarts = [t for t in state.restarts if now - t < self.policy.restart_window]
                state.restarts.append(now)
                count = len(state.restarts)
                self._line(f"task_failed name={state.spec.name} error={type(exc).__name__} restarts={count}")
                if count > self.policy.restart_limit:
                    self._line(f"restart_limit name={state.spec.name}")
                    self.fatal_task = state.spec.name
                    assert self._wake is not None
                    self._wake.set()
                    return
                await self._sleep(self.policy.backoff(state.failures))
                if self._stopping.is_set():
                    return
                continue
            self._line(f"task_finished name={state.spec.name}")
            return

    async def run(self, context: Any) -> int:
        """Run until SIGTERM/SIGINT or a restart limit; return the exit code.

        `context` is passed to every task and hook; when it has an attribute `stopping` that is an
        `asyncio.Event`, the supervisor uses that event as its stop flag so tasks can observe it.
        """
        loop = asyncio.get_running_loop()
        stopping = getattr(context, "stopping", None)
        self._stopping = stopping if isinstance(stopping, asyncio.Event) else asyncio.Event()
        self._wake = asyncio.Event()
        self._context = context
        installed: list[signal.Signals] = []
        if self._install_signals:
            for signum in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(signum, self.request_stop)
                installed.append(signum)
        try:
            for spec in self._initial:
                self.spawn(spec)
            self._line(f"started tasks={len(self._initial)}")
            await self._wake.wait()
            if self.fatal_task is not None:
                self._stopping.set()
                await self._cancel_all(self.policy.stop_timeout)
                return EXIT_FAILURE
            await self._graceful_stop(loop)
            return EXIT_OK
        finally:
            for signum in installed:
                loop.remove_signal_handler(signum)

    async def _graceful_stop(self, loop: asyncio.AbstractEventLoop) -> None:
        deadline = loop.time() + self.policy.stop_timeout
        hook_deadline = loop.time() + self.policy.stop_timeout * HOOK_SHARE
        for hook in self._hooks:
            remaining = hook_deadline - loop.time()
            try:
                await asyncio.wait_for(hook.run(self._context), timeout=max(remaining, 0.0))
            except TimeoutError:
                self._line(f"hook_timeout name={hook.name}")
            except Exception as exc:
                self._line(f"hook_failed name={hook.name} error={type(exc).__name__}")
        await self._cancel_all(max(deadline - loop.time(), 0.0))
        self._line("stopped")

    async def _cancel_all(self, timeout: float) -> None:
        tasks = [state.task for state in self._states.values() if state.task is not None and not state.task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=timeout)
            for task in pending:
                self._line(f"task_unresponsive name={task.get_name()}")
