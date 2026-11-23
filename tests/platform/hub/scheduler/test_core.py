"""U-HUB-scheduler (core): the deterministic order, skipping, estimates and the capacity warning of 03-api.md
section 4.6.2 as a table."""

from __future__ import annotations

import itertools
from typing import Any

import pytest

from agent_hub.scheduler.core import (
    Candidate,
    Capacity,
    NoPlacement,
    Placement,
    choose,
    order,
)

A = "01900000-0000-7000-8000-00000000000a"
B = "01900000-0000-7000-8000-00000000000b"
C = "01900000-0000-7000-8000-00000000000c"


def cap(free: int = 4, total: int = 8, budget: int = 65536, used: int = 0, pool: int = 0) -> Capacity:
    return Capacity(
        vm_memory_budget_mib=budget,
        vm_memory_used_mib=used,
        vm_slots_total=total,
        vm_slots_free=free,
        pool_ready=pool,
    )


def dev(device_id: str, *, online: bool = True, vm: str | None = None, **capacity: int) -> Candidate:
    return Candidate(device_id=device_id, online=online, capacity=cap(**capacity), wg_vm=vm)


# (name, candidates, expected device or reason, expected estimate)
TABLE: list[tuple[str, list[Candidate], str, int | None]] = [
    ("level 1 running VM beats more free slots", [dev(A, free=8), dev(B, vm="running", free=1)], B, 2500),
    ("level 2 stopped VM beats free slots", [dev(A, free=8, pool=3), dev(B, vm="stopped", free=1)], B, 6500),
    ("level 1 beats level 2", [dev(A, vm="stopped"), dev(B, vm="running")], B, 2500),
    ("level 3 most free slots", [dev(A, free=2), dev(B, free=5), dev(C, free=3)], B, 8000),
    ("level 3 with a warm pool", [dev(A, free=2, pool=1), dev(B, free=1, pool=5)], A, 3000),
    ("tie on slots: more free memory", [dev(A, free=4, used=60000), dev(B, free=4, used=1000)], B, 8000),
    ("tie on slots and memory: smallest ID", [dev(C, free=4), dev(A, free=4), dev(B, free=4)], A, 8000),
    ("tie between running VMs: more free slots", [dev(A, vm="running", free=1), dev(B, vm="running", free=2)], B, 2500),
    ("tie between running VMs: smallest ID", [dev(B, vm="running"), dev(A, vm="running")], A, 2500),
    ("tie between stopped VMs: more free memory", [dev(A, vm="stopped", used=9), dev(B, vm="stopped")], B, 6500),
    ("no free slot and no VM: skipped", [dev(A, free=0, pool=2), dev(B, free=1)], B, 8000),
    ("no free slot but a running VM: kept", [dev(A, free=0, vm="running"), dev(B, free=8)], A, 2500),
    ("no free slot but a stopped VM: kept", [dev(A, free=0, vm="stopped"), dev(B, free=8)], A, 6500),
    ("no free slot but a VM in another state: kept", [dev(A, free=0, vm="other"), dev(B, free=0)], A, 8000),
    ("offline device is never chosen", [dev(A, online=False, vm="running"), dev(B, free=1)], B, 8000),
    ("only offline devices", [dev(A, online=False), dev(B, online=False, vm="running")], "device_offline", None),
    ("no devices at all", [], "device_offline", None),
    ("every online device skipped", [dev(A, free=0), dev(B, online=False, free=8)], "capacity_exhausted", None),
]


@pytest.mark.parametrize(("name", "candidates", "expected", "estimate"), TABLE, ids=[row[0] for row in TABLE])
def test_scheduler_table(name: str, candidates: list[Candidate], expected: str, estimate: int | None) -> None:
    decision = choose(candidates)
    if estimate is None:
        assert decision == NoPlacement(expected), name
    else:
        assert decision == Placement(device_id=expected, estimate_ms=estimate), name


def test_the_decision_does_not_depend_on_the_input_order() -> None:
    candidates = [dev(A, free=4), dev(B, free=4), dev(C, free=4, vm="stopped"), dev("0" * 8, online=False)]
    decisions = {choose(list(permutation)) for permutation in itertools.permutations(candidates)}
    assert decisions == {Placement(device_id=C, estimate_ms=6500)}
    assert [item.device_id for item in order(reversed(candidates))] == [C, A, B]


@pytest.mark.parametrize(
    ("capacity", "low"),
    [
        ({"vm_slots_total": 10, "vm_slots_free": 1}, False),
        ({"vm_slots_total": 10, "vm_slots_free": 0}, True),
        ({"vm_slots_total": 56, "vm_slots_free": 5}, True),
        ({"vm_slots_total": 56, "vm_slots_free": 6}, False),
        ({"vm_slots_total": 0, "vm_slots_free": 0}, True),
    ],
)
def test_capacity_low_below_ten_percent(capacity: dict[str, int], low: bool) -> None:
    assert Capacity.of(capacity).low is low


@pytest.mark.parametrize(
    "stored",
    [None, [], {"vm_slots_free": True}, {"vm_slots_free": "3"}, {"vm_slots_free": -4}, {"vm_slots_free": 1.5}],
)
def test_malformed_capacity_counts_as_zero(stored: Any) -> None:
    capacity = Capacity.of(stored)
    assert capacity.vm_slots_free == 0
    assert capacity.json() == {
        "vm_memory_budget_mib": 0,
        "vm_memory_used_mib": 0,
        "vm_slots_total": 0,
        "vm_slots_free": 0,
        "pool_ready": 0,
    }


def test_capacity_counters_are_capped_and_memory_never_negative() -> None:
    capacity = Capacity.of({"vm_memory_budget_mib": 2**60, "vm_memory_used_mib": 2**61})
    assert capacity.vm_memory_budget_mib == 2**53 - 1
    assert capacity.free_memory_mib == 0
