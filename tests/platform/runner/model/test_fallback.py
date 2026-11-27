"""U-RUN-model (KOR-50, ADR-081 sections 4 and 5): running sessions on a used-up access - moved in place to another
operator file, paused and resumed at once on another kind of access with the switch sentence, or suspended with
`model_quota_exhausted`, `wait_until` and `dfx_choice` until an access is allowed again, then continued by themselves;
subagents end; credit first; the reconcile step and the daemon task `model_fallback`.

Real session starts and resumes against the fake guest over the credential hub and a real store; the deliverer of the
prompt area holds the session locks."""

from __future__ import annotations

import asyncio
import io
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agent_runner.billing import pause as credit
from agent_runner.guestlink.errors import GuestUnreachable
from agent_runner.model import fallback, gateways, limits, operator, pauses, reconcile, retry, tasks
from agent_runner.model.limits import EXHAUSTED, Limit
from agent_runner.prompts.resume import RETRY
from agent_runner.reconcile.engine import Run

from ..sessions.kit import desired, session_id
from .fallback_kit import AT, HOUR, T0, FKit, entry, fallback_kit, state
from .kit import ALICE, TOKEN, api_key_body, model_env, store_record
from .test_operator import pool_dir, token

OWN = limits.own_key(ALICE)
UNTIL = "2026-11-18 12:07 UTC"
Body = Callable[[FKit], Awaitable[None]]


def run(dsn: str, tmp_path: Path | None, body: Body, **options: Any) -> None:
    async def main() -> None:
        async with model_env(dsn) as env:
            directory = None if tmp_path is None else pool_dir(tmp_path)
            async with fallback_kit(env, directory, **options) as found:
                await body(found)

    asyncio.run(main())


async def mark(found: FKit, key: str, until: float = AT + HOUR, readable: bool = True) -> None:
    await limits.mark(found.context, Limit(key, EXHAUSTED, until, readable, T0))


async def running(found: FKit, n: int = 1, state_: str = "working", **overrides: Any) -> str:
    assert (await found.start(n, **overrides)).started, found.log()
    await found.state(session_id(n), state_)
    found.guest.calls.clear()
    return session_id(n)


def controls(found: FKit, sid: str) -> list[str]:
    return [value[1] for name, value in found.guest.calls if name == "control" and value[0] == sid]


async def transitions(found: FKit, sid: str) -> list[dict[str, Any]]:
    return [body for body in await found.kit.of_kind("session_transition") if body["session_id"] == sid]


def test_a_used_up_operator_file_with_another_one_is_left_to_the_renewal(store_dsn: str, tmp_path: Path) -> None:
    async def body(found: FKit) -> None:
        sid = await running(found)
        assert found.pool.bindings == {sid: "a"}
        await mark(found, limits.operator_key("a"))
        await fallback.run_once(found.deliverer)
        assert controls(found, sid) == []
        assert await found.pause_row(sid) is None
        await gateways.refresh(found.kit.session_env, await found.stored(sid))
        assert [c["auth_value"] for _s, c in found.credentials()] == [f"Bearer {token(2)}"]

    run(store_dsn, tmp_path, body, own=False)


@pytest.mark.parametrize("state_", ["working", "idle"])
def test_another_kind_of_access_pauses_and_resumes_at_once(store_dsn: str, tmp_path: Path, state_: str) -> None:
    async def body(found: FKit) -> None:
        sid = await running(found, state_=state_)
        await mark(found, OWN)
        await fallback.run_once(found.deliverer)
        reasons = [(t["state"], t["reason"]) for t in await transitions(found, sid)]
        assert ("suspended", "model_switched") in reasons
        assert await found.pause_row(sid) is None  # ended
        if state_ == "idle":
            assert controls(found, sid) == ["suspend"]
            assert found.guest.texts() == []  # stays suspended until its next prompt
            assert (await found.stored(sid)).state == "suspended"
            return
        assert controls(found, sid) == ["interrupt", "suspend"]
        assert [c["auth_value"] for _s, c in found.credentials()] == [f"Bearer {token(1)}"]
        assert found.guest.texts() == [
            "The platform switched this session's model access from the own Claude subscription to the operator's"
            f" Claude subscription because the own Claude subscription is used up until {UNTIL}."
            " Continue the interrupted work."
        ]
        assert f"runner.model model_continued session_id={sid} from=subscription to=operator" in found.log()
        assert pauses.holding(found.context) == set()

    run(store_dsn, tmp_path, body, desired_state=state(entry(1, own_to_dfx=True)))


