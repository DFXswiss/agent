"""Work VM slots (ADR-059 addendum 4, docs/platform/spec/05-runner.md section 3.7): a slot is held only while its VM
is needed - an idle VM is stopped and gives its slot back, a closed task's VMs give theirs back at once, a waiting
request gets a freed slot at once and in the order the requests came in, a stopped VM starts again with its disk
through the normal assignment, and the slot pass and the start reconciliation resolve slots nobody needs."""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from agent_monitoring import sample
from psycopg.rows import dict_row

from agent_runner.github import snapshot
from agent_runner.metrics import textfile
from agent_runner.metrics import workvm as workvm_metrics
from agent_runner.operations.extensions import OperationFailed
from agent_runner.sessions.records import LocalSession
from agent_runner.store.migrate import migrate
from agent_runner.vm.lima import Instance, LimaError
from agent_runner.workvm import records, service
from agent_runner.workvm.manager import CallFailed, SlotState, Waiter, WorkVms
from agent_runner.workvm.records import WorkVm

from .kit import CHILD, ORG, SESSION, START, TASK, TASK_B, WG, FakeLima, World, session

TASK_C = "0193000a-0000-7000-8000-0000000000c1"
TASK_D = "0193000a-0000-7000-8000-0000000000d1"
TASK_E = "0193000a-0000-7000-8000-0000000000e1"


@pytest.fixture
def world(runner_dsn: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    assert migrate(runner_dsn).status == "applied"
    created = World(dsn=runner_dsn, tmp=tmp_path)

    async def guest_for(env: Any, local: Any) -> Any:
        return created.guest()

    monkeypatch.setattr(snapshot, "guest_for", guest_for)
    return created


def main_of(task_id: str) -> str:
    """The ID of the main session of a task in these tests."""
    return "0193000b" + task_id[8:]


def of(task_id: str) -> LocalSession:
    return session(main_of(task_id), task_id=task_id)


def rows(world: World) -> list[dict[str, Any]]:
    with psycopg.connect(world.dsn, autocommit=True, row_factory=dict_row) as conn:
        return list(conn.execute("SELECT id, state, slot, task_id::text, delete_reason FROM runner.workvm ORDER BY id"))


def by_task(world: World) -> dict[str, dict[str, Any]]:
    return {row["task_id"]: row for row in rows(world) if row["state"] != "deleted"}


def waits(world: World) -> int:
    with psycopg.connect(world.dsn, autocommit=True) as conn:
        row = conn.execute("SELECT count(*) FROM runner.workvm_wait").fetchone()
        assert row is not None
        return int(row[0])


def states(world: World) -> list[tuple[str, str | None]]:
    return [(body["state"], body["reason"]) for _i, kind, body in world.events if kind == "workvm.state"]


def add_session(world: World, session_id: str, task_id: str, state: str) -> None:
    with psycopg.connect(world.dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO runner.session (id, task_id, org_id, wg_id, state, depth, created_at)"
            " VALUES (%s, %s, %s, %s, %s, 0, %s)",
            (session_id, task_id, ORG, WG, state, START),
        )


async def until(check: Callable[[], bool], what: str) -> None:
    """Let the other tasks run (real database round trips) until `check` holds, at most 5 s."""
    for _ in range(500):
        if check():
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"not reached: {what}")


def hold_polls(vms: WorkVms) -> list[float]:
    """Replace the queue's poll by one that never ends: only a slot that becomes free wakes a waiting request."""
    polls: list[float] = []

    async def hold(seconds: float) -> None:
        polls.append(seconds)
        await asyncio.Event().wait()

    vms.deps.sleep = hold
    return polls


async def vm_of(vms: WorkVms, task_id: str) -> WorkVm:
    found = await records.for_scope(vms.deps.db, task_id=task_id, session_id=None)
    assert found is not None
    return found


# -- freed when not needed (Soll 1) ----------------------------------------------------------------------------------


