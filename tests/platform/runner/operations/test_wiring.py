"""U-RUN-operations: registrations, extension points, the loop, the daemon task and the production wiring of the
pipeline (05-runner.md sections 1.1 and 12; ADR-027 section 4; ADR-041)."""

from __future__ import annotations

import asyncio
import io
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest

from agent_runner import registry
from agent_runner.errors import RunnerError
from agent_runner.inbox.dispatch import consumers as inbox_consumers
from agent_runner.operations import consumers, executors, journal, pipeline, records, tasks
from agent_runner.operations.extensions import finished_notifier, prechecks
from agent_runner.operations.pipeline import Pipeline, pipeline_for
from agent_runner.pings import op_finished

from ..reconcile.rig import Rig
from ..sessions.kit import KEY
from .kit import ok, run, setup


def test_the_area_is_registered() -> None:
    assert registry.journal_handlers()["operation.request"] is journal.operation_request
    assert registry.executors()["task.set_title"] is executors.set_title
    assert "operations" in [task.name for task in registry.tasks()]
    names = [consumer.name for consumer in inbox_consumers()]
    assert {"operations.settings", "operations.results"} <= set(names)
    assert consumers.CONSUMERS[0].kinds == frozenset({"settings.workgroup"})
    assert consumers.CONSUMERS[1].kinds == frozenset({"operation.result"})
    # RUN-09 registers the pre-checks of its GitHub types; this area registers none itself
    assert not any(check.__module__.startswith("agent_runner.operations") for check in prechecks().values())
    # RUN-14 registers the operation.finished ping
    assert finished_notifier() is op_finished.notify


@pytest.mark.parametrize(
    ("files", "code"),
    [
        ({"a/op_prechecks.py": "PRECHECKS = []\n"}, "registry_invalid"),
        ({"a/op_prechecks.py": "PRECHECKS = {1: print}\n"}, "registry_invalid"),
        ({"a/op_prechecks.py": "PRECHECKS = {'github.commit': 1}\n"}, "registry_invalid"),
        (
            {
                "a/op_prechecks.py": "PRECHECKS = {'github.commit': print}\n",
                "b/op_prechecks.py": "PRECHECKS = {'github.commit': print}\n",
            },
            "registry_duplicate",
        ),
    ],
)
def test_invalid_precheck_registrations(
    area_package: Callable[[dict[str, str]], str], files: dict[str, str], code: str
) -> None:
    with pytest.raises(RunnerError) as caught:
        prechecks(area_package(files))
    assert caught.value.code == code


def test_precheck_and_notifier_registrations(area_package: Callable[[dict[str, str]], str]) -> None:
    package = area_package(
        {"a/op_prechecks.py": "PRECHECKS = {'github.commit': print}\n", "b/op_finished.py": "NOTIFY = print\n"}
    )
    found: dict[str, object] = dict(prechecks(package))
    notifier: object = finished_notifier(package)
    assert found == {"github.commit": print}
    assert notifier is print


@pytest.mark.parametrize(
    ("files", "code"),
    [
        ({"a/op_finished.py": "NOTIFY = 1\n"}, "registry_invalid"),
        ({"a/op_finished.py": "NOTIFY = print\n", "b/op_finished.py": "NOTIFY = print\n"}, "registry_duplicate"),
    ],
)
def test_invalid_notifier_registrations(
    area_package: Callable[[dict[str, str]], str], files: dict[str, str], code: str
) -> None:
    with pytest.raises(RunnerError) as caught:
        finished_notifier(area_package(files))
    assert caught.value.code == code


# -- loop and task -------------------------------------------------------------------------------------------------


