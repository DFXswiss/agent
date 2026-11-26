"""The device's one admission for every kind of VM (`agent_runner.admission`; 05-runner.md section 3.8,
16-ci-runners.md section 6.6): only what is physically free is admitted, no fixed share for any kind, and when kinds
compete the one with the fewest active instances goes first, on a tie the older request."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from types import SimpleNamespace
from typing import Any

from agent_runner import admission
from agent_runner.admission import CI, MACOS_VMS_MAX, SESSION, WORK, Admission, Demand, Device, Ticket, Verdict

VM = 8192


async def idle() -> float:
    return 0.0


def counting(value: int) -> Callable[[], Awaitable[int]]:
    async def count() -> int:
        return value

    return count


def gate_at(now: list[float], **active: int) -> Admission:
    gate = Admission(cpu_load=idle, clock=lambda: now[0])
    for key, value in active.items():
        gate.counters[key] = counting(value)
    return gate


def judge(gate: Admission, demand: Demand, device: Device, own: Ticket | None = None) -> Verdict:
    return asyncio.run(gate.judge(demand, device, own))


def test_the_example_of_the_client_the_kind_with_fewer_active_goes_first() -> None:
    """80 % of the device run CI; a new session and a new CI runner wait for the one place that becomes free: the
    session goes first (no session is active, eight CI VMs are), even though the CI request came first."""
    now = [100.0]
    gate = gate_at(now, session=0, ci=8)

    async def body() -> tuple[Verdict, Verdict]:
        async with gate.waits(CI, VM) as ci:
            now[0] += 1
            async with gate.waits(SESSION, VM) as session:
                place = Device(free_mib=VM)  # room for one more VM
                return await gate.judge(Demand(CI, VM), place, ci), await gate.judge(
                    Demand(SESSION, VM), place, session
                )

    assert asyncio.run(body()) == (Verdict(False, "turn"), Verdict(True))


def test_one_kind_alone_may_fill_the_device() -> None:
    """No fixed share, CI included: with only CI demand, CI takes everything that is free."""
    gate = gate_at([0.0], ci=40)
    assert judge(gate, Demand(CI, 120_000), Device(free_mib=120_000)) == Verdict(True)
    assert judge(gate, Demand(WORK, 120_000), Device(free_mib=120_000)) == Verdict(True)
    assert judge(gate, Demand(CI, 120_001), Device(free_mib=120_000)) == Verdict(False, "memory")


def test_on_a_tie_the_older_request_goes_first_and_room_for_both_admits_both() -> None:
    now = [0.0]
    gate = gate_at(now, session=2, work=2)

    async def body() -> list[Verdict]:
        async with gate.waits(WORK, VM) as work:
            now[0] += 5
            async with gate.waits(SESSION, VM) as session:
                one = Device(free_mib=VM)
                two = Device(free_mib=2 * VM)
                return [
                    await gate.judge(Demand(SESSION, VM), one, session),  # the work request is older
                    await gate.judge(Demand(WORK, VM), one, work),
                    await gate.judge(Demand(SESSION, VM), two, session),  # room for both: nobody waits for nothing
                ]

    assert asyncio.run(body()) == [Verdict(False, "turn"), Verdict(True), Verdict(True)]
    assert gate.tickets == []


def test_within_ci_the_same_rule_holds_between_organisations_and_classes() -> None:
    now = [0.0]
    gate = gate_at(now, ci=5, **{"ci/org-a": 4, "ci/org-b": 1})

    async def body() -> tuple[Verdict, Verdict, Verdict]:
        async with gate.waits(CI, VM, group="ci/org-a") as first:
            now[0] += 1
            async with gate.waits(CI, VM, group="ci/org-b") as second:
                place = Device(free_mib=VM)
                return (
                    await gate.judge(Demand(CI, VM, group="ci/org-a"), place, first),
                    await gate.judge(Demand(CI, VM, group="ci/org-b"), place, second),
                    await gate.judge(Demand(CI, VM), place),  # no group: the queue of the kind keeps the order
                )

    assert asyncio.run(body()) == (Verdict(False, "turn"), Verdict(True), Verdict(True))


def test_only_what_is_physically_free_is_admitted() -> None:
    gate = gate_at([0.0])
    assert judge(gate, Demand(WORK, 1, disk_gib=50), Device(free_mib=8, disk_free_gib=49)) == Verdict(False, "disk")
    assert judge(gate, Demand(SESSION, 1, disk_gib=50), Device(free_mib=8, disk_free_gib=50)).admitted
    assert judge(gate, Demand(CI, 1, macos=True), Device(free_mib=8, macos_vms=MACOS_VMS_MAX - 1)).admitted
    full = Device(free_mib=8, macos_vms=MACOS_VMS_MAX)
    assert judge(gate, Demand(CI, 1, macos=True), full) == Verdict(False, "macos")
    assert MACOS_VMS_MAX == 2

    async def busy() -> float:
        return 0.9

    gate.cpu_load, gate.cpu_load_max = busy, 0.85
    assert judge(gate, Demand(WORK, 1), Device(free_mib=8)) == Verdict(False, "cpu")


class Store:
    """Records the statements of the mirror; `broken` makes the connection fail."""

    def __init__(self, *, broken: bool = False) -> None:
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self.broken = broken

    @contextlib.asynccontextmanager
    async def connection(self) -> AsyncIterator[Any]:
        if self.broken:
            raise OSError("down")

        async def execute(query: str, params: tuple[Any, ...] = ()) -> None:
            self.statements.append((query.split(maxsplit=1)[0], params))

        yield SimpleNamespace(execute=execute)


def test_a_refused_session_keeps_its_age_until_it_starts_or_its_hold_ends() -> None:
    now = [10.0]
    gate = gate_at(now, session=0, work=3)
    store = Store()
    gate.store = store

    async def body() -> list[Any]:
        await gate.refused(SESSION, VM)
        first = gate.hold(SESSION)
        now[0] += 60
        await gate.refused(SESSION, VM)  # the hub's next attempt is refused again: still the first refusal's age
        held = gate.hold(SESSION)
        ahead = await gate.ahead_mib(Demand(WORK, VM))  # a new work VM leaves the session its room
        summary = await gate.summary()
        await gate.started(SESSION)
        await gate.started(SESSION)  # nothing held any more
        await gate.refused(SESSION, VM)
        now[0] += admission.REFUSAL_HOLD_S
        return [first is held, held.since if held else None, ahead, summary, gate.hold(SESSION), await gate.waiting()]

    found = asyncio.run(body())
    assert found == [True, 10.0, VM, {SESSION: (0, 1), WORK: (3, 0), CI: (0, 0)}, None, {}]
    assert [statement for statement, _params in store.statements].count("INSERT") == 3
    gate.store = Store(broken=True)
    asyncio.run(gate.refused(SESSION, VM))  # the mirror is for display only
    asyncio.run(Admission(cpu_load=idle).mirror())  # no store: nothing to mirror


def test_one_admission_per_device_with_its_cpu_bound() -> None:
    context = SimpleNamespace(config=SimpleNamespace(cpu_load_max_percent=70))
    gate = admission.of(context)
    assert admission.of(context) is gate
    assert gate.cpu_load_max == 0.7
    assert gate.pending() == 0
    # without the setting about 1-2 cores stay free for macOS, the runner and the VMs' input and output
    assert admission.cpu_load_max(None, lambda: 16) == 14 / 16
    assert admission.of(SimpleNamespace(config=SimpleNamespace(cpu_load_max_percent=None))).cpu_load_max < 1
    assert asyncio.run(admission.host_cpu_load()) >= 0.0
