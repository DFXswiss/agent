"""U-RUN-vm retire hooks (`vm.extensions`, KOR-33 review no. 2): before a running VM is stopped or deleted the
hooks run (bounded in time, failures logged), and a stopped VM a hook still needs is restarted instead of replaced by a
clone of a newer golden."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from agent_runner.errors import RunnerError
from agent_runner.vm.extensions import Retire, RetireHook, retire_hooks
from agent_runner.vm.lifecycle import VmManager
from agent_runner.vm.records import VmRow

from .guests import GOLDEN, ORG, WG_A, Harness
from .test_lifecycle import settings, stop_vm_of


class Hooks:
    """A recording hook: `flush` notes the VM and its state at that moment, `pending` answers `keep`."""

    def __init__(self, harness: Harness, *, keep: bool = False) -> None:
        self.harness = harness
        self.keep = keep
        self.flushed: list[tuple[str, str]] = []
        self.asked: list[str] = []

    async def flush(self, context: Any, row: VmRow) -> None:
        assert context == "ctx"
        state = self.harness.sql("SELECT state FROM runner.vm WHERE id = %s", (row.id,))[0]["state"]
        self.flushed.append((row.id, state))

    async def pending(self, context: Any, row: VmRow) -> bool:
        self.asked.append(row.id)
        return self.keep

    def retire(self, **changes: Any) -> Retire:
        hook = RetireHook("test.hook", self.flush, self.pending)
        return Retire("ctx", [hook], **changes)


def test_stop_and_delete_run_the_hooks_while_the_vm_still_runs(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=0)
    hooks = Hooks(harness)

    async def body(manager: VmManager) -> Any:
        manager.retire = hooks.retire()
        row = await manager.ensure_vm(ORG, WG_A)
        stopped = await manager.stop_vm(row.id)
        again = await manager.ensure_vm(ORG, WG_A)
        deleted = await manager.delete_workgroup(WG_A)
        return row, stopped, again, deleted

    row, stopped, again, deleted = harness.run(body)
    assert stopped is not None
    assert stopped.state == "stopped"
    assert deleted is not None
    assert deleted.state == "deleted"
    # each time before the VM changed: the export still reaches a running guest
    assert hooks.flushed == [(row.id, "running"), (again.id, "running")]


def test_deleting_a_stopped_vm_runs_no_hook(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=0)
    stop_vm_of(harness)
    hooks = Hooks(harness)

    async def body(manager: VmManager) -> Any:
        manager.retire = hooks.retire()
        return await manager.delete_workgroup(WG_A)

    assert harness.run(body).state == "deleted"
    assert hooks.flushed == []


def test_a_stopped_vm_a_hook_needs_is_restarted_instead_of_replaced(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=0)
    stopped = stop_vm_of(harness)
    harness.sql("UPDATE runner.golden SET state = 'retired'")
    harness.add_golden("g2", minute=5)
    keeping = Hooks(harness, keep=True)

    async def kept(manager: VmManager) -> Any:
        manager.retire = keeping.retire()
        row = await manager.ensure_vm(ORG, WG_A)
        return row, await manager.stop_vm(row.id)

    row, _ = harness.run(kept)
    assert (row.id, row.golden_version) == (stopped.id, GOLDEN)
    assert keeping.asked == [stopped.id]
    # nothing due any more: the next start replaces it
    releasing = Hooks(harness)

    async def replaced(manager: VmManager) -> Any:
        manager.retire = releasing.retire()
        return await manager.ensure_vm(ORG, WG_A)

    fresh = harness.run(replaced)
    assert fresh.id != stopped.id
    assert fresh.golden_version == "g2"


def test_failing_and_slow_hooks_never_block_the_vm(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=0)
    logs: list[tuple[str, dict[str, Any]]] = []

    async def boom(_context: Any, _row: VmRow) -> None:
        raise RuntimeError("boom")

    async def slow(_context: Any, _row: VmRow) -> None:
        await asyncio.sleep(30)

    async def broken_pending(_context: Any, _row: VmRow) -> bool:
        raise RuntimeError("boom")

    async def body(manager: VmManager) -> Any:
        hooks = [RetireHook("a.boom", boom, broken_pending), RetireHook("b.slow", slow, broken_pending)]
        manager.retire = Retire("ctx", hooks, lambda name, fields: logs.append((name, fields)), wait_s=0.05)
        row = await manager.ensure_vm(ORG, WG_A)
        stopped = await manager.stop_vm(row.id)
        assert stopped is not None
        return row, await manager.retire.pending(stopped)

    row, pending = harness.run(body)
    assert pending is False
    names = [name for name, _ in logs]
    assert ("vm.retire_failed", {"vm_id": row.id, "hook": "a.boom", "error": "RuntimeError"}) in logs
    assert ("vm.retire_timeout", {"vm_id": row.id, "hooks": 1}) in logs
    assert names.count("vm.retire_failed") == 3  # the flush, then both pending answers


def test_without_hooks_nothing_runs() -> None:
    async def body() -> bool:
        retire = Retire()
        await retire.flush(None)  # type: ignore[arg-type]
        return await retire.pending(None)  # type: ignore[arg-type]

    assert asyncio.run(body()) is False
    assert "contexts.checkpoint" in [hook.name for hook in retire_hooks()]

    async def failing(_context: Any, _row: Any) -> bool:
        raise RuntimeError("boom")

    async def no_flush(_context: Any, _row: Any) -> None:
        return None

    quiet = Retire("ctx", [RetireHook("a.hook", no_flush, failing)])  # the default log drops the line
    assert asyncio.run(quiet.pending(SimpleNamespace(id="vm"))) is False  # type: ignore[arg-type]


GOOD = (
    "from agent_runner.vm.extensions import RetireHook\n"
    "async def flush(context, row):\n    return None\n"
    "async def pending(context, row):\n    return False\n"
)


@pytest.mark.parametrize(
    ("files", "code"),
    [
        ({"a/vm_retire.py": "RETIRE_HOOKS = 'x'\n"}, "registry_invalid"),
        ({"a/vm_retire.py": "RETIRE_HOOKS = [1]\n"}, "registry_invalid"),
        ({"a/vm_retire.py": GOOD + "RETIRE_HOOKS = [RetireHook('Bad Name', flush, pending)]\n"}, "registry_invalid"),
        ({"a/vm_retire.py": GOOD + "RETIRE_HOOKS = [RetireHook('x', 1, pending)]\n"}, "registry_invalid"),
        ({"a/vm_retire.py": GOOD + "RETIRE_HOOKS = [RetireHook('x', flush, 1)]\n"}, "registry_invalid"),
        (
            {"a/vm_retire.py": GOOD + "RETIRE_HOOKS = [RetireHook('x', flush, pending)] * 2\n"},
            "registry_duplicate",
        ),
    ],
)
def test_invalid_registrations(area_package: Callable[[dict[str, str]], str], files: dict[str, str], code: str) -> None:
    package = area_package(files)
    with pytest.raises(RunnerError) as caught:
        retire_hooks(package)
    assert caught.value.code == code


def test_a_valid_registration(area_package: Callable[[dict[str, str]], str]) -> None:
    package = area_package({"a/vm_retire.py": GOOD + "RETIRE_HOOKS = [RetireHook('a.hook', flush, pending)]\n"})
    assert [hook.name for hook in retire_hooks(package)] == ["a.hook"]
