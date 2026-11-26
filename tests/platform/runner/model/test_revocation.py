"""U-RUN-model (HOT-26; ADR-092 section 5, ADR-081 section 4): a session survives the renewal of its subscription.

A renewal revokes the former access token at once. The upstream double below does what the provider does: a renewal
makes the former token invalid in the middle of a turn, so the turn's next request is refused (`401`) and the turn
ends with the error. The runner holds the session's prompts from that `usage` record on, waits until its turn ended
and a valid access is there (the renewed file or record of the same subscription, else another usable file of the
operator's pool), and continues the session with the renewal sentence - no person writes anything; towards a current
hub the audit `session.model_switched` with the cause `renewed` gives the task its quiet line. After a few
continuations without a successful answer, without a valid access in time, or when the session ended, the error stays
visible as before and the prompts are delivered.

Real session starts, the real journal handlers and the reconcile step `model_fallback` against the fake guest over
the credential hub and a real store."""

from __future__ import annotations

import asyncio
import io
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent_runner.billing import pause as credit
from agent_runner.credentials import cache, subscriptions
from agent_runner.guestlink.errors import GuestUnreachable
from agent_runner.model import access, fallback, gateways, operator, pauses, policy, revocation
from agent_runner.model.operator import Entry, Use
from agent_runner.quota import renewal

from ..sessions.kit import desired, session_id
from .fallback_kit import FKit, entry, fallback_kit, state
from .kit import ALICE, TOKEN, api_key_body, model_env, store_record, subscription_body
from .test_operator import pool_dir, put, token

NEW = token(9)
"""The access token a renewal hands out."""
Body = Callable[[FKit], Awaitable[None]]


class Upstream:
    """The provider of the subscription tokens: a renewal revokes the former token at once."""

    def __init__(self, *valid: str) -> None:
        self.valid = set(valid)

    def renew(self, former: str, new: str) -> None:
        self.valid.discard(former)
        self.valid.add(new)

    def status(self, held: str) -> int:
        return 200 if held in self.valid else 401


def current(state_: dict[str, Any] | None = None) -> dict[str, Any]:
    """A desired state of a hub with `model_fallback.api` 2 (HOT-26)."""
    found = state_ or state(entry(1))
    return {**found, "model_fallback": {"api": 2}}


def run(dsn: str, tmp_path: Path | None, body: Body, **options: Any) -> None:
    async def main() -> None:
        async with model_env(dsn) as env:
            directory = None if tmp_path is None else pool_dir(tmp_path)
            async with fallback_kit(env, directory, **options) as found:
                found.kit.session_env.clock = found.clock
                await body(found)

    asyncio.run(main())


async def running(found: FKit, n: int = 1, state_: str = "working", **overrides: Any) -> str:
    assert (await found.start(n, **overrides)).started, found.log()
    await found.state(session_id(n), state_)
    return session_id(n)


def held_token(found: FKit, sid: str) -> str:
    """The token the session's gateway holds: the last provisioning of the session."""
    found_ = [str(c["auth_value"]) for s, c in found.credentials() if s == sid]
    return found_[-1].removeprefix("Bearer ")


async def request(found: FKit, upstream: Upstream, sid: str) -> int:
    """One model request of the session's gateway with the token it holds, journaled like the gateway does."""
    status = upstream.status(held_token(found, sid))
    found.emit(status, sid)
    await found.consume()
    return status


async def audits(found: FKit) -> list[dict[str, Any]]:
    return [body for body in await found.kit.of_kind("audit") if body["action"] == "session.model_switched"]


def held(found: FKit, sid: str) -> bool:
    return pauses.held(found.context, sid)


async def deferred(found: FKit, sid: str) -> bool:
    return credit.deferred(found.kit.session_env, await found.stored(sid))


