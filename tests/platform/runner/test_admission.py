"""The device's one admission for every kind of VM (`agent_runner.admission`; 05-runner.md section 3.8,
16-ci-runners.md section 6.6): free RAM, CPU and disk decide, the rank orders the kinds, only CI has a bound of its
own, and at most two macOS VMs per Mac."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from agent_runner import admission
from agent_runner.admission import CI, MACOS_VMS_MAX, SESSION, WORK, Admission, Cap, Demand, Device, Verdict


def judge(gate: Admission, demand: Demand, device: Device) -> Verdict:
    return asyncio.run(gate.judge(demand, device))


async def idle() -> float:
    return 0.0


def test_the_free_resources_decide_for_the_kinds_of_agent() -> None:
    gate = Admission(cpu_load=idle, cpu_load_max=0.85)
    assert judge(gate, Demand(WORK, 8192), Device(free_mib=8192)) == Verdict(True)
    assert judge(gate, Demand(WORK, 8192), Device(free_mib=8191)) == Verdict(False, "memory")
    assert judge(gate, Demand(WORK, 1, disk_gib=50), Device(free_mib=8, disk_free_gib=49)) == Verdict(False, "disk")
    assert judge(gate, Demand(SESSION, 1, disk_gib=50), Device(free_mib=8, disk_free_gib=50)).admitted

    async def busy() -> float:
        return 0.9

    gate.cpu_load = busy
    assert judge(gate, Demand(WORK, 1), Device(free_mib=8)) == Verdict(False, "cpu")


def test_a_lower_rank_does_not_start_while_a_higher_one_waits() -> None:
    now = [100.0]
    gate = Admission(cpu_load=idle, clock=lambda: now[0])
    roomy = Device(free_mib=1_000_000)
    with gate.waits(WORK):
        assert judge(gate, Demand(CI, 1), roomy) == Verdict(False, "rank")
        assert judge(gate, Demand(WORK, 1), roomy).admitted  # the same rank is FIFO in its own queue
        assert gate.ahead(SESSION) is None
    assert judge(gate, Demand(CI, 1), roomy).admitted
    gate.refused(SESSION)  # a session refused for lack of capacity: the hub places it again
    assert (gate.ahead(WORK), gate.ahead(CI)) == (SESSION, SESSION)
    now[0] += admission.REFUSAL_HOLD_S + 1  # its next attempt did not come back here
    assert gate.ahead(WORK) is None
    gate.refused(SESSION)
    gate.started(SESSION)
    assert gate.ahead(CI) is None
    assert gate.waiting == {}  # nothing left behind


def test_only_ci_has_a_bound_and_at_most_two_macos_vms_run_per_mac() -> None:
    gate = Admission(cpu_load=idle, caps={CI: Cap(memory_share=0.5, cpu_share=0.5)})
    device = Device(free_mib=100_000, budget_mib=64_000, cpus=16, held_mib={CI: 24_000}, held_cpus={CI: 4})
    assert judge(gate, Demand(CI, 8000, cpus=4), device).admitted  # 32 000 of 64 000 MiB, 8 of 16 CPUs
    assert judge(gate, Demand(CI, 8001, cpus=4), device) == Verdict(False, "cap")
    assert judge(gate, Demand(CI, 8000, cpus=5), device) == Verdict(False, "cap")
    assert judge(gate, Demand(WORK, 90_000, cpus=16), device).admitted  # no bound for the kinds of agent
    macos = Device(free_mib=100_000, budget_mib=100_000, cpus=16, macos_vms=MACOS_VMS_MAX - 1)
    assert judge(gate, Demand(CI, 8000, macos=True), macos).admitted
    full = Device(free_mib=100_000, budget_mib=100_000, cpus=16, macos_vms=MACOS_VMS_MAX)
    assert judge(gate, Demand(CI, 8000, macos=True), full) == Verdict(False, "macos")
    assert MACOS_VMS_MAX == 2


def test_one_admission_per_device_with_its_cpu_bound() -> None:
    context = SimpleNamespace(config=SimpleNamespace(cpu_load_max_percent=70))
    gate = admission.of(context)
    assert admission.of(context) is gate
    assert gate.cpu_load_max == 0.7
    assert gate.pending() == 0
    assert asyncio.run(admission.host_cpu_load()) >= 0.0
