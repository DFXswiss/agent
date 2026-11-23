"""U-RUN-pool-stop: a daemon stop during a VM start leaves no instance outside the register (KOR-03, AUD-01 A-06).

`limactl clone/start` run in worker threads; cancelling the awaiting task does not stop them. These tests hold a
`limactl` call of the fake (real guest control processes, `guests.py`) in its thread, stop or cancel the runner side
meanwhile and check that afterwards no `<prefix>arp-…` instance exists without a live `runner.vm` row, that the
deletion never overlaps the running call, and that no further clone is started once the daemon is stopping.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest

from agent_runner.errors import RunnerError
from agent_runner.vm.lifecycle import VmManager
from agent_runner.vm.records import GoldenRef

from .guests import GOLDEN, ORG, PREFIX, WG_A, Harness

GOLDEN_REF = GoldenRef(version=GOLDEN, lima_name=f"{PREFIX}golden-{GOLDEN}")
HOLD_S = 10.0


class Gate:
    """Holds the first `limactl <op>` of the fake in its worker thread until `release()`; records the call order."""

    def __init__(self, harness: Harness, op: str) -> None:
        self.op = op
        self.entered = threading.Event()
        self.released = threading.Event()
        self.order: list[str] = []
        self._held = False
        harness.fake.hook = self.hook

    def hook(self, argv: list[str]) -> None:
        if argv[0] == self.op and not self._held:
            self._held = True
            self.entered.set()
            assert self.released.wait(HOLD_S), "the held limactl call was never released"
            self.order.append(f"{self.op}-returned")
        elif argv[0] == "delete":
            self.order.append("delete")

    def release(self) -> None:
        self.released.set()

    async def wait_entered(self) -> None:
        for _ in range(int(HOLD_S / 0.01)):
            if self.entered.is_set():
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"limactl {self.op} was never called")


def unregistered(harness: Harness) -> list[str]:
    """Own pool instances without a live row (what an interrupted start must never leave behind)."""
    known = {row["lima_name"] for row in harness.live()}
    return sorted(name for name in harness.fake.instances if name.startswith(f"{PREFIX}arp-") and name not in known)


@pytest.mark.parametrize("op", ["clone", "start"])
def test_cancelled_pool_fill_deletes_the_instance_after_the_running_call(harness: Harness, op: str) -> None:
    harness.add_golden()
    gate = Gate(harness, op)

    async def body(manager: VmManager) -> Any:
        fill = asyncio.ensure_future(manager.pool.fill_one(GOLDEN_REF))
        await gate.wait_entered()
        fill.cancel()
        # The task stays in its cleanup while the call runs: the deletion waits for it.
        await asyncio.sleep(0.05)
        assert not fill.done()
        assert gate.order == []
        threading.Timer(0.05, gate.release).start()
        with pytest.raises(asyncio.CancelledError):
            await fill
        return manager.pool.filling

    assert harness.run(body) == 0
    assert gate.order == [f"{op}-returned", "delete"]
    assert unregistered(harness) == []
    assert harness.vms() == []
    assert list(harness.tokens_dir.glob("*.token")) == []
    assert ("vm.start_abandoned", {"lima_name": harness.fake.calls[-1][0][-1]}) in harness.logs


def test_cancelled_hot_path_clone_deletes_instance_and_row(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = harness.settings.__class__(**{**vars(harness.settings), "pool_size": 0})
    gate = Gate(harness, "start")

    async def body(manager: VmManager) -> Any:
        ensure = asyncio.ensure_future(manager.ensure_vm(ORG, WG_A))
        await gate.wait_entered()
        ensure.cancel()
        threading.Timer(0.05, gate.release).start()
        with pytest.raises(asyncio.CancelledError):
            await ensure

    harness.run(body)
    assert gate.order[0] == "start-returned"
    assert "delete" in gate.order
    assert unregistered(harness) == []
    assert harness.live() == []


def test_second_cancellation_does_not_stop_the_deletion(harness: Harness) -> None:
    harness.add_golden()
    gate = Gate(harness, "start")

    async def body(manager: VmManager) -> Any:
        fill = asyncio.ensure_future(manager.pool.fill_one(GOLDEN_REF))
        await gate.wait_entered()
        fill.cancel()
        await asyncio.sleep(0.05)
        fill.cancel()
        with pytest.raises(asyncio.CancelledError):
            await fill
        # The task is gone; the deletion still waits for the start in the worker thread.
        assert gate.order == []
        gate.release()

    # asyncio.run joins the worker threads before it returns, like the daemon's exit.
    harness.run(body)
    assert gate.order == ["start-returned", "delete"]
    assert unregistered(harness) == []


def test_second_cancellation_of_a_hot_path_clone_never_deletes_beside_the_start(harness: Harness) -> None:
    # The supervisor cancels once; asyncio.run cancels a task still running at shutdown a second time. The hot path's
    # own cleanup (row and instance) must wait for the start as well (review KOR-03 M1).
    harness.add_golden()
    harness.settings = harness.settings.__class__(**{**vars(harness.settings), "pool_size": 0})
    gate = Gate(harness, "start")

    async def body(manager: VmManager) -> Any:
        ensure = asyncio.ensure_future(manager.ensure_vm(ORG, WG_A))
        await gate.wait_entered()
        ensure.cancel()
        await asyncio.sleep(0.05)
        ensure.cancel()
        await asyncio.sleep(0.05)
        # Both cleanups (the abandoned provisioning's and the hot path's own) wait for the start.
        assert gate.order == []
        threading.Timer(0.05, gate.release).start()
        with pytest.raises(asyncio.CancelledError):
            await ensure

    harness.run(body)
    assert gate.order[0] == "start-returned"
    assert set(gate.order[1:]) == {"delete"}
    assert unregistered(harness) == []
    assert manager_guards_empty(harness)


def manager_guards_empty(harness: Harness) -> bool:
    return not any(name.startswith(f"{PREFIX}arp-") for name in harness.fake.instances)


def test_cancelled_while_waiting_for_the_guest_deletes_the_instance(harness: Harness) -> None:
    harness.add_golden()

    def no_guest(argv: list[str]) -> None:
        if argv[0] == "clone":
            harness.fake.dead.add(argv[-1])

    harness.fake.hook = no_guest
    waiting = asyncio.Event()

    async def body(manager: VmManager) -> Any:
        async def hang(_seconds: float) -> None:
            waiting.set()
            await asyncio.Event().wait()

        manager.env.sleep = hang
        fill = asyncio.ensure_future(manager.pool.fill_one(GOLDEN_REF))
        await waiting.wait()
        fill.cancel()
        with pytest.raises(asyncio.CancelledError):
            await fill

    harness.run(body)
    assert len(harness.fake.starts) == 1
    assert harness.fake.ops()[-1] == "delete"
    assert unregistered(harness) == []


def test_no_retry_and_no_new_clone_once_stopping(harness: Harness) -> None:
    harness.add_golden()
    stopping = threading.Event()

    def stop_signal(argv: list[str]) -> None:
        # SIGTERM to the daemon's process group: the running `limactl start` fails and the daemon is stopping.
        if argv[0] == "start":
            stopping.set()

    harness.fake.hook = stop_signal
    harness.fake.fail["start"] = (1, "", "FATA[0001] signal: terminated\n")

    async def body(manager: VmManager) -> Any:
        manager.env.stopping = stopping.is_set
        with pytest.raises(RunnerError) as failed:
            await manager.pool.fill_one(GOLDEN_REF)
        return failed.value.code, await manager.pool.missing(GOLDEN_REF), await manager.refill()

    assert harness.run(body) == ("vm_start_failed", 0, 0)
    assert harness.fake.ops().count("clone") == 1
    assert unregistered(harness) == []
    assert harness.vms() == []
    skipped = [fields for name, fields in harness.logs if name == "vm.start_skipped"]
    assert [fields["attempt"] for fields in skipped] == [2]


def test_hot_path_refuses_to_clone_while_stopping(harness: Harness) -> None:
    harness.add_golden()

    async def body(manager: VmManager) -> Any:
        manager.env.stopping = lambda: True
        with pytest.raises(RunnerError) as failed:
            await manager.ensure_vm(ORG, WG_A)
        return failed.value.code

    assert harness.run(body) == "vm_start_failed"
    assert "clone" not in harness.fake.ops()
    assert harness.live() == []