def test_the_renewal_of_the_operator_file_in_the_middle_of_a_turn(store_dsn: str, tmp_path: Path) -> None:
    async def body(found: FKit) -> None:
        sid = await running(found)
        assert found.pool.bindings == {sid: "a"}
        upstream = Upstream(token(1), token(2))
        assert await request(found, upstream, sid) == 200
        # the file is renewed: the provider revokes the former token before the gateway has the new one
        put(found.pool.directory, "a", NEW)  # type: ignore[arg-type]
        upstream.renew(token(1), NEW)
        assert await request(found, upstream, sid) == 401
        assert f"runner.model access_refused session_id={sid} access=operator" in found.log()
        assert held(found, sid)
        assert await deferred(found, sid)  # a waiting prompt is not handed to the dead token
        await fallback.run_once(found.deliverer)  # the turn still runs: nothing is sent
        assert found.guest.texts() == []
        assert held(found, sid)
        await found.state(sid, "idle")  # the turn ended with the error
        await fallback.run_once(found.deliverer)
        assert held_token(found, sid) == NEW
        assert found.guest.texts() == [fallback.RENEWED]
        assert f"runner.model model_continued session_id={sid} from=operator to=operator cause=renewed" in found.log()
        assert not held(found, sid)
        assert not await deferred(found, sid)  # the waiting prompts follow the sentence
        [audit] = await audits(found)
        assert audit["detail"] == {
            "from_access": "operator",
            "to_access": "operator",
            "from_model": audit["detail"]["to_model"],
            "to_model": (await found.stored(sid)).model_id,
            "cause": "renewed",
            "until": None,
        }
        await found.env.sync.pusher.drain()  # the contract-checking hub takes the audit row
        await found.state(sid, "working")
        assert await request(found, upstream, sid) == 200  # the turn goes on with the new token
        assert revocation.revocations(found.context).tries == {}
        await fallback.run_once(found.deliverer)
        assert found.guest.texts() == [fallback.RENEWED]
        for secret in (token(1), NEW):
            assert secret not in found.log()

    run(store_dsn, tmp_path, body, own=False, desired_state=current())


def test_a_new_token_that_arrived_before_the_refusal_continues_at_once(store_dsn: str, tmp_path: Path) -> None:
    async def body(found: FKit) -> None:
        sid = await running(found, state_="idle")
        upstream = Upstream(token(1), token(2))
        put(found.pool.directory, "a", NEW)  # type: ignore[arg-type]
        upstream.renew(token(1), NEW)
        await gateways.refresh(found.kit.session_env, await found.stored(sid))  # the operator task's tick
        assert held_token(found, sid) == NEW
        found.emit(401, sid)  # the late record of the request that carried the former token
        await found.consume()
        await fallback.run_once(found.deliverer)
        assert found.guest.texts() == [fallback.RENEWED]
        assert await audits(found) == []  # an older hub: no audit row

    run(store_dsn, tmp_path, body, own=False)


def test_a_refused_file_that_is_not_renewed_moves_the_session_to_another_file(store_dsn: str, tmp_path: Path) -> None:
    async def body(found: FKit) -> None:
        sid = await running(found)
        upstream = Upstream(token(2))  # the file `a` was revoked from outside and never replaced
        found.clock.now += operator.GRACE_S + 1
        assert await request(found, upstream, sid) == 401
        await found.state(sid, "idle")
        await fallback.run_once(found.deliverer)
        assert found.pool.bindings == {sid: "b"}
        assert held_token(found, sid) == token(2)
        assert found.guest.texts() == [fallback.RENEWED]
        assert await request(found, upstream, sid) == 200

    run(store_dsn, tmp_path, body, own=False, desired_state=current())


def test_the_renewed_record_of_the_own_subscription(store_dsn: str) -> None:
    async def body(found: FKit) -> None:
        sid = await running(found, model_access_pref="subscription")
        upstream = Upstream(NEW)
        assert await request(found, upstream, sid) == 401
        assert f"access_refused session_id={sid} access=subscription" in found.log()
        await found.state(sid, "idle")
        await fallback.run_once(found.deliverer)  # no new access yet: it waits
        assert found.guest.texts() == []
        assert held(found, sid)
        store_record(found.env, "claude_subscription", subscription_body(NEW), version=2)
        await cache.refresh_owner(found.context, ALICE)  # the hint of the renewed record
        await fallback.run_once(found.deliverer)
        assert held_token(found, sid) == NEW
        assert found.guest.texts() == [fallback.RENEWED]
        [audit] = await audits(found)
        assert (audit["detail"]["from_access"], audit["detail"]["to_access"]) == ("subscription", "subscription")

    run(store_dsn, None, body, desired_state=current())


def test_without_a_valid_access_in_time_the_error_stays(store_dsn: str) -> None:
    async def body(found: FKit) -> None:
        sid = await running(found, state_="idle")
        found.emit(401, sid)
        await found.consume()
        await fallback.run_once(found.deliverer)
        assert held(found, sid)
        found.clock.now += revocation.WAIT_S
        await fallback.run_once(found.deliverer)
        assert f"runner.model access_renewal_given_up session_id={sid} reason=no_access" in found.log()
        assert not held(found, sid)
        assert found.guest.texts() == []

    run(store_dsn, None, body)


