"""U-RUN-vm lifecycle: `ensure_vm` order, serialisation per workgroup, restart, golden replacement, capacity, idle
stop, health failures, deletion and startup reconciliation (05-runner.md sections 3.2-3.5, 6.1 step 1; ADR-009).

Lima is the fake of `guests.py` with real guest control processes, the store a real PostgreSQL database.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
from datetime import timedelta
from typing import Any

import pytest

from agent_runner.errors import RunnerError
from agent_runner.guestlink import cursor
from agent_runner.vm import lifecycle, records
from agent_runner.vm.capacity import Usage
from agent_runner.vm.lifecycle import VmManager
from agent_runner.vm.pool import VmSettings
from agent_runner.vm.records import GoldenRef

from .guests import GOLDEN, NOW, ORG, PREFIX, SETTINGS, WG_A, WG_B, WG_C, Harness

GOLDEN_REF = GoldenRef(version=GOLDEN, lima_name=f"{PREFIX}golden-{GOLDEN}")


def settings(**changes: Any) -> VmSettings:
    return VmSettings(**{**SETTINGS.__dict__, **changes})


def ensure(org: str = ORG, wg: str = WG_A) -> Any:
    async def body(manager: VmManager) -> Any:
        return await manager.ensure_vm(org, wg)

    return body


# -- ensure_vm order --------------------------------------------------------------------------------------------


def test_pool_path_then_running_vm(harness: Harness) -> None:
    harness.add_golden()

    async def body(manager: VmManager) -> Any:
        await manager.refill()
        first = await manager.ensure_vm(ORG, WG_A)
        again = await manager.ensure_vm(ORG, WG_A)
        return first, again

    first, again = harness.run(body)
    assert first.id == again.id
    assert first.state == "running"
    # The second call found the running VM: no further clone or assignment.
    assert [argv[0] for argv, _ in harness.fake.calls].count("clone") == 2
    assert harness.sql("SELECT last_session_at FROM runner.vm WHERE id = %s", (first.id,))[0]["last_session_at"]


def test_clone_path_when_the_pool_is_empty(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=0)

    async def body(manager: VmManager) -> Any:
        await manager.wait_reconciled()
        before = len(harness.fake.calls)
        row = await manager.ensure_vm(ORG, WG_A)
        calls = harness.fake.calls[before:]
        async with await manager.client(row) as client:
            return row, await client.health(), manager._refill_wanted.is_set(), calls

    row, health, woken, calls = harness.run(body)
    assert (row.state, row.wg_id, row.org_id) == ("running", WG_A, ORG)
    assert (health.state, health.wg_id) == ("assigned", WG_A)
    clone = next(argv for argv, _ in harness.fake.calls if argv[0] == "clone")
    # A hot-path clone never carries the self-test.
    assert not any(".provision" in value for value in clone)
    assert [event["state"] for event in harness.events] == ["assigning", "running"]
    assert woken is True
    # KOR-15: every lookup on the clone path names its instance; a full `limactl list --json` inspects every instance
    # of the device (0.17-0.20 s with 67 instances against 0.03 s by name).
    listed = [argv for argv, _ in calls if argv[0] == "list"]
    assert listed
    assert all(argv == ["list", "--json", row.lima_name] for argv in listed)


def test_no_golden_yet_is_capacity_exhausted(harness: Harness) -> None:
    """A device that still builds its golden refuses for now; the hub places the next attempt (KOR-67)."""

    async def body(manager: VmManager) -> Any:
        with pytest.raises(RunnerError) as caught:
            await manager.ensure_vm(ORG, WG_A)
        return caught.value.code, caught.value.name

    assert harness.run(body) == ("capacity_exhausted", "golden")


def test_clone_path_failure_marks_the_row_deleted(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=0)

    def broken(argv: list[str]) -> None:
        if argv[0] == "start":
            harness.fake.fail["start"] = (1, "", "boom\n")

    harness.fake.hook = broken

    async def body(manager: VmManager) -> Any:
        with pytest.raises(RunnerError) as caught:
            await manager.ensure_vm(ORG, WG_A)
        return caught.value.code

    assert harness.run(body) == "vm_start_failed"
    assert [row["state"] for row in harness.vms()] == ["deleted"]
    assert [event["state"] for event in harness.events] == ["assigning", "deleted"]
    assert ("vm.clone_failed", {"vm_id": harness.vms()[0]["id"], "code": "vm_start_failed"}) in harness.logs
    assert list(harness.tokens_dir.glob("*.token")) == []


def test_clone_path_assignment_refused(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=0)

    async def body(manager: VmManager) -> Any:
        original = manager.provisioner.create

        async def create(*args: Any, **kwargs: Any) -> str:
            name = await original(*args, **kwargs)
            # Somebody else assigned the fresh guest first.
            service = harness.fake.farm.services[name]  # type: ignore[union-attr]
            assert service.context is not None
            service.context.state.assign("vm-ffffffff", ORG, WG_B)
            return name

        manager.provisioner.create = create  # type: ignore[method-assign]
        with pytest.raises(RunnerError) as caught:
            await manager.ensure_vm(ORG, WG_A)
        return caught.value.code

    assert harness.run(body) == "vm_start_failed"
    assert [row["state"] for row in harness.vms()] == ["deleted"]
    assert any(
        name == "vm.clone_failed" and fields["code"] == "guest_refused" and fields["error"] == "already_assigned"
        for name, fields in harness.logs
    )


def test_two_sessions_of_one_workgroup_get_one_vm(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=0)

    async def body(manager: VmManager) -> Any:
        return await asyncio.gather(*(manager.ensure_vm(ORG, WG_A) for _ in range(3)), manager.ensure_vm(ORG, WG_B))

    rows = harness.run(body)
    assert rows[0].id == rows[1].id == rows[2].id
    assert rows[3].id != rows[0].id
    assert len(harness.live()) == 2
    assert [argv[0] for argv, _ in harness.fake.calls].count("clone") == 2


def test_a_workgroup_may_have_several_live_vms(harness: Harness) -> None:
    """ADR-075 section 4 (migration 0018): no unique index on the workgroup; an ID or name twice is refused."""

    async def body(manager: VmManager) -> Any:
        kwargs: dict[str, Any] = {"golden_version": GOLDEN, "state": "running", "token_sha256": "x", "cpus": 1,
                                  "memory_mib": 1, "created_at": NOW, "org_id": ORG, "wg_id": WG_A}  # fmt: skip
        await records.insert(manager.env.db, vm_id="vm-00000002", lima_name="a", **kwargs)
        await records.insert(manager.env.db, vm_id="vm-00000001", lima_name="b", **kwargs)
        with pytest.raises(RunnerError) as caught:
            await records.insert(manager.env.db, vm_id="vm-00000003", lima_name="b", **kwargs)
        with pytest.raises(RunnerError) as unknown:
            await records.update(manager.env.db, "vm-0000000f", state="deleted")
        found = [row.id for row in await records.of_workgroup(manager.env.db, WG_A)]
        return caught.value.code, unknown.value.code, found

    assert harness.run(body, reconcile=False) == ("vm_exists", "vm_unknown", ["vm-00000001", "vm-00000002"])


# -- restart and golden replacement -----------------------------------------------------------------------------


def stop_vm_of(harness: Harness, wg: str = WG_A) -> Any:
    async def body(manager: VmManager) -> Any:
        row = await manager.ensure_vm(ORG, wg)
        return await manager.stop_vm(row.id)

    return harness.run(body)


def test_stopped_vm_is_restarted(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=0)
    stopped = stop_vm_of(harness)
    assert stopped.state == "stopped"

    async def body(manager: VmManager) -> Any:
        row = await manager.ensure_vm(ORG, WG_A)
        async with await manager.client(row) as client:
            return row, await client.health()

    row, health = harness.run(body)
    assert row.id == stopped.id
    assert row.state == "running"
    assert (health.state, health.wg_id) == ("assigned", WG_A)
    starts = [argv for argv, _ in harness.fake.calls if argv[0] == "start"]
    # The restart passes no token parameter: the guest keeps its installed token.
    assert starts[-1] == ["start", "-y", row.lima_name]
    assert [e["state"] for e in harness.events if e["vm_id"] == row.id][-3:] == ["stopping", "stopped", "running"]


def test_restart_assigns_again_when_the_guest_forgot(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=0)
    stopped = stop_vm_of(harness)
    # The guest lost its state (journal and assignment) while stopped.
    shutil.rmtree(harness.fake.farm.homes[stopped.lima_name] / "state")  # type: ignore[union-attr]

    async def body(manager: VmManager) -> Any:
        row = await manager.ensure_vm(ORG, WG_A)
        async with await manager.client(row) as client:
            return await client.health()

    assert harness.run(body).wg_id == WG_A


def test_restart_retries_start_once(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=0)
    stopped = stop_vm_of(harness)
    harness.fake.fail["start"] = (1, "", "FATA[0000] boom\n")

    async def body(manager: VmManager) -> Any:
        row = await manager.ensure_vm(ORG, WG_A)
        return row, manager.env.start_failures

    row, failures = harness.run(body)
    assert row.id == stopped.id
    assert failures == 1


def test_restart_failure_leaves_the_vm_stopped(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=0)
    stopped = stop_vm_of(harness)

    def broken(argv: list[str]) -> None:
        if argv[0] == "start":
            harness.fake.fail["start"] = (1, "", "boom\n")

    harness.fake.hook = broken

    async def body(manager: VmManager) -> Any:
        with pytest.raises(RunnerError) as caught:
            await manager.ensure_vm(ORG, WG_A)
        return caught.value.code, manager.reserved

    assert harness.run(body) == ("vm_start_failed", 0)
    assert harness.live()[0]["state"] == "stopped"
    assert ("vm.restart_failed", {"vm_id": stopped.id, "code": "lima_failed"}) in harness.logs


def test_restart_of_an_unhealthy_guest_fails(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=0)
    stopped = stop_vm_of(harness)
    harness.fake.dead.add(stopped.lima_name)
    # A `stopping` row (interrupted stop) is restarted like a stopped one and ends `stopped` on failure.
    harness.sql("UPDATE runner.vm SET state = 'stopping' WHERE id = %s", (stopped.id,))

    async def body(manager: VmManager) -> Any:
        manager._reconciled.set()
        with pytest.raises(RunnerError):
            await manager.ensure_vm(ORG, WG_A)

    harness.run(body, reconcile=False)
    assert harness.live()[0]["state"] == "stopped"
    assert ("vm.restart_failed", {"vm_id": stopped.id, "code": "vm_health_timeout"}) in harness.logs


def test_newer_golden_replaces_a_stopped_vm(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=0)
    stopped = stop_vm_of(harness)
    harness.sql("UPDATE runner.golden SET state = 'retired'")
    harness.add_golden("g2", minute=5)

    row = harness.run(ensure())
    assert row.id != stopped.id
    assert row.golden_version == "g2"
    assert {r["id"]: r["state"] for r in harness.vms()} == {stopped.id: "deleted", row.id: "running"}
    assert ("vm.golden_replaced", {"vm_id": stopped.id, "golden_version": "g2"}) in harness.logs


def test_suspended_session_keeps_the_old_golden(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=0)
    stopped = stop_vm_of(harness)
    harness.session(stopped.id, WG_A, "suspended")
    harness.sql("UPDATE runner.golden SET state = 'retired'")
    harness.add_golden("g2", minute=5)

    row = harness.run(ensure())
    assert row.id == stopped.id
    assert row.golden_version == GOLDEN


def test_stopped_vm_restarts_without_any_ready_golden(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=0)
    stopped = stop_vm_of(harness)
    harness.sql("UPDATE runner.golden SET state = 'retired'")
    assert harness.run(ensure()).id == stopped.id


# -- capacity ---------------------------------------------------------------------------------------------------


def test_clone_gives_up_a_pool_vm_when_no_slot_is_free(harness: Harness) -> None:
    harness.add_golden()
    harness.physical_mib = 32768 + 2 * 8192
    harness.settings = settings(pool_size=2)

    async def body(manager: VmManager) -> Any:
        return await manager.refill()

    harness.run(body)
    harness.sql("UPDATE runner.golden SET state = 'retired'")
    harness.add_golden("g2", minute=5)

    row = harness.run(ensure())
    assert row.golden_version == "g2"
    assert any(name == "vm.slot_from_pool" for name, _ in harness.logs)


def test_lru_stop_frees_a_slot(harness: Harness) -> None:
    harness.add_golden()
    harness.physical_mib = 32768 + 2 * 8192
    harness.settings = settings(pool_size=0)
    a = harness.run(ensure(wg=WG_A))
    harness.now = NOW + timedelta(minutes=1)
    b = harness.run(ensure(wg=WG_B))
    harness.session(b.id, WG_B, "working", at=harness.now)
    harness.now = NOW + timedelta(hours=1)

    c = harness.run(ensure(wg=WG_C))
    states = {row["id"]: row["state"] for row in harness.vms()}
    assert states == {a.id: "stopped", b.id: "running", c.id: "running"}
    assert ("vm.lru_stop", {"vm_id": a.id}) in harness.logs


def test_work_vms_give_way_to_a_workgroup_vm(harness: Harness) -> None:
    """ADR-059 addendum 4: the only slot's RAM is held by a work VM that yields; the workgroup VM gets it."""
    harness.add_golden()
    harness.physical_mib = 32768 + 8192
    harness.settings = settings(pool_size=1)
    harness.other = Usage(vms=1, memory_mib=8192, yielding_vms=1, yielding_mib=8192)

    async def body(manager: VmManager) -> Any:
        filled = await manager.refill()  # a warm VM is no reason to make work VMs give way
        return filled, await manager.ensure_vm(ORG, WG_A)

    filled, row = harness.run(body)
    assert filled == 0
    assert row.state == "running"
    assert harness.reclaimed == [8192]
    assert ("vm.slot_from_work_vms", {"released_mib": 8192}) in harness.logs


