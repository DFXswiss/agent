"""U-RUN-outbox (hedged push, ADR-050): a push whose answer is late is sent once more on another connection; the first
answer counts, the hub stores every item once, and the wait adapts to the usual time to an answer."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from agent_runner.sync import outbox
from agent_runner.sync import push as push_module
from agent_runner.sync.http import HubRefusal, HubTransportError
from agent_runner.sync.push import HEDGE_FACTOR, HEDGE_MIN_S, HEDGE_SPACING_S, HEDGE_WINDOW, Pusher, SyncFatal

from .conftest import Env, Harness, run
from .fakehub import canonical
from .test_outbox import fixture_body
from .test_push import METRICS, Scope, adopt, enqueue_many, posts

OUTBOX = "/v1/device/outbox"


class StaleOnce(httpx.AsyncBaseTransport):
    """The first outbox request goes out on a reused connection, reaches the hub once `gate` is set and fails before
    its answer begins (the KOR-18 race): the client sends it once more on a new connection."""

    def __init__(self, gate: asyncio.Event) -> None:
        self.gate = gate
        self.inner: httpx.AsyncBaseTransport | None = None
        self.armed = True

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        assert self.inner is not None
        if self.armed and request.url.path == OUTBOX:
            self.armed = False
            await request.extensions["trace"]("http11.send_request_headers.started", {})
            await self.gate.wait()
            response = await self.inner.handle_async_request(request)  # the hub processes it ...
            await response.aclose()
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")  # ... the answer is lost
        return await self.inner.handle_async_request(request)


@dataclass
class Mono:
    """Monotonic clock of the hedge; each outbox answer takes `step` seconds."""

    now: float = 50.0
    step: float = 0.01

    def __call__(self) -> float:
        return self.now


def _events() -> list[asyncio.Event]:
    return [asyncio.Event() for _ in range(64)]


@dataclass
class Wire:
    """Scripted outbox requests. Request `n`:

    - waits for `late[n]` before it reaches the hub (it hangs on the way); cancelled meanwhile, its bytes still reach
      the hub when `late[n]` is set, and the hub's answer, which nobody reads, lands in `delivered[n]`;
    - waits for `hold[n]` after the hub answered or refused it (the answer hangs on the way back);
    - fails with `fail[n]` (instead of the hub's answer or refusal);
    - cancelled, finishes only when `linger[n]` is set (a request whose abort takes a while).

    `answered[n]` is set when the hub has processed request `n`; `order` records `enter n` and `end n` (the request
    has finished in the runner, answered, failed or cancelled)."""

    hold: dict[int, asyncio.Event] = field(default_factory=dict)
    fail: dict[int, Exception] = field(default_factory=dict)
    late: dict[int, asyncio.Event] = field(default_factory=dict)
    linger: dict[int, asyncio.Event] = field(default_factory=dict)
    entered: list[asyncio.Event] = field(default_factory=_events)
    answered: list[asyncio.Event] = field(default_factory=_events)
    delivered: dict[int, asyncio.Future[object]] = field(default_factory=dict)
    cancelled: list[int] = field(default_factory=list)
    order: list[str] = field(default_factory=list)
    calls: int = 0


@dataclass
class Scripted:
    """Stands in for `HubClient.request`: outbox requests follow `wire`, everything else goes straight through."""

    request: Callable[..., Awaitable[dict[str, object]]]
    wire: Wire
    mono: Mono

    async def reach_hub(self, n: int, method: str, target: str, body: object | None) -> object:
        """The hub's answer to request `n`, or its refusal (a verified `HubRefusal` as a value)."""
        try:
            answer: object = await self.request(method, target, body)
        except HubRefusal as refusal:
            answer = refusal
        self.wire.answered[n].set()
        return answer

    async def deliver_late(self, n: int, method: str, target: str, body: object | None) -> object:
        await self.wire.late[n].wait()
        return await self.reach_hub(n, method, target, body)

    async def __call__(self, method: str, target: str, body: object | None = None) -> dict[str, object]:
        if target != OUTBOX:
            return await self.request(method, target, body)
        wire = self.wire
        n = wire.calls
        wire.calls += 1
        wire.order.append(f"enter {n}")
        wire.entered[n].set()
        try:
            if n in wire.late:
                await wire.late[n].wait()
            answer = await self.reach_hub(n, method, target, body)
            if n in wire.hold:
                await wire.hold[n].wait()
        except asyncio.CancelledError:
            wire.cancelled.append(n)
            if n in wire.late and not wire.late[n].is_set():
                # the request is already on the wire: the runner gives it up, the hub still gets it
                wire.delivered[n] = asyncio.ensure_future(self.deliver_late(n, method, target, body))
            if n in wire.linger:
                await wire.linger[n].wait()
            wire.order.append(f"end {n}")
            raise
        wire.order.append(f"end {n}")
        if n in wire.fail:
            raise wire.fail[n]
        if isinstance(answer, HubRefusal):
            raise answer
        assert isinstance(answer, dict)
        self.mono.now += self.mono.step
        return answer


@dataclass
class Rig:
    env: Env
    pusher: Pusher
    wire: Wire
    mono: Mono
    fire: asyncio.Event
    delays: list[float]


@asynccontextmanager
async def rig(
    harness: Harness, *, hedge_min: float | None = HEDGE_MIN_S, transport: StaleOnce | None = None
) -> AsyncIterator[Rig]:
    mono, fire, delays, wire = Mono(), asyncio.Event(), [], Wire()

    async def timer(seconds: float) -> None:
        delays.append(seconds)
        await fire.wait()

    settings = {"hedge_min": hedge_min, "hedge_timer": timer, "monotonic": mono}
    options: dict[str, Any] = {"pusher_settings": settings}
    if transport is not None:
        options["transport"] = transport
    async with harness.env(options=options) as env:
        if transport is not None:
            transport.inner = httpx.ASGITransport(app=env.hub)  # type: ignore[arg-type]
        pusher = env.sync.pusher
        scripted = Scripted(pusher.client.request, wire, mono)
        pusher.client.request = scripted  # type: ignore[method-assign]
        await adopt(env)
        try:
            yield Rig(env=env, pusher=pusher, wire=wire, mono=mono, fire=fire, delays=delays)
        finally:
            for late in wire.delivered.values():
                late.cancel()
            await asyncio.gather(*wire.delivered.values(), return_exceptions=True)


async def acked(env: Env) -> int | None:
    async with env.pool.connection() as conn:
        return await outbox.pushed_origin_seq(conn)


def test_a_fast_answer_is_not_hedged(harness: Harness) -> None:
    async def body() -> None:
        async with rig(harness) as r:
            await enqueue_many(r.env, "metrics", [METRICS] * 3)
            outcome = await r.pusher.push_once()
            assert outcome.acked_through == 3
            assert r.delays == [HEDGE_MIN_S]
            assert len(posts(r.env)) == 1
            assert "push_hedged" not in r.env.log()

    run(body)


def test_a_late_answer_is_hedged_and_the_hub_stores_every_item_once(harness: Harness) -> None:
    async def body() -> None:
        async with rig(harness) as r:
            await enqueue_many(r.env, "metrics", [METRICS] * 3)
            r.wire.hold[0] = asyncio.Event()  # the first request reaches the hub, its answer hangs on the way
            push = asyncio.ensure_future(r.pusher.push_once())
            await r.wire.entered[0].wait()
            r.fire.set()
            outcome = await push
            assert outcome.acked_through == 3
            assert await acked(r.env) == 3
            sent = posts(r.env)
            assert len(sent) == 2
            assert sent[0] == sent[1]  # the same bytes, signed anew (the hub saw no replay)
            assert sorted(r.env.hub.stored) == [1, 2, 3]
            assert r.wire.cancelled == [0]  # the late request is given up
            assert f"runner.sync push_hedged after_ms={round(HEDGE_MIN_S * 1000)}\n" in r.env.log()

    run(body)


def test_the_first_answer_counts_when_it_arrives_after_the_hedge_left(harness: Harness) -> None:
    async def body() -> None:
        async with rig(harness) as r:
            await enqueue_many(r.env, "metrics", [METRICS] * 2)
            r.wire.hold[0], r.wire.hold[1] = asyncio.Event(), asyncio.Event()
            push = asyncio.ensure_future(r.pusher.push_once())
            await r.wire.entered[0].wait()
            r.fire.set()
            await r.wire.entered[1].wait()
            r.wire.hold[0].set()
            assert (await push).acked_through == 2
            assert r.wire.cancelled == [1]

    run(body)


def test_a_failed_hedge_waits_for_the_late_answer(harness: Harness) -> None:
    async def body() -> None:
        async with rig(harness) as r:
            await enqueue_many(r.env, "metrics", [METRICS] * 2)
            r.wire.hold[0] = asyncio.Event()
            r.wire.fail[1] = HubRefusal(429, "rate_limited", retry_after=1)
            push = asyncio.ensure_future(r.pusher.push_once())
            await r.wire.entered[0].wait()
            r.fire.set()
            await r.wire.entered[1].wait()
            await asyncio.sleep(0.01)
            assert not push.done()
            r.wire.hold[0].set()
            assert (await push).acked_through == 2
            assert r.wire.cancelled == []

    run(body)


def test_when_both_fail_the_error_of_the_first_request_propagates(harness: Harness) -> None:
    async def body() -> None:
        async with rig(harness) as r:
            await enqueue_many(r.env, "metrics", [METRICS] * 2)
            r.wire.hold[0] = asyncio.Event()
            r.wire.fail[0] = HubTransportError("hub_unreachable", reused=False, cause="ReadError")
            r.wire.fail[1] = HubRefusal(429, "rate_limited", retry_after=1)
            push = asyncio.ensure_future(r.pusher.push_once())
            await r.wire.entered[0].wait()
            r.fire.set()
            await r.wire.entered[1].wait()
            r.wire.hold[0].set()
            with pytest.raises(HubTransportError) as raised:
                await push
            assert raised.value.cause == "ReadError"
            assert await acked(r.env) == 0
            # a failed push teaches the hedge nothing
            assert r.pusher.hedge_after() == HEDGE_MIN_S

    run(body)


def test_the_wait_follows_the_usual_time_to_an_answer(harness: Harness) -> None:
    async def body() -> None:
        async with rig(harness) as r:
            r.mono.step = 0.2  # a hub that is slow for every push (many busy sessions)
            for _ in range(HEDGE_WINDOW):
                await enqueue_many(r.env, "metrics", [METRICS])
                await r.pusher.push_once()
            assert r.pusher.hedge_after() == pytest.approx(HEDGE_FACTOR * 0.2)
            r.mono.step = 0.01  # the hub is quick again: after a window of quick answers the floor holds
            for _ in range(HEDGE_WINDOW // 2 + 1):
                await enqueue_many(r.env, "metrics", [METRICS])
                await r.pusher.push_once()
            assert r.pusher.hedge_after() == HEDGE_MIN_S
            assert r.delays[0] == HEDGE_MIN_S
            assert r.delays[HEDGE_WINDOW] == pytest.approx(HEDGE_FACTOR * 0.2)
            assert "push_hedged" not in r.env.log()

    run(body)


def test_hedging_can_be_turned_off(harness: Harness) -> None:
    async def body() -> None:
        async with rig(harness, hedge_min=None) as r:
            await enqueue_many(r.env, "metrics", [METRICS])
            assert r.pusher.hedge_after() is None
            assert (await r.pusher.push_once()).acked_through == 1
            assert r.delays == []

    run(body)


def test_a_cancelled_push_leaves_no_request_behind(harness: Harness) -> None:
    """Shutdown cancels the push loop while both requests are on the way: both are cancelled."""

    async def body() -> None:
        async with rig(harness) as r:
            await enqueue_many(r.env, "metrics", [METRICS])
            r.wire.hold[0], r.wire.hold[1] = asyncio.Event(), asyncio.Event()
            push = asyncio.ensure_future(r.pusher.push_once())
            await r.wire.entered[0].wait()
            r.fire.set()
            await r.wire.entered[1].wait()
            push.cancel()
            with pytest.raises(asyncio.CancelledError):
                await push
            assert sorted(r.wire.cancelled) == [0, 1]

    run(body)


async def hanging_push(r: Rig, n: int) -> asyncio.Future[Any]:
    """Start a push whose request `n` reaches the hub and whose answer waits for `hold[n]`."""
    r.wire.hold[n] = asyncio.Event()
    push = asyncio.ensure_future(r.pusher.push_once())
    await r.wire.entered[n].wait()
    return push


def test_at_most_one_copy_within_the_spacing(harness: Harness) -> None:
    """Copies spend the hub's pacer (ADR-048): a second late push within the spacing waits for its own answer."""

    async def body() -> None:
        async with rig(harness) as r:
            r.fire.set()
            await enqueue_many(r.env, "metrics", [METRICS])
            push = await hanging_push(r, 0)
            assert (await push).acked_through == 1  # hedged: request 1 answered
            await enqueue_many(r.env, "metrics", [METRICS])
            push = await hanging_push(r, 2)
            await asyncio.sleep(0.01)
            assert not push.done()
            assert r.wire.calls == 3  # no copy
            r.wire.hold[2].set()
            assert (await push).acked_through == 2
            assert r.env.log().count("push_hedged") == 1
            r.mono.now += HEDGE_SPACING_S
            await enqueue_many(r.env, "metrics", [METRICS])
            push = await hanging_push(r, 3)
            assert (await push).acked_through == 3
            assert r.env.log().count("push_hedged") == 2
            assert r.delays == [HEDGE_MIN_S, HEDGE_MIN_S]

    run(body)


def test_a_large_batch_is_not_copied(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    async def body() -> None:
        async with rig(harness) as r:
            r.fire.set()
            await enqueue_many(r.env, "metrics", [METRICS] * 3)
            monkeypatch.setattr(push_module, "HEDGE_MAX_BYTES", 100)
            push = await hanging_push(r, 0)
            await asyncio.sleep(0.01)
            assert r.wire.calls == 1
            r.wire.hold[0].set()
            assert (await push).acked_through == 3
            assert r.delays == []
            assert "push_hedged" not in r.env.log()

    run(body)


# --- the hedge together with refusals, reseal, gap, realignment and the KOR-18 repeat (KOR-19) ------------------------
#
# A request the runner gives up may still reach the hub later: its bytes are on the wire. The hub answers such a late
# request `replayed`, or refuses it with `409 origin_seq_conflict` / `origin_seq_gap` without any effect when an item
# of its batch was stored meanwhile with other bytes (resealed or renumbered); nobody reads that answer. This holds
# because the runner cancels the other request and waits until it has ended before the push returns, so no later
# push ever reads it (ADR-050 section 2).


async def fatal_state(env: Env) -> str | None:
    async with env.pool.connection() as conn:
        return await outbox.sync_fatal(conn)


def test_a_refused_first_request_does_not_count_when_the_copy_is_answered(harness: Harness) -> None:
    """The first request is refused (`429`, verified) while the copy is on the way: the copy's answer counts, the
    push succeeds instead of raising the refusal, and the hub stores every item once."""

    async def body() -> None:
        async with rig(harness) as r:
            await enqueue_many(r.env, "metrics", [METRICS] * 2)
            r.env.hub.scripted.append((429, {"error": "rate_limited"}, {"retry-after": "1"}))
            r.wire.hold[0], r.wire.hold[1] = asyncio.Event(), asyncio.Event()
            push = asyncio.ensure_future(r.pusher.push_once())
            await r.wire.entered[0].wait()
            r.fire.set()
            await r.wire.answered[1].wait()
            r.wire.hold[0].set()  # the refusal arrives first
            await asyncio.sleep(0.01)
            assert not push.done()  # the copy is still awaited
            r.wire.hold[1].set()
            outcome = await push
            assert outcome.acked_through == 2
            assert await acked(r.env) == 2
            assert sorted(r.env.hub.stored) == [1, 2]
            assert r.wire.cancelled == []
            assert await fatal_state(r.env) is None

    run(body)


async def reseal_setup(r: Rig) -> Scope:
    """Items 1 (metrics) and 2 (an event, sealed with epoch 1 by the push); the hub has rotated the scope to epoch 2.
    `rotated` gives the device the new key once the push has left, so `reseal` for item 2 is followed by a new seal."""
    scope = Scope()
    await scope.install(r.env)
    await r.env.enqueue("metrics", METRICS)
    await r.env.enqueue("event", scope.draft())
    r.env.hub.epochs[scope.wg] = 2
    return scope


async def rotated(r: Rig, scope: Scope) -> None:
    await scope.install(r.env, epoch=2, head=None)


async def finish_after_reseal(r: Rig, late: int) -> None:
    """The next push stores the resealed event; afterwards request `late` (old bytes) reaches the hub."""
    assert r.pusher.reseal == {2: 1}
    follow = asyncio.ensure_future(r.pusher.push_once())
    await r.wire.answered[2].wait()  # the new seal of item 2 is stored
    r.wire.late[late].set()
    await r.wire.answered[late].wait()
    outcome = await follow
    assert outcome.acked_through == 2
    stored = dict(r.env.hub.stored)
    refusal = await r.wire.delivered[late]
    assert isinstance(refusal, HubRefusal)
    assert (refusal.status, refusal.code) == (409, "origin_seq_conflict")
    assert r.env.hub.stored == stored  # refused without any effect
    assert sorted(r.env.hub.stored) == [1, 2]
    assert r.env.hub.items()[1]["body"]["event"]["header"]["epoch"] == 2
    log = r.env.log()
    assert "runner.alert" not in log
    assert "stopped" not in log
    assert r.pusher.fatal is None
    assert await fatal_state(r.env) is None
    # pushing goes on
    await r.env.enqueue("metrics", METRICS)
    assert (await r.pusher.push_once()).acked_through == 3
    assert sorted(r.env.hub.stored) == [1, 2, 3]


def test_a_late_first_request_after_a_reseal_answered_to_the_copy_is_refused_without_effect(harness: Harness) -> None:
    """The first request hangs on the way; the copy is answered `reseal` for the event. The runner seals it anew and
    the next push stores the new bytes. Then the given-up first request reaches the hub with the old bytes: `409
    origin_seq_conflict`, nothing written, and the runner never sees it (no `sync_fatal`, no alert, pushing goes on)."""

    async def body() -> None:
        async with rig(harness) as r:
            scope = await reseal_setup(r)
            r.wire.late[0] = asyncio.Event()
            push = asyncio.ensure_future(r.pusher.push_once())
            await r.wire.entered[0].wait()
            await rotated(r, scope)
            r.fire.set()
            outcome = await push
            assert outcome.acked_through == 1
            assert outcome.more
            assert not r.wire.answered[0].is_set()  # still on the way
            await finish_after_reseal(r, late=0)
            assert r.wire.cancelled == [0]

    run(body)


def test_a_late_copy_after_a_reseal_answered_to_the_first_request_is_refused_without_effect(harness: Harness) -> None:
    """The mirror order: the first request is answered `reseal` after the copy left, the copy hangs on the way and
    reaches the hub only after the resealed event is stored."""

    async def body() -> None:
        async with rig(harness) as r:
            scope = await reseal_setup(r)
            r.wire.hold[0], r.wire.late[1] = asyncio.Event(), asyncio.Event()
            push = asyncio.ensure_future(r.pusher.push_once())
            await r.wire.answered[0].wait()
            await rotated(r, scope)
            r.fire.set()
            await r.wire.entered[1].wait()
            r.wire.hold[0].set()
            outcome = await push
            assert outcome.acked_through == 1
            await finish_after_reseal(r, late=1)
            assert r.wire.cancelled == [1]

    run(body)


def test_the_next_push_starts_only_after_the_given_up_request_has_ended(harness: Harness) -> None:
    """The copy wins; the abort of the first request takes a while. The push returns, and the next push starts, only
    after that request has ended: no later push can read its answer."""

    async def body() -> None:
        async with rig(harness) as r:
            await enqueue_many(r.env, "metrics", [METRICS])
            r.wire.hold[0], r.wire.linger[0] = asyncio.Event(), asyncio.Event()
            push = asyncio.ensure_future(r.pusher.push_once())
            await r.wire.entered[0].wait()
            r.fire.set()
            await r.wire.answered[1].wait()
            await enqueue_many(r.env, "metrics", [METRICS])
            following = asyncio.ensure_future(r.pusher.push_once())
            while 0 not in r.wire.cancelled:
                await asyncio.sleep(0)
            await asyncio.sleep(0.05)
            assert not push.done()
            assert r.wire.calls == 2  # no next request while the first one has not ended
            r.wire.linger[0].set()
            assert (await push).acked_through == 1
            assert (await following).acked_through == 2
            assert r.wire.order == ["enter 0", "enter 1", "end 1", "end 0", "enter 2", "end 2"]

    run(body)


def test_a_gap_answered_to_both_requests_rewinds_once_and_doubles_nothing(harness: Harness) -> None:
    async def body() -> None:
        async with rig(harness) as r:
            await enqueue_many(r.env, "metrics", [METRICS] * 5)
            await r.pusher.drain()
            for seq in (3, 4, 5):
                del r.env.hub.stored[seq]  # the hub lost the tail
            await r.env.enqueue("metrics", METRICS)
            first = r.wire.calls
            r.wire.hold[first], r.wire.hold[first + 1] = asyncio.Event(), asyncio.Event()
            push = asyncio.ensure_future(r.pusher.push_once())
            await r.wire.answered[first].wait()
            r.fire.set()
            await r.wire.answered[first + 1].wait()
            r.wire.hold[first].set()
            r.wire.hold[first + 1].set()
            assert await push == push_module.PushOutcome(more=True)
            assert r.env.log().count("runner.sync gap ") == 1
            assert "runner.sync gap pushed=5 expected=3\n" in r.env.log()
            await r.pusher.drain()
            assert [i["origin_seq"] for i in posts(r.env)[-1]["items"]] == [3, 4, 5, 6]
            assert sorted(r.env.hub.stored) == [1, 2, 3, 4, 5, 6]
            assert await acked(r.env) == 6
            assert r.env.log().count("push_hedged") == 1

    run(body)


def test_a_late_request_in_the_old_numbering_after_a_realignment_is_refused_without_effect(harness: Harness) -> None:
    """KOR-10: the copy of a push is answered, the first request hangs on the way. The hub is later restored below
    the items, which the runner has pruned meanwhile; it realigns (renumbers) and pushes again. Then the first request
    reaches the hub with the old numbers: refused without effect, unseen by the runner. (Pruned items are older than
    an hour, so in practice such a request cannot arrive before the realigned items are stored.)"""

    async def body() -> None:
        async with rig(harness) as r:
            await enqueue_many(r.env, "metrics", [METRICS])
            await r.pusher.drain()
            await enqueue_many(r.env, "metrics", [METRICS, METRICS])
            r.wire.late[1] = asyncio.Event()
            push = asyncio.ensure_future(r.pusher.push_once())
            await r.wire.entered[1].wait()
            r.fire.set()
            assert (await push).acked_through == 3
            assert r.wire.cancelled == [1]
            async with r.env.pool.connection() as conn, conn.transaction():
                await conn.execute("UPDATE runner.outbox SET created_at = now() - interval '2 hours'")
                assert await outbox.prune(conn) == 2
            await r.env.enqueue("audit", fixture_body("audit"))
            for seq in (2, 3):
                del r.env.hub.stored[seq]  # restored from an export that ends at origin_seq 1
            assert await r.pusher.push_once() == push_module.PushOutcome(more=True)
            assert "runner.alert outbox_items_lost expected=2 lost=1\n" in r.env.log()
            await r.pusher.drain()
            stored = dict(r.env.hub.stored)
            assert sorted(stored) == [1, 2, 3, 4]
            r.wire.late[1].set()
            refusal = await r.wire.delivered[1]
            assert isinstance(refusal, HubRefusal)
            assert (refusal.status, refusal.code) == (409, "origin_seq_conflict")
            assert r.env.hub.stored == stored
            assert r.pusher.fatal is None
            assert await fatal_state(r.env) is None
            await r.env.enqueue("metrics", METRICS)
            assert (await r.pusher.push_once()).acked_through == 5

    run(body)


def test_a_conflict_answered_to_the_first_request_stays_fatal_while_the_copy_fails(harness: Harness) -> None:
    """A real conflict (the hub holds other bytes) answered to the first request is fatal as before, also when the
    copy, still on the way, then fails on the transport."""

    async def body() -> None:
        async with rig(harness) as r:
            r.env.hub.stored[1] = canonical({"origin_seq": 1, "item_kind": "metrics", "body": {"other": 1}})
            await r.env.enqueue("metrics", METRICS)
            r.wire.hold[0], r.wire.hold[1] = asyncio.Event(), asyncio.Event()
            r.wire.fail[1] = HubTransportError("hub_unreachable", reused=False, cause="ReadError")
            push = asyncio.ensure_future(r.pusher.push_once())
            await r.wire.answered[0].wait()
            r.fire.set()
            await r.wire.entered[1].wait()
            r.wire.hold[0].set()  # the conflict arrives first, the copy is still on the way
            await asyncio.sleep(0.01)
            assert not push.done()
            r.wire.hold[1].set()
            with pytest.raises(SyncFatal, match="origin_seq_conflict"):
                await push
            assert "runner.alert origin_seq_conflict\n" in r.env.log()
            assert await fatal_state(r.env) == "origin_seq_conflict"

    run(body)


def test_the_hedge_together_with_the_repeat_on_a_stale_connection(harness: Harness) -> None:
    """KOR-18: the first request fails on a reused connection before its answer began (after the hub processed it)
    and the client sends it once more while the copy is on the way. Exactly one answer counts; the hub stores every
    item once."""

    async def body() -> None:
        gate = asyncio.Event()
        async with rig(harness, transport=StaleOnce(gate)) as r:
            await enqueue_many(r.env, "metrics", [METRICS] * 3)
            r.wire.hold[1] = asyncio.Event()
            push = asyncio.ensure_future(r.pusher.push_once())
            await r.wire.entered[0].wait()
            r.fire.set()
            await r.wire.answered[1].wait()  # the copy is processed, its answer hangs
            gate.set()  # now the first attempt reaches the hub and its connection breaks
            for _ in range(1000):  # the repeat of the first request is answered and the copy given up
                if "end 1" in r.wire.order:
                    break
                await asyncio.sleep(0)
            r.wire.hold[1].set()  # lets the copy answer only if the push still waits for it
            outcome = await push
            assert outcome.acked_through == 3
            assert r.wire.cancelled == [1]
            assert len(posts(r.env)) == 3  # first attempt, its repeat, the copy
            assert sorted(r.env.hub.stored) == [1, 2, 3]
            assert await acked(r.env) == 3
            assert "push_failed" not in r.env.log()

    run(body)