def test_nothing_allowed_suspends_with_the_wait_and_continues_after_a_consent(store_dsn: str, tmp_path: Path) -> None:
    async def body(found: FKit) -> None:
        sid = await running(found)
        await mark(found, OWN)
        await fallback.run_once(found.deliverer)
        assert controls(found, sid) == ["interrupt", "suspend"]
        last = (await transitions(found, sid))[-1]
        assert (last["state"], last["reason"], last["wait_until"], last["dfx_choice"]) == (
            "suspended",
            "model_quota_exhausted",
            "2026-11-18T12:07:00.000Z",
            True,
        )
        assert sid in pauses.holding(found.context)
        assert credit.deferred(found.kit.session_env, await found.stored(sid))
        await fallback.run_once(found.deliverer)  # still waiting: nothing changes
        assert controls(found, sid) == ["interrupt", "suspend"]
        # the task's consent allows the operator's pool: resumed with the switch sentence
        found.set_desired(state(entry(1, consent=True)))
        await fallback.run_once(found.deliverer)
        assert found.guest.texts()[-1].startswith("The platform switched this session's model access from the own")
        assert sid not in pauses.holding(found.context)
        assert await found.pause_row(sid) is None
        await found.env.sync.pusher.drain()  # the contract-checking hub takes the transitions

    run(store_dsn, tmp_path, body, desired_state=state(entry(1)))


def test_a_wait_ends_by_itself_at_its_release_time(store_dsn: str) -> None:
    async def body(found: FKit) -> None:
        sid = await running(found)
        await mark(found, OWN)
        await fallback.run_once(found.deliverer)
        last = (await transitions(found, sid))[-1]
        assert "wait_until" not in last  # an older hub: no new fields
        assert (last["reason"], last.get("dfx_choice")) == ("model_quota_exhausted", None)
        # an earlier release becomes known: the wait follows it
        await mark(found, OWN, AT + 600)
        await fallback.run_once(found.deliverer)
        pause = await found.pause_row(sid)
        assert pause is not None
        assert pause.until == datetime.fromtimestamp(AT + 600, UTC)
        found.wall.now = AT + 600
        await fallback.run_once(found.deliverer)
        assert found.guest.texts() == [fallback.AVAILABLE]
        assert [c["auth_value"] for _s, c in found.credentials()] == [f"Bearer {TOKEN}"]
        assert await found.pause_row(sid) is None

    run(store_dsn, None, body, desired_state=state(entry(1), api=False))


def test_a_subagent_on_a_used_up_access_ends(store_dsn: str) -> None:
    async def body(found: FKit) -> None:
        parent = await running(found)
        child = await running(found, 2, depth=1, parent_session_id=parent)
        await mark(found, OWN)
        found.set_desired(state(entry(1), api=True))
        await fallback.run_once(found.deliverer)
        assert ("stopping", "model_quota_exhausted") in [
            (t["state"], t["reason"]) for t in await transitions(found, child)
        ]
        assert f"runner.model model_subagent_stopped session_id={child}" in found.log()

    run(store_dsn, None, body)


def test_a_subagent_towards_an_older_hub_ends_with_the_known_reason(store_dsn: str) -> None:
    async def body(found: FKit) -> None:
        child = await running(found, 2, depth=1, parent_session_id=session_id(1))
        await mark(found, OWN)
        await fallback.run_once(found.deliverer)
        assert ("stopping", "model_access_unavailable") in [
            (t["state"], t["reason"]) for t in await transitions(found, child)
        ]

    run(store_dsn, None, body, desired_state=state(api=False))