def test_an_idle_vm_gives_its_slot_to_a_waiting_request_after_the_idle_time(world: World) -> None:
    world.settings = dataclasses.replace(world.settings, slots=1)

    async def body(vms: WorkVms) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
        first = await vms.request(of(TASK), {})
        hold_polls(vms)
        waiting = asyncio.ensure_future(vms.request(of(TASK_B), {}))
        await until(lambda: len(vms.queue) == 1, "the second request waits")
        assert waits(world) == 1
        assert await vms.idle_pass() == []  # not idle long enough
        world.now += timedelta(seconds=world.settings.idle_seconds)
        stopped = await vms.idle_pass()
        return first, stopped, await asyncio.wait_for(waiting, 5)

    first, stopped, second = world.run(body)
    assert stopped == [first["workvm_id"]]
    assert second["created"] is True
    found = by_task(world)
    assert (found[TASK]["state"], found[TASK_B]["state"]) == ("stopped", "running")
    assert found[TASK]["slot"] == found[TASK_B]["slot"] == 0  # the stopped VM's disk stays in slot 0's Lima home
    assert ("stopped", "idle") in states(world)
    assert waits(world) == 0


def test_a_vm_with_a_call_in_progress_is_not_idle(world: World) -> None:
    async def body(vms: WorkVms) -> list[str]:
        created = await vms.request(of(TASK), {})
        world.now += timedelta(seconds=world.settings.idle_seconds + 1)
        vms.calls[created["workvm_id"]] += 1  # e.g. a long `workvm_sync`
        return await vms.idle_pass()

    assert world.run(body) == []


def test_the_end_of_a_call_counts_as_activity(world: World) -> None:
    async def body(vms: WorkVms) -> tuple[int, Any]:
        await vms.request(of(TASK), {})
        vm = await vm_of(vms, TASK)
        world.now += timedelta(seconds=30)
        await vms.call(None, of(TASK), "workvm_exec", {"command": "true", "sync": "none"})
        return vms.calls[vm.id], vms.last_call[vm.id]

    assert world.run(body) == (0, START + timedelta(seconds=30))


def test_a_closed_task_gives_its_slot_to_a_waiting_request_at_once(world: World) -> None:
    world.settings = dataclasses.replace(world.settings, slots=1)

    async def body(vms: WorkVms) -> tuple[dict[str, Any], list[float]]:
        await vms.request(of(TASK), {})
        polls = hold_polls(vms)
        waiting = asyncio.ensure_future(vms.request(of(TASK_B), {}))
        await until(lambda: len(polls) == 1, "the second request waits")
        await vms.close_task(TASK)
        return await asyncio.wait_for(waiting, 5), polls

    second, polls = world.run(body)
    assert second["created"] is True
    assert polls == [2.0]  # woken by the freed slot, not by a later poll
    assert {(row["task_id"], row["state"], row["delete_reason"]) for row in rows(world)} == {
        (TASK, "deleted", "task_closed"),
        (TASK_B, "running", None),
    }
    assert waits(world) == 0


# -- in the order they came in; a stopped VM waits for its own slot (Soll 2, 3) --------------------------------------


def test_waiting_requests_go_in_order_and_a_stopped_vm_waits_for_its_own_slot(world: World) -> None:
    async def body(vms: WorkVms) -> tuple[dict[str, Any], dict[str, Any]]:
        first = await vms.request(of(TASK), {})
        await vms.stop(await vm_of(vms, TASK), reason="idle")
        await vms.request(of(TASK_B), {})  # slot 1: a free slot without a stopped VM comes first
        await vms.request(of(TASK_C), {})  # slot 0: the only free one, beside the stopped VM's disk
        hold_polls(vms)
        resumed = asyncio.ensure_future(vms.request(of(TASK), {}))  # waits for its own slot 0
        await until(lambda: len(vms.queue) == 1, "the stopped VM waits")
        new = asyncio.ensure_future(vms.request(of(TASK_D), {}))  # came later
        await until(lambda: len(vms.queue) == 2, "D waits")
        await vms.close_task(TASK_B)  # slot 1 is free: the stopped VM cannot use it, so D goes first
        await asyncio.wait_for(new, 5)
        assert not resumed.done()
        late = asyncio.ensure_future(vms.request(of(TASK_E), {}))
        await until(lambda: len(vms.queue) == 2, "E waits")
        await vms.close_task(TASK_C)  # slot 0 is free: the stopped VM before E
        again = await asyncio.wait_for(resumed, 5)
        assert not late.done()
        await vms.close_task(TASK_D)
        await asyncio.wait_for(late, 5)
        return first, again

    first, again = world.run(body)
    assert (again["workvm_id"], again["created"]) == (first["workvm_id"], False)
    found = by_task(world)
    assert {task: row["slot"] for task, row in found.items()} == {TASK: 0, TASK_E: 1}
    assert waits(world) == 0


