"""U-HUB-scheduler (core): the deterministic order, skipping, estimates and the capacity warning of 03-api.md
section 4.6.2 as a table; several VMs per workgroup, limits, booking, keys and a chosen device (ADR-075)."""

from __future__ import annotations

import itertools
from typing import Any

import pytest

from agent_hub.scheduler.core import (
    CATEGORIES,
    Candidate,
    Capacity,
    NoPlacement,
    Placement,
    Vm,
    choose,
    order,
)

A = "01900000-0000-7000-8000-00000000000a"
B = "01900000-0000-7000-8000-00000000000b"
C = "01900000-0000-7000-8000-00000000000c"


def cap(
    *,
    free: int = 4,
    total: int = 8,
    budget: int = 65536,
    used: int = 0,
    pool: int = 0,
    per_vm: int = 0,
    disk: int | None = None,
) -> Capacity:
    return Capacity(
        vm_memory_budget_mib=budget,
        vm_memory_used_mib=used,
        vm_slots_total=total,
        vm_slots_free=free,
        pool_ready=pool,
        sessions_per_vm=per_vm,
        disk_free_mib=disk,
    )


def dev(
    device_id: str,
    *,
    online: bool = True,
    vm: str | None = None,
    vms: tuple[Vm, ...] = (),
    pending: int = 0,
    refused: bool = False,
    keys: bool = True,
    pinned_vm: str | None = None,
    **capacity: Any,
) -> Candidate:
    found = (Vm("vm-0000000a", vm),) if vm is not None else vms
    return Candidate(
        device_id=device_id,
        online=online,
        capacity=cap(**capacity),
        vms=found,
        pending=pending,
        refused=refused,
        keys=keys,
        pinned_vm=pinned_vm,
    )


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
    (
        "after a refusal a stopped VM needs a slot",
        [dev(A, free=0, vm="stopped", refused=True), dev(B, free=1)],
        B,
        8000,
    ),
    ("after a refusal a running VM needs none", [dev(A, free=0, vm="running", refused=True), dev(B, free=8)], A, 2500),
    ("after a refusal a stopped VM with a slot", [dev(A, free=1, vm="stopped", refused=True), dev(B, free=8)], A, 6500),
    (
        "after a refusal another state needs a slot",
        [dev(A, free=0, vm="other", refused=True)],
        "capacity_exhausted",
        None,
    ),
]

V1, V2, V3 = "vm-0000000a", "vm-0000000b", "vm-0000000c"

# ADR-075 section 3: room of the VMs (an older runner's `sessions_per_vm`), pending sessions, new VMs, keys and disk;
# no fixed number of VMs per workgroup (ADR-075 addendum 2)
VM_TABLE: list[tuple[str, list[Candidate], str, int | None]] = [
    ("full running VM: a new VM on the same device", [dev(A, per_vm=2, vms=(Vm(V1, "running", 2),))], A, 8000),
    (
        "full running VM: a device with room wins",
        [dev(A, per_vm=2, vms=(Vm(V1, "running", 2),)), dev(B, per_vm=2, vms=(Vm(V2, "running", 1),), free=1)],
        B,
        2500,
    ),
    (
        "full running VM and no slot: skipped",
        [dev(A, per_vm=2, free=0, vms=(Vm(V1, "running", 2),)), dev(B, free=1)],
        B,
        8000,
    ),
    (
        "pending sessions use the room first",
        [dev(A, per_vm=3, free=0, vms=(Vm(V1, "running", 1),), pending=2)],
        "capacity_exhausted",
        None,
    ),
    ("room left after pending", [dev(A, per_vm=3, free=0, vms=(Vm(V1, "running", 1),), pending=1)], A, 2500),
    (
        "pending beyond the room take a slot: the device with more slots left wins",
        [dev(A, per_vm=2, free=1, vms=(Vm(V1, "running", 2),), pending=1), dev(B, free=1)],
        B,
        8000,
    ),
    (
        "pending beyond the room take the last slot",
        [dev(A, per_vm=2, free=1, vms=(Vm(V1, "running", 2),), pending=2), dev(B, free=1)],
        B,
        8000,
    ),
    ("a stopped VM with room", [dev(A, per_vm=2, free=0, vms=(Vm(V1, "running", 2), Vm(V2, "stopped", 0)))], A, 6500),
    ("a stopped VM without room", [dev(A, per_vm=1, free=0, vms=(Vm(V1, "stopped", 1),))], "capacity_exhausted", None),
    (
        "any number of VMs of the workgroup",
        [dev(A, per_vm=1, vms=tuple(Vm(f"vm-{n:08x}", "running", 1) for n in range(50)))],
        A,
        8000,
    ),
    ("keys missing: skipped", [dev(A, keys=False, vm="running"), dev(B, free=1)], B, 8000),
    ("keys missing on the only device with room", [dev(A, keys=False, free=4), dev(B, free=0)], "keys_pending", None),
    ("keys missing and no room anywhere", [dev(A, keys=False, free=0)], "capacity_exhausted", None),
    ("disk too full for a new VM", [dev(A, disk=1000), dev(B, free=1, disk=20480)], B, 8000),
    (
        "the booting VM of a pending session",
        [dev(A, per_vm=2, free=0, vms=(Vm(V1, "running", 2),), pending=0)],
        "capacity_exhausted",
        None,
    ),
    ("pending session on a fresh device", [dev(A, per_vm=2, free=1, pending=1), dev(B, free=1)], B, 8000),
    ("disk too full but a running VM", [dev(A, disk=0, vm="running")], A, 2500),
    (
        "no free slot but a ready pool VM: a new VM",
        [dev(A, per_vm=1, free=0, pool=1, vms=(Vm(V1, "running", 1),))],
        A,
        3000,
    ),
    (
        "running VM most room is not needed for the device order",
        [dev(B, per_vm=4, vms=(Vm(V1, "running", 3),)), dev(A, per_vm=4, vms=(Vm(V2, "running", 0),))],
        A,
        2500,
    ),
    (
        "a current runner reports no number: a running VM has room",
        [dev(A, free=0, vms=(Vm(V1, "running", 40),))],
        A,
        2500,
    ),
]