def test_the_loop_scans_on_wake_ups_and_works_every_open_operation(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_ = await setup(env, executors={"task.set_title": ok})
        sid = await kit_.session()
        first = await kit_.request(sid, "task.set_title", {"title": "a"})
        second = await kit_.request(sid, "task.set_title", {"title": "b"})
        started = await kit_.pipeline.scan()
        assert started == [first, second]
        assert await kit_.pipeline.scan() == []  # both are running
        await kit_.pipeline.settle()
        assert {(await kit_.op(i)).state for i in (first, second)} == {"succeeded"}
        assert await kit_.pipeline.scan() == []
        # the loop: wake-up, scan, stop
        stopping = asyncio.Event()
        third = await kit_.request(sid, "task.set_title", {"title": "c"})
        loop = asyncio.ensure_future(kit_.pipeline.loop(stopping))
        kit_.pipeline.wake()
        for _ in range(3000):  # up to 30 s (a loaded device), returns as soon as reached
            if (await kit_.op(third)).state == "succeeded":
                break
            await asyncio.sleep(0.01)
        assert (await kit_.op(third)).state == "succeeded"
        stopping.set()
        await loop

    run(rig, body)


def test_failed_scans_and_operations_are_logged_and_the_loop_cancels_its_tasks(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_ = await setup(env, executors={"task.set_title": ok})
        sid = await kit_.session()
        op_id = await kit_.request(sid, "task.set_title", {"title": "a"})
        original = records.pending_work
        calls = [0]

        async def flaky(conn: Any, window: float) -> list[str]:
            calls[0] += 1
            if calls[0] == 1:
                raise psycopg.OperationalError("down")
            return await original(conn, window)

        blocked = asyncio.Event()

        async def hang(_operation_id: str) -> None:
            await blocked.wait()

        kit_.pipeline.process = hang  # type: ignore[method-assign,assignment]
        stopping = asyncio.Event()
        records_module: Any = records
        records_module.pending_work = flaky
        try:
            loop = asyncio.ensure_future(kit_.pipeline.loop(stopping))
            for _ in range(6000):  # up to 30 s (a loaded device), returns as soon as reached
                if op_id in kit_.pipeline.running:
                    break
                await asyncio.sleep(0.01)
            assert op_id in kit_.pipeline.running
            stopping.set()
            await loop
        finally:
            records_module.pending_work = original
        assert any("scan_failed error=OperationalError" in line for line in kit_.lines)
        assert kit_.pipeline.running == {}

        async def broken(_operation_id: str) -> None:
            raise RuntimeError("bug")

        kit_.pipeline.process = broken  # type: ignore[method-assign,assignment]
        await kit_.pipeline.scan()
        await kit_.pipeline.settle()
        assert any("process_failed" in line and "RuntimeError" in line for line in kit_.lines)

    run(rig, body)


def test_the_task_ends_on_a_device_that_is_not_enrolled() -> None:
    context = SimpleNamespace(identity=None, log=io.StringIO())
    asyncio.run(tasks.run_operations(context))
    assert "runner.operations not_enrolled" in context.log.getvalue()


def test_the_listener_wakes_the_pipeline_and_reconnects(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_ = await setup(env)
        woken = asyncio.Event()
        kit_.pipeline.wake = woken.set  # type: ignore[method-assign]
        listener = asyncio.ensure_future(tasks.listen(env.context, kit_.pipeline))
        await asyncio.wait_for(woken.wait(), 5)  # once at connect
        woken.clear()
        async with await psycopg.AsyncConnection.connect(env.context.dsn, autocommit=True) as conn:
            await conn.execute("SELECT pg_notify(%s, '')", (journal.CHANNEL,))
        await asyncio.wait_for(woken.wait(), 5)
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)
        # a database that cannot be reached: logged, retried after LISTEN_RETRY_S
        broken = SimpleNamespace(dsn="postgresql://127.0.0.1:1/x", log=io.StringIO(), host=env.context.host)
        sleeps: list[float] = []

        async def stop_after_one(seconds: float) -> None:
            sleeps.append(seconds)
            raise asyncio.CancelledError

        broken.host = SimpleNamespace(sleep=stop_after_one)
        with pytest.raises(asyncio.CancelledError):
            await tasks.listen(broken, kit_.pipeline)
        assert sleeps == [tasks.LISTEN_RETRY_S]
        assert "runner.operations listen_failed" in broken.log.getvalue()

    run(rig, body)


class Manager:
    on_unreachable: Any = None

    async def wait_reconciled(self) -> None:
        return None


def configure(context: Any, tmp_path: Path) -> None:
    key_file = tmp_path / "key"
    key_file.write_text(KEY)
    key_file.chmod(0o600)
    context.config = SimpleNamespace(
        platform_root_key=context.config.platform_root_key,
        model_catalog=None,
        openrouter_key_file=key_file,
        model_upstream_openrouter="https://openrouter.ai/api",
        session_idle_seconds=120,
    )
    context._vm_manager = Manager()


def test_pipeline_for_builds_one_pipeline_over_the_session_service(rig: Rig, tmp_path: Path) -> None:
    async def body(env: Any) -> None:
        from agent_runner.sessions.service import service_for  # noqa: PLC0415

        context = env.context
        configure(context, tmp_path)
        built = pipeline_for(context)
        assert isinstance(built, Pipeline)
        assert pipeline_for(context) is built
        service = service_for(context)
        assert service is not None
        assert built.env is service.env
        assert built.sealer is env.sync.pusher.sealer
        assert built.executors["task.set_title"] is executors.set_title
        assert built.notify is op_finished.notify
        assert await built.refresh() == 0  # an inbox pull
        assert pipeline_for(SimpleNamespace(identity=None)) is None

    run(rig, body)


def test_pipeline_for_without_an_inbox_refreshes_nothing(rig: Rig, tmp_path: Path, monkeypatch: Any) -> None:
    async def body(env: Any) -> None:
        import agent_runner.inbox.pull as pull_module  # noqa: PLC0415

        configure(env.context, tmp_path)
        monkeypatch.setattr(pull_module, "inbox_for", lambda _context: None)
        built = pipeline_for(env.context)
        assert built is not None
        assert await built.refresh() is None

    run(rig, body)


def test_the_task_runs_the_pipeline_until_stopping(rig: Rig, tmp_path: Path) -> None:
    async def body(env: Any) -> None:
        configure(env.context, tmp_path)
        built = pipeline_for(env.context)
        assert built is not None
        task = asyncio.ensure_future(tasks.run_operations(env.context))
        await asyncio.sleep(0.2)
        env.context.stopping.set()
        await asyncio.wait_for(task, 10)
        assert pipeline.existing(env.context) is built

    run(rig, body)
