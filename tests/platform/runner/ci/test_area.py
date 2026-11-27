"""The CI area of the runner (`agent_runner.ci.area`): pull requests of the device, CI VMs through the work VM machinery
and the device's admission, the runner process with the JIT configuration on stdin, audit rows (spec 16 section 17.4).
Real PostgreSQL, fake Lima and job helper (`tests/platform/runner/workvm/kit.py`)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from agent_runner.admission import CI
from agent_runner.ci.area import RUN_COMMAND, Area, bearer
from agent_runner.ci.link import LinkRefused
from agent_runner.store.migrate import migrate
from agent_runner.workvm import records
from agent_runner.workvm.billing_vms import work_vms
from agent_runner.workvm.jobs import ShellResult, WorkVmError
from agent_runner.workvm.manager import CallFailed, WorkVms

from ..workvm.kit import START, World
from .kit import ORG, OTHER_OWNER, PR_OWNER, PR_SESSION, PR_TASK, REPO, WG

JIT = "eyJqaXQiOiAidGVzdCJ9" * 4
OWNER = {"org_id": ORG, "wg_id": WG, "task_id": PR_TASK, "session_id": PR_SESSION}


@pytest.fixture
def world(runner_dsn: str, tmp_path: Path) -> World:
    assert migrate(runner_dsn).status == "applied"
    return World(dsn=runner_dsn, tmp=tmp_path)


Body = Callable[[Area, list[Mapping[str, Any]]], Awaitable[Any]]


def _run(world: World, body: Body, **settings: Any) -> Any:
    audits: list[Mapping[str, Any]] = []

    async def enqueue(item: Mapping[str, Any]) -> None:
        audits.append(item)

    async def main(workvms: WorkVms) -> Any:
        area = Area(workvms, enqueue, world.logs.append, **{"poll_s": 0.0, **settings})
        try:
            return await body(area, audits)
        finally:
            await area.close()

    return world.run(main)


async def _pull_operation(area: Area, repo: str, number: Any, *, state: str = "succeeded", n: int = 1) -> None:
    async with area.db.connection() as conn:
        await conn.execute(
            "INSERT INTO runner.operation (id, session_id, task_id, wg_id, org_id, type, state, params, result)"
            " VALUES (%s, %s, %s, %s, %s, 'github.create_pull_request', %s, %s, %s)",
            (
                f"0193000c-0000-7000-8000-{n:012d}",
                PR_SESSION,
                PR_TASK,
                WG,
                ORG,
                state,
                Jsonb({"repo": repo, "head": "feature", "base": "develop", "title": "t"}),
                Jsonb({"number": number, "url": "https://github.test/pr", "id": 1, "draft": True}),
            ),
        )


def test_a_pull_request_counts_only_when_a_session_of_the_device_created_it(world: World) -> None:
    async def body(area: Area, _audits: list[Mapping[str, Any]]) -> None:
        await _pull_operation(area, "Example-Org/Example-Repo", 7)
        await _pull_operation(area, REPO, 8, state="failed", n=2)
        await _pull_operation(area, REPO, "9", n=3)
        assert (await area.handle("pull", {"repo": REPO, "number": 7}))["owner"] == OWNER
        for number in (8, 9, 10):
            assert (await area.handle("pull", {"repo": REPO, "number": number}))["owner"] is None
        for bad in (
            {"repo": "no-slash", "number": 7},
            {"repo": REPO, "number": True},
            {"repo": 5, "number": 7},
            {"repo": REPO, "number": 0},
            {"repo": "o/bad name", "number": 1},
        ):
            with pytest.raises(LinkRefused, match="request_invalid"):
                await area.handle("pull", bad)
        with pytest.raises(LinkRefused, match="unknown_op"):
            await area.handle("format_disk", {})

    _run(world, body)


def test_a_ci_vm_is_admitted_as_kind_ci_and_bills_the_job_s_session(world: World) -> None:
    async def body(area: Area, _audits: list[Mapping[str, Any]]) -> None:
        assert (await area.handle("capacity", {"size": "standard"}))["fits"] is True
        with pytest.raises(LinkRefused, match="request_invalid"):
            await area.handle("capacity", {"size": "huge"})
        answer = await area.handle("vm", {"size": "standard", "owner": OWNER})
        vm = await records.get(area.db, answer["workvm_id"])
        assert vm is not None
        assert vm.scope == "ci"
        assert vm.state == "running"
        assert (vm.task_id, vm.session_id, vm.requested_by, vm.wg_id) == (PR_TASK, PR_SESSION, PR_SESSION, WG)
        assert await area.workvms.active_ci() == 1
        assert await area.workvms.active() == 0
        assert (await area.workvms.deps.admission.active())[CI] == 1
        async with area.db.connection() as conn:
            metered = await work_vms(None, conn)
        assert [(m.vm_kind, m.size, m.sessions) for m in metered] == [("ci", "standard", (PR_SESSION,))]
        assert (await area.handle("vms", {}))["vms"] == [{"workvm_id": vm.id, "state": "running"}]
        await area.handle(
            "rebind",
            {
                "workvm_id": vm.id,
                "owner": {
                    "org_id": OTHER_OWNER.org_id,
                    "wg_id": OTHER_OWNER.wg_id,
                    "task_id": OTHER_OWNER.task_id,
                    "session_id": OTHER_OWNER.session_id,
                },
            },
        )
        rebound = await records.get(area.db, vm.id)
        assert rebound is not None
        assert (rebound.task_id, rebound.session_id) == (
            OTHER_OWNER.task_id,
            OTHER_OWNER.session_id,
        )
        assert await area.handle("end", {"workvm_id": vm.id}) == {}
        assert await records.delete_reason(area.db, vm.id) == "ci_cancelled"
        with pytest.raises(LinkRefused, match="not_found"):
            await area.handle("end", {"workvm_id": vm.id})

    _run(world, body)


def test_refusals_of_the_work_vms_are_answered_with_their_code(world: World) -> None:
    async def body(area: Area, _audits: list[Mapping[str, Any]]) -> None:
        world.exhausted.add(ORG)
        with pytest.raises(LinkRefused, match="credit_exhausted"):
            await area.handle("vm", {"size": "standard", "owner": OWNER})
        world.exhausted.clear()
        with pytest.raises(LinkRefused, match="limit_exceeded"):
            await area.handle("vm", {"size": "xlarge", "owner": OWNER})
        world.fail.add("start")
        with pytest.raises(LinkRefused, match="workvm_unavailable"):
            await area.handle("vm", {"size": "standard", "owner": OWNER})
        world.fail.clear()
        world.boundary = False
        assert (await area.handle("capacity", {"size": "standard"}))["fits"] is False
        with pytest.raises(LinkRefused, match="workvm_unavailable"):
            await area.handle("vm", {"size": "standard", "owner": OWNER})
        for bad in (
            {"size": "huge", "owner": OWNER},
            {"size": "standard", "owner": "x"},
            {"size": "standard", "owner": {**OWNER, "task_id": "nope"}},
        ):
            with pytest.raises(LinkRefused, match="request_invalid"):
                await area.handle("vm", bad)

    _run(world, body)


def test_capacity_follows_slots_memory_and_disk(world: World) -> None:
    async def body(area: Area, _audits: list[Mapping[str, Any]]) -> None:
        world.free_mib = 100
        assert (await area.handle("capacity", {"size": "standard"}))["fits"] is False
        world.free_mib = 1_000_000
        first = await area.handle("vm", {"size": "standard", "owner": OWNER})
        second = await area.handle("vm", {"size": "standard", "owner": OWNER})
        assert first["workvm_id"] != second["workvm_id"]
        assert (await area.handle("capacity", {"size": "standard"}))["fits"] is False  # both slots held

    _run(world, body)


def test_the_disk_quota_bounds_ci_vms(world: World) -> None:
    world.settings = type(world.settings)(**{**world.settings.__dict__, "disk_quota_gib": 10})

    async def body(area: Area, _audits: list[Mapping[str, Any]]) -> None:
        assert (await area.handle("capacity", {"size": "standard"}))["fits"] is False

    _run(world, body)


def test_the_runner_process_gets_the_jit_configuration_on_stdin_and_the_vm_goes_when_it_ends(world: World) -> None:
    world.scripts[RUN_COMMAND] = (b"done", "exited", 0)

    async def body(area: Area, _audits: list[Mapping[str, Any]]) -> str:
        vm_id = (await area.handle("vm", {"size": "standard", "owner": OWNER}))["workvm_id"]
        assert await area.handle("run", {"workvm_id": vm_id, "jit": JIT}) == {}
        await asyncio.gather(*area.watches.values())
        return str(vm_id)

    vm_id = _run(world, body)
    started = world.started[-1]
    assert started["command"] == RUN_COMMAND
    assert started["stdin"] == JIT + "\n"
    assert JIT not in started["command"]
    assert any(line == f"runner.ci vm_deleted workvm_id={vm_id} reason=ci_ended" for line in world.logs)


def test_a_job_over_its_limit_is_killed(world: World) -> None:
    async def body(area: Area, _audits: list[Mapping[str, Any]]) -> None:
        vm_id = (await area.handle("vm", {"size": "standard", "owner": OWNER}))["workvm_id"]
        await area.handle("run", {"workvm_id": vm_id, "jit": JIT})
        await asyncio.gather(*area.watches.values())
        assert await records.delete_reason(area.db, vm_id) == "ci_timeout"

    _run(world, body, job_limit_s=0.0)
    assert any(job.state == "killed" for job in world.helper.jobs.values())


def test_a_vm_that_stops_answering_goes(world: World) -> None:
    async def body(area: Area, _audits: list[Mapping[str, Any]]) -> None:
        vm_id = (await area.handle("vm", {"size": "standard", "owner": OWNER}))["workvm_id"]
        world.helper_fail["wait"] = ShellResult(1, b"", False)
        await area.handle("run", {"workvm_id": vm_id, "jit": JIT})
        await asyncio.gather(*area.watches.values())
        assert await records.delete_reason(area.db, vm_id) == "ci_ended"

    _run(world, body)


def test_end_stops_a_running_runner(world: World) -> None:
    async def body(area: Area, _audits: list[Mapping[str, Any]]) -> None:
        vm_id = (await area.handle("vm", {"size": "standard", "owner": OWNER}))["workvm_id"]
        await area.handle("run", {"workvm_id": vm_id, "jit": JIT})
        await asyncio.sleep(0)
        with pytest.raises(LinkRefused, match="request_invalid"):
            await area.handle("run", {"workvm_id": vm_id, "jit": JIT})  # one runner per VM
        assert await area.handle("end", {"workvm_id": vm_id}) == {}
        assert area.watches == {}
        assert await records.delete_reason(area.db, vm_id) == "ci_ended"

    _run(world, body, poll_s=0.01)


def test_closing_the_area_stops_following_its_runners(world: World) -> None:
    async def body(area: Area, _audits: list[Mapping[str, Any]]) -> str:
        vm_id = (await area.handle("vm", {"size": "standard", "owner": OWNER}))["workvm_id"]
        await area.handle("run", {"workvm_id": vm_id, "jit": JIT})
        await asyncio.sleep(0)
        await area.close()
        assert area.watches == {}
        assert await records.delete_reason(area.db, vm_id) == "ci_ended"
        return str(vm_id)

    _run(world, body, poll_s=0.01)


def test_a_runner_that_does_not_start_takes_its_vm_along(world: World) -> None:
    async def body(area: Area, _audits: list[Mapping[str, Any]]) -> None:
        vm_id = (await area.handle("vm", {"size": "standard", "owner": OWNER}))["workvm_id"]
        world.helper_fail["start"] = ShellResult(1, b"", False)
        with pytest.raises(LinkRefused, match="start_failed"):
            await area.handle("run", {"workvm_id": vm_id, "jit": JIT})
        assert await records.delete_reason(area.db, vm_id) == "ci_cancelled"
        world.helper_fail.clear()
        other = (await area.handle("vm", {"size": "standard", "owner": OWNER}))["workvm_id"]
        original = area.workvms.deps.shell_for

        def broken(vm: Any) -> Any:
            async def call(*_args: Any, **_kwargs: Any) -> ShellResult:
                raise WorkVmError("workvm_unreachable")

            return call

        area.workvms.deps.shell_for = broken
        with pytest.raises(LinkRefused, match="start_failed"):
            await area.handle("run", {"workvm_id": other, "jit": JIT})
        area.workvms.deps.shell_for = original
        for bad in ({"workvm_id": "nope", "jit": JIT}, {"workvm_id": other, "jit": "short"}):
            with pytest.raises(LinkRefused):
                await area.handle("run", bad)
        with pytest.raises(LinkRefused, match="not_found"):
            await area.handle("run", {"workvm_id": "wvm-00000000", "jit": JIT})

    _run(world, body)


def test_a_work_vm_is_no_ci_vm(world: World) -> None:
    async def body(area: Area, _audits: list[Mapping[str, Any]]) -> None:
        vm = await area.workvms.obtain(bearer(PR_OWNER, START), scope="session", size="standard")
        with pytest.raises(LinkRefused, match="not_found"):
            await area.handle("end", {"workvm_id": vm.id})
        assert (await area.handle("vms", {}))["vms"] == []

    _run(world, body)


def test_the_platform_browser_for_the_app_setup(world: World) -> None:
    class Browsers:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict[str, Any]]] = []

        async def call(self, env: Any, session: Any, tool: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
            if arguments.get("url") == "https://down.test/":
                raise CallFailed("unavailable", "no browser")
            self.calls.append((session.id, tool, dict(arguments)))
            return {"url": "https://github.com/x?code=abc", "title": "t", "ok": True, "error": None, "snapshot": "s"}

    browsers = Browsers()

    async def body(area: Area, _audits: list[Mapping[str, Any]]) -> None:
        with pytest.raises(LinkRefused, match="browser_unavailable"):
            await area.handle("browser", {"owner": OWNER, "tool": "browser_snapshot", "arguments": {}})
        area.browsers = browsers
        answer = await area.handle(
            "browser", {"owner": OWNER, "tool": "browser_navigate", "arguments": {"url": "https://github.com/"}}
        )
        assert answer == {"url": "https://github.com/x?code=abc", "title": "t", "ok": True, "error": None}
        with pytest.raises(LinkRefused, match="unavailable"):
            await area.handle(
                "browser", {"owner": OWNER, "tool": "browser_navigate", "arguments": {"url": "https://down.test/"}}
            )
        for bad in (
            {"owner": OWNER, "tool": "browser_type", "arguments": {}},
            {"owner": OWNER, "tool": "browser_click", "arguments": "x"},
        ):
            with pytest.raises(LinkRefused, match="request_invalid"):
                await area.handle("browser", bad)

    _run(world, body)
    assert browsers.calls == [(PR_SESSION, "browser_navigate", {"url": "https://github.com/"})]
    assert not any("code=" in line for line in world.logs)


def test_audit_rows_go_through_the_outbox_with_ids_only(world: World) -> None:
    detail = {
        "stage": "refused",
        "event": "pull_request",
        "basis": None,
        "reason": "pull_request_not_from_agent",
        "result": None,
        "size": None,
        "duration_ms": None,
        "workvm_id": None,
    }

    async def body(area: Area, audits: list[Mapping[str, Any]]) -> None:
        await area.handle("audit", {"task_id": PR_TASK, "target_id": "run:1:2", "result": "denied", "detail": detail})
        assert audits == [
            {
                "action": "ci.job",
                "result": "denied",
                "task_id": PR_TASK,
                "target_kind": "ci_job",
                "target_id": "run:1:2",
                "detail": detail,
            }
        ]
        for bad in (
            {"task_id": "x", "target_id": "run:1:2", "result": "ok", "detail": detail},
            {"task_id": PR_TASK, "target_id": "bad target", "result": "ok", "detail": detail},
            {"task_id": PR_TASK, "target_id": "run:1:2", "result": "maybe", "detail": detail},
            {"task_id": PR_TASK, "target_id": "run:1:2", "result": "ok", "detail": {**detail, "repo": REPO}},
            {"task_id": PR_TASK, "target_id": "run:1:2", "result": "ok", "detail": "x"},
        ):
            with pytest.raises(LinkRefused, match="request_invalid"):
                await area.handle("audit", bad)

    _run(world, body)
