"""U-RUN-vm choice (KOR-41, ADR-075 section 4; 05-runner.md sections 3.3-3.4): several VMs per workgroup, the
deterministic order of `ensure_vm` (preference, room, stopped VM, new VM), the booking of the session under the
workgroup lock, the deletion of every VM of a workgroup and the capacity report.

Lima is the fake of `guests.py` with real guest control processes, the store a real PostgreSQL database. Tests that
only choose among running VMs insert the rows directly and skip the startup reconciliation.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from agent_runner.errors import RunnerError
from agent_runner.vm import records
from agent_runner.vm.capacity import Usage
from agent_runner.vm.lifecycle import VmManager
from agent_runner.vm.pool import VmSettings

from .guests import GOLDEN, NOW, ORG, SETTINGS, WG_A, WG_B, Harness


def settings(**changes: Any) -> VmSettings:
    return VmSettings(**{**SETTINGS.__dict__, **changes})


def new_session(harness: Harness, wg: str = WG_A) -> str:
    """A session in `vm_assigning` without a VM yet (the start's row before step 1)."""
    session_id = str(uuid.uuid4())
    harness.sql(
        "INSERT INTO runner.session (id, task_id, org_id, wg_id, state, created_at)"
        " VALUES (%s, %s, %s, %s, 'vm_assigning', %s)",
        (session_id, str(uuid.uuid4()), ORG, wg, NOW),
    )
    return session_id


def vm_of(harness: Harness, session_id: str) -> str | None:
    found = harness.sql("SELECT vm_id FROM runner.session WHERE id = %s", (session_id,))[0]["vm_id"]
    return None if found is None else str(found)


def end(harness: Harness, session_id: str) -> None:
    harness.sql("UPDATE runner.session SET state = 'stopped' WHERE id = %s", (session_id,))


async def add_row(manager: VmManager, vm_id: str, *, state: str = "running", wg: str = WG_A) -> None:
    """A VM row without an instance (only for choices among rows that need no Lima call)."""
    await records.insert(
        manager.env.db,
        vm_id=vm_id,
        lima_name=f"row-{vm_id}",
        golden_version=GOLDEN,
        state=state,
        token_sha256="0" * 64,
        cpus=4,
        memory_mib=8192,
        created_at=NOW,
        org_id=ORG,
        wg_id=wg,
    )


def rows_only(body: Any) -> Any:
    """`body(manager)` without the startup reconciliation (it would delete rows without an instance)."""

    async def run(manager: VmManager) -> Any:
        manager._reconciled.set()
        return await body(manager)

    return run


def test_a_full_vm_gets_a_second_vm_and_the_choice_is_deterministic(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=0, sessions_per_vm=2)
    sessions = [new_session(harness) for _ in range(5)]

    async def body(manager: VmManager) -> Any:
        return [await manager.ensure_vm(ORG, WG_A, session_id=sid) for sid in sessions[:3]]

    first, second, third = harness.run(body)
    assert first.id == second.id != third.id
    assert [vm_of(harness, sid) for sid in sessions[:3]] == [first.id, first.id, third.id]
    assert [argv[0] for argv, _ in harness.fake.calls].count("clone") == 2
    # one place free on each VM: equal room -> the smallest VM ID
    end(harness, sessions[0])
    fourth = harness.run(lambda manager: manager.ensure_vm(ORG, WG_A, session_id=sessions[3]))
    assert fourth.id == min(first.id, third.id)
    # two places on the first VM, one on the other: most room first
    end(harness, sessions[1])
    end(harness, sessions[3])
    fifth = harness.run(lambda manager: manager.ensure_vm(ORG, WG_A, session_id=sessions[4]))
    assert fifth.id == first.id
    deleted = harness.run(lambda manager: manager.delete_workgroup(WG_A))
    assert sorted(row.id for row in deleted) == sorted([first.id, third.id])
    assert harness.live() == []


def test_two_concurrent_starts_never_share_the_last_place(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=0, sessions_per_vm=1)
    one, two = new_session(harness), new_session(harness)

    async def body(manager: VmManager) -> Any:
        return await asyncio.gather(
            manager.ensure_vm(ORG, WG_A, session_id=one), manager.ensure_vm(ORG, WG_A, session_id=two)
        )

    first, second = harness.run(body)
    assert first.id != second.id
    assert {vm_of(harness, one), vm_of(harness, two)} == {first.id, second.id}


def test_most_room_first_then_the_smallest_id_and_no_limit(harness: Harness) -> None:
    harness.settings = settings(sessions_per_vm=3)

    async def body(manager: VmManager) -> Any:
        await add_row(manager, "vm-00000002")
        await add_row(manager, "vm-00000001")
        harness.session("vm-00000001", WG_A, "working")
        harness.session("vm-00000001", WG_A, "idle")
        harness.session("vm-00000002", WG_A, "working")
        harness.session("vm-00000002", WG_A, "suspended")  # keeps its sandbox but occupies no place
        harness.session("vm-00000002", WG_A, "stopped")
        roomy = await manager.ensure_vm(ORG, WG_A)
        manager.env.settings = settings(sessions_per_vm=0)
        unbounded = await manager.ensure_vm(ORG, WG_A)
        return roomy.id, unbounded.id

    assert harness.run(rows_only(body), reconcile=False) == ("vm-00000002", "vm-00000001")


def test_without_a_bound_the_vms_own_resources_decide_and_the_existing_order_goes_on(harness: Harness) -> None:
    """No fixed number of sessions per VM: a running VM whose measured resources take no more session is passed over;
    then the existing order of 05 section 3.3 applies (here: the other running VM, else a new VM)."""
    harness.settings = settings(sessions_per_vm=0)
    full = {"vm-00000001"}

    async def body(manager: VmManager) -> Any:
        async def session_room(row: Any) -> bool:
            return row.id not in full

        manager.env.session_room = session_room
        await add_row(manager, "vm-00000001")
        await add_row(manager, "vm-00000002")
        for _ in range(11):  # e.g. eleven tasks on one VM: no number refuses the twelfth
            harness.session("vm-00000002", WG_A, "working")
        other = await manager.ensure_vm(ORG, WG_A)
        full.add("vm-00000002")
        with pytest.raises(RunnerError) as caught:
            await manager.ensure_vm(ORG, WG_A, new_allowed=False)  # no room anywhere, no new VM allowed
        return other.id, caught.value.code

    assert harness.run(rows_only(body), reconcile=False) == ("vm-00000002", "capacity_exhausted")


def test_preferences_come_first_and_only_vms_of_the_workgroup_count(harness: Harness) -> None:
    harness.settings = settings(sessions_per_vm=1)

    async def body(manager: VmManager) -> Any:
        await add_row(manager, "vm-00000001")
        await add_row(manager, "vm-00000002")
        await add_row(manager, "vm-00000003", wg=WG_B)
        await add_row(manager, "vm-00000004", state="assigning")
        harness.session("vm-00000001", WG_A, "working")
        booked = new_session(harness)
        # a full preferred VM is taken regardless of the limit, and the session is booked on it
        full = await manager.ensure_vm(ORG, WG_A, session_id=booked, prefer=["vm-00000001"])
        # a VM of another workgroup, an unknown VM and one that is not running or stopped are skipped
        skipped = await manager.ensure_vm(ORG, WG_A, prefer=["vm-00000003", "vm-0000dead", "vm-00000004"])
        harness.session("vm-00000002", WG_A, "working")
        with pytest.raises(RunnerError) as limit:
            await manager.ensure_vm(ORG, WG_A, new_allowed=False)
        return full.id, vm_of(harness, booked), skipped.id, limit.value

    full, booked, skipped, limit = harness.run(rows_only(body), reconcile=False)
    assert (full, booked, skipped) == ("vm-00000001", "vm-00000001", "vm-00000002")
    assert (limit.code, limit.name) == ("capacity_exhausted", "vm_new_allowed")
    skips = [fields["vm_id"] for name, fields in harness.logs if name == "vm.preference_skipped"]
    assert skips == ["vm-00000003", "vm-0000dead", "vm-00000004"]


def test_a_preferred_stopped_vm_is_restarted_even_with_an_older_golden(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=0)

    async def stopped(manager: VmManager) -> Any:
        row = await manager.ensure_vm(ORG, WG_A)
        return await manager.stop_vm(row.id)

    old = harness.run(stopped)
    newer = "g2"
    harness.add_golden(newer, minute=5)
    kept = harness.run(lambda manager: manager.ensure_vm(ORG, WG_A, prefer=[old.id]))
    assert (kept.id, kept.state, kept.golden_version) == (old.id, "running", GOLDEN)
    harness.run(lambda manager: manager.stop_vm(old.id))
    # without the preference the older golden is replaced, also when the hub allows no further VM
    replaced = harness.run(lambda manager: manager.ensure_vm(ORG, WG_A, new_allowed=False))
    assert replaced.id != old.id
    assert replaced.golden_version == newer
    assert [row["id"] for row in harness.live()] == [replaced.id]


def test_the_capacity_report(harness: Harness) -> None:
    harness.settings = settings(sessions_per_vm=4, max_vms=3)

    async def body(manager: VmManager) -> Any:
        await add_row(manager, "vm-00000001")
        await add_row(manager, "vm-00000002", state="stopped")
        harness.session("vm-00000001", WG_A, "working")
        harness.session("vm-00000001", WG_A, "suspended")

        async def work_vms() -> Usage:
            return Usage(vms=1, memory_mib=4096)

        manager.env.other_usage = work_vms
        manager.env.cpus = lambda: 8
        without_disk = await manager.report()
        manager.env.disk_free_mib = lambda: 51200
        return without_disk, await manager.report()

    without_disk, report = harness.run(rows_only(body), reconcile=False)
    assert without_disk == {
        "vm_memory_budget_mib": 32768,
        "vm_memory_used_mib": 8192 + 4096,
        "vm_slots_total": 3,
        "vm_slots_free": 1,
        "pool_ready": 0,
        "cpus": 8,
        "memory_total_mib": 32768 + 4 * 8192,
        "vms_running": 1,
        "sessions_active": 1,
        "work_vms_running": 1,
        "sessions_per_vm": 4,
        "vms_max": 3,
    }
    assert report == {**without_disk, "disk_free_mib": 51200}


def test_a_device_without_a_ready_golden_reports_no_room(harness: Harness) -> None:
    """KOR-67: a device that still builds its first golden (no VM either) offers no slot, so the hub places nothing on
    it and the task waits or runs elsewhere; once the golden is ready its slots count again."""
    harness.settings = settings(sessions_per_vm=4, max_vms=3)

    async def body(manager: VmManager) -> Any:
        return await manager.report()

    before = harness.run(rows_only(body), reconcile=False)
    assert (before["vm_slots_free"], before["pool_ready"], before["vm_slots_total"]) == (0, 0, 3)
    harness.add_golden()
    assert harness.run(rows_only(body), reconcile=False)["vm_slots_free"] == 3
