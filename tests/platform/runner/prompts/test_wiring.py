"""U-RUN-prompts: hints (`prompt`, `session.control`), reconcile step 60, the daemon task, the extension points and the
production wiring (05-runner.md sections 1.1, 2.2, 6.3; 03-api.md section 6.2; ADR-016, ADR-027, ADR-032)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_runner import registry
from agent_runner.errors import RunnerError
from agent_runner.guestlink.errors import GuestUnreachable
from agent_runner.inbox.dispatch import consumers as inbox_consumers
from agent_runner.prompts import consumers, deliver, hints, reconcile, session_request, tasks
from agent_runner.prompts.deliver import Deliverer, deliverer_for, parse_delivered, parse_pending
from agent_runner.reconcile.wire import ResponseInvalid
from agent_runner.sessions.extensions import (
    RequestAmender,
    first_prompt_provider,
    request_amenders,
    state_observers,
)
from agent_runner.sessions.service import service_for
from agent_runner.sessions.start import Starter

from ..reconcile.rig import Rig
from ..sessions.kit import KEY, TASK, WG, desired, session_id
from .kit import add_prompt, run, setup

AT = "2026-11-21T12:00:02.000Z"


# -- hints ----------------------------------------------------------------------------------------------------------


def test_the_prompt_hint_pulls_routes_and_wakes(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        sid = await pk.add_session()
        event_id = add_prompt(env, 1, "hinted")
        pk.deliverer.trigger.clear()
        await hints.prompt(env.context, {"type": "prompt", "session_id": sid, "scope_id": WG, "scope_seq": 1})
        assert await pk.rows() == {event_id: "pending"}
        assert pk.deliverer.trigger.is_set()
        # an event the hub does not have, and malformed hints
        await hints.prompt(env.context, {"type": "prompt", "session_id": sid, "scope_id": WG, "scope_seq": 5})
        assert any("hint_event_missing" in line for line in pk.lines)
        await hints.prompt(env.context, {"type": "prompt", "session_id": "x", "scope_id": WG, "scope_seq": 1})
        await hints.prompt(env.context, {"type": "prompt", "session_id": sid, "scope_id": WG, "scope_seq": 0})
        assert env.log().count("hint_invalid type=prompt") == 2

    run(rig, body)


def test_control_hints(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        sid = await pk.add_session(state="working")
        await hints.control(env.context, {"type": "session.control", "session_id": sid, "action": "interrupt"})
        assert ("control", (sid, "interrupt")) in pk.guest.calls
        await hints.control(env.context, {"type": "session.control", "session_id": sid, "action": "stop"})
        assert pk.stops == [sid]
        # invalid hints: no action
        for hint in (
            {"session_id": sid, "action": "suspend"},
            {"session_id": sid, "action": 1},
            {"session_id": "nope", "action": "stop"},
        ):
            await hints.control(env.context, {"type": "session.control", **hint})
        assert env.log().count("hint_invalid type=session.control") == 3
        assert not await pk.deliverer.control("nope", "stop")
        assert not await pk.deliverer.control(sid, "type")
        # an unknown or ended session, a VM that does not run, an unreachable guest
        assert not await pk.deliverer.control(session_id(9), "interrupt")
        assert not await pk.deliverer.control(session_id(9), "stop")
        pk.guest.fail["control"] = GuestUnreachable("control")
        assert not await pk.deliverer.control(sid, "interrupt")
        row = await pk.vm()
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.vm SET state = 'stopped' WHERE id = %s", (row.id,))
        assert not await pk.deliverer.control(sid, "interrupt")
        await pk.set_state(sid, "stopped")
        assert not await pk.deliverer.control(sid, "stop")
        assert pk.stops == [sid]
        # without a stop function the stop is not carried out
        pk.deliverer.stop = None
        await pk.set_state(sid, "idle")
        assert not await pk.deliverer.control(sid, "stop")

    run(rig, body)


def test_hints_of_a_device_that_is_not_enrolled_do_nothing() -> None:
    context = SimpleNamespace(identity=None, log=None)
    asyncio.run(hints.prompt(context, {"session_id": session_id(1), "scope_id": WG, "scope_seq": 1}))
    asyncio.run(hints.control(context, {"session_id": session_id(1), "action": "stop"}))


# -- reconcile step 60 ----------------------------------------------------------------------------------------------


def test_reconcile_step_applies_the_desired_prompts(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        sid = await pk.add_session()
        event_id = add_prompt(env, 1, "p")
        state = {
            "pending_prompts": [
                {"task_id": TASK, "session_id": sid, "scope_id": WG, "scope_seq": 1, "event_id": event_id}
            ],
            "delivered_prompt_event_ids": {},
        }
        await reconcile.prompts_step(SimpleNamespace(desired=state, context=env.context))  # type: ignore[arg-type]
        assert await pk.rows() == {event_id: "pending"}
        await reconcile.prompts_step(SimpleNamespace(desired=None, context=env.context))  # type: ignore[arg-type]
        nothing = SimpleNamespace(desired=state, context=SimpleNamespace(identity=None))
        await reconcile.prompts_step(nothing)  # type: ignore[arg-type]

    run(rig, body)


def test_the_desired_lists_are_checked() -> None:
    sid, wg = session_id(1), WG
    good = {"task_id": TASK, "session_id": sid, "scope_id": wg, "scope_seq": 1, "event_id": session_id(2)}
    assert parse_pending({"pending_prompts": [good]})[0].scope_seq == 1
    assert parse_pending({}) == []
    for bad in ([{**good, "extra": 1}], [{**good, "scope_seq": 0}], [{**good, "event_id": "x"}], ["x"], "x"):
        with pytest.raises(ResponseInvalid):
            parse_pending({"pending_prompts": bad})
    assert parse_delivered({"delivered_prompt_event_ids": {sid: [session_id(2)]}}) == {sid: [session_id(2)]}
    assert parse_delivered({}) == {}
    wrongs: list[object] = [{sid: "x"}, {"x": []}, {sid: ["x"]}, []]
    for wrong in wrongs:
        with pytest.raises(ResponseInvalid):
            parse_delivered({"delivered_prompt_event_ids": wrong})


# -- daemon task ----------------------------------------------------------------------------------------------------


def test_the_task_delivers_until_stopping_and_logs_failed_rounds(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        sid = await pk.add_session()
        event_id = add_prompt(env, 1, "p")
        await pk.route(sid, 1, event_id)
        stopping = asyncio.Event()
        sleeps: list[float] = []
        rounds = [0]
        original = pk.deliverer.run_once

        async def counted() -> None:
            rounds[0] += 1
            if rounds[0] == 2:
                raise RuntimeError("store")
            await original()

        async def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            if rounds[0] >= 3:
                stopping.set()
            await asyncio.sleep(0)

        pk.deliverer.run_once = counted  # type: ignore[method-assign]
        env.context.stopping = stopping
        env.context.host.sleep = sleep
        await asyncio.wait_for(tasks.run_prompts(env.context), 5)
        assert pk.guest.sent() == [event_id]
        assert any("round_failed error=RuntimeError" in line for line in pk.lines)
        assert sleeps[0] == deliver.PERIOD_S

    run(rig, body)


def test_the_task_ends_on_a_device_that_is_not_enrolled() -> None:
    import io  # noqa: PLC0415

    context = SimpleNamespace(identity=None, log=io.StringIO())
    asyncio.run(tasks.run_prompts(context))
    assert "runner.prompts not_enrolled" in context.log.getvalue()


# -- extension points and registrations ----------------------------------------------------------------------------


def test_the_area_is_registered() -> None:
    assert registry.hint_handlers()["prompt"] is hints.prompt
    assert registry.hint_handlers()["session.control"] is hints.control
    assert [step.name for step in registry.reconcile_steps()][-1:] == ["prompts"] or "prompts" in [
        step.name for step in registry.reconcile_steps()
    ]
    assert [(step.order, step.name) for step in registry.reconcile_steps() if step.name == "prompts"] == [
        (60, "prompts")
    ]
    assert "prompts" in [task.name for task in registry.tasks()]
    assert "prompts.events" in [consumer.name for consumer in inbox_consumers()]
    assert consumers.CONSUMERS[0].kinds == frozenset({"prompt"})
    assert first_prompt_provider().__module__ == "agent_runner.prompts.first"
    assert "prompts.delivery" in [observer.name for observer in state_observers()]
    assert next(amender.name for amender in request_amenders()) == "prompts.restart"


@pytest.mark.parametrize(
    ("files", "code"),
    [
        ({"a/session_request.py": "AMENDERS = 'x'\n"}, "registry_invalid"),
        ({"a/session_request.py": "AMENDERS = [1]\n"}, "registry_invalid"),
        (
            {
                "a/session_request.py": (
                    "from agent_runner.sessions.extensions import RequestAmender\n"
                    "AMENDERS = [RequestAmender('Bad Name', print)]\n"
                )
            },
            "registry_invalid",
        ),
        (
            {
                "a/session_request.py": (
                    "from agent_runner.sessions.extensions import RequestAmender\n"
                    "AMENDERS = [RequestAmender('x', print), RequestAmender('x', print)]\n"
                )
            },
            "registry_duplicate",
        ),
    ],
)
def test_invalid_amender_registrations(
    area_package: Callable[[dict[str, str]], str], files: dict[str, str], code: str
) -> None:
    package = area_package(files)
    with pytest.raises(RunnerError) as caught:
        request_amenders(package)
    assert caught.value.code == code
    assert request_amenders(area_package({"b/x.py": ""})) == []


def test_amenders_change_the_start_request_and_need_a_deliverer(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        seen: list[str] = []

        async def amend(context: Any, session: Any, request: dict[str, Any]) -> None:
            seen.append(session.id)
            request["harness"]["context_markdown"] += "- Extra\n"

        pk.kit.session_env.amenders = [RequestAmender("t.extra", amend)]
        assert (await Starter(pk.kit.session_env).start(desired(1))).started
        request = next(value for name, value in pk.guest.calls if name == "create_session")
        assert request["harness"]["context_markdown"].endswith("- Extra\n")
        assert seen == [session_id(1)]
        # the registered amender without a deliverer (device not enrolled) changes nothing
        found: dict[str, Any] = {"harness": {"context_markdown": "x"}}
        await session_request.amend(SimpleNamespace(identity=None), await pk.session(session_id(1)), found)
        assert found == {"harness": {"context_markdown": "x"}}

    run(rig, body)


# -- production wiring ----------------------------------------------------------------------------------------------


class Manager:
    on_unreachable: Any = None

    async def wait_reconciled(self) -> None:
        return None


def test_deliverer_for_builds_one_deliverer_over_the_session_service(rig: Rig, tmp_path: Path) -> None:
    async def body(env: Any) -> None:
        context = env.context
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
        built = deliverer_for(context)
        assert isinstance(built, Deliverer)
        assert deliverer_for(context) is built
        service = service_for(context)
        assert service is not None
        assert built.env is service.env
        assert built.idle_seconds == 120
        assert built.stop == service.schedule_stop
        assert deliverer_for(SimpleNamespace(identity=None)) is None

    run(rig, body)