def test_capacity_exhausted(harness: Harness) -> None:
    harness.add_golden()
    harness.physical_mib = 32768 + 8192
    harness.settings = settings(pool_size=0)
    a = harness.run(ensure(wg=WG_A))
    harness.session(a.id, WG_A, "working")

    async def body(manager: VmManager) -> Any:
        with pytest.raises(RunnerError) as caught:
            await manager.ensure_vm(ORG, WG_B)
        return caught.value.code

    assert harness.run(body) == "capacity_exhausted"
    assert len(harness.live()) == 1


def test_lru_victim_whose_lock_is_held_is_skipped(harness: Harness) -> None:
    harness.add_golden()
    harness.physical_mib = 32768 + 8192
    harness.settings = settings(pool_size=0)
    harness.run(ensure(wg=WG_A))
    harness.now = NOW + timedelta(hours=1)

    async def body(manager: VmManager) -> Any:
        async with manager.lock(WG_A):
            with pytest.raises(RunnerError) as caught:
                await manager.ensure_vm(ORG, WG_B)
        return caught.value.code

    assert harness.run(body) == "capacity_exhausted"


def test_lru_victim_that_stopped_meanwhile(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    harness.add_golden()
    harness.physical_mib = 32768 + 8192
    harness.settings = settings(pool_size=0)
    a = harness.run(ensure(wg=WG_A))
    harness.now = NOW + timedelta(hours=1)
    original = records.get

    async def get(db: Any, vm_id: str) -> Any:
        row = await original(db, vm_id)
        return None if vm_id == a.id else row

    monkeypatch.setattr(records, "get", get)

    async def body(manager: VmManager) -> Any:
        with pytest.raises(RunnerError) as caught:
            await manager.ensure_vm(ORG, WG_B)
        return caught.value.code

    assert harness.run(body) == "capacity_exhausted"


# -- stop, delete, idle -----------------------------------------------------------------------------------------


def test_stop_vm_and_failures(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=0)
    row = harness.run(ensure())

    async def body(manager: VmManager) -> Any:
        unknown = await manager.stop_vm("vm-ffffffff")
        harness.fake.fail["stop"] = (1, "", "boom\n")
        with pytest.raises(RunnerError):
            await manager.stop_vm(row.id)
        still = await records.get(manager.env.db, row.id)
        stopped = await manager.stop_vm(row.id)
        again = await manager.stop_vm(row.id)
        return unknown, still, stopped, again

    unknown, still, stopped, again = harness.run(body)
    assert unknown is None
    assert still.state == "running"
    assert stopped.state == "stopped"
    assert again is None
    assert [e["state"] for e in harness.events if e["vm_id"] == row.id][-4:] == [
        "stopping",
        "running",
        "stopping",
        "stopped",
    ]


def test_stop_vm_that_changed_while_waiting_for_the_lock(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=0)
    row = harness.run(ensure())

    async def body(manager: VmManager) -> Any:
        async with manager.lock(WG_A):
            waiting = asyncio.ensure_future(manager.stop_vm(row.id))
            await asyncio.sleep(0)
            await records.update(manager.env.db, row.id, state="stopped")
        return await waiting

    assert harness.run(body) is None


def test_delete_workgroup(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=0)
    row = harness.run(ensure())

    async def body(manager: VmManager) -> Any:
        await cursor.save_cursor(manager.env.db, row.id, 7)
        deleted = await manager.delete_workgroup(WG_A)
        none = await manager.delete_workgroup(WG_A)
        return deleted, none, await cursor.load_cursor(manager.env.db, row.id)

    deleted, none, position = harness.run(body)
    assert [item.state for item in deleted] == ["deleted"]
    assert none == []
    assert position == 0
    assert row.lima_name not in harness.fake.instances
    assert not harness.tokens_dir.joinpath(f"{row.id}.token").exists()
    assert harness.events[-1]["state"] == "deleted"


def test_idle_pass(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=0, vm_idle_seconds=600)
    a = harness.run(ensure(wg=WG_A))
    b = harness.run(ensure(wg=WG_B))
    c = harness.run(ensure(wg=WG_C))
    harness.session(b.id, WG_B, "idle")
    harness.session(c.id, WG_C, "suspended")

    async def idle(manager: VmManager) -> Any:
        return await manager.idle_pass()

    harness.now = NOW + timedelta(seconds=599)
    assert harness.run(idle) == []
    harness.now = NOW + timedelta(seconds=601)
    # The sessions were created at NOW; VMs touched at NOW: A (no session) and C (suspended only) are idle.
    assert sorted(harness.run(idle)) == sorted([a.id, c.id])
    assert ("vm.idle_stopped", {"vm_id": a.id}) in harness.logs
    assert {row["id"]: row["state"] for row in harness.vms()}[b.id] == "running"


def test_idle_pass_rechecks_under_the_lock_and_survives_stop_failures(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=0, vm_idle_seconds=60)
    a = harness.run(ensure(wg=WG_A))
    b = harness.run(ensure(wg=WG_B))
    harness.now = NOW + timedelta(hours=1)
    original = records.usage
    calls = {"n": 0}

    async def usage(db: Any) -> Any:
        calls["n"] += 1
        found = await original(db)
        if calls["n"] >= 2:
            # Between the first scan and the re-check, A got a new session.
            return [item for item in found if item.vm_id != a.id]
        return found

    monkeypatch.setattr(records, "usage", usage)

    def fail_stop(argv: list[str]) -> None:
        if argv[0] == "stop" and argv[-1] == b.lima_name:
            harness.fake.fail["stop"] = (1, "", "boom\n")

    harness.fake.hook = fail_stop

    async def idle(manager: VmManager) -> Any:
        return await manager.idle_pass()

    assert harness.run(idle) == []
    assert ("vm.idle_stop_failed", {"vm_id": b.id, "code": "lima_failed"}) in harness.logs


# -- health -----------------------------------------------------------------------------------------------------


def test_health_failures_restart_once_then_stop(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=0)
    row = harness.run(ensure())

    async def body(manager: VmManager) -> Any:
        farm = harness.fake.farm
        assert farm is not None
        await manager.health_pass()
        farm.halt(row.lima_name)
        for _ in range(2):
            await manager.health_pass()
        after_two = dict(manager._failures)
        await manager.health_pass()
        restarted = await records.get(manager.env.db, row.id)
        # Unhealthy again after the restart: stopped and reported.
        farm.halt(row.lima_name)
        for _ in range(3):
            await manager.health_pass()
        final = await records.get(manager.env.db, row.id)
        return after_two, restarted, final

    after_two, restarted, final = harness.run(body)
    assert after_two == {row.id: 2}
    assert restarted.state == "running"
    assert final.state == "stopped"
    assert harness.unreachable == [row.id, row.id]
    names = [name for name, _ in harness.logs]
    assert names.count("vm.unhealthy_restart") == 1
    assert ("vm.unhealthy_stopped", {"vm_id": row.id}) in harness.logs


def test_health_restart_failure_stops_the_vm(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=0)
    row = harness.run(ensure())

    async def body(manager: VmManager) -> Any:
        farm = harness.fake.farm
        assert farm is not None
        farm.halt(row.lima_name)
        harness.fake.dead.add(row.lima_name)
        for _ in range(3):
            await manager.health_pass()
        return await records.get(manager.env.db, row.id), row.id in manager._recovered

    final, recovered = harness.run(body)
    assert final.state == "stopped"
    assert recovered is False
    assert ("vm.unhealthy_stopped", {"vm_id": row.id, "code": "vm_start_failed"}) in harness.logs


def test_health_of_a_guest_assigned_elsewhere_and_vanished_rows(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=0)
    row = harness.run(ensure())

    async def body(manager: VmManager) -> Any:
        # The token file disappeared: the probe fails without reaching the guest.
        manager.env.tokens.remove(row.id)
        assert await manager._probe(row) is False
        # The row is no longer running when the failure is handled.
        await records.update(manager.env.db, row.id, state="stopped")
        await manager._unhealthy(row)

    harness.run(body)
    assert harness.unreachable == [row.id]
    assert not any(name.startswith("vm.unhealthy") for name, _ in harness.logs)


# -- reconciliation ---------------------------------------------------------------------------------------------


def test_reconcile(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=3)
    harness.physical_mib = 32768 + 8 * 8192

    async def setup(manager: VmManager) -> Any:
        await manager.refill()
        a = await manager.ensure_vm(ORG, WG_A)
        b = await manager.ensure_vm(ORG, WG_B)
        c = await manager.ensure_vm(ORG, WG_C)
        await manager.refill()
        return a, b, c

    a, b, c = harness.run(setup)
    pool = [row for row in harness.live() if row["state"] == "pool"]
    # Crash leftovers: A's instance is gone, B stopped behind the runner's back, C is half-assigned, the pool VM
    # stopped, an unknown pool clone and a stray token file exist; a VM marked stopped runs.
    harness.fake.instances.pop(a.lima_name)
    harness.fake.instances[b.lima_name]["status"] = "Stopped"
    harness.sql("UPDATE runner.vm SET state = 'assigning' WHERE id = %s", (c.id,))
    harness.fake.instances[pool[0]["lima_name"]]["status"] = "Stopped"
    harness.fake.add(f"{PREFIX}arp-0000abcd", status="Running")
    harness.fake.add(f"{PREFIX}golden-other")
    harness.tokens_dir.joinpath("vm-0000abcd.token").write_text("x", encoding="ascii")
    harness.sql("UPDATE runner.vm SET state = 'stopped' WHERE id = %s", (pool[1]["id"],))

    async def body(manager: VmManager) -> Any:
        return await manager.reconcile()

    counts = harness.run(body, reconcile=False)
    assert counts == {"deleted": 2, "missing": 1, "state": 2, "unknown": 1, "tokens": 1}
    states = {row["id"]: row["state"] for row in harness.vms()}
    assert states[a.id] == "deleted"
    assert states[b.id] == "stopped"
    assert states[c.id] == "deleted"
    assert states[pool[0]["id"]] == "deleted"
    assert states[pool[1]["id"]] == "running"
    assert f"{PREFIX}arp-0000abcd" not in harness.fake.instances
    assert f"{PREFIX}golden-other" in harness.fake.instances
    assert not harness.tokens_dir.joinpath("vm-0000abcd.token").exists()
    assert not harness.tokens_dir.joinpath(f"{a.id}.token").exists()
    assert ("vm.unknown_deleted", {"lima_name": f"{PREFIX}arp-0000abcd"}) in harness.logs
    # A second reconciliation finds nothing to do.
    assert harness.run(body, reconcile=False) == dict.fromkeys(counts, 0)


def test_reconcile_removes_an_interrupted_clone_and_survives_an_undeletable_one(harness: Harness) -> None:
    # A stop signal to the runner's process group killed `limactl clone` early: Lima lists an empty directory as
    # Broken and refuses `delete -f` on it (KOR-03). The next start removes it before any other VM work.
    harness.add_golden()
    leftover = harness.fake.add_broken(f"{PREFIX}arp-0000abcd")

    async def body(manager: VmManager) -> Any:
        return await manager.reconcile()

    assert harness.run(body, reconcile=False)["unknown"] == 1
    assert not leftover.exists()
    assert f"{PREFIX}arp-0000abcd" not in harness.fake.instances
    # One Lima cannot delete at all is logged and does not stop the reconciliation (the VM area keeps working).
    harness.fake.add(f"{PREFIX}arp-0000abce", status="Running")
    harness.fake.fail["delete"] = (1, "", "FATA[0000] boom\n")
    harness.fake.add(f"{PREFIX}arp-0000abcf", status="Running")
    counts = harness.run(body, reconcile=False)
    assert counts["unknown"] == 1
    assert ("vm.unknown_delete_failed", {"lima_name": f"{PREFIX}arp-0000abce", "code": "lima_failed"}) in harness.logs
    assert f"{PREFIX}arp-0000abcf" not in harness.fake.instances


def test_ensure_vm_waits_for_the_reconciliation(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=0)

    async def body(manager: VmManager) -> Any:
        waiting = asyncio.ensure_future(manager.ensure_vm(ORG, WG_A))
        await asyncio.sleep(0.05)
        early = waiting.done()
        await manager.reconcile()
        await manager.wait_reconciled()
        return early, await waiting

    early, row = harness.run(body, reconcile=False)
    assert early is False
    assert row.state == "running"


def test_wait_refill(harness: Harness) -> None:
    async def body(manager: VmManager) -> Any:
        manager.wake_refill()
        await manager.wait_refill(60)
        before = len(harness.clock.sleeps)
        await manager.wait_refill(60)
        slept = harness.clock.sleeps[before:]
        manager.env.sleep = asyncio.sleep  # a real sleep that the wake-up interrupts
        waiter = asyncio.ensure_future(manager.wait_refill(30))
        await asyncio.sleep(0)
        manager.wake_refill()
        await asyncio.wait_for(waiter, 5)
        return slept, manager._refill_wanted.is_set()

    slept, flag = harness.run(body, reconcile=False)
    assert slept == [60]
    assert flag is False


def test_touch_and_capacity(harness: Harness) -> None:
    harness.add_golden()

    async def body(manager: VmManager) -> Any:
        await manager.refill()
        capacity = await manager.capacity()
        row = (await records.live(manager.env.db, "pool"))[0]
        await manager.touch(row.id)
        return capacity, await records.get(manager.env.db, row.id)

    capacity, row = harness.run(body)
    assert (capacity.slots_total, capacity.slots_free, capacity.pool_ready) == (4, 2, 2)
    assert row.last_session_at is not None


# -- races and interruptions (review of VM-02) ------------------------------------------------------------------


def test_reconcile_wait_has_a_limit(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lifecycle, "RECONCILE_WAIT_S", 0.01)

    async def body(manager: VmManager) -> Any:
        assert manager.reconciled is False
        with pytest.raises(RunnerError) as caught:
            await manager.ensure_vm(ORG, WG_A)
        return caught.value.code, caught.value.name

    assert harness.run(body, reconcile=False) == ("vm_start_failed", "reconcile")


def test_cancelled_clone_leaves_no_row_instance_or_token(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=0)

    async def body(manager: VmManager) -> Any:
        original = manager.provisioner._provision

        async def provision(*args: Any, **kwargs: Any) -> None:
            await original(*args, **kwargs)
            raise asyncio.CancelledError

        manager.provisioner._provision = provision  # type: ignore[method-assign]
        with pytest.raises(asyncio.CancelledError):
            await manager.ensure_vm(ORG, WG_A)
        return manager.reserved

    assert harness.run(body) == 0
    assert [row["state"] for row in harness.vms()] == ["deleted"]
    assert sorted(harness.fake.instances) == [GOLDEN_REF.lima_name]
    assert list(harness.tokens_dir.glob("*.token")) == []


def test_clone_row_names_the_instance_of_the_retry(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=0)
    harness.fake.fail["start"] = (1, "", "boom\n")
    row = harness.run(ensure())
    clones = [argv[-1] for argv, _ in harness.fake.calls if argv[0] == "clone"]
    assert row.lima_name == clones[-1]
    assert len(clones) == 2


def test_clone_whose_row_vanished(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=0)

    async def get(_db: Any, _vm_id: str) -> Any:
        raise OSError("store gone")

    async def body(manager: VmManager) -> Any:
        async def boom(*_a: Any, **_k: Any) -> str:
            monkeypatch.setattr(records, "get", get)
            raise RunnerError("vm_start_failed")

        manager.provisioner.create = boom  # type: ignore[method-assign]
        with pytest.raises(RunnerError):
            await manager.ensure_vm(ORG, WG_A)

    harness.run(body)
    # The row could not be read back: nothing was deleted, the reconciliation of the next start removes it.
    assert [row["state"] for row in harness.vms()] == ["assigning"]


def test_restart_of_a_vanished_instance_frees_the_workgroup(harness: Harness) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=0)
    stopped = stop_vm_of(harness)
    harness.fake.instances.pop(stopped.lima_name)

    async def body(manager: VmManager) -> Any:
        manager._reconciled.set()
        with pytest.raises(RunnerError):
            await manager.ensure_vm(ORG, WG_A)
        return await manager.ensure_vm(ORG, WG_A)

    fresh = harness.run(body, reconcile=False)
    assert fresh.id != stopped.id
    assert {row["id"]: row["state"] for row in harness.vms()}[stopped.id] == "deleted"


