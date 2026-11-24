"""U-RUN-workvm tasks: daemon task `workvm` (13-work-vm.md section 7): reconciliation, then the idle pass every
`INTERVAL` seconds until the daemon stops; at once done on a device without work VMs."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from agent_runner.registry import TaskSpec
from agent_runner.workvm import tasks


class FakeWorkVms:
    def __init__(self, stopping: asyncio.Event, passes: int) -> None:
        self.stopping = stopping
        self.passes = passes
        self.calls: list[str] = []
        self.pool_wanted = asyncio.Event()

    async def reconcile(self) -> None:
        self.calls.append("reconcile")

    async def guard_pass(self) -> list[str]:
        self.calls.append("guard_pass")
        return []

    async def idle_pass(self) -> list[str]:
        self.calls.append("idle_pass")
        return []

    async def sweep(self) -> int:
        self.calls.append("sweep")
        return 0

    async def pool_pass(self) -> int:
        self.calls.append("pool_pass")
        if self.calls.count("pool_pass") == self.passes:
            self.stopping.set()
        return 0


class FakeRuns:
    def __init__(self, workvms: FakeWorkVms) -> None:
        self.workvms = workvms

    async def reconcile(self) -> int:
        self.workvms.calls.append("runs")
        return 0


@pytest.fixture(autouse=True)
def runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tasks, "runs_of", lambda context, workvms: FakeRuns(workvms))


def test_task_is_registered() -> None:
    assert [TaskSpec("workvm", tasks.run_workvm)] == tasks.TASKS


def test_without_work_vms_the_task_ends_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tasks, "workvms_for", lambda context: None)
    context = SimpleNamespace(stopping=None)
    assert asyncio.run(tasks.run_workvm(context)) is None


def test_reconcile_then_idle_passes_until_stopping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tasks, "INTERVAL", 0.01)

    async def run() -> list[str]:
        stopping = asyncio.Event()
        workvms = FakeWorkVms(stopping, passes=3)
        context: Any = SimpleNamespace(stopping=stopping)
        monkeypatch.setattr(tasks, "workvms_for", lambda found: workvms if found is context else None)
        await asyncio.wait_for(tasks.run_workvm(context), 5)
        return workvms.calls

    assert asyncio.run(run()) == ["runs", "reconcile", *["guard_pass", "idle_pass", "sweep", "pool_pass"] * 3]


def test_a_stop_during_the_wait_ends_the_task_without_another_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tasks, "INTERVAL", 60.0)

    async def run() -> list[str]:
        stopping = asyncio.Event()
        workvms = FakeWorkVms(stopping, passes=0)
        monkeypatch.setattr(tasks, "workvms_for", lambda context: workvms)
        task = asyncio.ensure_future(tasks.run_workvm(SimpleNamespace(stopping=stopping)))
        for _ in range(5):
            await asyncio.sleep(0)
        stopping.set()
        await asyncio.wait_for(task, 5)
        return workvms.calls

    assert asyncio.run(run()) == ["runs", "reconcile", "guard_pass", "idle_pass", "sweep", "pool_pass"]


def test_a_stopped_daemon_reconciles_only(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> list[str]:
        stopping = asyncio.Event()
        stopping.set()
        workvms = FakeWorkVms(stopping, passes=0)
        monkeypatch.setattr(tasks, "workvms_for", lambda context: workvms)
        await tasks.run_workvm(SimpleNamespace(stopping=stopping))
        return workvms.calls

    assert asyncio.run(run()) == ["runs", "reconcile"]


def test_an_assigned_pool_entry_refills_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tasks, "INTERVAL", 60.0)

    async def run() -> list[str]:
        stopping = asyncio.Event()
        workvms = FakeWorkVms(stopping, passes=2)
        monkeypatch.setattr(tasks, "workvms_for", lambda context: workvms)
        task = asyncio.ensure_future(tasks.run_workvm(SimpleNamespace(stopping=stopping)))
        for _ in range(5):
            await asyncio.sleep(0)
        workvms.pool_wanted.set()
        await asyncio.wait_for(task, 5)
        return workvms.calls

    assert asyncio.run(run()) == ["runs", "reconcile", *["guard_pass", "idle_pass", "sweep", "pool_pass"] * 2]
