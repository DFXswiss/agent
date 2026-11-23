"""U-RUN-subagents: registrations, extension points, production wiring and the refusals of the executor and the
request amender (ADR-027 section 4, ADR-041, ADR-038)."""

from __future__ import annotations

import asyncio
import dataclasses
import io
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_runner import registry
from agent_runner.crypto.ids import uuid7
from agent_runner.errors import RunnerError
from agent_runner.operations.extensions import OperationContext, OperationFailed, prechecks
from agent_runner.pings import subagent_finished
from agent_runner.reconcile.engine import Run
from agent_runner.sessions import records
from agent_runner.sessions.extensions import request_amenders, state_observers
from agent_runner.subagents import (
    controller as controller_module,
)
from agent_runner.subagents import (
    executors,
    journal,
    op_prechecks,
    query_tools,
    reconcile,
    session_request,
    session_state,
    tasks,
)
from agent_runner.subagents.controller import Subagents, controller_for
from agent_runner.subagents.extensions import finished_notifier
from agent_runner.subagents.extensions import query_tools as discover_tools
from agent_runner.subagents.rules import cut_text, initial_prompt

from ..operations.test_wiring import configure
from ..reconcile.rig import Rig
from .kit import SKit, run, setup


def test_the_area_is_registered() -> None:
    assert registry.executors()["subagent.request"] is executors.request_subagent
    assert registry.journal_handlers()["platform.query"] is journal.platform_query
    assert prechecks()["subagent.request"] is op_prechecks.limits
    assert ("subagents", 55) in [(step.name, step.order) for step in registry.reconcile_steps()]
    assert "subagents" in [task.name for task in registry.tasks()]
    assert "subagents.child" in [amender.name for amender in request_amenders()]
    assert "subagents.children" in [observer.name for observer in state_observers()]
    assert discover_tools()["get_subagent_result"] is query_tools.get_subagent_result
    # RUN-14 registers the subagent.finished ping
    assert finished_notifier() is subagent_finished.notify
    assert session_request.AMENDERS[0].amend is session_request.amend
    assert session_state.OBSERVERS[0].handle is session_state.observe
    assert reconcile.STEPS[0].run is reconcile.subagents_step
    assert tasks.TASKS[0].run is tasks.run_subagents


def test_tool_and_notifier_registrations(area_package: Callable[[dict[str, str]], str]) -> None:
    package = area_package(
        {"a/query_tools.py": "TOOLS = {'list_tasks': print}\n", "b/subagent_finished.py": "NOTIFY = print\n"}
    )
    found: dict[str, object] = dict(discover_tools(package))
    notifier: object = finished_notifier(package)
    assert found == {"list_tasks": print}
    assert notifier is print


@pytest.mark.parametrize(
    ("files", "code"),
    [
        ({"a/query_tools.py": "TOOLS = []\n"}, "registry_invalid"),
        ({"a/query_tools.py": "TOOLS = {1: print}\n"}, "registry_invalid"),
        ({"a/query_tools.py": "TOOLS = {'x': 1}\n"}, "registry_invalid"),
        (
            {"a/query_tools.py": "TOOLS = {'x': print}\n", "b/query_tools.py": "TOOLS = {'x': print}\n"},
            "registry_duplicate",
        ),
        ({"a/subagent_finished.py": "NOTIFY = 1\n"}, "registry_invalid"),
        (
            {"a/subagent_finished.py": "NOTIFY = print\n", "b/subagent_finished.py": "NOTIFY = print\n"},
            "registry_duplicate",
        ),
    ],
)
def test_invalid_registrations(area_package: Callable[[dict[str, str]], str], files: dict[str, str], code: str) -> None:
    package = area_package(files)
    find = discover_tools if any("query_tools" in name for name in files) else finished_notifier
    with pytest.raises(RunnerError) as caught:
        find(package)
    assert caught.value.code == code


def test_rules() -> None:
    assert initial_prompt("p", "q") == "Purpose: p\n\nq"
    assert cut_text("abc") == "abc"
    assert cut_text("é" * 40000) == "é" * 32768


def test_controller_for_builds_one_controller_over_the_session_service(rig: Rig, tmp_path: Path) -> None:
    async def body(env: Any) -> None:
        from agent_runner.sessions.service import service_for  # noqa: PLC0415

        configure(env.context, tmp_path)
        built = controller_for(env.context)
        assert isinstance(built, Subagents)
        assert controller_for(env.context) is built
        service = service_for(env.context)
        assert service is not None
        assert built.service is service
        assert built.env is service.env
        assert built.tools["get_subagent_result"] is query_tools.get_subagent_result
        assert built.notify is subagent_finished.notify
        assert controller_for(SimpleNamespace(identity=None)) is None

    run(rig, body)


def test_the_task_runs_the_controller_until_stopping(rig: Rig, tmp_path: Path) -> None:
    async def body(env: Any) -> None:
        configure(env.context, tmp_path)
        env.context.stopping.set()
        await tasks.run_subagents(env.context)
        context = SimpleNamespace(identity=None, log=io.StringIO())
        await tasks.run_subagents(context)
        assert "runner.subagents not_enrolled" in context.log.getvalue()

    run(rig, body)