def test_restart_whose_row_changed_meanwhile(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    harness.add_golden()
    harness.settings = settings(pool_size=0)
    stopped = stop_vm_of(harness)

    async def body(manager: VmManager) -> Any:
        original = manager._start_assigned

        async def start(row: Any) -> None:
            await original(row)
            await records.update(manager.env.db, row.id, state="deleted")

        manager._start_assigned = start  # type: ignore[method-assign]
        with pytest.raises(RunnerError) as caught:
            await manager.ensure_vm(ORG, WG_A)
        return caught.value.code

    assert harness.run(body) == "vm_start_failed"
    assert ("vm.restart_failed", {"vm_id": stopped.id, "code": "vm_state_changed"}) in harness.logs


def test_slot_from_a_pool_vm_that_was_taken_meanwhile(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    harness.add_golden()
    harness.physical_mib = 32768 + 2 * 8192
    harness.settings = settings(pool_size=2)

    async def fill(manager: VmManager) -> Any:
        return await manager.refill()

    harness.run(fill)
    harness.sql("UPDATE runner.golden SET state = 'retired'")
    harness.add_golden("g2", minute=5)
    first, second = (row["id"] for row in harness.live())
    original = records.transition

    async def transition(db: Any, vm_id: str, expect: Any, **fields: Any) -> Any:
        if vm_id == first and fields.get("state") == "deleted":
            return None  # another workgroup took it between the read and the claim
        return await original(db, vm_id, expect, **fields)

    monkeypatch.setattr(records, "transition", transition)
    row = harness.run(ensure())
    assert row.golden_version == "g2"
    assert [f["vm_id"] for n, f in harness.logs if n == "vm.slot_from_pool"] == [second]


def test_refill_counts_reserved_slots(harness: Harness) -> None:
    harness.add_golden()
    harness.physical_mib = 32768 + 8192

    async def body(manager: VmManager) -> Any:
        manager.reserved = 1
        return await manager.refill()

    assert harness.run(body) == 0


def test_tokens_never_appear_in_logs_or_events(harness: Harness) -> None:
    harness.add_golden()
    harness.fake.fail["start"] = (1, "", "boom\n")

    async def body(manager: VmManager) -> Any:
        await manager.refill()
        return await manager.ensure_vm(ORG, WG_A)

    harness.run(body)
    tokens = [path.read_text(encoding="ascii") for path in harness.tokens_dir.glob("*.token")]
    assert tokens
    text = repr(harness.logs) + repr(harness.events)
    assert not any(token in text for token in tokens)


def test_a_hot_path_clone_occupies_one_slot(harness: Harness) -> None:
    # PFM-01 (PERF-05): the reservation of a clone ends when its `assigning` row exists; counting both made 28
    # parallel clones on a device with 60 slots fail one session with `capacity_exhausted`.
    harness.add_golden()
    harness.settings = settings(pool_size=0)

    async def body(manager: VmManager) -> Any:
        original = manager.provisioner._provision
        during: list[int] = []

        async def provision(*args: Any, **kwargs: Any) -> None:
            during.append((await manager.capacity()).slots_free)
            await original(*args, **kwargs)

        manager.provisioner._provision = provision  # type: ignore[method-assign]
        before = (await manager.capacity()).slots_free
        await manager.ensure_vm(ORG, WG_A)
        return before, during, (await manager.capacity()).slots_free, manager.reserved

    before, during, after, reserved = harness.run(body)
    assert during == [before - 1]
    assert after == before - 1
    assert reserved == 0


def test_two_clones_never_share_the_last_slot(harness: Harness) -> None:
    # Review of PFM-01: the capacity check awaits the store; without one lock around check and claim two clones of
    # different workgroups both saw the last free slot and both cloned (overbooking the VM memory budget).
    harness.add_golden()
    harness.physical_mib = 32768 + 2 * 8192
    harness.settings = settings(pool_size=0)
    a = harness.run(ensure(wg=WG_A))
    harness.session(a.id, WG_A, "working")

    async def body(manager: VmManager) -> Any:
        original = manager.env.capacity
        arrived: list[int] = []
        both = asyncio.Event()

        async def capacity(in_flight: int = 0) -> Any:
            # Both checks start before either finishes (the store query is the await between check and claim).
            arrived.append(in_flight)
            if len(arrived) == 2:
                both.set()
            if len(arrived) <= 2:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(both.wait(), 0.5)
            return await original(in_flight)

        manager.env.capacity = capacity  # type: ignore[method-assign]
        results = await asyncio.gather(
            manager.ensure_vm(ORG, WG_B), manager.ensure_vm(ORG, WG_C), return_exceptions=True
        )
        codes = sorted(result.code if isinstance(result, RunnerError) else "ok" for result in results)
        return codes, manager.reserved, (await manager.capacity()).slots_free

    assert harness.run(body) == (["capacity_exhausted", "ok"], 0, 0)
    assert len(harness.live()) == 2
