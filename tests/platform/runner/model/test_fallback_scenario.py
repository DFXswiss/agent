"""U-RUN-model (KOR-50, ADR-081 sections 2 to 5): one main session through the whole fallback, driven by the model
answering `429` (the gateway's `usage` records) and the usage endpoint reporting the windows full - the owner's own
subscription used up -> the next operator file -> moved in place to the other file -> DFX access (its usage is
`platform`) -> a second session finds everything used up and waits before its first prompt until the earliest
release, then continues by itself; a restart keeps the limits.

Real session starts, resumes, prompt events and deliveries against the fake guest, the credential hub and a real
store; the usage endpoint is the local double."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from agent_runner.billing import pause as credit
from agent_runner.model import fallback, gateways, limits, operator, pauses
from agent_runner.model.pauses import stamp
from agent_runner.prompts import first

from ..prompts.kit import add_prompt, pull
from ..sessions.kit import TASK, WG, session_id
from .fallback_kit import AT, HOUR, FKit, entry, fallback_kit, state, usage_document
from .kit import ALICE, TOKEN, model_env
from .test_operator import pool_dir, token

OWN = limits.own_key(ALICE)
A, B = limits.operator_key("a"), limits.operator_key("b")
KEY = "Bearer test-platform-key-SECRETMARKER-0123456789abcdef"


async def transitions(found: FKit, sid: str) -> list[dict[str, Any]]:
    return [body for body in await found.kit.of_kind("session_transition") if body["session_id"] == sid]


def test_own_subscription_then_operator_files_then_dfx_access_then_a_wait_that_ends_by_itself(
    store_dsn: str, tmp_path: Path
) -> None:
    async def main() -> None:
        async with model_env(store_dsn) as env:
            desired_state = state(
                entry(1, own_to_dfx=True), entry(2, own_to_dfx=True, to_platform=False), billing={"api": 1}
            )
            async with fallback_kit(env, pool_dir(tmp_path), desired_state=desired_state) as found:
                await scenario(found)

    asyncio.run(main())


async def scenario(found: FKit) -> None:  # noqa: PLR0915 - one story, step by step
    senv = found.kit.session_env
    sid = session_id(1)
    assert (await found.start(1)).started
    assert found.credentials()[-1][1]["auth_value"] == f"Bearer {TOKEN}"
    await found.state(sid, "working")

    # 1. the own subscription is used up: confirmed by its quota at the first 429
    found.quotas.documents[TOKEN] = usage_document(five=100.0, five_resets=AT + HOUR)
    found.emit(429)
    await found.consume()
    assert (await found.limit(OWN) or ())[:3] == ("exhausted", AT + HOUR, True)

    # 2. the owner allows DFX access: paused, resumed at once on the next operator file, told in one sentence
    await fallback.run_once(found.deliverer)
    assert found.credentials()[-1][1]["auth_value"] == f"Bearer {token(1)}"
    assert found.pool.bindings == {sid: "a"}
    assert found.guest.texts() == [
        "The platform switched this session's model access from the own Claude subscription to the operator's Claude"
        " subscription because the own Claude subscription is used up until 2026-11-18 12:07 UTC."
        " Continue the interrupted work."
    ]
    await found.state(sid, "working")

    # 3. that file is used up too: the session moves in place to the other file (same model, no pause)
    found.clock.now += operator.GRACE_S
    found.quotas.documents[token(1)] = usage_document(five=100.0, five_resets=AT + 1800)
    found.emit(429)
    await found.consume()
    assert (await found.limit(A) or ())[:2] == ("exhausted", AT + 1800)
    await gateways.refresh(senv, await found.stored(sid))
    assert found.credentials()[-1][1]["auth_value"] == f"Bearer {token(2)}"
    assert found.pool.bindings == {sid: "b"}
    texts = len(found.guest.texts())
    await fallback.run_once(found.deliverer)
    assert len(found.guest.texts()) == texts

    # 4. the last file is used up (the readable quota decides, although the pool ignores the session's second file)
    found.clock.now += operator.GRACE_S
    found.quotas.documents[token(2)] = usage_document(five=100.0, five_resets=AT + 2 * HOUR)
    found.emit(429)
    await found.consume()
    assert (await found.limit(B) or ())[:2] == ("exhausted", AT + 2 * HOUR)
    await fallback.run_once(found.deliverer)
    assert found.credentials()[-1][1]["auth_value"] == KEY
    assert found.guest.texts()[-1].startswith(
        "The platform switched this session's model access from the operator's Claude subscription to DFX model access"
    )
    await found.state(sid, "working")
    found.emit(200)
    await found.consume()
    usage = (await found.kit.of_kind("usage"))[-1]
    assert (usage["access_origin"], usage["cost_basis"]) == ("platform", "provider")
    causes = [
        item["detail"]["cause"] for item in await found.env.audits() if item["action"] == "session.model_switched"
    ]
    assert causes == ["exhausted", "exhausted"]

    # 5. a second session (DFX over OpenRouter not allowed after the pool): everything is used up - it starts on the
    #    access with the earliest release (file a) and waits before its first prompt
    second = session_id(2)
    rig: Any = found.env
    event_id = add_prompt(rig, 1, "first", text="Weiter mit dem Bericht")
    await pull(rig)
    routing = {"task_id": TASK, "session_id": second, "scope_id": WG, "scope_seq": 1, "event_id": event_id}
    senv.desired = {**current_state(found), "pending_prompts": [routing], "delivered_prompt_event_ids": {}}
    senv.first_prompt = first.first_prompt
    assert (await found.start(2)).started
    create = [value for name, value in found.guest.calls if name == "create_session"][-1]
    assert "initial_prompt" not in create
    assert found.credentials()[-1][1]["auth_value"] == f"Bearer {token(1)}"
    await found.state(second, "prompt_delivering")
    await found.deliverer.advance(second)
    assert event_id not in [value[1] for name, value in found.guest.calls if name == "register_prompt"]
    await fallback.run_once(found.deliverer)  # still in its start chain: not suspended, its prompt held
    assert ("control", (second, "suspend")) not in found.guest.calls
    pause = await found.pause_row(second)
    assert pause is not None
    assert (pause.kind, pause.until, pause.dfx_choice) == ("wait", stamp(AT + 1800), False)
    assert credit.deferred(senv, await found.stored(second))

    # 6. the earliest release passes: the wait ends by itself and the held prompt is delivered
    found.wall.now = AT + 1800
    found.clock.now += 1800  # the pool's deferral of the 429 is over as well
    await fallback.run_once(found.deliverer)
    assert second not in pauses.holding(found.context)
    await found.deliverer.advance(second)
    assert found.guest.texts()[-1] == "Weiter mit dem Bericht"
    assert found.credentials()[-1][1]["auth_value"] == f"Bearer {token(1)}"
    await found.env.sync.pusher.drain()  # the contract-checking hub takes every item

    # 7. a restart keeps the limits that still hold and releases the passed one
    delattr(found.context, limits.ATTRIBUTE)
    limits.limits_for(found.context).wall = found.wall
    restarted = await limits.load(found.context)
    assert sorted(restarted.rows) == sorted([OWN, B])
    assert restarted.active(OWN) is not None


def current_state(found: FKit) -> dict[str, Any]:
    current = found.kit.session_env.desired
    assert current is not None
    return dict(current)