def test_after_a_few_failed_continuations_the_error_stays_visible(store_dsn: str, tmp_path: Path) -> None:
    async def body(found: FKit) -> None:
        sid = await running(found, state_="idle")
        upstream = Upstream()  # the new tokens do not work either
        for n in range(revocation.MAX_TRIES):
            put(found.pool.directory, "a", token(20 + n))  # type: ignore[arg-type]
            assert await request(found, upstream, sid) == 401
            await fallback.run_once(found.deliverer)
            assert len(found.guest.texts()) == n + 1
        assert await request(found, upstream, sid) == 401
        await fallback.run_once(found.deliverer)
        assert f"runner.model access_renewal_given_up session_id={sid} reason=tries" in found.log()
        assert len(found.guest.texts()) == revocation.MAX_TRIES
        assert len(set(found.guest.sent())) == revocation.MAX_TRIES  # one prompt ID per refusal
        assert not held(found, sid)
        # a later refusal starts again
        assert await request(found, upstream, sid) == 401
        assert held(found, sid)

    run(store_dsn, tmp_path, body, own=False)


def test_a_turn_that_goes_on_by_itself_needs_no_continuation(store_dsn: str) -> None:
    async def body(found: FKit) -> None:
        sid = await running(found)
        found.emit(401, sid)
        await found.consume()
        found.emit(401, sid)  # a repeated refusal keeps the first mark
        await found.consume()
        first = revocation.revocations(found.context).refused[sid]
        assert first.paused_at == datetime.fromtimestamp(found.wall.now, UTC)
        found.emit(200, sid)  # Claude Code retried with the new token
        await found.consume()
        assert f"runner.model access_answered session_id={sid}" in found.log()
        assert not held(found, sid)
        await found.state(sid, "idle")
        await fallback.run_once(found.deliverer)
        assert found.guest.texts() == []

    run(store_dsn, None, body)


def test_what_is_not_marked(store_dsn: str) -> None:
    async def body(found: FKit) -> None:
        parent = await running(found)
        child = await running(found, 2, depth=1, parent_session_id=parent)
        found.emit(401, child)  # a subagent's one turn ends with the error, like before
        found.emit(429, parent)  # another refusal: the quota flow
        await found.consume()
        assert revocation.revocations(found.context).refused == {}
        env = SimpleNamespace(context=None)
        revocation.observe(env, await found.stored(parent), 401, None, "subscription")
        context = SimpleNamespace()
        env = SimpleNamespace(context=context)
        session = await found.stored(parent)
        for status in (None, True, "401"):
            revocation.observe(env, session, status, None, "subscription")
        assert revocation.revocations(context).refused == {}

    run(store_dsn, None, body)


def test_an_api_key_session_is_not_marked(store_dsn: str) -> None:
    async def body(found: FKit) -> None:
        store_record(found.env, "anthropic_api_key", api_key_body())
        sid = await running(found, model_access_pref="api_key")
        found.emit(401, sid)
        await found.consume()
        assert not held(found, sid)

    run(store_dsn, None, body)


def test_ended_sessions_are_forgotten(store_dsn: str) -> None:
    async def body(found: FKit) -> None:
        sid = await running(found)
        other = await running(found, 2, state_="idle")
        for refused in (sid, other):
            found.emit(401, refused)
        await found.consume()
        await fallback.run_once(found.deliverer)
        found_ = revocation.revocations(found.context)
        found_.tries[other] = 1
        await found.state(sid, "stopped")
        await found.state(other, "failed")
        await fallback.run_once(found.deliverer)
        assert (found_.refused, found_.provisioned_at, found_.tries) == ({}, {}, {})
        assert not held(found, sid)
        assert not held(found, other)

    run(store_dsn, None, body)


def test_a_guest_that_cannot_take_the_sentence_now_gets_the_same_one_later(store_dsn: str, tmp_path: Path) -> None:
    async def body(found: FKit) -> None:
        sid = await running(found, state_="idle")
        put(found.pool.directory, "a", NEW)  # type: ignore[arg-type]
        found.emit(401, sid)
        await found.consume()
        found.guest.prompt_errors.append(GuestUnreachable("prompts"))
        await fallback.run_once(found.deliverer)
        assert f"runner.model model_continue_failed session_id={sid}" in found.log()
        assert held(found, sid)
        async with found.env.pool.connection() as conn:
            await conn.execute("UPDATE runner.vm SET state = 'stopped'")
        await fallback.run_once(found.deliverer)  # the VM is not running: tried again
        assert held(found, sid)
        async with found.env.pool.connection() as conn:
            await conn.execute("UPDATE runner.vm SET state = 'running'")
        await fallback.run_once(found.deliverer)
        assert len(found.guest.texts()) == 2
        assert len(set(found.guest.sent())) == 1  # the same prompt ID both times
        assert not held(found, sid)

    run(store_dsn, tmp_path, body, own=False)


