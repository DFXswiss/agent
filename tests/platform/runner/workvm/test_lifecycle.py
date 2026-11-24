"""Lifecycle of work VMs added by KOR-43 (ADR-078 sections 2, 4 and 5; docs/platform/spec/13-work-vm.md sections 7
and 12): paused sessions stop their VM and start it again, credit, closed tasks, run VMs, failed deletions and the
sweep, the countdown of an implicit start and the log of a long job."""

from __future__ import annotations

import asyncio
import dataclasses
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from agent_runner.github import snapshot
from agent_runner.operations.extensions import OperationFailed
from agent_runner.store.migrate import migrate
from agent_runner.workvm import records
from agent_runner.workvm.jobs import ShellResult
from agent_runner.workvm.manager import RUN_OUTPUT_LIMIT, CallFailed, Identity, WorkVms
from agent_runner.workvm.records import WorkVm

from .kit import CHILD, ORG, SESSION, TASK, TASK_B, World, session


@pytest.fixture
def world(runner_dsn: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    assert migrate(runner_dsn).status == "applied"
    created = World(dsn=runner_dsn, tmp=tmp_path)

    async def guest_for(env: Any, local: Any) -> Any:
        return created.guest()

    monkeypatch.setattr(snapshot, "guest_for", guest_for)
    return created


@pytest.fixture
def active(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    count = {"n": 0}

    async def active_sessions(db: Any, task_id: str, *, except_id: str | None = None) -> int:
        return count["n"]

    monkeypatch.setattr(records, "active_sessions", active_sessions)
    return count


def rows(world: World) -> list[dict[str, Any]]:
    with psycopg.connect(world.dsn, autocommit=True, row_factory=dict_row) as conn:
        return list(
            conn.execute(
                "SELECT id, state, scope, delete_reason, stopped_at FROM runner.workvm ORDER BY created_at, id"
            )
        )


def states(world: World) -> list[tuple[str, str | None]]:
    return [(body["state"], body["reason"]) for _i, kind, body in world.events if kind == "workvm.state"]


# -- paused sessions -------------------------------------------------------------------------------------------------


def test_a_paused_session_stops_its_vms_and_a_call_starts_them_again(world: World, active: dict[str, int]) -> None:
    async def body(vms: WorkVms) -> tuple[list[str], list[str], dict[str, Any]]:
        await vms.request(session(), {})
        await vms.request(session(), {"scope": "session"})
        active["n"] = 1  # another session of the task works: the task VM stays
        first = await vms.pause(session(), reason="session_paused")
        active["n"] = 0
        second = await vms.pause(session(), reason="credit_exhausted")
        assert await vms.stop(await records.get(vms.deps.db, first[0]), reason="x") is False  # type: ignore[arg-type]
        result = await vms.call(None, session(), "workvm_exec", {"command": "true", "sync": "none"})
        return first, second, result

    first, second, result = world.run(body)
    assert len(first) == 1
    assert len(second) == 1
    assert result["state"] == "running"
    by_scope = {row["scope"]: row for row in rows(world)}
    own, task = by_scope["session"], by_scope["task"]
    assert own["state"] == "running"  # the session's own VM started again
    assert task["state"] == "stopped"
    assert task["stopped_at"] is not None
    assert ("stopped", "session_paused") in states(world)
    assert ("stopped", "credit_exhausted") in states(world)
    assert states(world)[-1] == ("created", None)
    assert ("stop", 0) in world.calls


def test_a_paused_session_with_a_running_job_keeps_its_vm(world: World, active: dict[str, int]) -> None:
    world.scripts["sleep 600"] = (b"", "running", None)

    async def body(vms: WorkVms) -> tuple[list[str], list[str], list[str]]:
        await vms.request(session(), {})
        await vms.call(None, session(), "workvm_exec", {"command": "sleep 600", "sync": "none", "wait_seconds": 0})
        busy = await vms.pause(session(), reason="session_paused", when_idle=True)
        world.helper_fail["list"] = ShellResult(1, b"", False)
        unreadable = await vms.pause(session(), reason="session_paused", when_idle=True)
        del world.helper_fail["list"]
        world.helper.jobs.clear()
        idle = await vms.pause(session(), reason="session_paused", when_idle=True)
        return busy, unreadable, idle

    busy, unreadable, idle = world.run(body)
    assert (busy, unreadable, len(idle)) == ([], [], 1)


def test_a_stopped_vm_is_deleted_after_the_idle_time_and_gives_its_slot_to_a_waiting_request(world: World) -> None:
    async def body(vms: WorkVms) -> None:
        await vms.request(session(), {})
        await vms.request(session(CHILD, task_id=TASK_B), {})
        for vm in await records.live(vms.deps.db, "running"):
            await vms.stop(vm, reason="session_paused")
        assert await vms.idle_pass() == []
        # both slots hold stopped VMs: a new request takes the oldest one's slot
        third = session("0193000a-0000-7000-8000-0000000000c3", task_id="0193000a-0000-7000-8000-0000000000c4")
        await vms.request(third, {})
        world.now += timedelta(seconds=vms.deps.settings.idle_seconds)
        removed = await vms.idle_pass()  # the remaining stopped VM and the now idle running one
        assert len(removed) == 2

    world.run(body)
    assert Counter(row["delete_reason"] for row in rows(world)) == {"session_paused": 2, "idle": 1}


def test_resume_without_memory_or_with_a_failing_start_gives_a_new_vm(world: World) -> None:
    async def body(vms: WorkVms) -> list[bool]:
        first = await vms.request(session(), {})
        vm = await records.get(vms.deps.db, first["workvm_id"])
        assert vm is not None
        await vms.stop(vm, reason="session_paused")
        world.free_mib = 1024
        assert await vms._resume(vm) is None
        world.free_mib = 1_000_000
        second = await vms.request(session(), {})
        vm2 = await records.get(vms.deps.db, second["workvm_id"])
        assert vm2 is not None
        await vms.stop(vm2, reason="session_paused")
        world.fail.add("start")
        gone = await vms._resume(vm2)
        world.fail.discard("start")
        third = await vms.request(session(), {})
        current = await records.get(vms.deps.db, third["workvm_id"])
        assert current is not None
        running = await vms._resume(current)
        deleted = await vms._resume(vm)
        return [gone is None, running is not None, deleted is None, second["created"]]

    assert world.run(body) == [True, True, True, True]
    assert Counter(row["delete_reason"] for row in rows(world)) == {"session_paused": 2, None: 1}


def test_a_failing_stop_deletes_the_vm(world: World) -> None:
    async def body(vms: WorkVms) -> bool:
        created = await vms.request(session(), {})
        world.fail.add("stop")
        return await vms.stop(await records.get(vms.deps.db, created["workvm_id"]), reason="session_paused")  # type: ignore[arg-type]

    assert world.run(body) is False
    assert rows(world)[0]["delete_reason"] == "failed"
    assert states(world)[-1] == ("deleted", "failed")


# -- credit ----------------------------------------------------------------------------------------------------------


def test_without_credit_no_new_vm_and_no_restart(world: World) -> None:
    async def body(vms: WorkVms) -> list[str]:
        created = await vms.request(session(), {})
        await vms.stop(await records.get(vms.deps.db, created["workvm_id"]), reason="credit_exhausted")  # type: ignore[arg-type]
        world.exhausted.add(ORG)
        found: list[str] = []
        with pytest.raises(OperationFailed) as failed:
            await vms.request(session(), {})
        found.append(failed.value.code)
        with pytest.raises(OperationFailed) as failed:
            await vms.request(session(CHILD, task_id=TASK_B), {})
        found.append(failed.value.code)
        with pytest.raises(CallFailed) as call:
            await vms.call(None, session(), "workvm_exec", {"command": "true"})
        found.append(call.value.error)
        return found

    assert world.run(body) == ["credit_exhausted", "credit_exhausted", "unavailable"]


# -- closed tasks, run VMs, deletion --------------------------------------------------------------------------------


def test_closing_a_task_deletes_every_vm_of_it(world: World) -> None:
    async def body(vms: WorkVms) -> list[str]:
        await vms.request(session(), {})
        await vms.obtain(session(), scope="run", size="standard")
        going = await vms.request(session(CHILD), {"scope": "session"})
        await records.update(vms.deps.db, going["workvm_id"], state="deleting")
        await vms.request(session(CHILD, task_id=TASK_B), {})
        return await vms.close_task(TASK)

    world.settings = dataclasses.replace(world.settings, slots=4)
    removed = world.run(body)
    assert len(removed) == 2
    assert Counter(row["delete_reason"] for row in rows(world)) == {"task_closed": 2, None: 2}
    assert states(world).count(("deleted", "task_closed")) == 1  # the run VM writes no work VM event


def test_run_vms_skip_the_per_task_limit_and_the_idle_pass(world: World) -> None:
    world.settings = dataclasses.replace(world.settings, max_per_task=1, slots=3)

    async def body(vms: WorkVms) -> list[str]:
        await vms.request(session(), {})
        run = await vms.obtain(session(), scope="run", size="standard")
        world.now += timedelta(seconds=10_000)
        removed = await vms.idle_pass()
        assert run.id not in removed
        assert run.session_id == SESSION
        return removed

    assert len(world.run(body)) == 1


def test_a_failed_deletion_keeps_the_row_until_the_sweep_deletes_it(world: World) -> None:
    async def body(vms: WorkVms) -> tuple[int, int, int]:
        created = await vms.request(session(), {})
        run = await vms.obtain(session(), scope="run", size="standard")
        world.fail.add("delete")
        await vms.delete(await records.get(vms.deps.db, created["workvm_id"]), reason="released")  # type: ignore[arg-type]
        await vms.delete(run, reason="run_ended")
        assert {row["state"] for row in rows(world)} == {"deleting"}
        assert await records.used_slots(vms.deps.db) == {0, 1}  # no slot is given back for a VM that may run
        failing = await vms.sweep()
        world.fail.discard("delete")
        lock = vms._lock(created["workvm_id"])
        async with lock:
            locked = await vms.sweep()  # the VM being deleted now is left alone, the other goes
        swept = await vms.sweep()
        return failing, locked, swept

    assert world.run(body) == (0, 1, 1)
    assert [row["delete_reason"] for row in rows(world)] == ["released", "run_ended"]
    assert states(world).count(("deleted", "released")) == 1


def test_the_sweep_deletes_orphan_instances_and_logs_a_failure(world: World) -> None:
    world.instances["wp-t-wvm-deadbeef"] = "Running"
    world.instances["wp-t-wbase-0000"] = "Stopped"

    async def body(vms: WorkVms) -> tuple[int, int]:
        world.fail.add("delete")
        failing = await vms.sweep()
        world.fail.discard("delete")
        return failing, await vms.sweep()

    assert world.run(body) == (0, 1)
    assert "wp-t-wvm-deadbeef" not in world.instances
    assert "wp-t-wbase-0000" in world.instances
    assert any("orphan_delete_failed" in line for line in world.logs)


def test_a_sweep_finds_a_row_that_changed_meanwhile(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    async def body(vms: WorkVms) -> int:
        created = await vms.request(session(), {})
        await records.update(vms.deps.db, created["workvm_id"], state="deleting")
        original = records.get

        async def changed(db: Any, workvm_id: str) -> WorkVm | None:
            found = await original(db, workvm_id)
            assert found is not None
            await records.update(db, workvm_id, state="running")
            return await original(db, workvm_id)

        monkeypatch.setattr(records, "get", changed)
        swept = await vms.sweep()
        assert await records.delete_reason(vms.deps.db, created["workvm_id"]) is None
        assert await records.delete_reason(vms.deps.db, "wvm-00000000") is None
        return swept

    assert world.run(body) == 0
    assert rows(world)[0]["state"] == "running"


# -- implicit start, logs --------------------------------------------------------------------------------------------


def test_an_implicit_start_announces_itself_first(world: World) -> None:
    world.run(lambda vms: vms.call(None, session(), "workvm_exec", {"command": "true", "sync": "none"}))
    assert [state for state, _reason in states(world)] == ["starting", "created"]


def test_a_long_job_is_kept_as_a_log_and_the_teardown_waits_for_it(world: World) -> None:
    long_output = b"x" * (RUN_OUTPUT_LIMIT + 10)
    world.scripts["noisy"] = (long_output, "exited", 0)
    world.scripts["broken"] = (long_output, "exited", 0)
    captured: list[tuple[str, str, int]] = []

    async def capture(vm: WorkVm, identity: Identity, job_id: str, output_bytes: int) -> dict[str, Any] | None:
        await asyncio.sleep(0.01)
        if len(captured) == 1:
            captured.append((vm.id, job_id, output_bytes))
            raise OSError("disk full")
        captured.append((vm.id, job_id, output_bytes))
        return {
            "object_id": "0193000a-0000-7000-8000-0000000000c1",
            "bytes": output_bytes,
            "stored_bytes": 1,
            "truncated": True,
        }

    world.capture = capture

    async def body(vms: WorkVms) -> None:
        await vms.call(None, session(), "workvm_exec", {"command": "noisy", "sync": "none"})
        await vms.call(None, session(), "workvm_exec", {"command": "broken", "sync": "none"})
        vm = await records.for_scope(vms.deps.db, task_id=TASK, session_id=None)
        assert vm is not None
        assert vms.captures[vm.id]
        await vms.delete(vm, reason="released")
        assert not vms.captures[vm.id]

    world.run(body)
    assert [output for _vm, _job, output in captured] == [RUN_OUTPUT_LIMIT + 10] * 2
    finals = [body for _i, kind, body in world.events if kind == "workvm.run" and body["state"] == "exited"]
    assert finals[0]["log"]["truncated"] is True
    assert "log" not in finals[1]
    assert any("capture_failed" in line for line in world.logs)


def test_a_capture_of_a_vm_that_is_gone_records_the_run_without_a_log(world: World) -> None:
    world.scripts["noisy"] = (b"y" * (RUN_OUTPUT_LIMIT + 1), "exited", 0)
    calls: list[str] = []

    async def capture(*_args: Any) -> dict[str, Any] | None:
        calls.append("called")
        return None

    world.capture = capture

    async def body(vms: WorkVms) -> None:
        original = records.get

        async def gone(db: Any, workvm_id: str) -> WorkVm | None:
            return None if asyncio.current_task() is not main else await original(db, workvm_id)

        main = asyncio.current_task()
        records_get = records.get
        records.get = gone
        try:
            await vms.call(None, session(), "workvm_exec", {"command": "noisy", "sync": "none"})
            for tasks in vms.captures.values():
                await asyncio.gather(*tasks)
        finally:
            records.get = records_get

    world.run(body)
    assert calls == []
    finals = [body for _i, kind, body in world.events if kind == "workvm.run" and body["state"] == "exited"]
    assert "log" not in finals[0]


def test_a_request_starts_a_stopped_vm_again_and_pause_skips_a_vm_that_does_not_run(
    world: World, active: dict[str, int]
) -> None:
    async def body(vms: WorkVms) -> tuple[dict[str, Any], list[str], list[str]]:
        first = await vms.request(session(), {})
        assert await vms.pause(session(), reason="session_paused") == [first["workvm_id"]]
        again = await vms.pause(session(), reason="session_paused", when_idle=True)  # stopped: not busy, not running
        resumed = await vms.request(session(), {})
        return resumed, again, [first["workvm_id"]]

    resumed, again, first = world.run(body)
    assert again == []
    assert (resumed["workvm_id"], resumed["created"]) == (first[0], False)


def test_a_run_vm_from_the_pool_writes_no_work_vm_event(world: World) -> None:
    world.settings = dataclasses.replace(world.settings, pool_size=1)

    async def body(vms: WorkVms) -> WorkVm:
        assert await vms.pool_pass() == 1
        return await vms.obtain(session(), scope="run", size="standard")

    vm = world.run(body)
    assert vm.scope == "run"
    assert states(world) == []
    assert any("pooled=1" in line for line in world.logs)


def test_the_default_credit_check_never_refuses() -> None:
    from agent_runner.workvm import manager  # noqa: PLC0415

    assert manager._never(ORG) is False
