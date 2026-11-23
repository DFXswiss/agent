"""U-RUN-prompts: the first prompt of a new main session through the session start (05-runner.md section 6.1 step 5,
ADR-032 `FIRST_PROMPT`; 03-api.md sections 6.1 and 7.1)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from agent_runner.prompts import first
from agent_runner.prompts.deliver import ATTRIBUTE
from agent_runner.sessions.start import Starter

from ..reconcile.rig import Rig
from ..sessions.kit import TASK, WG, desired, session_id
from .kit import add_prompt, pull, run, setup

AT = "2026-11-21T12:00:02.000Z"


def routed(sid: str, *pairs: tuple[int, str], delivered: dict[str, list[str]] | None = None) -> dict[str, Any]:
    return {
        "sessions": [],
        "pending_prompts": [
            {"task_id": TASK, "session_id": sid, "scope_id": WG, "scope_seq": seq, "event_id": event_id}
            for seq, event_id in pairs
        ],
        "delivered_prompt_event_ids": delivered or {},
    }


def test_the_routed_first_prompt_goes_with_the_start_and_is_not_sent_again(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        sid = session_id(1)
        event_id = add_prompt(env, 1, "first", text="Fasse das Repo zusammen")
        follow = add_prompt(env, 2, "second")
        # not pulled yet: the provider pulls once
        pk.kit.session_env.desired = routed(sid, (1, event_id), (2, follow))
        report = await Starter(pk.kit.session_env).start(desired(1))
        assert report.started
        request = next(value for name, value in pk.guest.calls if name == "create_session")
        assert request["initial_prompt"] == {"prompt_id": event_id, "text": "Fasse das Repo zusammen"}
        assert "get_task_messages" not in request["harness"]["context_markdown"]
        row = await pk.row(event_id)
        assert (row.state, row.channel) == ("registered", "start")
        # during the start nothing is sent through the prompt endpoint
        await pk.deliverer.advance(sid)
        await pk.journal(sid, state="agent_starting", at=AT)
        assert (await pk.row(event_id)).state == "typed"
        await pk.journal(sid, state="prompt_delivering", at=AT)
        await pk.deliverer.advance(sid)
        assert pk.guest.sent() == []
        # accepted by the hook: working; the second prompt waits for idle
        await pk.journal(sid, state="prompt_accepted", prompt_id=event_id, at=AT)
        assert (await pk.session(sid)).state == "working"
        await pk.deliverer.advance(sid)
        assert pk.guest.sent() == []
        await pk.journal(sid, state="idle", at=AT)
        await pk.deliverer.advance(sid)
        assert pk.guest.sent() == [follow]

    run(rig, body)


def test_a_start_without_a_routed_prompt_waits_for_the_delivery(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        sid = session_id(1)
        report = await Starter(pk.kit.session_env).start(desired(1))
        assert report.started
        request = next(value for name, value in pk.guest.calls if name == "create_session")
        assert "initial_prompt" not in request
        await pk.journal(sid, state="prompt_delivering", at=AT)
        event_id = add_prompt(env, 1, "first")
        await pull(env)
        await pk.deliverer.apply_desired(routed(sid, (1, event_id)))
        await pk.deliverer.run_once()
        assert pk.guest.sent() == [event_id]
        assert (await pk.row(event_id)).channel == "prompt"

    run(rig, body)


def test_the_first_prompt_skips_refused_prompts_and_respects_the_delivered_list(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        sid = await pk.add_session(state="sandbox_ready")
        empty = add_prompt(env, 1, "empty", text=" ")
        done = add_prompt(env, 2, "done")
        good = add_prompt(env, 3, "good")
        await pull(env)
        pk.kit.session_env.desired = routed(sid, (1, empty), (2, done), (3, good), delivered={sid: [done]})
        found = await first.first_prompt(env.context, await pk.session(sid))
        assert found is not None
        assert found.prompt_id == good
        assert await pk.rows() == {empty: "refused", done: "accepted", good: "registered"}
        # asked again (a retried start): the head is no longer pending
        assert await first.first_prompt(env.context, await pk.session(sid)) is None

    run(rig, body)


def test_no_first_prompt_for_subagents_malformed_states_or_without_deliverer(rig: Rig) -> None:
    async def body(env: Any) -> None:
        pk = await setup(env)
        child = await pk.add_session(2, depth=1, state="sandbox_ready")
        assert await pk.deliverer.first_prompt(await pk.session(child)) is None
        sid = await pk.add_session(state="sandbox_ready")
        pk.kit.session_env.desired = {"pending_prompts": "junk"}
        assert await pk.deliverer.first_prompt(await pk.session(sid)) is None
        pk.kit.session_env.desired = None
        assert await pk.deliverer.first_prompt(await pk.session(sid)) is None
        assert await first.first_prompt(SimpleNamespace(identity=None), await pk.session(sid)) is None
        assert getattr(env.context, ATTRIBUTE) is pk.deliverer

    run(rig, body)
