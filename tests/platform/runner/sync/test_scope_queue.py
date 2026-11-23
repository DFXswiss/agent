"""KOR-08 (AUD-01 A-01, ADR-047): an item whose scope key the device lacks holds back only its own scope.

Each case of the package (key missing because the wrap has not arrived, no verified log head yet, a new epoch without
a wrap for this device, the hub unreachable, an item the hub refuses) is shown with the real store, the real outbox
and push logic and the fake hub of the sync suites: what happens with the items of **other** scopes, and when the
waiting ends. The cases that need verified logs (unassignment, removal, a stopped org) are in
`test_scope_discard.py`.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from agent_runner.subagents.results import drafts
from agent_runner.sync import outbox
from agent_runner.sync.http import HubTransportError
from agent_runner.sync.push import Pusher, PushOutcome

from .conftest import Env, Harness, run, uuid
from .test_outbox import draft, fixture_body
from .test_push import METRICS, Scope, adopt, posts

OUTBOX = "/v1/device/outbox"


def hub_items(env: Env) -> list[dict[str, Any]]:
    return env.hub.items()


def event_ids(env: Env, scope: Scope | None = None) -> list[str]:
    """The IDs of the events the hub stored (in `origin_seq` order), optionally of one scope only."""
    found = []
    for item in hub_items(env):
        if item["item_kind"] == "event":
            header = item["body"]["event"]["header"]
            if scope is None or header["scope_id"] == scope.wg:
                found.append(str(header["id"]))
    return found


def gapless(env: Env) -> None:
    assert sorted(env.hub.stored) == list(range(1, len(env.hub.stored) + 1))


async def held(env: Env) -> list[tuple[str, str, str]]:
    async with env.pool.connection() as conn:
        cur = await conn.execute("SELECT scope, scope_id::text, item_kind FROM runner.outbox_held ORDER BY id")
        return [(str(a), str(b), str(c)) for a, b, c in await cur.fetchall()]


async def enqueue_event(env: Env, scope: Scope, text: str = "Grüezi") -> str:
    item = scope.draft(text)
    await env.enqueue("event", item)
    return str(item["header"]["id"])


@pytest.mark.parametrize("missing", ["key", "head"])
def test_a_scope_without_its_key_holds_only_its_own_items(harness: Harness, missing: str) -> None:
    """Wrap not yet arrived (or rejected and not yet re-checked, SCN-08), or no verified log head of the org yet: the
    other workgroup's events and the device's other items go out; the waiting scope follows once sealable."""

    async def body() -> None:
        async with harness.env() as env:
            waiting, other = Scope(), Scope()
            await other.install(env)
            if missing == "head":
                await waiting.install(env, head=None)
            await adopt(env)
            a1 = await enqueue_event(env, waiting, "a1")
            b1 = await enqueue_event(env, other, "b1")
            await env.enqueue("metrics", METRICS)
            a2 = await enqueue_event(env, waiting, "a2")
            outcome = await env.sync.pusher.drain()
            assert outcome == PushOutcome(waiting="idle")
            # before KOR-08 nothing at all reached the hub: a1 at origin_seq 1 waited for its key
            assert event_ids(env) == [b1]
            assert [i["item_kind"] for i in hub_items(env)] == ["event", "metrics"]
            gapless(env)
            assert await held(env) == [("wg", waiting.wg, "event")] * 2
            async with env.pool.connection() as conn:
                # held items are no backlog: the metrics area must not stop its samples for a waiting scope
                assert await outbox.backlog(conn) == 0
            assert f"runner.sync scope_held scope=wg scope_id={waiting.wg} items=2\n" in env.log()
            # still waiting: another round changes nothing and logs nothing new
            assert await env.sync.pusher.drain() == PushOutcome(waiting="idle")
            assert env.log().count("scope_held") == 1
            # the scope's key (or head) arrives: its items follow in their order, numbered after the others
            if missing == "key":
                await waiting.install(env)
            else:
                await env.set_log_head(waiting.org, 5)
            await env.sync.pusher.drain()
            assert event_ids(env) == [b1, a1, a2]
            gapless(env)
            assert await held(env) == []
            assert f"runner.sync scope_released scope=wg scope_id={waiting.wg} items=2\n" in env.log()
            async with env.pool.connection() as conn:
                assert await outbox.backlog(conn) == 0

    run(body)


def test_items_of_a_held_scope_queue_behind_it(harness: Harness) -> None:
    """New items of a waiting scope never overtake its waiting ones: they join its queue and get no number yet."""

    async def body() -> None:
        async with harness.env() as env:
            waiting, other = Scope(), Scope()
            await other.install(env)
            await adopt(env)
            a1 = await enqueue_event(env, waiting, "a1")
            await env.sync.pusher.drain()
            async with env.pool.connection() as conn, conn.transaction():
                assert await env.sync.enqueue(conn, "event", waiting.draft("a2")) == 0
            item = waiting.draft("a3")
            a3 = str(item["header"]["id"])
            await env.enqueue("event", item)
            b1 = await enqueue_event(env, other, "b1")
            await env.sync.pusher.drain()
            assert event_ids(env) == [b1]
            assert len(await held(env)) == 3
            await waiting.install(env)
            await env.sync.pusher.drain()
            ids = event_ids(env, waiting)
            assert (len(ids), ids[0], ids[2]) == (3, a1, a3)
            gapless(env)

    run(body)


def test_a_new_epoch_without_a_wrap_holds_only_its_scope(harness: Harness) -> None:
    """A rotation the device has no wrap for yet: the hub answers `reseal` (E6), the item waits for a key of a newer
    epoch than the refused one; the items behind it of other scopes are renumbered and go out at once."""

    async def body() -> None:
        async with harness.env() as env:
            rotated, other = Scope(), Scope()
            await rotated.install(env)
            await other.install(env)
            await adopt(env)
            await env.enqueue("metrics", METRICS)
            a1 = await enqueue_event(env, rotated, "a1")
            await env.enqueue("metrics", METRICS)
            b1 = await enqueue_event(env, other, "b1")
            env.hub.epochs[rotated.wg] = 2
            await env.sync.pusher.drain()
            # before KOR-08 only the first metrics item arrived; everything behind a1 waited for the new wrap
            assert [i["item_kind"] for i in hub_items(env)] == ["metrics", "metrics", "event"]
            assert event_ids(env) == [b1]
            gapless(env)
            assert env.sync.pusher.reseal == {}
            async with env.pool.connection() as conn:
                cur = await conn.execute("SELECT min_epoch, sealed IS NULL FROM runner.outbox_held")
                assert await cur.fetchall() == [(2, True)]
            # the key of the refused epoch is not enough
            assert await env.sync.pusher.drain() == PushOutcome(waiting="idle")
            assert len(await held(env)) == 1
            await rotated.install(env, epoch=2, head=None)
            await env.sync.pusher.drain()
            assert event_ids(env) == [b1, a1]
            assert hub_items(env)[-1]["body"]["event"]["header"]["epoch"] == 2
            gapless(env)

    run(body)


def operation_result(scope: Scope, operation_id: str) -> dict[str, Any]:
    return draft(
        org=scope.org,
        wg=scope.wg,
        kind="operation.result",
        session="",
        ref=operation_id,
        body={"operation_id": operation_id, "status": "succeeded", "result": {}},
    )


def test_an_operation_transition_waits_for_its_result_event(harness: Harness) -> None:
    """The hub accepts a `result_event_id` only for a stored `operation.result` event (03-api.md section 9), so the
    transition that names a waiting result waits with it; a transition without a result does not."""

    async def body() -> None:
        async with harness.env() as env:
            waiting = Scope()
            await adopt(env)
            first, second = uuid(), uuid()
            result = operation_result(waiting, first)
            await env.enqueue("event", result)
            transition = {
                "operation_id": first,
                "state": "succeeded",
                "result_event_id": result["header"]["id"],
                "at": "2026-11-21T12:00:01.000Z",
            }
            await env.enqueue("operation_transition", transition)
            executing = {**fixture_body("operation_transition"), "operation_id": second}
            await env.enqueue("operation_transition", executing)
            await env.sync.pusher.drain()
            assert [i["body"] for i in hub_items(env)] == [executing]
            assert [kind for _, _, kind in await held(env)] == ["event", "operation_transition"]
            # a later transition naming the held result joins the queue at once
            again = {**transition, "at": "2026-11-21T12:00:02.000Z"}
            async with env.pool.connection() as conn, conn.transaction():
                assert await env.sync.enqueue(conn, "operation_transition", again) == 0
            await waiting.install(env)
            await env.sync.pusher.drain()
            kinds = [i["item_kind"] for i in hub_items(env)]
            assert kinds == ["operation_transition", "event", "operation_transition", "operation_transition"]
            assert hub_items(env)[2]["body"] == transition
            gapless(env)

    run(body)


def test_a_restart_in_the_middle_of_a_blockade_delivers_every_item_once(harness: Harness) -> None:
    """The queue is in the store: a new pusher (daemon restart) after a lost answer finds the held scope, resends the
    batch whose answer was lost with the same bytes (`replayed`) and delivers the held items once, in order."""

    async def body() -> None:
        async with harness.env() as env:
            waiting, other = Scope(), Scope()
            await other.install(env)
            await adopt(env)
            a1 = await enqueue_event(env, waiting, "a1")
            b1 = await enqueue_event(env, other, "b1")
            a2 = await enqueue_event(env, waiting, "a2")
            b2 = await enqueue_event(env, other, "b2")
            env.hub.tamper.append("unsigned")  # the hub stores b1 and b2, the answer does not verify
            with pytest.raises(HubTransportError):
                await env.sync.pusher.push_once()
            assert event_ids(env) == [b1, b2]
            old = env.sync.pusher
            fresh = Pusher(
                pool=env.pool,
                client=env.sync.client,
                storage_key=env.sync.storage_key,
                sealer=old.sealer,
                log=env.context.log,
                clock=old.clock,
                sleep=old.sleep,
                hedge_min=None,
            )
            await waiting.install(env)
            await fresh.drain()
            assert event_ids(env) == [b1, b2, a1, a2]
            assert len([r for r in env.hub.requests if r.target == OUTBOX]) == 2
            gapless(env)
            ids = event_ids(env)
            assert len(ids) == len(set(ids))
            async with env.pool.connection() as conn:
                assert await outbox.backlog(conn) == 0
            assert await held(env) == []

    run(body)


def test_an_unreachable_hub_holds_back_every_scope_until_it_answers(harness: Harness) -> None:
    """Hub unreachable: no scope is singled out, nothing is lost; everything goes once the hub answers again."""

    async def body() -> None:
        async with harness.env() as env:
            one, two = Scope(), Scope()
            await one.install(env)
            await two.install(env)
            await adopt(env)
            a1 = await enqueue_event(env, one, "a1")
            b1 = await enqueue_event(env, two, "b1")
            pusher = env.sync.pusher
            request = pusher.client.request
            down = [True]

            async def unreachable(method: str, target: str, body: object | None = None) -> dict[str, object]:
                if down[0]:
                    raise HubTransportError("unreachable")
                return await request(method, target, body)

            pusher.client.request = unreachable  # type: ignore[method-assign]
            for _ in range(3):
                with pytest.raises(HubTransportError):
                    await pusher.push_once()
            assert env.hub.stored == {}
            assert await held(env) == []
            down[0] = False
            await pusher.drain()
            assert event_ids(env) == [a1, b1]
            gapless(env)

    run(body)


def test_an_item_the_hub_refuses_does_not_stop_the_others(harness: Harness) -> None:
    """Archived workgroup or a device that is no longer a writer while it still holds the key: the event is sealed,
    the hub answers `rejected` (`author_not_writer`) and the sequence continues (03-api.md section 6.1)."""

    async def body() -> None:
        async with harness.env() as env:
            archived, other = Scope(), Scope()
            await archived.install(env)
            await other.install(env)
            await adopt(env)

            def refuse(item: dict[str, Any]) -> str | None:
                if item["item_kind"] != "event":
                    return None
                return "author_not_writer" if item["body"]["event"]["header"]["scope_id"] == archived.wg else None

            env.hub.reject = refuse
            await enqueue_event(env, archived, "a1")
            b1 = await enqueue_event(env, other, "b1")
            await env.sync.pusher.drain()
            assert len(event_ids(env)) == 2
            assert event_ids(env)[1] == b1
            assert "item_rejected origin_seq=1 item_kind=event error=author_not_writer" in env.log()
            gapless(env)
            assert json.dumps(await held(env)) == "[]"

    run(body)


def test_a_reseal_moves_the_later_items_of_its_scope_with_their_sealed_bytes(harness: Harness) -> None:
    """After `reseal` the refused event loses its sealed bytes; a later event of its scope that was sealed and sent in
    the same batch (never stored: the hub stops at the reseal) keeps them and is sent with them after the release."""

    async def body() -> None:
        async with harness.env() as env:
            rotated, other = Scope(), Scope()
            await rotated.install(env)
            await other.install(env)
            await adopt(env)
            a1 = await enqueue_event(env, rotated, "a1")
            b1 = await enqueue_event(env, other, "b1")
            a2 = await enqueue_event(env, rotated, "a2")
            env.hub.epochs[rotated.wg] = 2
            outcome = await env.sync.pusher.push_once()
            assert outcome.acked_through == 0
            assert env.sync.pusher.reseal == {1: 1}
            async with env.pool.connection() as conn:
                sent_a2 = (await outbox.load_batch(conn, env.sync.storage_key, 2, 1))[0].sealed
            assert sent_a2 is not None
            await env.sync.pusher.drain()
            assert event_ids(env) == [b1]
            async with env.pool.connection() as conn:
                cur = await conn.execute("SELECT min_epoch, sealed FROM runner.outbox_held ORDER BY id")
                assert await cur.fetchall() == [(2, None), (1, sent_a2)]
            await rotated.install(env, epoch=2, head=None)
            await env.sync.pusher.drain()
            assert event_ids(env) == [b1, a1, a2]
            # a2 went out with its kept bytes (epoch 1) at its new number, was refused with reseal and sealed again
            resent = [i["body"] for p in posts(env)[2:] for i in p["items"] if i["origin_seq"] == 3]
            assert resent[0] == {"event": sent_a2}
            assert [i["body"]["event"]["header"]["epoch"] for i in hub_items(env)[1:]] == [2, 2]
            gapless(env)

    run(body)


def test_items_that_do_not_belong_to_a_queue_get_their_number(harness: Harness) -> None:
    """Only events of the queued scope and transitions naming one of its results join a queue."""

    async def body() -> None:
        async with harness.env() as env:
            waiting = Scope()
            await adopt(env)
            await env.enqueue("event", waiting.draft("a1"))
            await env.sync.pusher.drain()
            assert len(await held(env)) == 1
            unrelated = {
                "operation_id": uuid(),
                "state": "succeeded",
                "result_event_id": uuid(),
                "at": "2026-11-21T12:00:01.000Z",
            }
            async with env.pool.connection() as conn, conn.transaction():
                assert await env.sync.enqueue(conn, "operation_transition", unrelated) == 1
                assert await env.sync.enqueue(conn, "operation_transition", {**unrelated, "result_event_id": "x"}) == 2
                assert await env.sync.enqueue(conn, "metrics", METRICS) == 3
                assert await env.sync.enqueue(conn, "event", Scope().draft("c1")) == 4
            assert len(await held(env)) == 1

    run(body)


def test_a_queue_discarded_meanwhile_is_skipped(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    async def body() -> None:
        async with harness.env() as env:
            await adopt(env)
            gone = ("wg", uuid(), 1)

            async def listed(conn: Any) -> list[tuple[str, str, int]]:
                return [gone]

            monkeypatch.setattr(outbox, "held_scopes", listed)
            assert await env.sync.pusher.push_once() == PushOutcome(waiting="idle")
            assert "scope_released" not in env.log()

    run(body)


def test_held_drafts_are_read_newest_first_and_before_the_numbered_ones(harness: Harness) -> None:
    """ADR-038: the final text of a subagent is read from the local drafts; drafts waiting in a scope queue are newer
    than every numbered item."""

    async def body() -> None:
        async with harness.env() as env:
            waiting, other = Scope(), Scope()
            await other.install(env)
            await adopt(env)
            child = uuid()

            def output(scope: Scope, text: str, session: str = child) -> dict[str, Any]:
                item = scope.draft(text)
                item["header"]["session"] = session
                return item

            await env.enqueue("event", output(other, "numbered"))
            await env.enqueue("event", output(waiting, "first"))
            await env.enqueue("event", output(waiting, "foreign", session=uuid()))
            await env.enqueue("event", output(waiting, "second"))
            await env.sync.pusher.drain()
            since = datetime.now(UTC) - timedelta(minutes=5)
            async with env.pool.connection() as conn:
                texts = [d["body"]["text"] async for d in outbox.event_drafts(conn, env.sync.storage_key, since, 2)]
                assert texts == ["second", "foreign", "first", "numbered"]
                session = SimpleNamespace(id=child, created_at=since)
                found = [body["text"] async for _, body in drafts(conn, env.sync.storage_key, session)]  # type: ignore[arg-type]
            assert found == ["second", "first", "numbered"]

    run(body)


def test_a_waiting_queue_is_logged_after_a_start_and_then_hourly(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            waiting = Scope()
            await adopt(env)
            await env.enqueue("event", waiting.draft("a1"))
            await env.sync.pusher.drain()
            await env.sync.pusher.drain()
            assert "scope_waiting" not in env.log()  # just logged as held
            old = env.sync.pusher
            fresh = Pusher(
                pool=env.pool,
                client=env.sync.client,
                storage_key=env.sync.storage_key,
                sealer=old.sealer,
                log=env.context.log,
                clock=old.clock,
                sleep=old.sleep,
                hedge_min=None,
            )
            line = f"runner.sync scope_waiting scope=wg scope_id={waiting.wg} items=1\n"
            await fresh.drain()
            await fresh.drain()
            assert env.log().count(line) == 1
            env.clock.now += 3600
            await fresh.drain()
            assert env.log().count(line) == 2
            await waiting.install(env)
            await fresh.drain()
            assert fresh._waiting_logged == {}

    run(body)


def test_a_gap_rewind_with_a_waiting_queue_resends_the_numbered_items(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            waiting, other = Scope(), Scope()
            await other.install(env)
            await adopt(env)
            await env.enqueue("metrics", METRICS)
            await env.enqueue("event", waiting.draft("a1"))
            b1 = await enqueue_event(env, other, "b1")
            await env.sync.pusher.drain()
            assert len(env.hub.stored) == 2
            del env.hub.stored[2]  # the hub lost the last item: it answers origin_seq_gap with expected 2
            await env.enqueue("metrics", METRICS)
            await env.sync.pusher.drain()
            assert "runner.sync gap pushed=2 expected=2" in env.log()
            assert [i["item_kind"] for i in hub_items(env)] == ["metrics", "event", "metrics"]
            assert event_ids(env) == [b1]
            await waiting.install(env)
            await env.sync.pusher.drain()
            assert len(event_ids(env)) == 2
            gapless(env)

    run(body)


def test_a_hold_after_a_lost_answer_keeps_the_items_the_hub_stored(harness: Harness) -> None:
    """The hub stored a batch whose answer was lost (`pushed` lags behind); a later hold renumbers only items behind
    that batch, so the resend is `replayed` and nothing conflicts."""

    async def body() -> None:
        async with harness.env() as env:
            waiting, other = Scope(), Scope()
            await other.install(env)
            await adopt(env)
            b1 = await enqueue_event(env, other, "b1")
            await env.enqueue("metrics", METRICS)
            env.hub.tamper.append("unsigned")
            with pytest.raises(HubTransportError):
                await env.sync.pusher.push_once()
            assert len(env.hub.stored) == 2
            await env.enqueue("event", waiting.draft("a1"))
            b2 = await enqueue_event(env, other, "b2")
            await env.sync.pusher.drain()
            assert event_ids(env) == [b1, b2]
            assert [i["item_kind"] for i in hub_items(env)] == ["event", "metrics", "event"]
            async with env.pool.connection() as conn:
                assert await outbox.sync_fatal(conn) is None
            gapless(env)

    run(body)


def test_writers_running_beside_a_hold_keep_the_numbering_gapless(harness: Harness) -> None:
    """`enqueue` and `hold` serialise on the outbox lock: concurrent writers of both scopes during holds and releases
    leave a gapless stream at the hub with every item once and each scope in order."""

    async def body() -> None:
        async with harness.env() as env:
            waiting, other = Scope(), Scope()
            await other.install(env)
            await adopt(env)
            written: list[tuple[str, str]] = []

            async def writer(scope: Scope, name: str) -> None:
                for n in range(15):
                    item = scope.draft(f"{name}{n}")
                    await env.enqueue("event", item)
                    written.append((name, str(item["header"]["id"])))
                    await asyncio.sleep(0)

            async def pusher() -> None:
                for _ in range(20):
                    await env.sync.pusher.push_once()
                    await asyncio.sleep(0)

            await asyncio.gather(writer(waiting, "a"), writer(other, "b"), pusher())
            await waiting.install(env)
            await env.sync.pusher.drain()
            gapless(env)
            for name, scope in (("a", waiting), ("b", other)):
                assert event_ids(env, scope) == [i for n, i in written if n == name]

    run(body)