def test_without_any_release_time_a_session_is_left_alone(store_dsn: str) -> None:
    async def body(found: FKit) -> None:
        sid = await running(found, model_access_pref="subscription")
        await mark(found, OWN)
        del found.env.hub.records[(ALICE, "claude_subscription")]  # the owner removed the record meanwhile
        async with found.env.pool.connection() as conn:
            await conn.execute("DELETE FROM runner.credential_cache")
        await fallback.run_once(found.deliverer)
        assert controls(found, sid) == []
        assert await found.pause_row(sid) is None

    run(store_dsn, None, body)


def test_a_waiting_start_is_suspended_once_its_agent_is_up(store_dsn: str) -> None:
    async def body(found: FKit) -> None:
        await mark(found, OWN)
        assert (await found.start()).started
        sid = session_id()
        await fallback.run_once(found.deliverer)  # still starting: not asked yet
        assert controls(found, sid) == []
        await found.state(sid, "prompt_delivering")
        await fallback.run_once(found.deliverer)  # the hub refuses a start state going straight to `suspended`
        assert controls(found, sid) == []
        assert sid in pauses.holding(found.context)
        await found.state(sid, "idle")
        await fallback.run_once(found.deliverer)
        assert controls(found, sid) == ["suspend"]
        pause = await found.pause_row(sid)
        assert pause is not None
        assert pause.suspended
        # the time is up: the pause ends, the session stays suspended until its prompt is delivered
        found.wall.now = AT + HOUR
        await fallback.run_once(found.deliverer)
        assert await found.pause_row(sid) is None
        assert found.guest.texts() == []
        assert not credit.deferred(found.kit.session_env, await found.stored(sid))

    run(store_dsn, None, body)


def test_a_waiting_start_whose_access_is_free_again_is_never_suspended(store_dsn: str) -> None:
    async def body(found: FKit) -> None:
        await mark(found, OWN)
        assert (await found.start()).started
        sid = session_id()
        await found.state(sid, "prompt_delivering")
        found.wall.now = AT + HOUR
        await fallback.run_once(found.deliverer)
        assert controls(found, sid) == []
        assert await found.pause_row(sid) is None

    run(store_dsn, None, body)


def test_a_pause_of_a_session_suspended_meanwhile_and_failing_guests(store_dsn: str) -> None:
    async def body(found: FKit) -> None:
        await mark(found, OWN)
        assert (await found.start()).started
        sid = session_id()
        await found.state(sid, "suspended")  # the idle rule suspended it meanwhile
        await fallback.run_once(found.deliverer)
        pause = await found.pause_row(sid)
        assert pause is not None
        assert pause.suspended
        assert controls(found, sid) == []
        # a running session whose guest cannot be reached: tried again
        other = await running(found, 2)
        found.guest.fail["control"] = GuestUnreachable("control")
        await fallback.run_once(found.deliverer)
        assert "model_interrupt_failed session_id=" + other in found.log()
        pause = await found.pause_row(other)
        assert pause is not None
        assert not pause.suspended
        del found.guest.fail["control"]
        await fallback.run_once(found.deliverer)
        pause = await found.pause_row(other)
        assert pause is not None
        assert pause.suspended

    run(store_dsn, None, body)


def test_a_switch_whose_resume_must_wait_or_fails(store_dsn: str, tmp_path: Path, monkeypatch: Any) -> None:
    async def body(found: FKit) -> None:
        sid = await running(found)
        await mark(found, OWN)
        monkeypatch.setattr(found.deliverer.resumer, "resume", AsyncMock(return_value=RETRY))
        await fallback.run_once(found.deliverer)
        assert sid in pauses.holding(found.context)
        pause = await found.pause_row(sid)
        assert pause is not None
        assert (pause.kind, pause.suspended) == ("switch", True)
        # the guest takes no prompt now: tried again
        monkeypatch.undo()
        found.guest.prompt_errors.append(GuestUnreachable("prompts"))
        await fallback.run_once(found.deliverer)
        assert f"runner.model model_continue_failed session_id={sid}" in found.log()
        await fallback.run_once(found.deliverer)
        assert len(found.guest.texts()) == 2  # the same prompt ID both times
        assert len(set(found.guest.sent())) == 1
        assert await found.pause_row(sid) is None
        # a resume that fails ends the pause
        other = await running(found, 2)
        await operator.unbind(found.context, other)
        await mark(found, OWN)
        monkeypatch.setattr(found.deliverer.resumer, "resume", AsyncMock(return_value="sandbox_failed"))
        await fallback.run_once(found.deliverer)
        assert await found.pause_row(other) is None

    run(store_dsn, tmp_path, body, desired_state=state(entry(1, own_to_dfx=True), entry(2, own_to_dfx=True)))