@pytest.mark.parametrize(("name", "candidates", "expected", "estimate"), VM_TABLE, ids=[row[0] for row in VM_TABLE])
def test_vm_table(name: str, candidates: list[Candidate], expected: str, estimate: int | None) -> None:
    decision = choose(candidates)
    if estimate is None:
        assert decision == NoPlacement(expected), name
    else:
        assert isinstance(decision, Placement), name
        assert (decision.device_id, decision.estimate_ms) == (expected, estimate), name


def test_the_fit_names_a_new_vm() -> None:
    assert dev(A, per_vm=1, vms=(Vm(V1, "running", 1),)).fit().new_vm is True
    assert dev(A, vm="running").fit().new_vm is False


def test_a_chosen_vm_counts_alone() -> None:
    vms = (Vm(V1, "running", 2), Vm(V2, "running", 0))
    assert choose([dev(A, per_vm=2, vms=vms, pinned_vm=V2)], pinned=True) == Placement(A, 2500)
    assert choose([dev(A, per_vm=2, vms=vms, pinned_vm=V1)], pinned=True) == NoPlacement("placement_full")
    # a chosen VM never gets a new VM, also not for pending sessions beyond its room
    assert choose([dev(A, per_vm=2, vms=vms, pinned_vm=V1, pending=3)], pinned=True) == NoPlacement("placement_full")
    assert choose([dev(A, online=False, vm="running")], pinned=True) == NoPlacement("placement_offline")
    assert choose([dev(A, vm="running", keys=False)], pinned=True) == NoPlacement("keys_pending")


def test_order_with_vms_does_not_depend_on_the_input_order() -> None:
    candidates = [
        dev(A, per_vm=2, vms=(Vm(V1, "running", 2),)),
        dev(B, per_vm=2, vms=(Vm(V2, "running", 1), Vm(V3, "stopped", 0))),
        dev(C, per_vm=2, free=1),
    ]
    decisions = {choose(list(permutation)) for permutation in itertools.permutations(candidates)}
    assert decisions == {Placement(device_id=B, estimate_ms=2500)}
    assert [item.device_id for item in order(candidates)] == [B, A, C]


def test_categories_cover_every_waiting_reason() -> None:
    assert set(CATEGORIES.values()) == {
        "waiting_for_capacity",
        "waiting_for_device",
        "waiting_for_credit",
        "interrupted",  # ADR-087: the main session's device failed
    }
    assert {"capacity_exhausted", "wg_vm_limit", "no_device", "platform_access_required"} <= set(CATEGORIES)


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


def test_optional_counters_and_settings_are_kept() -> None:
    stored = {
        "vm_memory_budget_mib": 8,
        "vm_memory_used_mib": 0,
        "vm_slots_total": 1,
        "vm_slots_free": 1,
        "pool_ready": 0,
        "sessions_per_vm": 16,
        "disk_free_mib": 30000,
        "cpus": 18,
    }
    capacity = Capacity.of(stored)
    assert (capacity.sessions_per_vm, capacity.disk_free_mib) == (16, 30000)
    assert capacity.json() == stored
    assert capacity.room(15) == 1
    assert capacity.room(20) == 0


def test_capacity_counters_are_capped_and_memory_never_negative() -> None:
    capacity = Capacity.of({"vm_memory_budget_mib": 2**60, "vm_memory_used_mib": 2**61})
    assert capacity.vm_memory_budget_mib == 2**53 - 1
    assert capacity.free_memory_mib == 0
