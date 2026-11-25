"""Test runs (docs/platform/spec/13-work-vm.md section 12, ADR-080) against a migrated store, a fake Lima, a fake job
helper and a fake guest: a fresh VM per run, the result, the log, the teardown on every path, duplicates, the host
time limit, credit, cancellation and the restart."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from agent_runner.github import snapshot
from agent_runner.guestlink.errors import GuestUnreachable
from agent_runner.operations.extensions import OperationContext, OperationFailed
from agent_runner.store.migrate import migrate
from agent_runner.workvm import jobs, records, testrun
from agent_runner.workvm.jobs import ShellResult
from agent_runner.workvm.manager import WorkVms
from agent_runner.workvm.records import WorkVm
from agent_runner.workvm.testrun import Store, read_output, summarize
from agent_runner.workvm.testrun import TestRuns as Runs

from .kit import ORG, SESSION, TASK, WG, World, session

OP = "0193000a-0000-7000-8000-0000000000f1"
OP_B = "0193000a-0000-7000-8000-0000000000f2"
ACTOR = "0193000a-0000-7000-8000-0000000000f3"


@pytest.fixture
def world(runner_dsn: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    assert migrate(runner_dsn).status == "applied"
    created = World(dsn=runner_dsn, tmp=tmp_path)
    created.workspace = {"repos/o/r/test_calc.py": b"assert 1 + 1 == 2\n"}

    async def guest_for(env: Any, local: Any) -> Any:
        return created.guest()

    monkeypatch.setattr(snapshot, "guest_for", guest_for)
    return created


class Clock:
    """A monotonic clock that advances by `step` seconds on every read."""

    def __init__(self, step: float = 0.0) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def ctx(params: dict[str, Any], operation: str = OP, local: Any = None) -> OperationContext:
    op: Any = SimpleNamespace(id=operation, actor_id=ACTOR)
    return OperationContext(
        context=None, operation=op, session=local or session(), params=params, data={}, attempt=1, env="env"
    )


def runs_for(
    vms: WorkVms,
    *,
    capture: Callable[..., Awaitable[dict[str, Any] | None]] | None = None,
    clock: Clock | None = None,
) -> Runs:
    async def captured(vm: WorkVm, identity: Any, job_id: str, output_bytes: int, ref: str) -> dict[str, Any] | None:
        return {
            "object_id": "0193000a-0000-7000-8000-0000000000c1",
            "bytes": output_bytes,
            "stored_bytes": 1,
            "truncated": False,
        }

    return Runs(
        workvms=vms,
        store=Store(db=vms.deps.db, emit=vms.deps.emit, now=vms.deps.now, log=vms.deps.log),
        capture=capture or captured,
        poll_seconds=0.0,
        clock=clock or Clock(),
    )


def run_rows(world: World) -> list[dict[str, Any]]:
    with psycopg.connect(world.dsn, autocommit=True, row_factory=dict_row) as conn:
        return list(
            conn.execute("SELECT run_id, state, result, workvm_id, digest FROM runner.test_run ORDER BY started_at")
        )


def vm_rows(world: World) -> list[dict[str, Any]]:
    with psycopg.connect(world.dsn, autocommit=True, row_factory=dict_row) as conn:
        return list(
            conn.execute(
                "SELECT id, scope, state, delete_reason, session_id::text AS s, triggered_by::text AS t"
                " FROM runner.workvm"
            )
        )


def events(world: World) -> list[dict[str, Any]]:
    return [body for _identity, kind, body in world.events if kind == "test.run"]


PYTEST_FAIL = b"collected 15 items\n...F..\n==== 3 failed, 12 passed, 1 skipped in 1.20s ====\n"


# -- a run ---------------------------------------------------------------------------------------------------------


def test_a_failing_run_gets_a_fresh_vm_keeps_its_log_and_the_vm_goes(world: World) -> None:
    world.scripts["pytest -q"] = (PYTEST_FAIL, "exited", 1)

    async def body(vms: WorkVms) -> dict[str, Any]:
        return await runs_for(vms).execute(ctx({"kind": "unit", "command": "pytest -q", "cwd": "/workspace/repos/o/r"}))

    result = world.run(body)
    assert result["state"] == "failed"
    assert result["exit_code"] == 1
    assert result["summary"] == {"passed": 12, "failed": 3, "skipped": 1, "errors": None}
    assert result["tail"].endswith("in 1.20s ====\n")
    assert result["log"]["bytes"] == len(PYTEST_FAIL)
    assert result["reason"] is None
    assert result["run_id"].startswith("tr-")
    (vm,) = vm_rows(world)
    assert (vm["scope"], vm["state"], vm["delete_reason"], vm["s"]) == ("run", "deleted", "run_ended", SESSION)
    assert vm["t"] == ACTOR  # the run VM keeps the person whose instruction requested the run (ADR-083 section 2)
    assert world.instances == {}
    (row,) = run_rows(world)
    assert (row["state"], row["result"], row["workvm_id"]) == ("failed", result, vm["id"])
    started = world.started[0]
    assert (started["command"], started["cwd"], started["timeout"]) == ("pytest -q", "/workspace/repos/o/r", "3600")
    assert world.helper.applied[0][0] == "."
    running, final = events(world)
    assert (running["state"], running["ended_at"], running["operation_id"]) == ("running", None, OP)
    assert (final["state"], final["summary"], final["log"]) == ("failed", result["summary"], result["log"])
    assert not [kind for _i, kind, _b in world.events if kind == "workvm.state"]  # a run VM is no work VM
    assert any(line.startswith("runner.testrun ready run_id=") for line in world.logs)


def test_a_passing_run_without_output_has_no_log(world: World) -> None:
    world.scripts["true"] = (b"", "exited", 0)

    async def body(vms: WorkVms) -> dict[str, Any]:
        return await runs_for(vms).execute(ctx({"kind": "lint", "command": "true", "size": "standard"}))

    result = world.run(body)
    assert (result["state"], result["exit_code"], result["log"], result["summary"], result["tail"]) == (
        "passed",
        0,
        None,
        None,
        "",
    )


def test_a_time_limit_of_the_vm_and_a_killed_job(world: World) -> None:
    world.scripts["slow"] = (b"x", "timed_out", None)
    world.scripts["killed"] = (b"x", "killed", 137)

    async def body(vms: WorkVms) -> tuple[dict[str, Any], dict[str, Any]]:
        runs = runs_for(vms)
        first = await runs.execute(ctx({"kind": "unit", "command": "slow"}))
        second = await runs.execute(ctx({"kind": "unit", "command": "killed"}, OP_B))
        return first, second

    first, second = world.run(body)
    assert first["state"] == "timed_out"
    assert (second["state"], second["reason"], second["exit_code"]) == ("cancelled", "cancelled", 137)


def test_the_host_clock_ends_a_run_past_its_time_limit(world: World) -> None:
    world.scripts["forever"] = (b"still running\n", "running", None)

    async def body(vms: WorkVms) -> dict[str, Any]:
        return await runs_for(vms, clock=Clock(step=100.0)).execute(
            ctx({"kind": "integration", "command": "forever", "timeout_seconds": 120})
        )

    result = world.run(body)
    assert result["state"] == "timed_out"
    assert [command for _vm, command in world.shell_calls].count("kill") == 1
    assert vm_rows(world)[0]["state"] == "deleted"


def test_credit_used_up_cancels_a_running_run(world: World) -> None:
    world.scripts["forever"] = (b"", "running", None)

    async def body(vms: WorkVms) -> dict[str, Any]:
        runs = runs_for(vms)
        original = vms.deps.shell_for

        def shell_for(vm: WorkVm) -> Any:
            call = original(vm)

            async def wrapped(argv: Any, **kwargs: Any) -> ShellResult:
                if argv[3] == "wait":
                    world.exhausted.add(ORG)
                return await call(argv, **kwargs)

            return wrapped

        vms.deps.shell_for = shell_for
        return await runs.execute(ctx({"kind": "unit", "command": "forever"}))

    result = world.run(body)
    assert (result["state"], result["reason"]) == ("cancelled", "credit_exhausted")
    assert vm_rows(world)[0]["delete_reason"] == "credit_exhausted"


def test_no_run_starts_without_credit_and_the_checks_fail_the_operation(world: World) -> None:
    async def body(vms: WorkVms) -> list[str]:
        runs = runs_for(vms)
        codes: list[str] = []
        world.exhausted.add(ORG)
        for params in ({"kind": "unit", "command": "x"},):
            with pytest.raises(OperationFailed) as failed:
                await runs.execute(ctx(params))
            codes.append(failed.value.code)
        world.exhausted.clear()
        with pytest.raises(OperationFailed) as failed:
            await runs.execute(ctx({"kind": "unit", "command": "x", "size": "xlarge"}))
        codes.append(failed.value.code)
        world.boundary = False
        with pytest.raises(OperationFailed) as failed:
            await runs.execute(ctx({"kind": "unit", "command": "x"}))
        codes.append(failed.value.code)
        return codes

    assert world.run(body) == ["credit_exhausted", "limit_exceeded", "workvm_unavailable"]
    assert vm_rows(world) == []
    assert run_rows(world) == []


def test_an_unreadable_workspace_fails_the_operation_retryable(world: World) -> None:
    world.export_error = GuestUnreachable("guest_unreachable")

    async def body(vms: WorkVms) -> OperationFailed:
        with pytest.raises(OperationFailed) as failed:
            await runs_for(vms).execute(ctx({"kind": "unit", "command": "x"}))
        return failed.value

    failed = world.run(body)
    assert (failed.code, failed.retryable) == ("upstream_error", True)


def test_a_failed_transfer_start_or_lost_vm_ends_the_run_error_and_the_vm_goes(world: World) -> None:
    async def body(vms: WorkVms) -> list[tuple[str, str | None]]:
        runs = runs_for(vms)
        found = []
        world.helper_fail["apply"] = ShellResult(1, b"", False)
        found.append(await runs.execute(ctx({"kind": "unit", "command": "a"}, OP)))
        del world.helper_fail["apply"]
        world.helper_fail["start"] = ShellResult(1, b"", False)
        found.append(await runs.execute(ctx({"kind": "unit", "command": "b"}, OP_B)))
        del world.helper_fail["start"]
        world.helper_fail["wait"] = ShellResult(255, b"", False)
        found.append(await runs.execute(ctx({"kind": "unit", "command": "c"}, "0193000a-0000-7000-8000-0000000000f3")))
        return [(result["state"], result["reason"]) for result in found]

    assert world.run(body) == [("error", "transfer_failed"), ("error", "start_failed"), ("error", "vm_lost")]
    assert all(row["state"] == "deleted" for row in vm_rows(world))
    assert world.instances == {}


def test_a_failing_capture_keeps_the_tail_and_a_failing_event_keeps_the_result(world: World) -> None:
    world.scripts["pytest"] = (PYTEST_FAIL, "exited", 1)
    world.emit_error = RuntimeError("store down")

    async def broken(*_args: Any) -> dict[str, Any] | None:
        raise OSError("disk full")

    async def body(vms: WorkVms) -> dict[str, Any]:
        return await runs_for(vms, capture=broken).execute(ctx({"kind": "unit", "command": "pytest"}))

    result = world.run(body)
    assert (result["state"], result["log"]) == ("failed", None)
    assert "3 failed" in result["tail"]
    assert any("capture_failed" in line and "OSError" in line for line in world.logs)
    assert any("event_failed" in line for line in world.logs)


def test_a_cancelled_executor_still_tears_the_vm_down(world: World) -> None:
    world.scripts["forever"] = (b"", "running", None)

    async def body(vms: WorkVms) -> None:
        runs = runs_for(vms)
        original = vms.deps.shell_for
        started = asyncio.Event()

        def shell_for(vm: WorkVm) -> Any:
            call = original(vm)

            async def wrapped(argv: Any, **kwargs: Any) -> ShellResult:
                if argv[3] == "wait" and argv[6] == "0":
                    started.set()
                    await asyncio.sleep(0.01)
                return await call(argv, **kwargs)

            return wrapped

        vms.deps.shell_for = shell_for
        task = asyncio.ensure_future(runs.execute(ctx({"kind": "unit", "command": "forever"})))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.2)

    world.run(body)
    (row,) = run_rows(world)
    assert (row["state"], row["result"]["reason"]) == ("error", "vm_lost")
    assert vm_rows(world)[0]["state"] == "deleted"


# -- duplicates and cancellation -----------------------------------------------------------------------------------


def test_the_same_run_on_the_same_state_while_it_runs_is_joined(world: World) -> None:
    world.scripts["pytest"] = (PYTEST_FAIL, "exited", 1)

    async def body(vms: WorkVms) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        runs = runs_for(vms)
        gate = asyncio.Event()
        original = vms.deps.shell_for

        def shell_for(vm: WorkVm) -> Any:
            call = original(vm)

            async def wrapped(argv: Any, **kwargs: Any) -> ShellResult:
                if argv[3] == "start":
                    await gate.wait()
                return await call(argv, **kwargs)

            return wrapped

        vms.deps.shell_for = shell_for
        first = asyncio.ensure_future(runs.execute(ctx({"kind": "unit", "command": "pytest"}, OP)))
        while not runs.active:
            await asyncio.sleep(0)
        second = asyncio.ensure_future(runs.execute(ctx({"kind": "unit", "command": "pytest"}, OP_B)))
        await asyncio.sleep(0.05)
        gate.set()
        a, b = await asyncio.gather(first, second)
        world.workspace["repos/o/r/new.py"] = b"x = 1\n"
        c = await runs.execute(ctx({"kind": "unit", "command": "pytest"}, "0193000a-0000-7000-8000-0000000000f4"))
        return a, b, c

    a, b, c = world.run(body)
    assert a == b
    assert c["run_id"] != a["run_id"]
    assert len(run_rows(world)) == 2
    assert any("joined" in line for line in world.logs)


def test_a_failure_of_the_joined_run_reaches_the_duplicate(world: World) -> None:
    async def body(vms: WorkVms) -> tuple[str, str]:
        runs = runs_for(vms)
        gate = asyncio.Event()
        original = vms.obtain

        async def obtain(*args: Any, **kwargs: Any) -> WorkVm:
            await gate.wait()
            raise OperationFailed("timeout", retryable=True)

        vms.obtain = obtain  # type: ignore[method-assign]
        first = asyncio.ensure_future(runs.execute(ctx({"kind": "unit", "command": "x"}, OP)))
        while not runs.active:
            await asyncio.sleep(0)
        second = asyncio.ensure_future(runs.execute(ctx({"kind": "unit", "command": "x"}, OP_B)))
        await asyncio.sleep(0.05)
        gate.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        vms.obtain = original  # type: ignore[method-assign]
        return tuple(type(item).__name__ for item in results)  # type: ignore[return-value]

    assert world.run(body) == ("OperationFailed", "OperationFailed")


def test_a_session_end_and_a_closed_task_cancel_their_runs(world: World) -> None:
    world.scripts["forever"] = (b"", "running", None)

    async def body(vms: WorkVms) -> list[Any]:
        runs = runs_for(vms)
        found = []
        for operation, reason, by in ((OP, "session_ended", "session"), (OP_B, "task_closed", "task")):
            task = asyncio.ensure_future(runs.execute(ctx({"kind": "unit", "command": "forever"}, operation)))
            while not runs.active:
                await asyncio.sleep(0)
            assert runs.cancel(reason="other", session_id="0193000a-0000-7000-8000-0000000000ff") == []
            if by == "session":
                assert len(runs.cancel(reason=reason, session_id=SESSION)) == 1
            else:
                assert len(runs.cancel(reason=reason, task_id=TASK)) == 1
            found.append(await task)
        return found

    first, second = world.run(body)
    assert (first["state"], first["reason"]) == ("cancelled", "cancelled")
    assert (second["state"], second["reason"]) == ("cancelled", "cancelled")
    assert sorted(row["delete_reason"] for row in vm_rows(world)) == ["session_ended", "task_closed"]


def test_after_a_restart_a_running_run_ends_error(world: World) -> None:
    world.scripts["forever"] = (b"", "running", None)

    async def body(vms: WorkVms) -> int:
        runs = runs_for(vms)
        task = asyncio.ensure_future(runs.execute(ctx({"kind": "unit", "command": "forever"})))
        while not run_rows(world):
            await asyncio.sleep(0.01)
        task.cancel()  # the daemon stopped; its row stays `running` as after a crash
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.2)
        with psycopg.connect(world.dsn, autocommit=True) as conn:
            conn.execute("UPDATE runner.test_run SET state = 'running', result = NULL")
        world.events.clear()
        return await runs_for(vms).reconcile()

    assert world.run(body) == 1
    (row,) = run_rows(world)
    assert (row["state"], row["result"]["reason"]) == ("error", "restart")
    (final,) = events(world)
    assert (final["state"], final["reason"]) == ("error", "restart")
    assert final["ended_at"] is not None


RUN_ID = "tr-00000000000a"


async def left_running(vms: WorkVms, *, command: str = "forever", job: str | None = "j-00000000000a") -> WorkVm:
    """A run of the daemon before its restart: its row `running`, its VM and (when `job`) its job still there."""
    vm = await vms.obtain(session(), scope="run", size="standard")
    if job is not None:
        await vms.deps.shell_for(vm)(
            jobs.start_argv(job, "/workspace", 3600),
            stdin=json.dumps({"command": command, "env": {}}).encode(),
            timeout=1,
            limit=10,
        )
    async with vms.deps.db.connection() as conn:
        await conn.execute(
            "INSERT INTO runner.test_run (run_id, operation_id, session_id, task_id, org_id, wg_id, workvm_id, kind,"
            " command, cwd, size, timeout_seconds, digest, state, started_at) VALUES (%s, %s, %s, %s, %s, %s, %s,"
            " 'unit', 'forever', '/workspace', 'standard', 3600, 'd', 'running', %s)",
            (RUN_ID, OP, SESSION, TASK, ORG, WG, vm.id, vms.deps.now()),
        )
    return vm


def test_after_a_restart_the_log_of_a_run_whose_vm_still_runs_is_kept_before_the_vm_goes(world: World) -> None:
    """ADR-087 section 5."""
    world.scripts["forever"] = (PYTEST_FAIL, "running", None)
    world.scripts["other"] = (b"x", "exited", 0)
    captured: list[tuple[str, str, int, str]] = []

    async def capture(vm: WorkVm, identity: Any, job_id: str, output_bytes: int, ref: str) -> dict[str, Any]:
        assert (await records.get(vms_db[0], vm.id)).state != "deleted"  # type: ignore[union-attr]
        captured.append((vm.id, job_id, output_bytes, ref))
        return {"object_id": "0193000a-0000-7000-8000-0000000000c1", "bytes": output_bytes}

    vms_db: list[Any] = []

    async def body(vms: WorkVms) -> int:
        vms_db.append(vms.deps.db)
        vm = await left_running(vms, job="j-000000000001")
        shell = vms.deps.shell_for(vm)
        await shell(
            jobs.start_argv("j-000000000002", "/", 0), stdin=b'{"command": "other", "env": {}}', timeout=1, limit=1
        )
        await shell(
            jobs.start_argv("j-000000000003", "/", 0), stdin=b'{"command": "forever", "env": {}}', timeout=1, limit=1
        )
        world.helper.jobs["j-000000000001"].output = b"older"
        world.events.clear()
        return await runs_for(vms, capture=capture).reconcile()

    assert world.run(body) == 1
    (vm,) = vm_rows(world)
    assert (vm["state"], vm["delete_reason"]) == ("deleted", "restart")
    assert captured == [(vm["id"], "j-000000000003", len(PYTEST_FAIL), RUN_ID)]  # the newest job of the run's command
    assert world.helper.jobs["j-000000000003"].state == "killed"
    (row,) = run_rows(world)
    assert (row["state"], row["result"]["reason"]) == ("error", "restart")
    assert row["result"]["log"]["bytes"] == len(PYTEST_FAIL)
    assert row["result"]["summary"] == {"passed": 12, "failed": 3, "skipped": 1, "errors": None}
    assert row["result"]["tail"].endswith("in 1.20s ====\n")
    (final,) = events(world)
    assert (final["state"], final["reason"], final["log"]) == ("error", "restart", row["result"]["log"])
    assert any("reason=restart logged=1" in line for line in world.logs)


@pytest.mark.parametrize("case", ["no_job", "other_job", "unreadable", "stopped", "no_vm", "capture_fails"])
def test_after_a_restart_a_run_without_readable_output_ends_without_log(world: World, case: str) -> None:
    world.scripts["forever"] = (b"some output", "running", None)

    async def failing(*_: Any) -> dict[str, Any]:
        raise RuntimeError("store")

    async def body(vms: WorkVms) -> int:
        vm = await left_running(
            vms,
            job=None if case == "no_job" else "j-00000000000a",
            command="other" if case == "other_job" else "forever",
        )
        if case == "unreadable":
            world.helper_fail["list"] = ShellResult(1, b"", False)
        if case == "stopped":
            await records.update(vms.deps.db, vm.id, state="stopped")
        if case == "no_vm":
            async with vms.deps.db.connection() as conn:
                await conn.execute("UPDATE runner.test_run SET workvm_id = NULL")
        world.events.clear()
        return await runs_for(vms, capture=failing if case == "capture_fails" else None).reconcile()

    assert world.run(body) == 1
    (row,) = run_rows(world)
    assert (row["state"], row["result"]["reason"], row["result"]["log"]) == ("error", "restart", None)
    (final,) = events(world)
    assert final["log"] is None
    (vm,) = vm_rows(world)
    # a VM that does not run (or is not named) is left to the work VM reconciliation
    assert (vm["state"], vm["delete_reason"]) == (
        ("stopped", None) if case == "stopped" else ("running", None) if case == "no_vm" else ("deleted", "restart")
    )
    assert any("reason=restart logged=0" in line for line in world.logs)


# -- output ----------------------------------------------------------------------------------------------------------


def test_read_output_yields_the_last_part_in_pieces(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(testrun, "PIECE_BYTES", 4)
    world.scripts["noisy"] = (b"0123456789", "exited", 0)

    async def body(vms: WorkVms) -> tuple[list[bytes], list[bytes]]:
        vm = await vms.obtain(session(), scope="run", size="standard")
        shell = vms.deps.shell_for(vm)
        await shell(
            ["sudo", "-n", "/usr/local/sbin/workvm-job", "start", "j-00000000000a", "/", "0"],
            stdin=json.dumps({"command": "noisy", "env": {}}).encode(),
            timeout=1,
            limit=10,
        )
        tail = [piece async for piece in read_output(shell, "j-00000000000a", total=10, keep=6)]
        short = [piece async for piece in read_output(shell, "j-00000000000a", total=20, keep=20)]
        return tail, short

    tail, short = world.run(body)
    assert tail == [b"4567", b"89"]
    assert short == [b"0123", b"4567", b"89"]  # the output was shorter than announced: it ends where it ends


@pytest.mark.parametrize(
    ("tail", "expected"),
    [
        ("=== 2 passed, 1 warning in 0.10s ===", (2, 0, None, None)),
        ("==== 1 failed, 4 passed, 2 errors in 3.00s (0:00:03) ====", (4, 1, None, 2)),
        ("============ no tests ran in 0.01s ============", None),
        (" Test Files  1 failed (1)\n      Tests  2 failed | 5 passed | 1 skipped (8)\n", (5, 2, 1, None)),
        ("Tests:       1 failed, 1 todo, 9 passed, 11 total", (9, 1, 1, None)),
        ("      Tests  3 passed (3)", (3, 0, None, None)),
        ("Tests:       none", None),
        ("test result: FAILED. 7 passed; 2 failed; 1 ignored; 0 measured", (7, 2, 1, None)),
        ("  5 passing (20ms)\n  1 failing\n  2 pending\n", (5, 1, 2, None)),
        ("  5 passing (20ms)\n", (5, 0, None, None)),
        (
            "--- PASS: TestA (0.00s)\n--- FAIL: TestB (0.00s)\n--- SKIP: TestC (0.00s)\n"
            "FAIL\tpkg/a\t0.1s\nok  \tpkg/b\t0.1s",
            (1, 2, 1, None),
        ),
        ("--- PASS: TestA (0.00s)\nok  \tpkg/b\t0.1s", (1, 0, None, None)),
        ("✖ 4 problems (3 errors, 1 warning)", (None, 4, None, None)),
        ("Found 2 errors.", (None, 2, None, None)),
        ("All checks passed!", None),
    ],
)
def test_summaries_of_common_runners(tail: str, expected: tuple[int | None, ...] | None) -> None:
    found = summarize(tail)
    if expected is None:
        assert found is None
    else:
        assert found is not None
        assert (found["passed"], found["failed"], found["skipped"], found["errors"]) == expected


def test_a_run_whose_vm_a_closed_task_took_ends_cancelled(world: World) -> None:
    world.scripts["forever"] = (b"", "running", None)

    async def body(vms: WorkVms) -> dict[str, Any]:
        runs = runs_for(vms)
        original = vms.deps.shell_for

        def shell_for(vm: WorkVm) -> Any:
            call = original(vm)

            async def wrapped(argv: Any, **kwargs: Any) -> ShellResult:
                if argv[3] == "wait" and argv[6] == "0":
                    runs.cancel(reason="task_closed", task_id=TASK)
                    return ShellResult(255, b"", False)  # the VM is gone already
                return await call(argv, **kwargs)

            return wrapped

        vms.deps.shell_for = shell_for
        return await runs.execute(ctx({"kind": "unit", "command": "forever"}))

    result = world.run(body)
    assert (result["state"], result["reason"]) == ("cancelled", "cancelled")
    assert vm_rows(world)[0]["delete_reason"] == "task_closed"


def test_a_run_of_another_command_is_never_joined(world: World) -> None:
    world.scripts["b"] = (b"b\n", "exited", 0)

    async def body(vms: WorkVms) -> tuple[str, int]:
        runs = runs_for(vms)
        other = testrun.Active(
            run_id="tr-00000000000f",
            key=(SESSION, "unit", "a"),
            session_id=SESSION,
            task_id=TASK,
            org_id=ORG,
            result=asyncio.get_running_loop().create_future(),
        )
        runs.active[other.run_id] = other
        result = await runs.execute(ctx({"kind": "unit", "command": "b"}))
        return result["run_id"], len(runs.active)

    run_id, left = world.run(body)
    assert run_id != "tr-00000000000f"
    assert left == 1


def test_a_store_failure_after_the_vm_started_still_tears_it_down(world: World) -> None:
    async def body(vms: WorkVms) -> None:
        runs = runs_for(vms)

        async def broken(*_args: Any) -> None:
            raise RuntimeError("store down")

        runs._insert = broken  # type: ignore[method-assign,assignment]
        with pytest.raises(RuntimeError):
            await runs.execute(ctx({"kind": "unit", "command": "x"}))

    world.run(body)
    (vm,) = vm_rows(world)
    assert (vm["state"], vm["delete_reason"]) == ("deleted", "run_ended")
    assert world.instances == {}
