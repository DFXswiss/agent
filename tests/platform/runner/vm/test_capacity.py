"""U-RUN-vm capacity: slot formulas, heartbeat object, low-capacity flag and LRU choice (05-runner.md section 3.4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agent_runner.vm import capacity
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