def test_a_resumed_session_whose_vm_stopped_is_tried_again(store_dsn: str, tmp_path: Path, monkeypatch: Any) -> None:
    async def body(found: FKit) -> None:
        sid = await running(found)
        await mark(found, OWN)

        async def resume(_session: Any) -> str:
            async with found.env.pool.connection() as conn:
                await conn.execute("UPDATE runner.vm SET state = 'stopped'")
            return "resumed"

        monkeypatch.setattr(found.deliverer.resumer, "resume", resume)
        await fallback.run_once(found.deliverer)
        assert found.guest.texts() == []
        pause = await found.pause_row(sid)
        assert pause is not None

    run(store_dsn, tmp_path, body, desired_state=state(entry(1, own_to_dfx=True)))


def test_credit_first(store_dsn: str) -> None:
    async def body(found: FKit) -> None:
        sid = await running(found)
        await mark(found, OWN)
        await fallback.run_once(found.deliverer)
        session = await found.stored(sid)
        # the organization's credit runs out while the session waits: both records, no guest call
        exhausted = {"api": 1, "active": True, "orgs": {desired().org_id: {"exhausted": True}}}
        await credit.enforce(found.deliverer, {"billing": exhausted})
        rows = await found.env.rows("SELECT interrupted, resumed_at FROM runner.credit_pause")
        assert rows == [(True, None)]
        assert controls(found, sid) == ["interrupt", "suspend"]
        # the quota is back first: the credit still holds the session
        found.wall.now = AT + HOUR
        await fallback.run_once(found.deliverer)
        assert sid in pauses.holding(found.context)
        assert await credit.credit_holds(found.kit.session_env, session)
        # credit back while a model pause holds another session: that one is not continued by the credit
        pauses.holding(found.context).add(sid)
        await credit.enforce(found.deliverer, {"billing": {**exhausted, "orgs": {}}})
        assert found.guest.texts() == []
        pauses.holding(found.context).discard(sid)
        await credit.enforce(found.deliverer, {"billing": {**exhausted, "orgs": {}}})
        assert found.guest.texts() == [credit.CONTINUATION]  # one continuation for both pauses
        assert await found.pause_row(sid) is None
        await fallback.run_once(found.deliverer)
        assert found.guest.texts() == [credit.CONTINUATION]

    run(store_dsn, None, body)


def test_ended_sessions_lose_their_pause_and_a_failing_session_is_logged(store_dsn: str, monkeypatch: Any) -> None:
    async def body(found: FKit) -> None:
        sid = await running(found)
        await mark(found, OWN)
        await fallback.run_once(found.deliverer)
        await found.state(sid, "stopped")
        await fallback.run_once(found.deliverer)
        assert await found.pause_row(sid) is None
        assert sid not in pauses.holding(found.context)
        other = await running(found, 2)

        async def broken(_deliverer: Any, _session: Any) -> bool:
            raise RuntimeError("store")

        monkeypatch.setattr(fallback, "check", broken)
        monkeypatch.setattr(fallback, "carry_on", lambda *_args: broken(None, None))
        await fallback.run_once(found.deliverer)
        assert f"runner.model model_fallback_failed session_id={other} error=RuntimeError" in found.log()
        found.kit.session_env.context = None
        await fallback.run_once(found.deliverer)

    run(store_dsn, None, body)