def test_a_new_vm_prefers_a_free_slot_without_stopped_vms(world: World) -> None:
    world.settings = dataclasses.replace(world.settings, slots=3)

    async def body(vms: WorkVms) -> tuple[list[int], int | None]:
        await vms.request(of(TASK), {})
        await vms.request(of(TASK_B), {})
        await vms.stop(await vm_of(vms, TASK), reason="idle")  # slot 0 keeps a stopped VM's disk
        await vms.request(of(TASK_C), {})
        await vms.request(of(TASK_D), {})
        return [(await vm_of(vms, task_id)).slot for task_id in (TASK_C, TASK_D)], await vms._free_slot()

    assert world.run(body) == ([2, 0], None)


# -- a stopped VM starts again with its state (Soll 2) ---------------------------------------------------------------


def test_a_stopped_vm_starts_again_with_its_state_on_a_new_request(world: World) -> None:
    world.workspace = {"a.txt": b"one"}
    world.scripts["true"] = (b"", "exited", 0)

    async def body(vms: WorkVms) -> dict[str, Any]:
        first = await vms.call(None, of(TASK), "workvm_exec", {"command": "true"})  # mirrors the workspace
        vm = await vm_of(vms, TASK)
        world.now += timedelta(seconds=world.settings.idle_seconds)
        stopped = await vms.idle_pass()
        status = await vms.call(None, of(TASK), "workvm_status", {})
        again = await vms.request(of(TASK), {})
        second = await vms.call(None, of(TASK), "workvm_exec", {"command": "true"})
        later = await vm_of(vms, TASK)
        return {"vm": vm, "stopped": stopped, "first": first, "status": status, "again": again, "second": second,
                "later": later}  # fmt: skip

    found = world.run(body)
    assert found["stopped"] == [found["vm"].id]
    assert found["status"]["workvm"]["state"] == "stopped"
    assert (found["again"]["workvm_id"], found["again"]["created"]) == (found["vm"].id, False)
    assert found["later"].lima_name == found["vm"].lima_name  # the same instance and disk, no new clone
    assert sum(1 for operation, _ in world.calls if operation == "clone") == 1
    assert [operation for operation, _ in world.calls if operation in ("stop", "start")] == ["start", "stop", "start"]
    assert found["first"]["synced"]["files"] == 1
    assert found["second"]["synced"]["files"] == 0  # what the VM had is still there: nothing to send again
    assert [state for state, _reason in states(world)] == ["starting", "created", "stopped", "created"]


def test_a_stopped_vm_takes_its_slot_back_from_a_ready_pool_entry(world: World) -> None:
    world.settings = dataclasses.replace(world.settings, slots=1, pool_size=1)

    async def body(vms: WorkVms) -> tuple[dict[str, Any], int]:
        await vms.request(of(TASK), {})
        await vms.stop(await vm_of(vms, TASK), reason="idle")
        assert await vms.pool_pass() == 1  # the stopped VM holds no slot: the pool takes slot 0
        again = await vms.request(of(TASK), {})
        return again, len(await records.pool(vms.deps.db))

    again, pool = world.run(body)
    assert (again["created"], pool) == (False, 0)


def test_a_stopped_vm_waits_while_a_starting_pool_entry_holds_its_slot(world: World) -> None:
    world.settings = dataclasses.replace(world.settings, slots=1, queue_seconds=2)

    async def body(vms: WorkVms) -> str:
        await vms.request(of(TASK), {})
        await vms.stop(await vm_of(vms, TASK), reason="idle")
        await records.pool_add(vms.deps.db, records.Pooled("wp-t-wvm-000000ab", 0, "img", 8192, "starting", START))
        with pytest.raises(OperationFailed) as caught:
            await vms.request(of(TASK), {})
        return caught.value.code

    assert world.run(body) == "timeout"
    assert [row["state"] for row in rows(world)] == ["stopped"]  # nothing is lost: the disk stays


