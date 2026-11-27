"""Implicit start of a work VM (docs/platform/spec/13-work-vm.md sections 3 and 7): one start per scope in flight,
joined by every further call, answered as `starting` within the guest's budget, reported by `workvm_status`, released
however it ends; the phases of every start are measured; times of the store are written in UTC whatever the local
time zone is. Against a migrated store, a fake Lima, a fake job helper and a fake guest (`kit.py`)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from agent_runner.github import snapshot
from agent_runner.store.migrate import migrate
from agent_runner.workvm import manager as manager_module
from agent_runner.workvm import records
from agent_runner.workvm.jobs import ShellResult
from agent_runner.workvm.manager import (
    NOT_RUNNING,
    START_CANCELLED,
    START_PHASES,
    STARTING,
    CallFailed,
    Starting,
    WorkVms,
)

from .kit import CHILD, SESSION, START, TASK, VM_ID, World, session

THIRD = "0193000a-0000-7000-8000-0000000000a7"
"""A second main session of the task (no parent)."""
FOURTH = "0193000a-0000-7000-8000-0000000000a8"
"""A third main session of the task."""
EXEC = {"command": "true", "sync": "none"}
STARTED_AT = "2026-11-23T10:00:00.000Z"
DID_NOT_START = "No work VM could be obtained: The work VM did not start."


@pytest.fixture
def world(runner_dsn: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    assert migrate(runner_dsn).status == "applied"
    created = World(dsn=runner_dsn, tmp=tmp_path)

    async def guest_for(env: Any, local: Any) -> Any:
        return created.guest()

    monkeypatch.setattr(snapshot, "guest_for", guest_for)
    return created


async def current_guest_vm(vms: WorkVms, guest_version: str = "0.8.0") -> None:
    """The workgroup VM of `session()` on a golden with `guest_version` (`guest_features.current_guest`)."""
    async with vms.deps.db.connection() as conn:
        await conn.execute(
            "INSERT INTO runner.golden (version, lima_name, claude_version, guest_version, image_digest, state)"
            " VALUES ('g-test', 'golden-test', '2.1.330', %s, 'sha256:x', 'ready')",
            (guest_version,),
        )
        await conn.execute(
            "INSERT INTO runner.vm (id, lima_name, golden_version, state, control_token_hash, cpus, memory_mib)"
            " VALUES (%s, 'wp-t-vm', 'g-test', 'running', 'x', 2, 4096)",
            (VM_ID,),
        )


class Gate:
    """Holds the clone of a cold start (`Deps.base_for`) until it is opened; `fail` makes the base fail instead."""

    def __init__(self) -> None:
        self.event = asyncio.Event()
        self.waiting = 0
        self.fail: BaseException | None = None

    async def base_for(self, slot: int, image: str) -> str:
        self.waiting += 1
        await self.event.wait()
        if self.fail is not None:
            raise self.fail
        return image


async def poll(_seconds: float) -> None:
    """`Deps.sleep` that really yields (the kit's moves its clock at once, so a queued request would time out)."""
    await asyncio.sleep(0.01)


async def settle(vms: WorkVms) -> None:
    """Wait until every implicit start in flight has ended."""
    for start in list(vms.starting.values()):
        await asyncio.wait({start.task})
    await asyncio.sleep(0)


def states(world: World) -> list[tuple[str, str | None]]:
    return [(body["state"], body["reason"]) for _, kind, body in world.events if kind == "workvm.state"]


def failures(world: World) -> list[str]:
    """The `error=` of every `implicit_start_failed` log line."""
    return [line.rsplit("error=", 1)[1] for line in world.logs if "implicit_start_failed" in line]


# -- in-flight dedupe ----------------------------------------------------------------------------------------------


def test_two_concurrent_implicit_calls_produce_exactly_one_vm(world: World) -> None:
    """Two calls of the task's main sessions and one of a subagent at once: one task VM, one session VM (the
    subagent's own scope); every call runs its command; no marker is left."""

    async def body(vms: WorkVms) -> tuple[list[dict[str, Any]], dict[Any, Starting]]:
        vms.deps.sleep = poll  # the session VM waits in the queue behind the task VM while that one boots
        results = await asyncio.gather(
            vms.call(None, session(), "workvm_exec", EXEC),
            vms.call(None, session(THIRD), "workvm_exec", EXEC),
            vms.call(None, session(CHILD, parent_id=SESSION), "workvm_exec", EXEC),
        )
        return list(results), dict(vms.starting)

    results, left = world.run(body)
    assert [result["state"] for result in results] == ["running", "running", "running"]
    assert left == {}
    live = world.run(lambda vms: records.live(vms.deps.db))
    assert sorted(vm.scope for vm in live) == ["session", "task"]
    assert len(world.instances) == 2
    assert [state for state, _ in states(world)].count("starting") == 2
    assert [state for state, _ in states(world)].count("created") == 2
    assert len(world.started) == 3
    task_vm = next(vm for vm in live if vm.scope == "task")
    assert task_vm.implicit is True


def test_calls_during_the_start_answer_starting_promptly_and_join_it(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The start is held at the clone: every exec and browse answers `starting` at once (same VM, nothing ran), the
    other tools say it is still starting; once it is up the next exec runs in that one VM."""
    monkeypatch.setattr(manager_module, "START_JOIN_SECONDS", 0.05)
    gate = Gate()

    async def body(vms: WorkVms) -> tuple[list[Any], float, list[str], dict[str, Any]]:
        await current_guest_vm(vms)
        vms.deps.base_for = gate.base_for
        begun = time.monotonic()
        first = await vms.call(None, session(), "workvm_exec", EXEC)
        elapsed = time.monotonic() - begun
        while gate.waiting == 0:  # the start reached the clone
            await asyncio.sleep(0.01)
        answers = [first, await vms.call(None, session(THIRD), "workvm_browse", {"url": "https://example.test/"})]
        refused = []
        others = (("workvm_output", {"job_id": "j-000000000000"}), ("workvm_sync", {"direction": "to_vm"}))
        for tool, arguments in others:
            with pytest.raises(CallFailed) as caught:
                await vms.call(None, session(), tool, arguments)
            refused.append(f"{caught.value.error}: {caught.value.message}")
        gate.event.set()
        await settle(vms)
        ran = await vms.call(None, session(), "workvm_exec", EXEC)
        return answers, elapsed, refused, ran

    answers, elapsed, refused, ran = world.run(body)
    first, second = answers
    assert second == {
        "workvm_id": first["workvm_id"],
        "state": "starting",
        "started_at": STARTED_AT,
        "queue_position": 1,
        "message": STARTING,
    }
    assert (first["state"], first["started_at"], first["message"]) == ("starting", STARTED_AT, STARTING)
    assert elapsed < 5
    assert refused == [f"unavailable: {NOT_RUNNING}"] * 2
    assert gate.waiting == 1
    assert ran["state"] == "running"
    [row] = world.run(lambda vms: records.live(vms.deps.db))
    assert row.id == first["workvm_id"]
    assert len(world.started) == 1  # only the exec after the start ran
    assert states(world) == [("starting", None), ("created", None)]


def test_status_reports_a_start_that_waits_in_the_queue(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """Before its row exists (the start waits behind another request) the start is `starting` with its place in the
    queue; it goes on and the status then shows the running VM."""
    monkeypatch.setattr(manager_module, "START_JOIN_SECONDS", 0.05)

    async def body(vms: WorkVms) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
        await current_guest_vm(vms)
        vms.deps.sleep = poll
        vms.queue.append("earlier")
        before = await vms.call(None, session(), "workvm_status", {})
        await vms.call(None, session(), "workvm_exec", EXEC)
        while vms.starting[("task", TASK)].ticket is None:  # the start waits in the queue
            await asyncio.sleep(0.01)
        answer = await vms.call(None, session(), "workvm_exec", EXEC)
        during = await vms.call(None, session(), "workvm_status", {})
        vms.queue.remove("earlier")
        await settle(vms)
        after = await vms.call(None, session(), "workvm_status", {})
        return before, answer, during, after

    before, answer, during, after = world.run(body)
    assert before == {"workvm": None, "jobs": []}
    assert (answer["state"], answer["queue_position"]) == ("starting", 2)
    assert during == {
        "workvm": {
            "workvm_id": answer["workvm_id"],
            "state": "starting",
            "scope": "task",
            "size": "standard",
            "cpus": 4,
            "memory_mib": 8192,
            "disk_gib": 50,
            "created_at": STARTED_AT,
            "last_used_at": STARTED_AT,
            "started_at": STARTED_AT,
            "queue_position": 2,
        },
        "jobs": [],
    }
    assert after["workvm"]["workvm_id"] == answer["workvm_id"]
    assert after["workvm"]["state"] == "running"
    assert "started_at" not in after["workvm"]


def test_a_guest_before_the_starting_answer_gets_a_prompt_tool_error(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A guest of an older golden refuses fields and answers its contracts do not know (HOT-23): it gets the existing
    tool error `unavailable` of a VM that does not run yet, and the status without `started_at` and
    `queue_position`."""
    monkeypatch.setattr(manager_module, "START_JOIN_SECONDS", 0.05)
    gate = Gate()

    async def body(vms: WorkVms) -> tuple[CallFailed, dict[str, Any]]:
        await current_guest_vm(vms, "0.7.0")
        vms.deps.base_for = gate.base_for
        with pytest.raises(CallFailed) as caught:
            await vms.call(None, session(), "workvm_exec", EXEC)
        status = await vms.call(None, session(), "workvm_status", {})
        gate.event.set()
        await settle(vms)
        return caught.value, status

    failed, status = world.run(body)
    assert (failed.error, failed.message) == ("unavailable", NOT_RUNNING)
    assert status["workvm"]["state"] == "starting"
    assert "started_at" not in status["workvm"]
    assert "queue_position" not in status["workvm"]


def test_a_session_with_its_own_vm_is_not_held_by_the_start_of_the_task_vm(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(manager_module, "START_JOIN_SECONDS", 0.05)
    gate = Gate()

    async def body(vms: WorkVms) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        await current_guest_vm(vms)
        own = await vms.request(session(THIRD), {"scope": "session"})
        vms.deps.base_for = gate.base_for
        starting = await vms.call(None, session(), "workvm_exec", EXEC)
        ran = await vms.call(None, session(THIRD), "workvm_exec", EXEC)
        status = await vms.call(None, session(THIRD), "workvm_status", {})
        gate.event.set()
        await settle(vms)
        return own, starting, ran | {"status": status}

    own, starting, ran = world.run(body)
    assert starting["state"] == "starting"
    assert ran["state"] == "running"
    assert ran["status"]["workvm"]["workvm_id"] == own["workvm_id"]
    assert "queue_position" not in ran["status"]["workvm"]


# -- release on failure and cancellation -----------------------------------------------------------------------------


def test_a_failed_start_releases_the_marker_and_the_next_call_starts_again(world: World) -> None:
    world.fail.add("clone")

    async def body(vms: WorkVms) -> tuple[CallFailed, dict[Any, Starting], dict[str, Any]]:
        with pytest.raises(CallFailed) as caught:
            await vms.call(None, session(), "workvm_exec", EXEC)
        left = dict(vms.starting)
        world.fail.clear()
        ran = await vms.call(None, session(), "workvm_exec", EXEC)
        return caught.value, left, ran

    failed, left, ran = world.run(body)
    assert failed.error == "unavailable"
    assert failed.message == DID_NOT_START
    assert left == {}
    assert ran["state"] == "running"
    assert states(world) == [("starting", None), ("deleted", "failed"), ("starting", None), ("created", None)]
    assert failures(world) == ["upstream_error"]


def test_a_joined_start_that_fails_fails_every_waiting_call(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every call that waits for the start gets its failure; as they saw it, the next call starts anew."""
    monkeypatch.setattr(manager_module, "START_JOIN_SECONDS", 0.05)
    gate = Gate()

    async def body(vms: WorkVms) -> tuple[dict[str, Any], list[CallFailed], dict[str, Any]]:
        await current_guest_vm(vms)
        vms.deps.base_for = gate.base_for
        first = await vms.call(None, session(), "workvm_exec", EXEC)
        monkeypatch.setattr(manager_module, "START_JOIN_SECONDS", 10.0)
        world.fail.add("clone")
        joined = [
            asyncio.ensure_future(vms.call(None, session(sid), "workvm_exec", EXEC)) for sid in (THIRD, FOURTH)
        ]
        await asyncio.sleep(0.2)  # both calls wait for the start held at the clone (it cannot end before the gate)
        gate.event.set()
        failed = []
        for call in joined:
            with pytest.raises(CallFailed) as caught:
                await call
            failed.append(caught.value)
        world.fail.clear()
        return first, failed, await vms.call(None, session(), "workvm_exec", EXEC)

    first, failed, ran = world.run(body)
    assert first["state"] == "starting"
    assert [(call.error, call.message) for call in failed] == [("unavailable", DID_NOT_START)] * 2
    assert ran["state"] == "running"
    assert gate.waiting == 2  # the failed start and the new one


def test_a_start_that_fails_unobserved_is_answered_once_to_the_next_call(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first call already answered `starting` when the start fails: the failure is logged, the countdown ends, no
    row is left, and the next call of the scope gets the failure once instead of silently starting again; the call
    after that starts a new VM."""
    monkeypatch.setattr(manager_module, "START_JOIN_SECONDS", 0.05)
    gate = Gate()

    async def body(vms: WorkVms) -> tuple[dict[str, Any], list[Any], CallFailed, dict[str, Any]]:
        await current_guest_vm(vms)
        vms.deps.base_for = gate.base_for
        first = await vms.call(None, session(), "workvm_exec", EXEC)
        world.fail.add("clone")
        gate.event.set()
        await settle(vms)
        live = await records.live(vms.deps.db)
        with pytest.raises(CallFailed) as caught:
            await vms.call(None, session(THIRD), "workvm_exec", EXEC)
        world.fail.clear()
        monkeypatch.setattr(manager_module, "START_JOIN_SECONDS", 10.0)
        return first, live, caught.value, await vms.call(None, session(), "workvm_exec", EXEC)

    first, live, failed, ran = world.run(body)
    assert first["state"] == "starting"
    assert live == []
    assert (failed.error, failed.message) == ("unavailable", DID_NOT_START)
    assert ran["state"] == "running"
    assert failures(world) == ["upstream_error"]
    assert states(world) == [("starting", None), ("deleted", "failed"), ("starting", None), ("created", None)]
    assert gate.waiting == 2


def test_an_unexpected_error_of_the_start_is_logged_and_leaves_no_row(world: World) -> None:
    """An error that is no operation failure (here after the row was inserted) is logged with its type, ends the
    countdown once, tears the row down, and reaches the call as the failure of the start."""
    gate = Gate()
    gate.fail = RuntimeError("broken base")
    gate.event.set()

    async def body(vms: WorkVms) -> tuple[CallFailed, dict[Any, Starting], list[Any]]:
        vms.deps.base_for = gate.base_for
        with pytest.raises(CallFailed) as caught:
            await vms.call(None, session(), "workvm_exec", EXEC)
        return caught.value, dict(vms.starting), await records.live(vms.deps.db)

    failed, left, live = world.run(body)
    assert (failed.error, failed.message) == ("unavailable", DID_NOT_START)
    assert (left, live) == ({}, [])
    assert failures(world) == ["RuntimeError"]
    assert states(world) == [("starting", None), ("deleted", "failed")]
    assert not any("broken base" in line for line in world.logs)


def test_a_cancelled_start_releases_the_marker_and_ends_the_waiting_calls(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The start is cancelled during its ready check (the runner shuts down): a waiting call ends with a tool error,
    the row and the instance are torn down with exactly one `deleted` event, the marker is gone, and the next call
    starts a new VM that runs."""
    monkeypatch.setattr(manager_module, "START_JOIN_SECONDS", 10.0)
    hold = asyncio.Event()

    async def held(_seconds: float) -> None:
        await hold.wait()

    async def body(vms: WorkVms) -> tuple[CallFailed, dict[Any, Starting], bool, list[Any], dict[str, Any]]:
        world.helper_fail["list"] = ShellResult(1, b"", False)  # the helper does not answer yet
        vms.deps.sleep = held
        waiting = asyncio.ensure_future(vms.call(None, session(), "workvm_exec", EXEC))
        while "Running" not in world.instances.values():  # cloned and booted, in the ready check
            await asyncio.sleep(0.01)
        start = vms.starting[("task", TASK)]
        start.task.cancel()
        with pytest.raises(CallFailed) as caught:
            await waiting
        await asyncio.sleep(0)
        live = await records.live(vms.deps.db)
        world.helper_fail.clear()
        vms.deps.sleep = poll
        ran = await vms.call(None, session(), "workvm_exec", EXEC)
        return caught.value, dict(vms.starting), start.task.cancelled(), live, ran

    failed, left, cancelled, live, ran = world.run(body)
    assert (failed.error, failed.message) == ("unavailable", START_CANCELLED)
    assert left == {}
    assert cancelled is True
    assert live == []
    assert ran["state"] == "running"
    assert len(world.instances) == 1
    [row] = world.run(lambda vms: records.live(vms.deps.db))
    assert row.state == "running"
    assert failures(world) == ["cancelled"]
    assert states(world) == [("starting", None), ("deleted", "failed"), ("starting", None), ("created", None)]


def test_a_call_that_is_cancelled_leaves_the_start_running(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """The guest gives up on a call: the start goes on for the next call of the scope."""
    monkeypatch.setattr(manager_module, "START_JOIN_SECONDS", 10.0)
    gate = Gate()

    async def body(vms: WorkVms) -> tuple[bool, dict[str, Any]]:
        vms.deps.base_for = gate.base_for
        call = asyncio.ensure_future(vms.call(None, session(), "workvm_exec", EXEC))
        while gate.waiting == 0:
            await asyncio.sleep(0.01)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        running = not vms.starting[("task", TASK)].task.done()
        gate.event.set()
        await settle(vms)
        return running, await vms.call(None, session(), "workvm_exec", EXEC)

    running, ran = world.run(body)
    assert running is True
    assert ran["state"] == "running"
    assert len(world.instances) == 1


def test_a_restart_forgets_the_start(world: World) -> None:
    """The marker lives in the manager's memory only: a new manager (a restarted runner) has none and starts anew;
    the reconciliation deleted what the old one left."""

    async def body(vms: WorkVms) -> tuple[dict[Any, Starting], dict[str, Any]]:
        fresh = WorkVms(vms.deps)
        return dict(fresh.starting), await fresh.call(None, session(), "workvm_exec", EXEC)

    left, ran = world.run(body)
    assert left == {}
    assert ran["state"] == "running"


# -- helpers -------------------------------------------------------------------------------------------------------


def test_the_join_wait_fits_the_guest_budget() -> None:
    now = time.monotonic()
    assert manager_module._join_seconds("workvm_exec", {}, now) == pytest.approx(15.0, abs=0.5)
    assert manager_module._join_seconds("workvm_exec", {"wait_seconds": 0}, now) == pytest.approx(2.0, abs=0.5)
    assert manager_module._join_seconds("workvm_browse", {}, now) == pytest.approx(15.0, abs=0.5)
    assert manager_module._join_seconds("workvm_browse", {}, now - 30) == 0.0


def test_the_queue_position_of_a_start(world: World) -> None:
    async def body(vms: WorkVms) -> list[int | None]:
        start = Starting(vms._starting(session(), "task", "wvm-0000000b"))
        found = [vms._queue_position(start)]
        start.ticket = "gone"
        found.append(vms._queue_position(start))
        vms.queue.extend(["first", "gone"])
        found.append(vms._queue_position(start))
        return found

    assert world.run(body) == [None, None, 2]


def test_the_scope_of_an_implicit_start() -> None:
    assert manager_module._start_key(session()) == ("task", TASK)
    assert manager_module._start_key(session(CHILD, parent_id=SESSION)) == ("session", CHILD)


# -- start phases --------------------------------------------------------------------------------------------------


def test_every_phase_of_a_cold_start_is_logged_and_counted(world: World) -> None:
    async def body(vms: WorkVms) -> manager_module.StartTimes:
        await vms.request(session(), {})
        return vms.start_times

    times = world.run(body)
    [line] = [line for line in world.logs if line.startswith("runner.workvm start_phases")]
    parts = dict(part.split("=", 1) for part in line.split()[2:])
    assert (parts["scope"], parts["pooled"]) == ("task", "0")
    assert set(parts) == {"workvm_id", "scope", "pooled", "total_ms"} | {f"{phase}_ms" for phase in START_PHASES}
    assert all(int(parts[f"{phase}_ms"]) >= 0 for phase in START_PHASES)
    assert (times.starts, times.cold) == (1, 1)
    assert set(times.seconds) == set(START_PHASES)
    assert all(seconds >= 0 for seconds in times.seconds.values())


def test_a_start_from_the_warm_pool_is_counted_without_clone(world: World) -> None:
    from dataclasses import replace  # noqa: PLC0415

    world.settings = replace(world.settings, pool_size=1)

    async def body(vms: WorkVms) -> manager_module.StartTimes:
        assert await vms.pool_pass() == 1
        await vms.request(session(), {})
        return vms.start_times

    times = world.run(body)
    [line] = [line for line in world.logs if line.startswith("runner.workvm start_phases")]
    assert " pooled=1 queue_ms=0 clone_ms=0 boot_ms=0 ready_ms=0 total_ms=0" in line
    assert (times.starts, times.cold) == (1, 0)
    assert times.seconds == dict.fromkeys(START_PHASES, 0.0)


# -- times in UTC whatever the local time zone ---------------------------------------------------------------------


@pytest.fixture
def central_european_time(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The device runs in CET (UTC+1 in November): the process' local zone and the session time zone of PostgreSQL
    (`PGTZ`, which libpq sends when it connects), as on a production device."""
    monkeypatch.setenv("TZ", "Europe/Zurich")
    monkeypatch.setenv("PGTZ", "Europe/Zurich")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.mark.usefixtures("central_european_time")
def test_started_at_is_utc_when_the_device_runs_in_another_time_zone(world: World) -> None:
    """Regression: `created`, `stopped` and `deleted` carried `started_at` one hour late (the row's `created_at`, read
    back from PostgreSQL in the device's zone, was formatted with `Z` without converting it)."""

    async def body(vms: WorkVms) -> tuple[timedelta | None, dict[str, Any]]:
        await vms.call(None, session(), "workvm_exec", EXEC)
        [row] = await records.live(vms.deps.db)
        status = await vms.call(None, session(), "workvm_status", {})
        assert await vms.stop(row, reason="session_paused")
        await vms.release(session(), {})
        return row.created_at.utcoffset(), status

    offset, status = world.run(body)
    assert offset == timedelta(hours=1)  # the store really answers in local time
    started = [(body["state"], body["started_at"]) for _, kind, body in world.events if kind == "workvm.state"]
    assert started == [
        ("starting", STARTED_AT),
        ("created", STARTED_AT),
        ("stopped", STARTED_AT),
        ("deleted", STARTED_AT),
    ]
    ended = [body["ended_at"] for _, kind, body in world.events if kind == "workvm.state" and body["ended_at"]]
    assert ended == [STARTED_AT, STARTED_AT]
    assert (status["workvm"]["created_at"], status["workvm"]["last_used_at"]) == (STARTED_AT, STARTED_AT)
    assert world.now == START
