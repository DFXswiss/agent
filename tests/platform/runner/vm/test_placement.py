"""U-RUN-vm placement (HOT-32; 05-runner.md sections 1.1, 3.2, 3.3): the room of a running VM is measured before every
placement, no fixed number of sessions per VM; a full or unmeasured VM gets no further session, a new VM takes it; a
change of the VM size applies to new VMs only - a VM of the older size keeps its sessions, takes no new one and stops
when its last session ended; the pool follows the current size; a restart of the runner ends no VM and no session;
every placement is logged and counted with the chosen VM, the measured room and the reason.

Lima is the fake of `guests.py` with real guest control processes, the store a real PostgreSQL database; every
`harness.run` builds a new `VmManager` over the same instances and store, as the daemon does after a restart.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from agent_runner.errors import RunnerError
from agent_runner.vm.lifecycle import VmManager
from agent_runner.vm.pool import VmSettings
from agent_runner.vm.room import Room

from .guests import NOW, ORG, SETTINGS, WG_A, Harness

LARGER = {"cpus": 8, "memory_mib": 16384}
"""A new size for the tests: it fits the harness budget next to one VM of the old size (8 GiB)."""


def settings(**changes: Any) -> VmSettings:
    return VmSettings(**{**SETTINGS.__dict__, **changes})


def new_session(harness: Harness) -> str:
    """A session in `vm_assigning` without a VM yet (the start's row before step 1)."""
    session_id = str(uuid.uuid4())
    harness.sql(
        "INSERT INTO runner.session (id, task_id, org_id, wg_id, state, created_at)"
        " VALUES (%s, %s, %s, %s, 'vm_assigning', %s)",
        (session_id, str(uuid.uuid4()), ORG, WG_A, NOW),
    )
    return session_id


def sessions(harness: Harness) -> dict[str, tuple[str | None, str]]:
    rows = harness.sql("SELECT id::text AS id, vm_id, state FROM runner.session")
    return {row["id"]: (row["vm_id"], row["state"]) for row in rows}


def set_state(harness: Harness, session_id: str, state: str) -> None:
    harness.sql("UPDATE runner.session SET state = %s WHERE id = %s", (state, session_id))


def measured(manager: VmManager, rooms: dict[str, Room]) -> list[str]:
    """The measured room per VM ID (`vm.room`); a VM not named has room. Returns the IDs in the order measured."""
    seen: list[str] = []

    async def session_room(row: Any) -> Room:
        seen.append(row.id)
        return rooms.get(row.id, ROOM)

    manager.env.session_room = session_room
    return seen


ROOM = Room(True, 4096, 480, 0.35)
"""Room measured: 4 GiB available, the largest session 480 MiB, load 35 % per vCPU."""
FULL = Room(False, 3072, 430, 1.075)
"""The finding of 2026-11-27: load 4.3 on 4 vCPUs."""


def placements(harness: Harness) -> list[dict[str, str | int]]:
    return [fields for name, fields in harness.logs if name == "vm.placement"]


def test_a_full_vm_the_new_session_lands_in_a_new_vm(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=0)
    first, second = new_session(harness), new_session(harness)
    rooms: dict[str, Room] = {}

    async def body(manager: VmManager) -> Any:
        measured(manager, rooms)
        old = await manager.ensure_vm(ORG, WG_A, session_id=first)
        rooms[old.id] = FULL  # measured before the placement: no room
        new = await manager.ensure_vm(ORG, WG_A, session_id=second)
        return old, new, dict(manager.env.placements), dict(manager.env.room_checks)

    old, new, counted, checks = harness.run(body)
    assert new.id != old.id
    assert sessions(harness)[second] == (new.id, "vm_assigning")
    assert sessions(harness)[first] == (old.id, "vm_assigning")  # the first session stays where it is
    assert placements(harness)[-1] == {
        "wg_id": WG_A,
        "vm_id": new.id,
        "reason": "new_vm",
        "session_id": second,
        "cpus": 4,
        "memory_mib": 8192,
        "passed_over": f"{old.id}:full",
    }
    assert counted == {"new_vm": 2}
    assert checks == {"full": 1}


def test_a_vm_with_room_the_new_session_lands_in_the_same_vm(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=0)
    first, second = new_session(harness), new_session(harness)

    async def body(manager: VmManager) -> Any:
        seen = measured(manager, {})
        one = await manager.ensure_vm(ORG, WG_A, session_id=first)
        two = await manager.ensure_vm(ORG, WG_A, session_id=second)
        return one, two, seen, dict(manager.env.placements), dict(manager.env.room_checks)

    one, two, seen, counted, checks = harness.run(body)
    assert two.id == one.id
    assert seen == [one.id]  # measured before the second placement
    assert [argv[0] for argv, _ in harness.fake.calls].count("clone") == 1
    assert placements(harness)[-1] == {
        "wg_id": WG_A,
        "vm_id": one.id,
        "reason": "room",
        "session_id": second,
        "cpus": 4,
        "memory_mib": 8192,
        "fits": "yes",
        "available_mib": 4096,
        "need_mib": 480,
        "load_pct": 35,
        "passed_over": "none",
    }
    assert (counted, checks) == ({"new_vm": 1, "room": 1}, {"room": 1})


def test_an_unmeasured_vm_gets_no_new_session(harness: Harness) -> None:
    """A guest that does not answer its metrics in time is not a VM with room (HOT-32: look first)."""
    harness.add_golden()
    harness.settings = settings(pool_size=0)
    first, second = new_session(harness), new_session(harness)
    rooms: dict[str, Room] = {}

    async def body(manager: VmManager) -> Any:
        measured(manager, rooms)
        old = await manager.ensure_vm(ORG, WG_A, session_id=first)
        rooms[old.id] = Room(False)
        return old, await manager.ensure_vm(ORG, WG_A, session_id=second)

    old, new = harness.run(body)
    assert new.id != old.id
    assert placements(harness)[-1]["passed_over"] == f"{old.id}:unmeasured"


def test_after_a_size_change_new_sessions_land_in_a_new_vm_and_the_old_ones_go_on(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=0)
    first = new_session(harness)
    old = harness.run(lambda manager: manager.ensure_vm(ORG, WG_A, session_id=first))
    set_state(harness, first, "working")
    # the operator sets a larger size and restarts the runner (the settings are read at the start)
    harness.settings = settings(pool_size=0, **LARGER)
    second = new_session(harness)

    async def body(manager: VmManager) -> Any:
        measured(manager, {})  # the old VM would still have room: its size alone decides
        new = await manager.ensure_vm(ORG, WG_A, session_id=second)
        return new, await manager.idle_pass()

    new, stopped = harness.run(body)
    assert new.id != old.id
    assert (new.cpus, new.memory_mib) == (8, 16384)
    assert stopped == []  # the old VM keeps its working session
    assert {row["id"]: row["state"] for row in harness.live()} == {old.id: "running", new.id: "running"}
    assert sessions(harness) == {first: (old.id, "working"), second: (new.id, "vm_assigning")}
    assert placements(harness)[-1]["passed_over"] == f"{old.id}:size"
    # its last session ends: the old VM stops (no idle time), the new one keeps running
    set_state(harness, first, "stopped")
    set_state(harness, second, "working")
    assert harness.run(lambda manager: manager.idle_pass()) == [old.id]
    assert {row["id"]: row["state"] for row in harness.live()} == {old.id: "stopped", new.id: "running"}
    assert ("vm.resized_stopped", {"vm_id": old.id}) in harness.logs
    # a further session goes to the new VM, never back to the stopped VM of the old size; that one is replaced
    third = new_session(harness)

    async def again(manager: VmManager) -> Any:
        measured(manager, {new.id: FULL})
        return await manager.ensure_vm(ORG, WG_A, session_id=third)

    third_vm = harness.run(again)
    assert third_vm.id not in (old.id, new.id)
    assert (third_vm.cpus, third_vm.memory_mib) == (8, 16384)
    assert ("vm.size_replaced", {"vm_id": old.id, "cpus": 4, "memory_mib": 8192}) in harness.logs
    assert old.id not in {row["id"] for row in harness.live()}
    assert placements(harness)[-1]["reason"] == "replaced"


def test_a_stopped_vm_of_the_old_size_whose_sessions_may_not_move_stays_for_them(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=0)
    first = new_session(harness)

    async def stop(manager: VmManager) -> Any:
        row = await manager.ensure_vm(ORG, WG_A, session_id=first)
        return await manager.stop_vm(row.id)

    old = harness.run(stop)
    set_state(harness, first, "suspended")  # paused without a checkpoint: it may not move
    harness.settings = settings(pool_size=0, **LARGER)
    second = new_session(harness)
    new = harness.run(lambda manager: manager.ensure_vm(ORG, WG_A, session_id=second))
    assert new.id != old.id
    assert {row["id"]: row["state"] for row in harness.live()} == {old.id: "stopped", new.id: "running"}
    assert placements(harness)[-1]["passed_over"] == f"{old.id}:size"
    # the suspended session's own VM is still restarted for it (its work lives there)
    resumed = harness.run(lambda manager: manager.ensure_vm(ORG, WG_A, prefer=[old.id]))
    assert (resumed.id, resumed.state, resumed.cpus) == (old.id, "running", 4)
    assert placements(harness)[-1]["reason"] == "preferred"


def test_a_restart_of_the_runner_ends_no_session(harness: Harness) -> None:
    """05-runner.md section 1.1: VMs and sessions go on over a restart of the runner, also when the size changed."""
    harness.add_golden()
    harness.settings = settings(pool_size=0)
    working, idle = new_session(harness), new_session(harness)

    async def start(manager: VmManager) -> Any:
        row = await manager.ensure_vm(ORG, WG_A, session_id=working)
        await manager.ensure_vm(ORG, WG_A, session_id=idle)
        return row

    vm = harness.run(start)
    set_state(harness, working, "working")
    set_state(harness, idle, "idle")
    before = sessions(harness)
    harness.settings = settings(pool_size=1, **LARGER)
    harness.logs.clear()

    async def restart(manager: VmManager) -> Any:
        counts = await manager.reconcile()  # what the daemon does first after its start
        await manager.refill()
        await manager.golden_switch.run_pass()
        await manager.health_pass()
        return counts, await manager.idle_pass()

    counts, stopped = harness.run(restart, reconcile=False)
    assert counts == {"deleted": 0, "missing": 0, "state": 0, "unknown": 0, "tokens": 0}
    assert stopped == []
    assert sessions(harness) == before
    states = {row["id"]: row["state"] for row in harness.live()}
    assert states[vm.id] == "running"
    assert harness.fake.instances[vm.lima_name]["status"] == "Running"
    stops = [argv for argv, _ in harness.fake.calls if argv[0] in ("stop", "delete") and vm.lima_name in argv]
    assert stops == []
    assert not [name for name, _fields in harness.logs if name in ("vm.idle_stopped", "vm.resized_stopped")]


def test_the_pool_follows_the_current_size(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=1)
    assert harness.run(lambda manager: manager.refill()) == 1
    old = harness.live()[0]["id"]
    harness.settings = settings(pool_size=1, **LARGER)
    first = new_session(harness)

    async def take(manager: VmManager) -> Any:
        # before the refill replaced it, the pool VM of the old size is not taken for a session
        return await manager.ensure_vm(ORG, WG_A, session_id=first)

    taken = harness.run(take)
    assert taken.id != old
    assert (taken.cpus, taken.memory_mib) == (8, 16384)
    assert harness.run(lambda manager: manager.refill()) == 1
    pool = [row for row in harness.sql("SELECT id, state, cpus, memory_mib FROM runner.vm") if row["state"] == "pool"]
    assert [(row["cpus"], row["memory_mib"]) for row in pool] == [(8, 16384)]
    assert ("vm.pool_resized", {"vm_id": old, "cpus": 4, "memory_mib": 8192}) in harness.logs


def test_a_refused_placement_is_logged_and_counted(harness: Harness) -> None:
    """No running VM with room, no stopped VM and no ready golden: refused (the hub places the next attempt)."""
    harness.settings = settings(pool_size=0)

    async def body(manager: VmManager) -> Any:
        with pytest.raises(RunnerError) as caught:
            await manager.ensure_vm(ORG, WG_A)
        return caught.value.code, dict(manager.env.placements)

    assert harness.run(body) == ("capacity_exhausted", {"refused": 1})
    assert placements(harness) == [
        {"wg_id": WG_A, "vm_id": "none", "reason": "refused", "passed_over": "none", "code": "capacity_exhausted"}
    ]