def test_a_call_answers_unavailable_while_the_stopped_vm_waits_for_its_slot(world: World) -> None:
    world.settings = dataclasses.replace(world.settings, slots=1, queue_seconds=2)

    async def body(vms: WorkVms) -> CallFailed:
        await vms.request(of(TASK), {})
        await vms.stop(await vm_of(vms, TASK), reason="idle")
        await vms.request(of(TASK_B), {})
        with pytest.raises(CallFailed) as caught:
            await vms.call(None, of(TASK), "workvm_exec", {"command": "true", "sync": "none"})
        return caught.value

    failed = world.run(body)
    assert failed.error == "unavailable"
    assert failed.message.startswith("The stopped work VM could not start yet:")
    assert by_task(world)[TASK]["state"] == "stopped"


def test_a_short_disk_quota_deletes_the_oldest_stopped_vm_visibly(world: World) -> None:
    world.settings = dataclasses.replace(world.settings, disk_quota_gib=100)

    async def body(vms: WorkVms) -> dict[str, Any]:
        await vms.request(of(TASK), {})
        await vms.request(of(TASK_B), {})
        await vms.stop(await vm_of(vms, TASK), reason="idle")  # its slot is free, its 50 GiB are not
        return await vms.request(of(TASK_C), {})

    assert world.run(body)["created"] is True
    [gone] = [row for row in rows(world) if row["state"] == "deleted"]
    assert (gone["task_id"], gone["delete_reason"]) == (TASK, "idle")
    assert ("deleted", "idle") in states(world)


# -- reconciliation (Soll 4) -----------------------------------------------------------------------------------------


def test_the_start_reconciliation_resolves_every_assignment_left_over(world: World) -> None:
    async def body(vms: WorkVms) -> set[int]:
        await vms.request(of(TASK), {})
        await vms.request(of(TASK_B), {})
        await vms.stop(await vm_of(vms, TASK_B), reason="idle")
        await records.wait_add(vms.deps.db, "00000000000000aa", None, START)  # a request of the old daemon
        fresh = WorkVms(vms.deps)  # the restarted runner
        mine = Waiter("00000000000000bb")
        fresh.queue.append(mine)  # obtained while the test runs were reconciled
        await records.wait_add(vms.deps.db, mine.ticket, None, START)
        await fresh.reconcile()
        return await records.used_slots(vms.deps.db)

    assert world.run(body) == set()
    assert {(row["state"], row["delete_reason"]) for row in rows(world)} == {("deleted", "restart")}
    assert "runner.workvm waits_cleared count=1" in world.logs
    assert waits(world) == 1  # the request of the running daemon stays


def test_the_slot_pass_resolves_assignments_without_a_vm(world: World) -> None:
    world.settings = dataclasses.replace(world.settings, slots=4)
    for task_id in (TASK, TASK_B, TASK_C, TASK_D):
        add_session(world, main_of(task_id), task_id, "working")

    async def body(vms: WorkVms) -> tuple[list[str], list[str], list[str]]:
        created = [(await vms.request(of(task_id), {}))["workvm_id"] for task_id in (TASK, TASK_B, TASK_C, TASK_D)]
        _missing, interrupted, _outside, _fine = created
        world.instances.pop((await vm_of(vms, TASK)).lima_name)  # deleted by hand
        await records.update(vms.deps.db, interrupted, state="starting")  # a start nobody finishes
        world.instances[(await vm_of(vms, TASK_C)).lima_name] = "Stopped"  # stopped outside the runner
        changed = await vms.slot_pass()
        return created, changed, await vms.slot_pass()

    (missing, interrupted, outside, fine), changed, again = world.run(body)
    assert sorted(changed) == sorted([missing, interrupted, outside])
    assert again == []  # nothing left to resolve
    by_id = {row["id"]: row for row in rows(world)}
    assert (by_id[missing]["state"], by_id[missing]["delete_reason"]) == ("deleted", "failed")
    assert (by_id[interrupted]["state"], by_id[interrupted]["delete_reason"]) == ("deleted", "failed")
    assert by_id[outside]["state"] == "stopped"  # its disk stays; it starts again when needed
    assert by_id[fine]["state"] == "running"
    assert ("stopped", "failed") in states(world)
    assert sum("slot_released" in line for line in world.logs) == 3
    assert any("cause=instance_missing" in line for line in world.logs)
    assert any("cause=start_interrupted" in line for line in world.logs)
    assert any("cause=not_running" in line for line in world.logs)