def test_a_session_that_moved_on_meanwhile_is_looked_at_again(store_dsn: str, monkeypatch: Any) -> None:
    async def body(found: FKit) -> None:
        sid = await running(found, state_="idle")
        found.emit(401, sid)
        await found.consume()
        revocation.provisioned(found.context, sid, found.clock())

        class Changing:
            def __init__(self, session_id: str) -> None:
                self.session_id = session_id

            async def __aenter__(self) -> None:
                await found.state(self.session_id, "working")  # a prompt of a person was taken meanwhile

            async def __aexit__(self, *_exc: object) -> None:
                return None

        monkeypatch.setattr(found.deliverer, "lock", Changing)
        assert await fallback.renew(found.deliverer, await found.stored(sid))
        assert found.guest.texts() == []

    run(store_dsn, None, body)


def test_credit_first(store_dsn: str) -> None:
    async def body(found: FKit) -> None:
        sid = await running(found, state_="idle")
        found.emit(401, sid)
        await found.consume()
        revocation.provisioned(found.context, sid, found.clock())
        exhausted = {"api": 1, "active": True, "orgs": {desired().org_id: {"exhausted": True}}}
        found.set_desired(state(entry(1), billing=exhausted))
        await fallback.run_once(found.deliverer)
        assert found.guest.texts() == []
        assert held(found, sid)
        found.set_desired(state(entry(1)))
        await fallback.run_once(found.deliverer)
        assert found.guest.texts() == [fallback.RENEWED]

    run(store_dsn, None, body)


def test_the_hub_interface_version() -> None:
    assert not policy.renewed_api(state(entry(1)))
    assert policy.renewed_api(current())
    assert policy.api(current())
    assert not policy.renewed_api(None)


# -- hand-over of a renewal on this device (ADR-092 section 5) ------------------------------------------------------

RECORD = "01930099-0000-7000-8000-0000000000c1"
SHARED = operator.SHARED_PREFIX + RECORD


def test_a_renewal_on_this_device_reaches_the_bound_sessions_first(store_dsn: str, monkeypatch: Any) -> None:
    async def body(found: FKit) -> None:
        sid = await running(found, state_="idle")
        other = await running(found, 2, state_="idle")
        pool = found.pool
        operator.attach(found.context, pool)
        setattr(found.context, operator.ATTRIBUTE, pool)

        async def kept(_context: Any, _pool: Any) -> None:
            return None  # the record is not fetched in this unit: the entry stays as set

        monkeypatch.setattr(operator, "refresh_shared", kept)
        pool.shared[SHARED] = Entry((0, 0, 1, 0), token(1), None)
        pool.uses[SHARED] = Use(owner=ALICE, pool=False, orgs=frozenset({desired().org_id}), version=1)
        await operator.bind(found.context, pool, sid, SHARED)
        await gateways.refresh(found.kit.session_env, await found.stored(sid))
        assert held_token(found, sid) == token(1)
        before = len(found.credentials())
        await gateways.hand_over(found.context, RECORD, 2, NEW)
        assert held_token(found, sid) == NEW
        assert pool.current(SHARED) == (NEW, (0, 0, 2, 0))
        assert len(found.credentials()) == before + 1  # the session bound elsewhere got nothing
        assert held_token(found, other) == TOKEN
        assert access.provisions(found.context).stamps[sid].operator == (SHARED, (0, 0, 2, 0))
        # what the pool would not take, or an entry it does not hold, changes nothing
        await gateways.hand_over(found.context, RECORD, 3, "not-a-token")
        await gateways.hand_over(found.context, "01930099-0000-7000-8000-0000000000c2", 2, NEW)
        assert pool.current(SHARED) == (NEW, (0, 0, 2, 0))
        assert len(found.credentials()) == before + 1

    run(store_dsn, None, body)


def test_a_failing_hand_over_only_logs(monkeypatch: Any) -> None:
    async def failing(*_args: Any) -> None:
        raise RuntimeError("pool")

    monkeypatch.setattr(gateways, "hand_over", failing)
    log = io.StringIO()
    renewer = renewal.Renewer(context=SimpleNamespace(log=log))
    item = subscriptions.Subscription(RECORD, ALICE, False, 4, {})
    asyncio.run(renewer._hand_over(item, NEW))
    assert log.getvalue() == f"runner.renewal hand_over_failed id={RECORD} error=RuntimeError\n"
