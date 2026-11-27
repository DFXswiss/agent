"""U-RUN-model (HOT-26; ADR-081 section 4 point 8, ADR-092 section 5): a subscription that does not cover the
session's model.

The upstream double below answers like the provider for a subscription whose plan does not include a model: `429`
"Usage credits are required for this model" (journaled by the gateway with `refusal: model_not_in_plan`). The runner
remembers the refusal for that subscription and model - no deferral, no exhaustion -, holds the session's prompts and,
once its turn ended, moves it to another file of the pool that is not known to refuse the model and continues it with
a sentence. Without one the session keeps its model and waits; the task gets the line "no subscription with access to
<model> free" once, and the session continues when a file that serves the model becomes usable or the refusal's time
passed. The pool's choice leaves such a file out for the model from the start, also for a new session.

Real session starts, the real journal handlers and the reconcile step `model_fallback` against the fake guest over
the credential hub and a real store."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from agent_runner.billing import pause as credit
from agent_runner.model import access, fallback, limits, operator, pauses, revocation

from ..sessions.kit import desired, session_id
from .fallback_kit import FKit, entry, fallback_kit, state
from .kit import ALICE, OPUS, TOKEN, model_env
from .test_operator import pool_dir, put, token

Body = Callable[[FKit], Awaitable[None]]
OTHER_MODEL = "claude-haiku-5-5"


class Plans:
    """The provider of the subscription tokens: each token's plan names the models it does not cover."""

    def __init__(self, **refused: set[str]) -> None:
        self.refused = refused

    def answer(self, held: str, model: str) -> tuple[int, str | None]:
        if model in self.refused.get(held, set()):
            return 429, "model_not_in_plan"  # "Usage credits are required for this model."
        return 200, None


def current() -> dict[str, Any]:
    return {**state(entry(1)), "model_fallback": {"api": 2}}


def run(dsn: str, tmp_path: Path | None, body: Body, **options: Any) -> None:
    async def main() -> None:
        async with model_env(dsn) as env:
            directory = None if tmp_path is None else pool_dir(tmp_path)
            async with fallback_kit(env, directory, **options) as found:
                found.kit.session_env.clock = found.clock
                await body(found)

    asyncio.run(main())


async def running(found: FKit, n: int = 1, state_: str = "working", **overrides: Any) -> str:
    """A running session on the model the task must use."""
    overrides.setdefault("model_id_pref", OPUS)
    assert (await found.start(n, **overrides)).started, found.log()
    await found.state(session_id(n), state_)
    return session_id(n)


def held_token(found: FKit, sid: str) -> str:
    return [str(c["auth_value"]) for s, c in found.credentials() if s == sid][-1].removeprefix("Bearer ")


async def request(found: FKit, plans: Plans, sid: str, model: str | None = None) -> int:
    """One model request of the session's gateway with the token it holds, journaled like the gateway does."""
    asked = model or (await found.stored(sid)).model_id
    assert asked is not None
    status, refusal = plans.answer(held_token(found, sid), asked)
    zero = dict.fromkeys(("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"), 0)
    body: dict[str, Any] = {
        "request_id": "req_1",
        "model": asked,
        **zero,
        "thinking_tokens": 0,
        "context_window_tokens": 0,
        "request_bytes": 10,
        "response_bytes": 20,
        "status": status,
    }
    if refusal is not None:
        body["refusal"] = refusal
    found.guest.emit(sid, "usage", body)
    await found.consume()
    return status


async def switches(found: FKit) -> list[dict[str, Any]]:
    return [body["detail"] for body in await found.kit.of_kind("audit") if body["action"] == "session.model_switched"]


def refuses(found: FKit, name: str, model: str) -> bool:
    return found.pool.refuses(name, model)