def test_the_slot_pass_finishes_an_interrupted_stop_of_a_running_vm(world: World) -> None:
    add_session(world, SESSION, TASK, "idle")

    async def body(vms: WorkVms) -> list[str]:
        created = await vms.request(session(), {})
        await records.update(vms.deps.db, created["workvm_id"], state="stopping")
        return await vms.slot_pass()

    assert len(world.run(body)) == 1
    assert [row["state"] for row in rows(world)] == ["running"]
    assert any("slot_settled" in line for line in world.logs)


def test_the_slot_pass_deletes_a_run_vm_that_no_longer_runs(world: World) -> None:
    async def body(vms: WorkVms) -> list[str]:
        run = await vms.obtain(session(), scope="run", size="standard")
        world.instances[run.lima_name] = "Stopped"
        return await vms.slot_pass()

    assert len(world.run(body)) == 1
    assert [(row["state"], row["delete_reason"]) for row in rows(world)] == [("deleted", "failed")]
    assert states(world) == []  # a run VM writes no work VM event


def test_the_slot_pass_releases_vms_whose_sessions_all_ended(world: World) -> None:
    world.settings = dataclasses.replace(world.settings, slots=4)
    other = "0193000a-0000-7000-8000-0000000000b2"
    add_session(world, SESSION, TASK, "stopped")
    add_session(world, CHILD, TASK, "failed")
    add_session(world, other, TASK_B, "working")

    async def body(vms: WorkVms) -> tuple[list[str], list[str]]:
        task_vm = await vms.request(session(), {})
        own = await vms.request(session(CHILD), {"scope": "session"})
        await vms.request(session(other, task_id=TASK_B), {})
        await vms.obtain(session(), scope="run", size="standard")  # its run ends it, not the slot pass
        return sorted(await vms.slot_pass()), sorted([task_vm["workvm_id"], own["workvm_id"]])

    changed, expected = world.run(body)
    assert changed == expected
    reasons = sorted(row["delete_reason"] for row in rows(world) if row["state"] == "deleted")
    assert reasons == ["session_ended", "session_ended"]
    assert sum("cause=sessions_ended" in line for line in world.logs) == 2


def test_the_slot_pass_leaves_vms_alone_that_are_busy_or_whose_slot_it_cannot_list(world: World) -> None:
    world.settings = dataclasses.replace(world.settings, slots=3)
    for task_id in (TASK, TASK_B, TASK_C):
        add_session(world, main_of(task_id), task_id, "working")

    class Unlisted(FakeLima):
        def list(self) -> list[Instance]:
            raise LimaError("lima_failed", "list", detail='level=fatal msg="no home"')

    async def body(vms: WorkVms) -> list[str]:
        for task_id in (TASK, TASK_B, TASK_C):
            await vms.request(of(task_id), {})
        locked, starting, unlisted = [await vm_of(vms, task_id) for task_id in (TASK, TASK_B, TASK_C)]
        for vm in (locked, starting, unlisted):
            world.instances.pop(vm.lima_name)
        await records.update(vms.deps.db, starting.id, state="starting")
        vms.queue.append(Waiter("00000000000000cc", slot=starting.slot, workvm_id=starting.id))  # being started
        original = vms.deps.lima_for

        def lima_for(slot: int) -> Any:
            return Unlisted(world, slot) if slot == unlisted.slot else original(slot)

        vms.deps.lima_for = lima_for
        async with vms._lock(locked.id):  # being stopped or deleted
            return await vms.slot_pass()

    assert world.run(body) == []
    assert 'runner.workvm slot_check_failed slot=2 code=lima_failed lima="no home"' in world.logs