def test_the_reconcile_step_needs_a_desired_state_and_a_controller(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        calls: list[Any] = []

        async def record(desired: Any, book: Any) -> None:
            calls.append((desired, book))

        found.controller.reconcile = record  # type: ignore[method-assign]
        book = object()
        await reconcile.subagents_step(Run(context=env.context, sync=env.sync, book=book, desired=None))  # type: ignore[arg-type]
        await reconcile.subagents_step(Run(context=env.context, sync=env.sync, book=book, desired={"sessions": []}))  # type: ignore[arg-type]
        assert calls == [([], book)]
        context = SimpleNamespace(identity=None)
        await reconcile.subagents_step(Run(context=context, sync=env.sync, book=book, desired={"sessions": []}))  # type: ignore[arg-type]

    run(rig, body)


# -- executor and amender refusals ---------------------------------------------------------------------------------


async def context_for(found: SKit, parent: str, **changes: Any) -> OperationContext:
    op_id = await found.ops.request(parent, "subagent.request", {"purpose": "p", "prompt": "q"})
    op = dataclasses.replace(await found.ops.op(op_id), **changes)
    session = await found.stored(parent)
    params = {"purpose": "p", "prompt": "q"}
    return OperationContext(found.env.context, op, session, params, {}, 1, found.ops.kit.session_env)


async def refused(found: SKit, ctx: OperationContext) -> str:
    with pytest.raises(OperationFailed) as caught:
        await found.controller.start_child(ctx)
    return caught.value.code


def test_the_executor_refuses_what_it_cannot_start(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent = await found.parent()
        assert await refused(found, await context_for(found, parent)) == "upstream_error"  # no child named
        assert await refused(found, await context_for(found, parent, child_session_id=parent)) == "upstream_error"
        ctx = await context_for(found, parent, child_session_id=uuid7())
        no_access = dataclasses.replace(ctx, session=dataclasses.replace(ctx.session, model_access=None))
        assert await refused(found, no_access) == "upstream_error"
        assert await refused(
            found, dataclasses.replace(ctx, session=dataclasses.replace(ctx.session, owner_id=None))
        ) == ("upstream_error")
        for state in ("stopping", "stopped", "failed"):
            ended = dataclasses.replace(ctx, session=dataclasses.replace(ctx.session, state=state))
            assert await refused(found, ended) == "not_found"
        deep = dataclasses.replace(ctx, session=dataclasses.replace(ctx.session, depth=2))
        assert await refused(found, deep) == "limit_exceeded"
        # a start already scheduled for the child ID
        blocker = asyncio.Event()
        waiting = asyncio.ensure_future(blocker.wait())
        found.service.tasks[ctx.operation.child_session_id] = waiting  # type: ignore[index]
        assert await refused(found, ctx) == "upstream_error"
        assert found.controller.specs == {}
        blocker.set()
        await waiting
        found.service.tasks.clear()
        # without a controller (device not enrolled)
        bare = dataclasses.replace(ctx, context=SimpleNamespace(identity=None))
        with pytest.raises(OperationFailed) as caught:
            await executors.request_subagent(bare)
        assert caught.value.code == "upstream_error"

    run(rig, body)


def test_a_known_child_is_its_own_result_and_a_foreign_one_is_refused(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent, other = await found.parent(), await found.parent(2)
        child = uuid7()
        assert (await found.subagent(parent, child=child)).state == "succeeded"
        again = await context_for(found, parent, child_session_id=child)
        assert await found.controller.start_child(again) == {"session_id": child}
        foreign = await context_for(found, other, child_session_id=child)
        assert await refused(found, foreign) == "upstream_error"
        assert await refused(found, await context_for(found, parent, child_session_id=other)) == "upstream_error"

    run(rig, body)


def test_the_amender_refuses_a_child_the_platform_did_not_request(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent = await found.stored(await found.parent())
        child = dataclasses.replace(parent, id=uuid7(), depth=1, parent_id=parent.id)
        request: dict[str, Any] = {"harness": {"context_markdown": "x"}}
        await session_request.amend(env.context, parent, request)  # a main session is not changed
        assert request == {"harness": {"context_markdown": "x"}}
        with pytest.raises(RunnerError, match="subagent_not_requested"):
            await session_request.amend(env.context, child, request)
        spec = controller_module.ChildSpec(uuid7(), parent.id, parent.task_id, parent.vm_id, "p", "q")
        found.controller.specs[child.id] = dataclasses.replace(spec, parent_id=uuid7())
        with pytest.raises(RunnerError):
            await session_request.amend(env.context, child, request)
        found.controller.specs[child.id] = dataclasses.replace(spec, vm_id="vm-other")
        with pytest.raises(RunnerError):
            await session_request.amend(env.context, child, request)
        found.controller.specs[child.id] = spec
        await session_request.amend(env.context, child, request)
        assert request["limits"]["runtime_max_sec"] == 1800
        with pytest.raises(RunnerError):
            await session_request.amend(SimpleNamespace(), child, request)

    run(rig, body)


def test_an_unrequested_child_start_fails(rig: Rig) -> None:
    async def body(env: Any) -> None:
        from ..sessions.kit import desired  # noqa: PLC0415

        found = await setup(env)
        parent = await found.parent()
        item = desired(7, parent_session_id=parent, depth=1)
        report = await found.service.starter.start(item)
        assert (report.started, report.reason) == (False, "agent_start_failed")
        async with env.pool.connection() as conn:
            stored = await records.get(conn, item.session_id)
        assert stored is not None
        assert stored.state == "failed"

    run(rig, body)