def test_after_a_restart_the_open_waits_hold_their_prompts(store_dsn: str) -> None:
    async def body(found: FKit) -> None:
        sid = await running(found)
        await mark(found, OWN)
        await fallback.run_once(found.deliverer)
        delattr(found.context, pauses.ATTRIBUTE)  # a new daemon
        delattr(found.context, limits.ATTRIBUTE)
        limits.limits_for(found.context).wall = found.wall
        await fallback.run_once(found.deliverer)
        assert sid in pauses.holding(found.context)

    run(store_dsn, None, body)


def test_the_sentences() -> None:
    at = datetime.fromtimestamp(AT + HOUR, UTC)
    assert fallback.sentence("platform", "platform", at) == fallback.AVAILABLE
    unknown = fallback.sentence("operator", "platform", None)
    assert unknown.endswith("used up until further notice. Continue the interrupted work.")
    assert UNTIL in fallback.sentence("api_key", "platform", at)
    assert fallback.prompt_id("s", at) == fallback.prompt_id("s", at) != fallback.prompt_id("t", at)


def test_the_reconcile_step_and_the_daemon_task(store_dsn: str, monkeypatch: Any) -> None:
    async def body(found: FKit) -> None:
        names = [(step.order, step.name) for step in reconcile.STEPS]
        assert names == [(75, "model"), (57, "model_fallback")]
        sid = await running(found)
        await mark(found, OWN)
        context = found.context
        await reconcile.fallback_step(Run(context=context, sync=None, book=None, desired=None))  # type: ignore[arg-type]
        assert controls(found, sid) == []
        await reconcile.fallback_step(Run(context=context, sync=None, book=None, desired={}))  # type: ignore[arg-type]
        assert controls(found, sid) == ["interrupt", "suspend"]
        monkeypatch.setattr(reconcile, "deliverer_for", lambda _context: None)
        await reconcile.fallback_step(Run(context=context, sync=None, book=None, desired={}))  # type: ignore[arg-type]

    run(store_dsn, None, body)


def test_the_daemon_task_runs_every_few_seconds_and_logs_failing_rounds(monkeypatch: Any) -> None:
    async def main() -> None:
        stopping = asyncio.Event()
        sleeps: list[float] = []
        seen: list[Any] = []

        async def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) == 3:
                stopping.set()

        async def run_once(deliverer: Any) -> None:
            seen.append(deliverer)
            if len(seen) == 1:
                raise RuntimeError("round")

        retried: list[Any] = []

        async def retry_once(deliverer: Any) -> None:
            retried.append(deliverer)
            if len(retried) == 2:
                raise ValueError("retry round")

        # each round asks for the deliverer once per step: the fallback, then the retry of FIX-02
        deliverers = iter([None, None, "d1", "d1", "d2", "d2"])
        monkeypatch.setattr(tasks, "deliverer_for", lambda _context: next(deliverers))
        monkeypatch.setattr(fallback, "run_once", run_once)
        monkeypatch.setattr(retry, "run_once", retry_once)
        log = io.StringIO()
        context = SimpleNamespace(stopping=stopping, host=SimpleNamespace(sleep=sleep), log=log)
        await tasks.run_fallback(context)
        assert (seen, retried, sleeps) == (["d1", "d2"], ["d1", "d2"], [fallback.TICK_S] * 3)
        assert log.getvalue() == (
            "runner.model model_fallback_tick_failed error=RuntimeError\n"
            "runner.model model_retry_tick_failed error=ValueError\n"
        )

    asyncio.run(main())