def test_a_stop_that_finished_meanwhile_is_not_stopped_again(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    add_session(world, SESSION, TASK, "working")

    async def body(vms: WorkVms) -> list[str]:
        created = await vms.request(session(), {})
        world.instances[(await vm_of(vms, TASK)).lima_name] = "Stopped"

        async def settled(vm: WorkVm, *, at_teardown: bool) -> None:
            await records.update(vms.deps.db, created["workvm_id"], state="stopped")  # the other stop was first

        monkeypatch.setattr(vms, "_settle_runs", settled)
        return await vms.slot_pass()

    assert world.run(body) == []
    assert states(world) == [("created", None)]


# -- admitted by the free resources, no fixed cap (extension of the task) --------------------------------------------


def test_new_vms_are_admitted_while_the_free_resources_last(world: World) -> None:
    """No fixed number (ADR-075 addendum 2): with 8 slot accounts, a VM is admitted while the RAM above the reserve
    holds it and the CPU load is below the bound; short of either, the request waits and goes when it is there."""
    world.settings = dataclasses.replace(world.settings, slots=8, queue_seconds=10)
    load = [0.2]

    async def body(vms: WorkVms) -> list[Any]:
        async def cpu() -> float:
            return load[0]

        vms.deps.admission.cpu_load = cpu
        vms.deps.admission.cpu_load_max = 0.85
        world.free_mib = 2 * 8192  # room for two work VMs
        found: list[Any] = []
        for task_id in (TASK, TASK_B):
            found.append((await vms.request(of(task_id), {}))["created"])
            world.free_mib -= 8192  # the started VM holds its RAM
        with pytest.raises(OperationFailed) as short:
            await vms.request(of(TASK_C), {})  # no RAM left: waits, then times out
        found.append(short.value.code)
        world.free_mib = 1_000_000
        load[0] = 0.9  # the CPU is busy: waits until the load is down again

        def calm(w: World) -> None:
            load[0] = 0.5

        world.on_sleep = calm
        found.append((await vms.request(of(TASK_C), {}))["created"])
        found.append(len(world.sleeps))
        return found

    assert world.run(body) == [True, True, "timeout", True, 6]  # 5 polls of the time-out, 1 for the CPU


def test_the_slot_accounts_follow_the_ram() -> None:
    """No setting of a number (ADR-075 addendum 2): the accounts are isolation units, as many as the RAM could hold."""
    from agent_runner.workvm import accounts  # noqa: PLC0415

    reserve = 32768
    assert accounts.slot_count(reserve + 16 * 8192 + 100, reserve) == 16  # e.g. the platform device: 16
    assert accounts.slot_count(reserve + 1000 * 8192, reserve) == accounts.MAX_SLOTS
    assert accounts.slot_count(16384, reserve) == 1  # a small machine still has one


def test_a_waiting_session_holds_back_new_work_vms(world: World) -> None:
    """Rank (05 section 3.8): while a session refused for lack of capacity waits for its next attempt, no new work VM
    starts - also with RAM to spare; once the session started, the work VM goes."""
    from agent_runner.admission import SESSION  # noqa: PLC0415

    world.settings = dataclasses.replace(world.settings, queue_seconds=4)

    async def body(vms: WorkVms) -> list[Any]:
        vms.deps.admission.refused(SESSION)
        with pytest.raises(OperationFailed) as held:
            await vms.request(of(TASK), {})
        vms.deps.admission.started(SESSION)
        return [held.value.code, (await vms.request(of(TASK), {}))["created"]]

    assert world.run(body) == ["timeout", True]


def test_work_vms_give_way_to_workgroup_vms_only_when_idle(world: World) -> None:
    """Sessions rank first: asked for room, the pool goes first, then idle VMs (stopped `idle`), least recently used
    first. Running work is never ended: a busy VM keeps running; run VMs belong to their run."""
    world.settings = dataclasses.replace(world.settings, slots=5, pool_size=1)

    async def body(vms: WorkVms) -> list[Any]:
        assert await vms.pool_pass() == 1
        for task_id in (TASK, TASK_B, TASK_C):
            await vms.request(of(task_id), {})  # the first takes the pool entry; two cold clones
        run = await vms.obtain(of(TASK_D), scope="run", size="standard")
        assert await vms.pool_pass() == 1  # warm again
        world.now += timedelta(seconds=60)
        await vms.call(None, of(TASK_C), "workvm_exec", {"command": "true", "sync": "none"})  # used later than B
        world.helper.jobs.clear()  # (the fake's job table is shared by all VMs)
        vms.calls[(await vm_of(vms, TASK)).id] += 1  # TASK is busy: a call in progress
        first = await vms.give_way(8192)  # the pool entry is enough
        second = await vms.give_way(8192)  # the idle VM of TASK_B (TASK_C was used later, TASK is busy)
        third = await vms.give_way(3 * 8192)  # TASK_C (idle); TASK is busy and keeps running
        left = await vms.give_way(8192)  # only the busy VM and the run VM are left: both stay
        return [first, second, third, left, (await records.get(vms.deps.db, run.id)).state]  # type: ignore[union-attr]

    assert world.run(body) == [8192, 8192, 8192, 0, "running"]
    found = {row["task_id"]: row["state"] for row in rows(world) if row["task_id"] in (TASK, TASK_B, TASK_C)}
    assert found == {TASK: "running", TASK_B: "stopped", TASK_C: "stopped"}
    reasons = [reason for state, reason in states(world) if state == "stopped"]
    assert reasons == ["idle", "idle"]  # TASK_B, then TASK_C
    assert any("gave_way need_mib=24576 released_mib=8192" in line for line in world.logs)


def test_giving_way_takes_only_what_is_needed_and_passes_over_a_vm_that_stopped_meanwhile(world: World) -> None:
    world.settings = dataclasses.replace(world.settings, slots=4, pool_size=2)

    async def body(vms: WorkVms) -> list[Any]:
        filled = await vms.pool_pass() + await vms.pool_pass()  # one entry per pass
        dropped = await vms.give_way(8192)  # one of the two pool entries is enough: the other stays
        kept = len(await records.pool(vms.deps.db))
        await vms.request(of(TASK), {})
        world.helper.jobs.clear()

        async def gone(vm: Any, *, reason: str) -> bool:  # it stopped meanwhile (another path got there first)
            return False

        vms.stop = gone  # type: ignore[method-assign]
        return [filled, dropped, kept, await vms.give_way(8192)]

    assert world.run(body) == [2, 8192, 1, 0]


# -- visible: metrics and status (Soll 5) ----------------------------------------------------------------------------


def test_the_slot_state_and_its_metrics(world: World) -> None:
    world.settings = dataclasses.replace(world.settings, slots=3)

    async def body(vms: WorkVms) -> tuple[SlotState, SlotState | None]:
        await vms.request(of(TASK), {})
        await vms.request(of(TASK_B), {})
        await vms.stop(await vm_of(vms, TASK), reason="idle")
        vms.queue.append(Waiter("00000000000000dd"))
        context = SimpleNamespace()
        service.install(context, vms)
        return await vms.slot_state(), await workvm_metrics.slots(context)

    state, measured = world.run(body)
    assert state == SlotState(
        total=3, held=1, free=2, stopped=1, waiting=1, memory_free_mib=1_000_000, cpu_load=0.0, disk_used_gib=100
    )
    assert measured == state


def test_without_work_vms_there_is_no_slot_state() -> None:
    assert asyncio.run(workvm_metrics.slots(SimpleNamespace())) is None


def test_the_slot_metrics_are_rendered_only_with_work_vms() -> None:
    with_slots = textfile.render(textfile.metrics_of(sample.snapshot(1.0)))
    assert 'agent_runner_workvm_slots{state="held"} 2' in with_slots
    assert 'agent_runner_workvm_slots{state="free"} 2' in with_slots
    assert "agent_runner_workvm_requests_waiting 0" in with_slots
    assert "agent_runner_workvm_stopped 1" in with_slots
    assert f"agent_runner_workvm_memory_free_bytes {16384 * 1024 * 1024}" in with_slots
    assert "agent_runner_workvm_cpu_load_ratio 0.25" in with_slots
    assert f"agent_runner_workvm_disk_used_bytes {150 * 1024**3}" in with_slots
    without = textfile.render(textfile.metrics_of(dataclasses.replace(sample.snapshot(1.0), workvm_slots=None)))
    assert "agent_runner_workvm_" not in without


def test_a_failing_mirror_row_does_not_hold_the_queue(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(db: Any, ticket: str) -> None:
        raise psycopg.OperationalError("gone")

    monkeypatch.setattr(records, "wait_remove", broken)
    assert world.run(lambda vms: vms.request(of(TASK), {}))["created"] is True
    assert waits(world) == 1  # the mirror is for display only; the next start of the daemon clears it