def test_a_refused_model_moves_the_session_to_a_subscription_that_serves_it(store_dsn: str, tmp_path: Path) -> None:
    async def body(found: FKit) -> None:
        sid = await running(found)
        model = (await found.stored(sid)).model_id
        assert model is not None
        assert found.pool.bindings == {sid: "a"}
        plans = Plans(**{token(1): {model}})
        assert await request(found, plans, sid) == 429
        assert f"runner.model model_refused session_id={sid} access=operator model={model}" in found.log()
        # remembered for this file and this model only - no deferral, no exhaustion of the file
        assert refuses(found, "a", model)
        assert not refuses(found, "a", OTHER_MODEL)
        assert found.pool.state("a") == operator.USABLE
        assert await found.limit(limits.operator_key("a")) is None
        assert pauses.held(found.context, sid)
        assert credit.deferred(found.kit.session_env, await found.stored(sid))
        await fallback.run_once(found.deliverer)  # the turn still runs
        assert found.guest.texts() == []
        await found.state(sid, "idle")
        await fallback.run_once(found.deliverer)
        assert found.pool.bindings == {sid: "b"}
        assert held_token(found, sid) == token(2)
        assert found.guest.texts() == [fallback.MODEL_SERVED.format(model=model)]
        assert (await found.stored(sid)).model_id == model  # the model stays
        assert [d["cause"] for d in await switches(found)] == ["renewed"]
        await found.env.sync.pusher.drain()  # the contract-checking hub takes the audit row
        assert not pauses.held(found.context, sid)
        await found.state(sid, "working")
        assert await request(found, plans, sid) == 200
        # the choice leaves the file out for that model from the start, also for a new session
        org = desired().org_id
        assert found.pool.choose(org_id=org, model=model) == "b"
        assert found.pool.choose(exclude=("b",), org_id=org) == "a"
        assert await operator.pick(found.context, org, model) is not None
        assert operator.used_up(found.context, org, model) == []
        # a new content of the file (a new login, another plan) is tried again
        put(found.pool.directory, "a", token(7))  # type: ignore[arg-type]
        found.pool.scan()
        assert not refuses(found, "a", model)

    run(store_dsn, tmp_path, body, own=False, desired_state=current())


def test_a_new_session_does_not_start_on_a_file_that_refused_its_model(store_dsn: str, tmp_path: Path) -> None:
    async def body(found: FKit) -> None:
        sid = await running(found, state_="idle")
        model = (await found.stored(sid)).model_id
        assert model is not None
        assert await request(found, Plans(**{token(1): {model}}), sid) == 429
        other = await running(found, 2)
        assert found.pool.bindings[other] == "b"  # "a" has more room but does not serve the model
        assert held_token(found, other) == token(2)

    run(store_dsn, tmp_path, body, own=False)


def test_without_a_subscription_for_the_model_the_session_waits_with_a_line(store_dsn: str, tmp_path: Path) -> None:
    async def body(found: FKit) -> None:
        sid = await running(found)
        model = (await found.stored(sid)).model_id
        assert model is not None
        plans = Plans(**{token(1): {model}, token(2): {model}})
        assert await request(found, plans, sid) == 429
        await found.state(sid, "idle")
        await fallback.run_once(found.deliverer)  # moved to "b", which refuses the model as well
        assert found.pool.bindings == {sid: "b"}
        await found.state(sid, "working")
        assert await request(found, plans, sid) == 429
        await found.state(sid, "idle")
        await fallback.run_once(found.deliverer)
        await fallback.run_once(found.deliverer)
        # nothing serves the model: it keeps its model and file, holds its prompts and says so once
        assert found.pool.bindings == {sid: "b"}
        assert (await found.stored(sid)).model_id == model
        assert len(found.guest.texts()) == 1
        assert pauses.held(found.context, sid)
        assert f"runner.model model_access_wait session_id={sid} access=operator model={model}" in found.log()
        assert [d["cause"] for d in await switches(found)] == ["renewed", "model_unavailable"]
        waiting = (await switches(found))[-1]
        assert (waiting["from_model"], waiting["to_model"], waiting["until"]) == (model, model, None)
        assert operator.used_up(found.context, desired().org_id, model) == []
        await found.env.sync.pusher.drain()
        # a file that serves the model becomes usable: the session continues there by itself
        put(found.pool.directory, "c", token(3))  # type: ignore[arg-type]
        await fallback.run_once(found.deliverer)
        assert found.pool.bindings == {sid: "c"}
        assert found.guest.texts()[-1] == fallback.MODEL_SERVED.format(model=model)
        assert not pauses.held(found.context, sid)
        await found.state(sid, "working")
        assert await request(found, plans, sid) == 200
        assert revocation.revocations(found.context).announced == set()

    run(store_dsn, tmp_path, body, own=False, desired_state=current())


