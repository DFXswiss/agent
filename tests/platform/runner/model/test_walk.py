"""U-RUN-model (KOR-50, ADR-081 sections 1 and 3): the order of `model.access.resolve` skips used-up accesses, keeps
inside the cost class of the owner's own access unless the owner's setting or the task's consent allows DFX access,
takes `platform` after a used-up pool only with `operator_to_platform`, and answers a wait (the earliest release, the
DFX choice): a new main session starts on the access with the earliest release and waits before its first prompt, a
subagent fails `model_quota_exhausted`. A resume that moves a session writes `session.model_switched`."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_runner.model import access, dfxkey, limits, operator, pauses
from agent_runner.model import session_request as model_request
from agent_runner.model.access import AccessRefused, Resolved, Wait
from agent_runner.model.limits import EXHAUSTED, Limit
from agent_runner.sessions import records

from ..credentials.kit import make_record
from ..sessions.kit import desired, session_id
from .fallback_kit import AT, HOUR, T0, FKit, entry, fallback_kit, state
from .kit import ALICE, TOKEN, api_key_body, model_env, store_record
from .test_operator import pool_dir, token

OWN = limits.own_key(ALICE)
Body = Callable[[FKit], Awaitable[None]]


def run(dsn: str, tmp_path: Path | None, body: Body, **options: Any) -> None:
    async def main() -> None:
        async with model_env(dsn) as env:
            directory = None if tmp_path is None else pool_dir(tmp_path)
            async with fallback_kit(env, directory, **options) as found:
                await body(found)

    asyncio.run(main())


async def mark(found: FKit, key: str, until: float, readable: bool = True) -> None:
    await limits.mark(found.context, Limit(key, EXHAUSTED, until, readable, T0))


def until(pause: pauses.Pause | None) -> float:
    assert pause is not None
    assert pause.until is not None
    return pause.until.timestamp()


def access_of(found: FKit) -> list[str]:
    return [credential["auth_value"] for _sid, credential in found.credentials()]


async def audits(found: FKit) -> list[dict[str, Any]]:
    return [item for item in await found.env.audits() if item["action"] == "session.model_switched"]


def test_a_used_up_own_subscription_waits_and_never_crosses_to_dfx_silently(store_dsn: str, tmp_path: Path) -> None:
    async def body(found: FKit) -> None:
        await mark(found, OWN, AT + HOUR)
        report = await found.start()
        assert report.started, found.log()
        # it starts on its own subscription (the earliest release) and waits before its first prompt
        assert access_of(found) == [f"Bearer {TOKEN}"]
        pause = await found.pause_row(session_id())
        assert pause is not None
        assert (pause.kind, until(pause), pause.dfx_choice, pause.from_access) == (
            "wait",
            AT + HOUR,
            True,
            "subscription",
        )
        assert pauses.held(found.context, session_id())
        assert f"runner.model model_wait session_id={session_id()}" in found.log()
        # the task's consent allows the DFX access: the operator's pool
        found.set_desired(state(entry(2, consent=True)))
        assert (await found.start(2)).started
        assert access_of(found)[-1] == f"Bearer {token(1)}"
        assert await found.pause_row(session_id(2)) is None
        # the owner's setting too
        found.set_desired(state(entry(3, own_to_dfx=True)))
        assert (await found.start(3)).started
        assert access_of(found)[-1] == f"Bearer {token(2)}"

    run(store_dsn, tmp_path, body, desired_state=state(entry(1)))


def test_inside_the_own_class_the_api_key_follows_the_used_up_subscription(store_dsn: str) -> None:
    async def body(found: FKit) -> None:
        store_record(found.env, "anthropic_api_key", api_key_body())
        await mark(found, OWN, AT + HOUR)
        assert (await found.start()).started
        assert access_of(found)[-1].startswith("test-api-key")
        assert await found.pause_row(session_id()) is None

    run(store_dsn, None, body)


@pytest.mark.parametrize("to_platform", [True, False])
def test_platform_after_a_used_up_pool_only_with_the_owners_setting(
    store_dsn: str, tmp_path: Path, *, to_platform: bool
) -> None:
    async def body(found: FKit) -> None:
        found.pool.scan()
        await mark(found, limits.operator_key("a"), AT + 2 * HOUR)
        await mark(found, limits.operator_key("b"), AT + HOUR)
        found.set_desired(state(entry(1, to_platform=to_platform)))
        assert (await found.start()).started
        pause = await found.pause_row(session_id())
        if to_platform:
            assert access_of(found) == ["Bearer test-platform-key-SECRETMARKER-0123456789abcdef"]
            assert pause is None
            return
        # the earliest release: file b; no DFX choice (no own access)
        assert access_of(found) == [f"Bearer {token(2)}"]
        assert found.pool.bindings == {session_id(): "b"}
        assert pause is not None
        assert (until(pause), pause.dfx_choice, pause.from_access) == (AT + HOUR, False, "operator")

    run(store_dsn, tmp_path, body, own=False)


def test_a_subagent_fails_with_model_quota_exhausted(store_dsn: str) -> None:
    async def body(found: FKit) -> None:
        await mark(found, OWN, AT + HOUR)
        child = desired(2, depth=1, parent_session_id=session_id(1), model_access_pref="subscription")
        with pytest.raises(AccessRefused) as refused:
            await access.resolve(found.kit.session_env, child)
        assert refused.value.reason == "model_quota_exhausted"
        found.set_desired(state(api=False))
        with pytest.raises(AccessRefused) as older:
            await access.resolve(found.kit.session_env, child)
        assert older.value.reason == "model_access_unavailable"

    run(store_dsn, None, body)


def test_an_own_credential_that_cannot_be_used_waits_without_a_start(store_dsn: str) -> None:
    async def body(found: FKit) -> None:
        owner = desired().owner_id
        found.env.hub.records[(owner, "claude_subscription")] = make_record(
            {"token": TOKEN, "email": None, "kind": "setup_token"}, kind="claude_subscription", epoch=2
        )
        walk = await access.walk(found.kit.session_env, desired())
        assert walk.found is None
        assert walk.unusable
        assert walk.wait(found.kit.session_env.desired, desired().org_id) == Wait(T0 + limits.RECHECK_S, True)
        assert walk.earliest() is None
        report = await found.start()
        assert (report.started, report.reason) == (False, "model_access_unavailable")
        # the organization's credit is used up: no DFX choice
        exhausted = {"api": 1, "active": True, "orgs": {desired().org_id: {"exhausted": True}}}
        assert walk.wait(state(billing=exhausted), desired().org_id) == Wait(T0 + limits.RECHECK_S, False)
        # with the owner's setting the platform takes over
        found.set_desired(state(entry(2, own_to_dfx=True)))
        assert (await found.start(2)).started

    run(store_dsn, None, body, own=False)


def test_the_dfx_key_of_an_org_device_waits_for_its_release(store_dsn: str) -> None:
    async def body(found: FKit) -> None:
        key = limits.dfx_key(found.kit.session_env.device_id)
        await mark(found, key, T0 + limits.DFX_KEY_S, readable=False)
        assert (await found.start(model_access_pref="platform")).started
        pause = await found.pause_row(session_id())
        assert pause is not None
        assert (pause.from_access, until(pause)) == ("platform", T0 + limits.DFX_KEY_S)

        def never(*_args: object) -> str:
            raise AssertionError("a probe opens no DFX envelope")

        senv = found.kit.session_env
        senv.platform_key = never
        platform = desired(model_access_pref="platform")
        assert await access.decide(senv, platform) is None  # no accepted envelope held: no DFX access
        org = desired().org_id
        dfxkey.held_keys(found.context).rows[org] = dfxkey.Held(org, "key-ref-0001", desired().owner_id, "sealed")
        assert dfxkey.has_key(found.context, org)
        assert not dfxkey.has_key(found.context, desired().task_id)
        decided = await access.decide(senv, platform)
        assert decided == Wait(T0 + limits.DFX_KEY_S, False)
        found.wall.now += limits.DFX_KEY_S
        decided = await access.decide(found.kit.session_env, desired(model_access_pref="platform"))
        assert isinstance(decided, Resolved)
        assert decided.key == key

    run(store_dsn, None, body, device_kind="org", key="dfx-key-SECRETMARKER-0123456789abcdef", own=False)


def test_decide_binds_nothing_and_knows_no_budget_refusal(store_dsn: str, tmp_path: Path) -> None:
    async def body(found: FKit) -> None:
        senv = found.kit.session_env
        decided = await access.decide(senv, desired())
        assert isinstance(decided, Resolved)
        assert (decided.label, decided.key) == ("operator", limits.operator_key("a"))
        assert found.pool.bindings == {}
        assert await access.decide(senv, desired(model_access_pref="bogus")) is None
        assert await access.decide(senv, desired(model_access_pref="api_key")) is None
        org = desired().org_id
        found.set_desired({"platform_budget": {"usd_per_org_day": 1, "spent_today_by_org": {org: 1.0}}})
        assert await access.decide(senv, desired(model_access_pref="platform")) is None
        with pytest.raises(AccessRefused) as refused:
            await access.resolve(senv, desired(model_access_pref="platform"))
        assert refused.value.reason == "budget_exceeded"
        # a used-up pool in a probe: the earliest file, nothing bound
        for name in ("a", "b"):
            await mark(found, limits.operator_key(name), AT + HOUR)
        found.set_desired(state(entry(1, to_platform=False)))
        assert await access.decide(senv, desired()) == Wait(AT + HOUR, False)
        walk = await access.walk(senv, desired(), probe=True)
        build = walk.earliest()
        assert build is not None
        assert (await build()).key == limits.operator_key("a")
        assert found.pool.bindings == {}

    run(store_dsn, tmp_path, body, own=False)


def test_a_resume_that_moves_the_session_writes_the_audit(store_dsn: str, tmp_path: Path) -> None:
    async def body(found: FKit) -> None:
        senv = found.kit.session_env
        found.set_desired(state(entry(1, own_to_dfx=True)))
        assert (await found.start()).started  # own subscription
        await mark(found, OWN, AT + HOUR)
        moved = await access.resolve(senv, desired())
        assert moved.label == "operator"
        async with found.env.pool.connection() as conn:
            await records.update(conn, session_id(), model_access="subscription", model_id=None)
        [audit] = await audits(found)
        assert audit["detail"] == {
            "from_access": "subscription",
            "to_access": "operator",
            "from_model": None,
            "to_model": None,
            "cause": "exhausted",
            "until": "2026-11-18T12:07:00.000Z",
        }
        assert audit["target_id"] == session_id()
        # the same access again: no audit; released after a pause: `released`
        await access.resolve(senv, desired())
        assert len(await audits(found)) == 1
        await limits.release(found.context, [OWN])
        async with found.env.pool.connection() as conn:
            await pauses.record(
                conn,
                session_id(),
                kind="switch",
                interrupted=False,
                at=T0,
                until=None,
                choice=False,
                from_access="subscription",
                from_model=None,
            )
        back = await access.resolve(senv, desired())
        assert back.label == "subscription"
        assert [a["detail"]["cause"] for a in await audits(found)] == ["exhausted", "released"]
        await found.env.sync.pusher.drain()  # the contract-checking hub takes the audits

    run(store_dsn, tmp_path, body)


def test_a_move_without_a_cause_and_towards_an_older_hub_writes_no_audit(store_dsn: str, tmp_path: Path) -> None:
    async def body(found: FKit) -> None:
        senv = found.kit.session_env
        found.set_desired(state(entry(1, own_to_dfx=True), api=False))
        assert (await found.start(model_access_pref="platform")).started
        # the owner changed the preference: a move, but no fallback
        await access.resolve(senv, desired())
        assert await audits(found) == []
        # own credential unusable: `unusable`, logged, but an older hub gets no audit
        async with found.env.pool.connection() as conn:
            await records.update(conn, session_id(), model_access="subscription")
        await operator.unbind(found.context, session_id())
        owner = desired().owner_id
        found.env.hub.records[(owner, "claude_subscription")] = make_record(
            {"token": TOKEN, "email": None, "kind": "setup_token"}, kind="claude_subscription", epoch=2
        )
        async with found.env.pool.connection() as conn:
            await conn.execute("DELETE FROM runner.credential_cache")
        await access.resolve(senv, desired())
        assert f"access_switched session_id={session_id()} from=subscription to=operator cause=unusable" in found.log()
        assert await audits(found) == []

    run(store_dsn, tmp_path, body, own=False)


def test_the_first_prompt_of_a_waiting_start_is_held(store_dsn: str) -> None:
    async def body(found: FKit) -> None:
        session = SimpleNamespace(id=session_id())
        request: dict[str, Any] = {"initial_prompt": {"prompt_id": desired().task_id, "text": "x"}}
        await model_request.amend(found.context, session, request)  # type: ignore[arg-type]
        assert "initial_prompt" in request  # not waiting
        pauses.holding(found.context).add(session_id())
        await model_request.amend(found.context, session, {})  # type: ignore[arg-type]
        await model_request.amend(found.context, session, request)  # type: ignore[arg-type]
        assert request == {}
        assert f"runner.model first_prompt_held session_id={session_id()}" in found.log()
        assert pauses.holding(None) == set()

    run(store_dsn, None, body)


def test_a_platform_step_without_its_strategy_is_skipped(store_dsn: str) -> None:
    async def body(found: FKit) -> None:
        found.kit.session_env.strategies = {}
        with pytest.raises(AccessRefused):
            await access.resolve(found.kit.session_env, desired())

    run(store_dsn, None, body, own=False)
