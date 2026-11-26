"""U-RUN-vm capacity: slot formulas, heartbeat object, low-capacity flag and LRU choice (05-runner.md section 3.4)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from agent_runner.vm import capacity, pool
from agent_runner.vm.capacity import Candidate, compute, lru_victim

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


def test_budget_and_slot_formula() -> None:
    # 512 GiB host, 32 GiB reserve, 8 GiB per VM: floor(491520 / 8192) slots.
    result = compute(physical_mib=512 * 1024, reserve_mib=32768, vm_memory_mib=8192, vms=[])
    assert (result.budget_mib, result.slots_total, result.slots_free, result.used_mib) == (491520, 60, 60, 0)
    # The slot count is rounded down.
    assert compute(physical_mib=32768 + 3 * 8192 + 8191, reserve_mib=32768, vm_memory_mib=8192, vms=[]).slots_total == 3


def test_slot_states_and_stopped_vms() -> None:
    vms = [("pool", 8192), ("assigning", 8192), ("running", 4096), ("stopping", 8192), ("stopped", 8192)]
    result = compute(physical_mib=32768 + 10 * 8192, reserve_mib=32768, vm_memory_mib=8192, vms=vms)
    assert result.slots_total == 10
    # Stopped VMs give their RAM back (Spike A): four VMs occupy slots.
    assert result.slots_free == 6
    assert result.used_mib == 8192 * 3 + 4096
    assert result.pool_ready == 1
    assert result.heartbeat() == {
        "vm_memory_budget_mib": 81920,
        "vm_memory_used_mib": 28672,
        "vm_slots_total": 10,
        "vm_slots_free": 6,
        "pool_ready": 1,
    }


def test_in_flight_clones_count_and_free_never_negative() -> None:
    result = compute(
        physical_mib=32768 + 2 * 8192, reserve_mib=32768, vm_memory_mib=8192, vms=[("running", 8192)], in_flight=3
    )
    assert result.slots_free == 0
    assert result.used_mib == 4 * 8192
    tiny = compute(physical_mib=1024, reserve_mib=32768, vm_memory_mib=8192, vms=[])
    assert (tiny.budget_mib, tiny.slots_total, tiny.slots_free) == (0, 0, 0)


def test_capacity_low_threshold() -> None:
    def low(free: int, total: int) -> bool:
        return capacity.Capacity(
            budget_mib=0, used_mib=0, slots_total=total, slots_free=free, pool_ready=0
        ).capacity_low

    assert low(0, 0) is True
    assert low(1, 10) is False
    assert low(0, 10) is True
    assert low(5, 56) is True
    assert low(6, 56) is False


def test_physical_memory_from_sysconf() -> None:
    values = {"SC_PHYS_PAGES": 131072, "SC_PAGE_SIZE": 16384}
    assert capacity.physical_memory_mib(values.__getitem__) == 2048
    assert capacity.physical_memory_mib() > 0


def candidate(vm_id: str, wg: str, minutes_ago: int, active: int = 0, suspended_ago: int | None = None) -> Candidate:
    return Candidate(
        vm_id=vm_id,
        wg_id=wg,
        last_used=NOW - timedelta(minutes=minutes_ago),
        active_sessions=active,
        newest_suspended=None if suspended_ago is None else NOW - timedelta(minutes=suspended_ago),
    )


def test_lru_victim_rules() -> None:
    items = [
        candidate("vm-00000001", "wg-a", 50),  # the requesting workgroup itself
        candidate("vm-00000002", "wg-b", 40, active=1),  # busy
        candidate("vm-00000003", "wg-c", 30, suspended_ago=10),  # suspended session younger than the idle time
        candidate("vm-00000004", "wg-d", 5, suspended_ago=31),  # suspended long ago
        candidate("vm-00000005", "wg-e", 20),
    ]
    chosen = lru_victim(items, exclude_wg="wg-a", now=NOW, idle_seconds=1800)
    assert chosen is not None
    assert chosen.vm_id == "vm-00000005"
    assert lru_victim(items[:3], exclude_wg="wg-a", now=NOW, idle_seconds=1800) is None
    # Ties are broken by the VM ID.
    tied = [candidate("vm-0000000b", "wg-x", 9), candidate("vm-0000000a", "wg-y", 9)]
    assert lru_victim(tied, exclude_wg=None, now=NOW, idle_seconds=1800) == tied[1]


def test_the_budget_alone_gives_the_slots_and_other_areas_hold_slots_and_memory() -> None:
    """ADR-075 addendum 2: no fixed number of VMs, the memory budget alone; the VMs of other areas count against it."""
    host = 32768 + 10 * 8192
    found = compute(physical_mib=host, reserve_mib=32768, vm_memory_mib=8192, vms=[("running", 8192)])
    assert (found.slots_total, found.slots_free) == (10, 9)
    # two work VMs of 4 GiB: two slots by count, one by memory -> two slots, 8 GiB used
    other = capacity.Usage(vms=2, memory_mib=8192)
    shared = compute(physical_mib=host, reserve_mib=32768, vm_memory_mib=8192, vms=[], other=other)
    assert (shared.slots_free, shared.used_mib, shared.other_vms) == (8, 8192, 2)
    # one large work VM of 20 GiB: three slots by memory
    large = compute(physical_mib=host, reserve_mib=32768, vm_memory_mib=8192, vms=[], other=capacity.Usage(1, 20480))
    assert large.slots_free == 7
    assert capacity.Usage(1, 2) + capacity.Usage(3, 4) == capacity.Usage(4, 6)
    assert capacity.Usage() == capacity.NO_USAGE


def test_yielding_usage_takes_no_slot_of_a_workgroup_vm() -> None:
    """ADR-059 addendum 4: work VMs that give way on demand leave `slots_free` alone; `slots_free_now` counts them."""
    host = 32768 + 4 * 8192
    work = capacity.Usage(vms=3, memory_mib=3 * 8192, yielding_vms=2, yielding_mib=2 * 8192)  # one run VM is firm
    found = compute(physical_mib=host, reserve_mib=32768, vm_memory_mib=8192, vms=[("running", 8192)], other=work)
    assert (found.slots_free, found.slots_yielding, found.slots_free_now) == (2, 2, 0)
    assert (found.used_mib, found.other_vms) == (4 * 8192, 3)
    full = compute(physical_mib=host, reserve_mib=32768, vm_memory_mib=8192, vms=[("running", 8192)] * 4, other=work)
    assert (full.slots_free, full.slots_free_now) == (0, 0)
    assert capacity.Usage(1, 2, 1, 2) + capacity.Usage(3, 4) == capacity.Usage(4, 6, 1, 2)


def test_cpu_load_per_core() -> None:
    assert capacity.cpu_load_per_core(lambda: (6.0, 1.0, 1.0), lambda: 8) == 0.75
    assert capacity.cpu_load_per_core(lambda: (2.0, 1.0, 1.0), lambda: None) == 2.0
    assert capacity.cpu_load_per_core() >= 0.0


def test_own_usage_and_disk_free() -> None:
    assert asyncio.run(capacity.USAGE(None)) == capacity.NO_USAGE
    assert asyncio.run(capacity.RECLAIM(None, 8192)) == 0
    assert asyncio.run(pool._no_reclaim(8192)) == 0  # a `VmEnv` without other areas
    assert asyncio.run(pool._no_usage()) == capacity.NO_USAGE

    def usage(path: str) -> SimpleNamespace:
        if path == "/missing":
            raise FileNotFoundError(path)
        return SimpleNamespace(free=3 * 1024 * 1024 + 5)

    assert capacity.disk_free_mib("/lima", usage) == 3
    assert capacity.disk_free_mib("/missing", usage) is None
    assert isinstance(capacity.disk_free_mib("/"), int)