def test_the_own_subscription_waits_until_the_refusal_is_tried_again(store_dsn: str) -> None:
    async def body(found: FKit) -> None:
        sid = await running(found, model_access_pref="subscription")
        model = (await found.stored(sid)).model_id
        assert model is not None
        plans = Plans(**{TOKEN: {model}})
        assert await request(found, plans, sid) == 429
        assert f"model_refused session_id={sid} access=subscription model={model}" in found.log()
        assert limits.limits_for(found.context).refuses(limits.own_key(ALICE), model, None)
        assert await found.limit(limits.own_key(ALICE)) is None  # no exhaustion
        await found.state(sid, "idle")
        await fallback.run_once(found.deliverer)
        assert found.guest.texts() == []
        assert f"model_access_wait session_id={sid} access=subscription" in found.log()
        assert pauses.held(found.context, sid)
        await fallback.run_once(found.deliverer)  # still waiting, the line is not repeated
        assert found.guest.texts() == []
        # the refusal's time passed (the plan may have changed): the same access is tried again
        found.wall.now += limits.REFUSED_MODEL_S
        await fallback.run_once(found.deliverer)
        assert found.guest.texts() == [fallback.MODEL_SERVED.format(model=model)]
        assert not pauses.held(found.context, sid)

    run(store_dsn, None, body)


def test_a_session_suspended_meanwhile_is_left_to_its_resume(store_dsn: str, tmp_path: Path) -> None:
    async def body(found: FKit) -> None:
        sid = await running(found)
        model = (await found.stored(sid)).model_id
        assert model is not None
        assert await request(found, Plans(**{token(1): {model}}), sid) == 429
        await found.state(sid, "suspended")  # the idle rule suspended it before the run
        await fallback.run_once(found.deliverer)
        assert f"runner.model access_renewal_given_up session_id={sid} reason=suspended" in found.log()
        assert not pauses.held(found.context, sid)  # its next prompt resumes it on a file that serves the model

    run(store_dsn, tmp_path, body, own=False)


def test_a_refusal_of_another_model_is_remembered_but_holds_nothing(store_dsn: str, tmp_path: Path) -> None:
    async def body(found: FKit) -> None:
        sid = await running(found)
        plans = Plans(**{token(1): {OTHER_MODEL}})
        assert await request(found, plans, sid, OTHER_MODEL) == 429
        assert refuses(found, "a", OTHER_MODEL)
        assert not pauses.held(found.context, sid)
        # a model refusal after a revoked token replaces the mark; a repeated one keeps it
        found.emit(401, sid)
        await found.consume()
        model = (await found.stored(sid)).model_id
        assert model is not None
        plans = Plans(**{token(1): {model}})
        assert await request(found, plans, sid) == 429
        first = revocation.revocations(found.context).refused[sid]
        assert first.model == model
        assert await request(found, plans, sid) == 429
        assert revocation.revocations(found.context).refused[sid] is first
        # the credit holds the session: nothing moves
        exhausted = {"api": 1, "active": True, "orgs": {desired().org_id: {"exhausted": True}}}
        found.set_desired(state(entry(1), billing=exhausted))
        await found.state(sid, "idle")
        await fallback.run_once(found.deliverer)
        assert found.pool.bindings == {sid: "a"}
        assert access.provisions(found.context).stamps[sid].operator is not None

    run(store_dsn, tmp_path, body, own=False)
