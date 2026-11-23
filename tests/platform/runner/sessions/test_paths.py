"""U-RUN-sessions: every start path of 05-runner.md section 6.1 step 1 with the real `VmManager` of VM-02.

Lima is the fake of `vm/guests.py` whose instances run real guest control processes (GST-01): the VM manager
clones, starts, assigns and restarts them, and step 2 of the start asks the real guest for its health. The session
calls of steps 4-5 (`POST /v1/sessions`, credential) go to the fake guest, because the guest's session sandbox needs
Linux (covered by the integration run on the runner device).
"""

from __future__ import annotations

import asyncio
from types import TracebackType
from typing import Any

from agent_runner.guestlink.client import GuestClient, Health
from agent_runner.sessions.start import Starter
from agent_runner.vm.lifecycle import VmManager
from agent_runner.vm.records import VmRow

from ..reconcile.rig import Rig, standard_world
from .kit import FakeGuest, desired, kit


class Composite:
    """Health from the real guest, every session call from the fake one."""

    def __init__(self, real: GuestClient, fake: FakeGuest) -> None:
        self.real = real
        self.fake = fake

    async def __aenter__(self) -> Composite:
        return self

    async def __aexit__(
        self, kind: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        await self.real.aclose()

    async def health(self, *, timeout: float | None = None) -> Health:
        return await self.real.health(timeout=timeout)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.fake, name)


class PathVms:
    def __init__(self, manager: VmManager, fake: FakeGuest) -> None:
        self.manager = manager
        self.fake = fake

    async def ensure_vm(self, org_id: str, wg_id: str) -> VmRow:
        return await self.manager.ensure_vm(org_id, wg_id)

    async def client(self, row: VmRow) -> Any:
        return Composite(await self.manager.client(row), self.fake)

    async def touch(self, vm_id: str) -> None:
        await self.manager.touch(vm_id)

    async def wait_reconciled(self) -> None:
        await self.manager.wait_reconciled()


def run(rig: Rig, harness: Any, body: Any) -> None:
    async def main() -> None:
        async with rig.env(standard_world()) as env:
            harness.add_golden()
            manager = VmManager(harness.env(env.pool))
            await manager.reconcile()
            found = await kit(env)
            found.session_env.vms = PathVms(manager, found.guest)
            await body(env, found, manager, Starter(found.session_env))

    asyncio.run(main())


def test_pool_path_then_running_path(rig: Rig, harness: Any) -> None:
    async def body(env: Any, found: Any, manager: VmManager, starter: Starter) -> None:
        await manager.refill()
        first = await starter.start(desired(1))
        # once red in a loaded series, not reproduced since (FIX-01): the VM log and the runner log name the cause
        assert (first.started, first.path) == (True, "pool"), (harness.logs, found.env.log())
        second = await starter.start(desired(2))
        assert (second.started, second.path) == (True, "running")
        local = await found.stored(desired(1).session_id)
        assert local.vm_id == (await found.stored(desired(2).session_id)).vm_id
        transitions = await found.of_kind("session_transition")
        assert transitions[1]["agent_version"] == "2.1.330"

    run(rig, harness, body)


def test_clone_path_with_an_empty_pool(rig: Rig, harness: Any) -> None:
    async def body(env: Any, found: Any, manager: VmManager, starter: Starter) -> None:
        report = await starter.start(desired(1))
        assert (report.started, report.path) == (True, "clone")
        assert [argv[0] for argv, _ in harness.fake.calls].count("clone") == 1

    run(rig, harness, body)


def test_restart_path_for_a_stopped_vm_and_golden_replacement(rig: Rig, harness: Any) -> None:
    async def body(env: Any, found: Any, manager: VmManager, starter: Starter) -> None:
        first = await starter.start(desired(1))
        assert first.path == "clone"
        vm_id = (await found.stored(desired(1).session_id)).vm_id
        assert vm_id is not None
        await manager.stop_vm(vm_id)
        again = await starter.start(desired(2))
        assert (again.started, again.path) == (True, "restart")
        assert (await found.stored(desired(2).session_id)).vm_id == vm_id
        # a newer golden: the stopped VM is replaced by a fresh clone of it (no suspended session)
        await manager.stop_vm(vm_id)
        harness.add_golden("g2", minute=5)
        replaced = await starter.start(desired(3))
        assert (replaced.started, replaced.path) == (True, "clone")
        assert (await found.stored(desired(3).session_id)).vm_id != vm_id

    run(rig, harness, body)


def test_unhealthy_guest_after_the_vm_fails_with_guest_unreachable(rig: Rig, harness: Any) -> None:
    async def body(env: Any, found: Any, manager: VmManager, starter: Starter) -> None:
        first = await starter.start(desired(1))
        assert first.started
        # the guest dies while the VM row still says running: step 2 cannot reach it
        for name in list(harness.fake.farm.services):
            harness.fake.farm.halt(name)
        report = await starter.start(desired(2))
        assert (report.started, report.reason, report.path) == (False, "guest_unreachable", "running")

    run(rig, harness, body)