def test_sessions_that_cannot_run_out_or_changed_meanwhile_are_left_alone(store_dsn: str, monkeypatch: Any) -> None:
    async def body(found: FKit) -> None:
        store_record(found.env, "anthropic_api_key", api_key_body())
        keyed = await running(found, 1, model_access_pref="api_key")
        sid = await running(found, 2, model_access_pref="subscription", state_="idle")
        await mark(found, OWN)
        assert not await fallback.check(found.deliverer, await found.stored(keyed))
        assert controls(found, keyed) == []

        # a session that moved on while its pause waited for the lock
        class Changing:
            def __init__(self, session_id: str) -> None:
                self.session_id = session_id

            async def __aenter__(self) -> None:
                await found.state(self.session_id, "stopping")

            async def __aexit__(self, *_exc: object) -> None:
                return None

        monkeypatch.setattr(found.deliverer, "lock", Changing)
        assert not await fallback.check(found.deliverer, await found.stored(sid))
        assert await found.pause_row(sid) is None
        # an idle session whose guest does not suspend: tried again
        monkeypatch.undo()
        await found.state(sid, "idle")
        found.guest.fail["control"] = GuestUnreachable("control")
        assert await fallback.check(found.deliverer, await found.stored(sid))
        assert f"model_suspend_failed session_id={sid}" in found.log()
        # a transition that is no transition any more only marks the pause
        await found.state(sid, "suspended")
        before = len(await transitions(found, sid))
        await fallback.suspend(found.kit.session_env, sid, "model_quota_exhausted", {})
        assert len(await transitions(found, sid)) == before
        pause = await found.pause_row(sid)
        assert pause is not None
        assert pause.suspended

    run(store_dsn, None, body)


def test_a_wait_without_any_access_left_keeps_waiting(store_dsn: str) -> None:
    async def body(found: FKit) -> None:
        sid = await running(found, model_access_pref="subscription")
        await mark(found, OWN)
        await fallback.run_once(found.deliverer)
        del found.env.hub.records[(ALICE, "claude_subscription")]
        async with found.env.pool.connection() as conn:
            await conn.execute("DELETE FROM runner.credential_cache")
        await fallback.run_once(found.deliverer)
        assert sid in pauses.holding(found.context)
        assert await found.pause_row(sid) is not None

    run(store_dsn, None, body)


def test_a_session_resumed_meanwhile_gets_its_sentence_without_a_second_resume(store_dsn: str) -> None:
    async def body(found: FKit) -> None:
        sid = await running(found)
        await mark(found, OWN)
        await fallback.run_once(found.deliverer)
        await found.state(sid, "idle")  # a prompt resumed it meanwhile
        found.wall.now = AT + HOUR
        found.guest.calls.clear()
        await fallback.run_once(found.deliverer)
        assert "create_session" not in found.guest.names()
        assert found.guest.texts() == [fallback.AVAILABLE]

    run(store_dsn, None, body)


def test_an_operator_file_without_another_one_pauses_its_session(store_dsn: str, tmp_path: Path) -> None:
    async def body(found: FKit) -> None:
        sid = await running(found)
        await mark(found, limits.operator_key("a"))
        await mark(found, limits.operator_key("b"))
        await fallback.run_once(found.deliverer)
        assert controls(found, sid) == ["interrupt", "suspend"]
        assert found.guest.texts()[-1].startswith(
            "The platform switched this session's model access from the operator's Claude subscription to DFX"
        )

    run(store_dsn, tmp_path, body, own=False, desired_state=state(entry(1)))


def test_an_exhausted_org_holds_the_session_for_credit(store_dsn: str) -> None:
    async def body(found: FKit) -> None:
        sid = await running(found)
        exhausted = {"api": 1, "active": True, "orgs": {desired().org_id: {"exhausted": True}}}
        found.set_desired(state(billing=exhausted))
        assert await credit.credit_holds(found.kit.session_env, await found.stored(sid))

    run(store_dsn, None, body)


def test_a_waiting_start_ends_its_pause_when_another_access_is_allowed(store_dsn: str, tmp_path: Path) -> None:
    async def body(found: FKit) -> None:
        await mark(found, OWN)
        assert (await found.start()).started
        sid = session_id()
        await found.state(sid, "prompt_delivering")
        await fallback.run_once(found.deliverer)
        assert sid in pauses.holding(found.context)
        found.set_desired(state(entry(1, consent=True)))
        await fallback.run_once(found.deliverer)
        assert controls(found, sid) == []
        assert await found.pause_row(sid) is None
        assert sid not in pauses.holding(found.context)

    run(store_dsn, tmp_path, body, desired_state=state(entry(1)))
