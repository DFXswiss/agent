"""U-RUN-workvm session state: observer `workvm.session_end` of the guest's `session.state` records (ADR-059
decision 8; 13-work-vm.md section 2).

A final state hands the session to `WorkVms.session_ended` in the background; a state that is not final, or a device
without work VMs, does nothing. The daemon's `WorkVms` is a subclass that records the call instead of tearing down.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agent_runner.sessions.extensions import StateObserver
from agent_runner.sessions.records import FINAL, LocalSession
from agent_runner.workvm import service, session_state
from agent_runner.workvm.manager import Deps, WorkVms

SESSION = cast(LocalSession, SimpleNamespace(id="0192f3a4-0000-7000-8000-00000000000a"))
STATES = ["created", "starting", "working", "waiting", "stopping", "stopped", "failed", None]


class RecordingWorkVms(WorkVms):
    def __init__(self, gate: asyncio.Event | None = None) -> None:
        super().__init__(cast(Deps, SimpleNamespace()))
        self.ended: list[LocalSession] = []
        self.gate = gate

    async def session_ended(self, session: LocalSession) -> None:
        if self.gate is not None:
            await self.gate.wait()
        self.ended.append(session)


def journal(context: Any) -> Any:
    return SimpleNamespace(env=SimpleNamespace(context=context))


def observe(context: Any, body: dict[str, Any]) -> list[LocalSession]:
    """Run the observer and let its background task finish; the sessions handed to `session_ended`."""

    async def run() -> None:
        await session_state.observe(journal(context), cast(Any, None), SESSION, body)
        while session_state.PENDING:
            await asyncio.gather(*session_state.PENDING)

    asyncio.run(run())
    workvms = service.existing(context)
    return [] if workvms is None else cast(RecordingWorkVms, workvms).ended


def test_observer_is_registered() -> None:
    assert [StateObserver("workvm.session_end", session_state.observe)] == session_state.OBSERVERS


def test_final_states_are_the_session_finals() -> None:
    assert {"stopped", "failed"} == FINAL


@pytest.mark.parametrize("state", STATES)
def test_only_a_final_state_ends_the_session_work_vms(state: str | None) -> None:
    context = SimpleNamespace()
    service.install(context, RecordingWorkVms())
    ended = observe(context, {"state": state} if state is not None else {})
    assert ended == ([SESSION] if state in FINAL else [])
    assert set() == session_state.PENDING


@pytest.mark.parametrize("state", sorted(FINAL))
def test_without_work_vms_nothing_happens(state: str) -> None:
    context = SimpleNamespace()
    assert observe(context, {"state": state}) == []
    context._workvms = "not work vms"
    assert observe(context, {"state": state}) == []
    assert set() == session_state.PENDING


def test_the_teardown_runs_in_the_background() -> None:
    async def run() -> tuple[int, list[LocalSession], list[LocalSession], int]:
        gate = asyncio.Event()
        workvms = RecordingWorkVms(gate)
        context = SimpleNamespace()
        service.install(context, workvms)
        await session_state.observe(journal(context), cast(Any, None), SESSION, {"state": "stopped"})
        held = len(session_state.PENDING)
        await asyncio.sleep(0)
        before = list(workvms.ended)
        gate.set()
        await asyncio.gather(*session_state.PENDING)
        return held, before, workvms.ended, len(session_state.PENDING)

    held, before, after, left = asyncio.run(run())
    assert (held, before, after, left) == (1, [], [SESSION], 0)
