"""U-RUN-outbox (push): batches (500 items / 4 MiB), rate, seal-once over retries, reseal, gap, conflict, rejected,
continuation after a store wipe and the push loop (03-api.md section 6.1, 04-crypto.md section 5.12)."""

from __future__ import annotations

import asyncio
import itertools
import json
import os
from typing import Any

import pytest

from agent_runner.crypto.event import open_event
from agent_runner.sync import outbox
from agent_runner.sync.http import HubRefusal, HubTransportError, encode_body
from agent_runner.sync.push import IDLE_POLL_S, MAX_BATCH_BYTES, MIN_INTERVAL_S, Pusher, PushOutcome, SyncFatal

from .conftest import Env, Harness, run, uuid
from .fakehub import CONTRACTS, canonical
from .test_outbox import draft, fixture_body

METRICS = fixture_body("metrics")


async def adopt(env: Env, last: int = 0) -> None:
    async with env.pool.connection() as conn:
        assert await outbox.adopt_hub_position(conn, env.sync.storage_key, last)


async def enqueue_many(env: Env, kind: str, bodies: list[dict[str, Any]]) -> None:
    async with env.pool.connection() as conn, conn.transaction():
        for body in bodies:
            await env.sync.enqueue(conn, kind, body)


class Scope:
    """A workgroup scope the device holds keys for, with a verified log head."""

    def __init__(self) -> None:
        self.org, self.wg = uuid(), uuid()
        self.keys = {1: os.urandom(32), 2: os.urandom(32)}

    async def install(self, env: Env, epoch: int = 1, head: int | None = 5) -> None:
        await env.add_scope_key(self.wg, epoch, self.keys[epoch])
        if head is not None:
            await env.set_log_head(self.org, head)

    def draft(self, text: str = "Grüezi") -> dict[str, Any]:
        return draft(org=self.org, wg=self.wg, body={"message_id": "m1", "role": "assistant", "text": text, "turn": 1})


def posts(env: Env) -> list[dict[str, Any]]:
    return [r.body for r in env.hub.requests if r.target == "/v1/device/outbox"]


def test_batches_of_500_items_at_most_once_per_interval(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            await enqueue_many(env, "metrics", [METRICS] * 1201)
            outcome = await env.sync.pusher.drain()
            assert outcome == PushOutcome(waiting="idle")
            assert [r.target for r in env.hub.requests][:1] == ["/v1/device/state"]
            assert [len(p["items"]) for p in posts(env)] == [500, 500, 201]
            # the first push leaves at once; the hub answers in no time, so each later one waits a whole interval
            assert env.clock.sleeps == pytest.approx([MIN_INTERVAL_S, MIN_INTERVAL_S])
            assert MIN_INTERVAL_S < 0.25
            assert sorted(env.hub.stored) == list(range(1, 1202))
            async with env.pool.connection() as conn:
                assert await outbox.pushed_origin_seq(conn) == 1201
                assert await outbox.backlog(conn) == 0
                cur = await conn.execute("SELECT count(*) FROM runner.outbox WHERE pushed_at IS NULL")
                assert (await cur.fetchone()) == (0,)
            assert "runner.sync position_adopted last_origin_seq=0\n" in env.log()

    run(body)


def slow_answers(env: Env, seconds: float, starts: list[float]) -> None:
    """Every outbox answer arrives `seconds` after its request left; `starts` collects the clock at each send."""
    pusher = env.sync.pusher
    request = pusher.client.request

    async def slow(method: str, target: str, body: object | None = None) -> dict[str, object]:
        if target == "/v1/device/outbox":
            starts.append(env.clock.now)
        answer = await request(method, target, body)
        if target == "/v1/device/outbox":
            env.clock.now += seconds
        return answer

    pusher.client.request = slow  # type: ignore[method-assign]


def test_the_interval_counts_from_the_start_of_the_previous_push(harness: Harness) -> None:
    """KOR-13: pushes are paced start to start, so the time a push spends on the way counts towards the interval;
    the hub's pacer tolerates the jitter of arrival times (agent-core `agent_hub.outbox.pacing`)."""

    async def body() -> None:
        async with harness.env() as env:
            await enqueue_many(env, "metrics", [METRICS] * 1001)
            starts: list[float] = []
            slow_answers(env, 0.4 * MIN_INTERVAL_S, starts)
            await env.sync.pusher.drain()
            assert [len(p["items"]) for p in posts(env)] == [500, 500, 1]
            assert env.clock.sleeps == pytest.approx([0.6 * MIN_INTERVAL_S, 0.6 * MIN_INTERVAL_S])
            assert [b - a for a, b in itertools.pairwise(starts)] == pytest.approx([MIN_INTERVAL_S] * 2)

    run(body)


def test_a_push_slower_than_the_interval_is_followed_at_once(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            await enqueue_many(env, "metrics", [METRICS] * 1001)
            starts: list[float] = []
            slow_answers(env, 3 * MIN_INTERVAL_S, starts)
            await env.sync.pusher.drain()
            assert [len(p["items"]) for p in posts(env)] == [500, 500, 1]
            assert env.clock.sleeps == []
            assert [b - a for a, b in itertools.pairwise(starts)] == pytest.approx([3 * MIN_INTERVAL_S] * 2)

    run(body)


def test_an_item_after_a_quiet_interval_is_pushed_at_once(harness: Harness) -> None:
    """Leading edge: when no push ran within the last interval, a new item leaves without waiting."""

    async def body() -> None:
        async with harness.env() as env:
            pusher = env.sync.pusher
            await enqueue_many(env, "metrics", [METRICS])
            await pusher.push_once()
            env.clock.now += MIN_INTERVAL_S
            await enqueue_many(env, "metrics", [METRICS])
            assert (await pusher.push_once()).acked_through == 2
            env.clock.now += 0.25 * MIN_INTERVAL_S
            await enqueue_many(env, "metrics", [METRICS])
            assert (await pusher.push_once()).acked_through == 3
            assert env.clock.sleeps == [pytest.approx(0.75 * MIN_INTERVAL_S)]
            assert [len(p["items"]) for p in posts(env)] == [1, 1, 1]

    run(body)


def test_items_written_during_a_push_go_together_with_the_next(harness: Harness) -> None:
    """Items committed while a push is on the way are collected and leave together one interval after its start."""

    async def body() -> None:
        async with harness.env() as env:
            pusher = env.sync.pusher
            request = pusher.client.request
            await enqueue_many(env, "metrics", [METRICS])

            async def writing(method: str, target: str, body: object | None = None) -> dict[str, object]:
                if target == "/v1/device/outbox" and len(posts(env)) == 0:
                    await enqueue_many(env, "metrics", [METRICS] * 3)
                return await request(method, target, body)

            pusher.client.request = writing  # type: ignore[method-assign]
            assert (await pusher.drain()) == PushOutcome(waiting="idle")
            assert [len(p["items"]) for p in posts(env)] == [1, 3]
            assert env.clock.sleeps == pytest.approx([MIN_INTERVAL_S])
            assert sorted(env.hub.stored) == [1, 2, 3, 4]

    run(body)


def test_never_two_pushes_at_once(harness: Harness) -> None:
    """The push loop, a reconnect drain and the shutdown flush may ask at the same time; one push is on the way."""

    async def body() -> None:
        async with harness.env() as env:
            pusher = env.sync.pusher
            request = pusher.client.request
            flight = {"now": 0, "most": 0}

            async def tracked(method: str, target: str, body: object | None = None) -> dict[str, object]:
                if target != "/v1/device/outbox":
                    return await request(method, target, body)
                flight["now"] += 1
                flight["most"] = max(flight["most"], flight["now"])
                await asyncio.sleep(0.01)
                try:
                    return await request(method, target, body)
                finally:
                    flight["now"] -= 1

            pusher.client.request = tracked  # type: ignore[method-assign]
            await enqueue_many(env, "metrics", [METRICS] * 1201)
            await asyncio.gather(pusher.drain(), pusher.drain(), pusher.drain())
            assert flight["most"] == 1
            assert sorted(env.hub.stored) == list(range(1, 1202))
            assert sum(len(p["items"]) for p in posts(env)) == 1201

    run(body)


def test_a_restart_within_the_interval_loses_and_doubles_nothing(harness: Harness) -> None:
    """A runner that restarts right after a push pushes at once (no interval is remembered); the hub's pacer takes
    it from its reserve. Items whose answer was lost with the old process are resent and answered `replayed`."""

    async def body() -> None:
        async with harness.env() as env:
            await enqueue_many(env, "metrics", [METRICS] * 3)
            old = env.sync.pusher
            await old.push_once()
            async with env.pool.connection() as conn:
                await outbox.rewind(conn, 2)  # the answer of the last push was lost: only item 1 is known acked
            await enqueue_many(env, "metrics", [METRICS] * 2)
            request = old.client.request
            answers: list[dict[str, Any]] = []

            async def recorded(method: str, target: str, body: object | None = None) -> dict[str, object]:
                answer = await request(method, target, body)
                answers.append(answer)
                return answer

            old.client.request = recorded  # type: ignore[method-assign]
            restarted = Pusher(
                pool=env.pool,
                client=old.client,
                storage_key=old.storage_key,
                sealer=old.sealer,
                log=old.log,
                clock=env.clock,
                sleep=env.clock.sleep,
                hedge_min=None,
            )
            outcome = await restarted.push_once()
            assert outcome.acked_through == 5
            assert env.clock.sleeps == []
            last = posts(env)[-1]["items"]
            assert [item["origin_seq"] for item in last] == [2, 3, 4, 5]
            assert sorted(env.hub.stored) == [1, 2, 3, 4, 5]
            assert answers[-1]["results"][:2] == [
                {"origin_seq": 2, "status": "replayed"},
                {"origin_seq": 3, "status": "replayed"},
            ]

    run(body)


def test_items_written_during_the_wait_go_with_the_batch(harness: Harness) -> None:
    """PERF-03 (SCN-08): the batch is taken after the wait for the interval, so an item committed while the pusher
    waits goes out with it instead of waiting for one more interval (outbox write -> hub at most one interval)."""

    async def body() -> None:
        async with harness.env() as env:
            await enqueue_many(env, "metrics", [METRICS])
            pusher = env.sync.pusher
            await pusher.push_once()
            await enqueue_many(env, "metrics", [METRICS])
            wait = pusher.sleep

            async def written_meanwhile(seconds: float) -> None:
                await enqueue_many(env, "metrics", [METRICS])
                await wait(seconds)

            pusher.sleep = written_meanwhile
            outcome = await pusher.push_once()
            assert [len(p["items"]) for p in posts(env)] == [1, 2]
            assert outcome.acked_through == 3
            assert env.clock.sleeps == pytest.approx([MIN_INTERVAL_S])

    run(body)


def test_an_event_read_again_after_the_wait_is_sealed_once(harness: Harness) -> None:
    """The batch read again after the wait sends the bytes the first read sealed and committed (seal once)."""

    async def body() -> None:
        async with harness.env() as env:
            scope = Scope()
            await scope.install(env)
            await adopt(env)
            await enqueue_many(env, "metrics", [METRICS])
            pusher = env.sync.pusher
            await pusher.push_once()
            await env.enqueue("event", scope.draft())
            seals: list[int] = []
            seal = pusher.sealer.seal

            async def counted(*args: Any, **kwargs: Any) -> Any:
                seals.append(1)
                return await seal(*args, **kwargs)

            pusher.sealer.seal = counted  # type: ignore[method-assign]
            outcome = await pusher.push_once()
            assert env.clock.sleeps == pytest.approx([MIN_INTERVAL_S])
            assert outcome.acked_through == 2
            assert len(seals) == 1
            async with env.pool.connection() as conn:
                stored = (await outbox.load_batch(conn, env.sync.storage_key, 1, 1))[0].sealed
            assert stored is not None
            assert posts(env)[1]["items"][0]["body"] == {"event": stored}

    run(body)


def test_batches_stay_within_4_mib(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            scope = Scope()
            await scope.install(env)
            await adopt(env)
            await enqueue_many(env, "event", [scope.draft("x" * 65000) for _ in range(60)])
            await env.sync.pusher.drain()
            sizes = [len(encode_body(p)) for p in posts(env)]
            counts = [len(p["items"]) for p in posts(env)]
            assert sum(counts) == 60
            assert len(counts) == 2
            assert all(size <= MAX_BATCH_BYTES for size in sizes)
            # the first batch is full: one more item would have crossed the limit
            one_item = len(encode_body(posts(env)[1]["items"][0])) + 1
            assert sizes[0] + one_item > MAX_BATCH_BYTES
            assert sorted(env.hub.stored) == list(range(1, 61))

    run(body)


def test_byte_limit_with_small_settings(harness: Harness) -> None:
    async def body() -> None:
        settings = {"max_bytes": 900, "max_items": 3, "hedge_min": None}
        async with harness.env(options={"pusher_settings": settings}) as env:
            await adopt(env)
            await enqueue_many(env, "metrics", [METRICS] * 7)
            await env.sync.pusher.drain()
            for post in posts(env):
                assert len(encode_body(post)) <= 900 or len(post["items"]) == 1
            assert sum(len(p["items"]) for p in posts(env)) == 7

    run(body)


def test_an_event_is_sealed_once_and_the_same_bytes_are_resent(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            scope = Scope()
            await scope.install(env)
            await adopt(env)
            item = scope.draft()
            await env.enqueue("event", item)
            env.hub.tamper.append("unsigned")  # the hub stores the item, the answer does not verify
            with pytest.raises(HubTransportError, match="response_unsigned"):
                await env.sync.pusher.push_once()
            async with env.pool.connection() as conn:
                first = (await outbox.load_batch(conn, env.sync.storage_key, 0, 1))[0].sealed
                assert await outbox.pushed_origin_seq(conn) == 0
            assert first is not None
            outcome = await env.sync.pusher.push_once()
            assert outcome.acked_through == 1
            sent = posts(env)
            raw = [r.raw for r in env.hub.requests if r.target == "/v1/device/outbox"]
            assert raw[0] == raw[1]  # byte for byte, although the second attempt read the event back from jsonb
            assert sent[1]["items"][0]["body"] == {"event": first}
            assert env.hub.items()[0]["body"]["event"] == first
            header = first["header"]
            assert (header["epoch"], header["lh"], header["author"]) == (1, 5, env.hub.device_id)
            identity = env.context.identity
            assert identity is not None
            public = identity.keys.signing_key().public_key().public_bytes_raw()
            plain = json.loads(open_event(scope.keys[1], first, public))
            assert plain == item["body"]
            assert {k: header[k] for k in item["header"]} == item["header"]

    run(body)


def test_reseal_after_epoch_stale(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            scope = Scope()
            await scope.install(env)
            await adopt(env)
            await env.enqueue("metrics", METRICS)
            await env.enqueue("event", scope.draft())
            await env.enqueue("metrics", METRICS)
            env.hub.epochs[scope.wg] = 2  # rotated while the item was queued
            outcome = await env.sync.pusher.push_once()
            assert outcome.acked_through == 1
            assert env.sync.pusher.reseal == {2: 1}
            assert "runner.sync reseal origin_seq=2 epoch=1\n" in env.log()
            # no key of a newer epoch yet: the event waits in its scope's queue (KOR-08), the item behind it goes on
            outcome = await env.sync.pusher.push_once()
            assert (outcome.sent, outcome.acked_through) == (1, 2)
            assert [item["item_kind"] for item in env.hub.items()] == ["metrics", "metrics"]
            assert env.sync.pusher.reseal == {}
            await scope.install(env, epoch=2, head=None)
            await env.sync.pusher.drain()
            assert sorted(env.hub.stored) == [1, 2, 3]
            stored = env.hub.items()[2]["body"]["event"]
            assert stored["header"]["epoch"] == 2
            async with env.pool.connection() as conn:
                sealed = (await outbox.load_batch(conn, env.sync.storage_key, 2, 1))[0].sealed
            assert sealed == stored
            assert env.sync.pusher.reseal == {}

    run(body)


def test_sealing_waits_for_scope_key_and_log_head(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            scope = Scope()
            await adopt(env)
            await env.enqueue("metrics", METRICS)
            await env.enqueue("event", scope.draft())
            outcome = await env.sync.pusher.push_once()
            assert (outcome.sent, outcome.acked_through, outcome.more) == (1, 1, True)
            # the event waits in its scope's queue (KOR-08) until both its key and the org's log head are there
            assert await env.sync.pusher.push_once() == PushOutcome(waiting="idle")
            await scope.install(env, head=None)
            assert await env.sync.pusher.push_once() == PushOutcome(waiting="idle")
            assert env.log().count(f"runner.sync scope_held scope=wg scope_id={scope.wg} items=1\n") == 1
            await env.set_log_head(scope.org, 0)
            assert (await env.sync.pusher.push_once()).acked_through == 2

    run(body)


def test_gap_resends_from_expected(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            await adopt(env)
            await enqueue_many(env, "metrics", [METRICS] * 5)
            await env.sync.pusher.drain()
            for seq in (3, 4, 5):
                del env.hub.stored[seq]  # the hub lost the tail (restored from an older backup)
            await env.enqueue("metrics", METRICS)
            outcome = await env.sync.pusher.push_once()
            assert outcome == PushOutcome(more=True)
            assert "runner.sync gap pushed=5 expected=3\n" in env.log()
            await env.sync.pusher.drain()
            assert [i["origin_seq"] for i in posts(env)[-1]["items"]] == [3, 4, 5, 6]
            assert sorted(env.hub.stored) == [1, 2, 3, 4, 5, 6]

    run(body)


def test_a_hub_restored_below_the_pruned_items_takes_the_rest_and_reports_the_loss(harness: Harness) -> None:
    """KOR-10 (AUD-02 NA-02): acknowledged items older than an hour are pruned (KOR-03); a hub restored from an older
    export asks for one of them again. The runner cannot resend it, so it continues at the hub's position: the items
    it still holds follow `expected` (with their sealed bytes), the pruned ones are reported lost by one log line and
    one audit `runner.alert` `{alert: "outbox_items_lost", value: n}`, and pushing goes on (no `sync_fatal`)."""

    async def body() -> None:
        async with harness.env() as env:
            scope = Scope()
            await scope.install(env)
            await adopt(env)
            await enqueue_many(env, "metrics", [METRICS] * 3)
            await env.enqueue("event", scope.draft())
            await env.sync.pusher.drain()
            sealed = env.hub.stored[4]
            async with env.pool.connection() as conn, conn.transaction():
                await conn.execute("UPDATE runner.outbox SET created_at = now() - interval '2 hours'")
                assert await outbox.prune(conn) == 3
            await env.enqueue("metrics", METRICS)
            for seq in (2, 3, 4):
                del env.hub.stored[seq]  # restored from an export that ends at origin_seq 1
            outcome = await env.sync.pusher.push_once()
            assert outcome == PushOutcome(more=True)
            assert "runner.alert outbox_items_lost expected=2 lost=2\n" in env.log()
            await env.sync.pusher.drain()
            assert sorted(env.hub.stored) == [1, 2, 3, 4]
            items = env.hub.items()
            assert json.loads(sealed)["body"] == items[1]["body"]  # the event, same sealed bytes, new number
            assert [i["item_kind"] for i in items] == ["metrics", "event", "metrics", "audit"]
            assert items[3]["body"] == {
                "action": "runner.alert",
                "result": "failed",
                "target_kind": "outbox",
                "target_id": "origin_seq_gap",
                "detail": {"alert": "outbox_items_lost", "value": 2},
            }
            async with env.pool.connection() as conn:
                assert await outbox.sync_fatal(conn) is None
                assert await outbox.pushed_origin_seq(conn) == 4
            assert env.sync.pusher.fatal is None

    run(body)


async def pushed_and_restored(env: Env, count: int, keep: int) -> None:
    """`count` metrics pushed, the hub restored to its first `keep` items (its later ones are gone), one more item
    due."""
    await adopt(env)
    await enqueue_many(env, "metrics", [METRICS] * count)
    await env.sync.pusher.drain()
    for seq in range(keep + 1, count + 1):
        del env.hub.stored[seq]
    await env.enqueue("metrics", METRICS)


def test_a_hole_in_the_stored_items_is_closed_by_the_realignment(harness: Harness) -> None:
    """A hub that asks for a number the outbox no longer holds although later ones are stored (a hole left by an
    earlier prune of an older version, which deleted by age only) gets the later items renumbered without a gap;
    a rewind to the hole would ask for it again for ever."""

    async def body() -> None:
        async with harness.env() as env:
            await pushed_and_restored(env, 6, keep=2)
            async with env.pool.connection() as conn:
                await conn.execute("DELETE FROM runner.outbox WHERE origin_seq IN (1, 3)")
            assert await env.sync.pusher.push_once() == PushOutcome(more=True)
            assert "runner.alert outbox_items_lost expected=3 lost=1\n" in env.log()
            await env.sync.pusher.drain()
            assert sorted(env.hub.stored) == [1, 2, 3, 4, 5, 6, 7]  # old 4 to 7 and the audit
            assert env.hub.items()[-1]["body"]["detail"] == {"alert": "outbox_items_lost", "value": 1}
            async with env.pool.connection() as conn:
                assert await outbox.pushed_origin_seq(conn) == 7

    run(body)


def test_a_gap_the_hub_position_does_not_confirm_is_retried_without_giving_items_up(harness: Harness) -> None:
    """One `origin_seq_gap` is not enough to give items up: `GET /v1/device/state` must name `expected - 1`."""

    async def body() -> None:
        async with harness.env() as env:
            await pushed_and_restored(env, 4, keep=4)
            async with env.pool.connection() as conn, conn.transaction():
                await conn.execute("UPDATE runner.outbox SET created_at = now() - interval '2 hours'")
                assert await outbox.prune(conn) == 4
            env.hub.scripted.append((409, {"error": "origin_seq_gap", "detail": {"expected": 2}}, {}))
            with pytest.raises(HubTransportError, match="position_unconfirmed"):
                await env.sync.pusher.push_once()
            assert "runner.sync gap_unconfirmed expected=2\n" in env.log()
            async with env.pool.connection() as conn:
                assert await outbox.first_origin_seq(conn) == 5
                assert await outbox.pushed_origin_seq(conn) == 4
            env.hub.device_state = {**env.hub.active_state(), "last_origin_seq": 3}
            env.hub.scripted.append((409, {"error": "origin_seq_gap", "detail": {"expected": 2}}, {}))
            with pytest.raises(HubTransportError, match="position_unconfirmed"):
                await env.sync.pusher.push_once()
            env.hub.device_state = None
            await env.sync.pusher.drain()  # the hub was not restored: the next push goes on
            assert sorted(env.hub.stored) == [1, 2, 3, 4, 5]

    run(body)


def test_a_realignment_forgets_the_pending_reseals_of_the_old_numbers(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            await pushed_and_restored(env, 4, keep=1)
            async with env.pool.connection() as conn, conn.transaction():
                await conn.execute("UPDATE runner.outbox SET created_at = now() - interval '2 hours'")
                assert await outbox.prune(conn) == 4
            pusher = env.sync.pusher
            pusher.reseal[2] = 7
            assert await pusher.push_once() == PushOutcome(more=True)
            assert pusher.reseal == {}

    run(body)


def test_a_gap_settled_meanwhile_is_not_realigned(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    """`realign` looks again under the outbox lock; when nothing is left to realign it changes nothing."""

    async def nothing(conn: Any, storage_key: bytes, expected: int) -> None:
        return None

    async def body() -> None:
        async with harness.env() as env:
            await pushed_and_restored(env, 4, keep=1)
            async with env.pool.connection() as conn:
                await conn.execute("DELETE FROM runner.outbox WHERE origin_seq = 2")
            monkeypatch.setattr(outbox, "realign", nothing)
            assert await env.sync.pusher.push_once() == PushOutcome(more=True)
            assert "outbox_items_lost" not in env.log()

    run(body)


def test_a_corrupt_row_met_by_the_realignment_is_fatal(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            await pushed_and_restored(env, 4, keep=1)
            async with env.pool.connection() as conn:
                await conn.execute("DELETE FROM runner.outbox WHERE origin_seq < 3")
                await conn.execute("UPDATE runner.outbox SET body_ct = 'x'::bytea WHERE origin_seq = 3")
            with pytest.raises(SyncFatal, match="outbox_corrupt"):
                await env.sync.pusher.push_once()
            async with env.pool.connection() as conn:
                assert await outbox.first_origin_seq(conn) == 3  # rolled back, nothing moved
                assert await outbox.pushed_origin_seq(conn) == 4

    run(body)


@pytest.mark.parametrize("detail", [{}, {"expected": 99}, {"expected": 0}, {"expected": "3"}])
def test_gap_with_an_impossible_expectation_is_fatal(harness: Harness, detail: dict[str, Any]) -> None:
    async def body() -> None:
        async with harness.env() as env:
            await adopt(env)
            await env.enqueue("metrics", METRICS)
            env.hub.scripted.append((409, {"error": "origin_seq_gap", "detail": detail}, {}))
            with pytest.raises(SyncFatal, match="origin_seq_gap"):
                await env.sync.pusher.push_once()
            async with env.pool.connection() as conn:
                assert await outbox.sync_fatal(conn) == "origin_seq_gap"

    run(body)


def test_conflict_stops_syncing_until_cleared(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            await adopt(env)
            env.hub.stored[1] = canonical({"origin_seq": 1, "item_kind": "metrics", "body": {"other": 1}})
            await env.enqueue("metrics", METRICS)
            with pytest.raises(SyncFatal, match="origin_seq_conflict"):
                await env.sync.pusher.push_once()
            assert env.sync.pusher.fatal == "origin_seq_conflict"
            assert "runner.alert origin_seq_conflict\n" in env.log()
            requests = len(env.hub.requests)
            await env.sync.pusher.run(env.context.stopping)  # returns at once, sends nothing
            assert len(env.hub.requests) == requests
            assert "runner.sync stopped code=origin_seq_conflict\n" in env.log()
            async with env.pool.connection() as conn:
                assert await outbox.sync_fatal(conn) == "origin_seq_conflict"
                await outbox.clear_fatal(conn)
            del env.hub.stored[1]
            assert (await env.sync.pusher.push_once()).acked_through == 1

    run(body)


def test_rejected_items_are_logged_and_skipped(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            await adopt(env)
            env.hub.reject = lambda item: "kind_not_allowed" if item["item_kind"] == "audit" else None
            await env.enqueue("metrics", METRICS)
            await env.enqueue("audit", fixture_body("audit"))
            await env.enqueue("metrics", METRICS)
            outcome = await env.sync.pusher.push_once()
            assert outcome.acked_through == 3
            assert "runner.sync item_rejected origin_seq=2 item_kind=audit error=kind_not_allowed\n" in env.log()

    run(body)


@pytest.mark.parametrize(
    "answer",
    [
        {"acked_through": 9, "results": []},
        {"acked_through": 0, "results": []},
        {"results": []},
        {"acked_through": 1, "results": {}},
        {"acked_through": 1, "results": ["x"]},
        {"acked_through": 1, "results": [{"origin_seq": 7, "status": "ok"}]},
        {"acked_through": True, "results": []},
    ],
)
def test_answers_outside_the_batch_are_transport_errors(harness: Harness, answer: dict[str, Any]) -> None:
    async def body() -> None:
        async with harness.env() as env:
            await adopt(env, 1)
            env.hub.stored[1] = b"{}"
            await env.enqueue("metrics", METRICS)
            await env.enqueue("metrics", METRICS)
            env.hub.scripted.append((200, answer, {}))
            with pytest.raises(HubTransportError, match="response_invalid"):
                await env.sync.pusher.push_once()
            async with env.pool.connection() as conn:
                assert await outbox.pushed_origin_seq(conn) == 1

    run(body)


def test_a_reseal_answer_for_a_non_event_is_ignored(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            await adopt(env)
            await env.enqueue("metrics", METRICS)
            env.hub.scripted.append((200, {"acked_through": 0, "results": [{"origin_seq": 1, "status": "reseal"}]}, {}))
            assert (await env.sync.pusher.push_once()).acked_through == 0
            assert env.sync.pusher.reseal == {}

    run(body)


def test_continuation_after_a_store_wipe(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            for seq in range(1, 8):  # pushed before the wipe
                env.hub.stored[seq] = canonical({"origin_seq": seq})
            await env.enqueue("metrics", METRICS)
            await env.enqueue("quota", fixture_body("quota"))
            await env.sync.pusher.drain()
            assert [i["origin_seq"] for i in posts(env)[0]["items"]] == [8, 9]
            assert sorted(env.hub.stored) == list(range(1, 10))
            assert "runner.sync position_adopted last_origin_seq=7\n" in env.log()
            assert await env.enqueue("metrics", METRICS) == 10

    run(body)


def test_continuation_after_a_store_wipe_adopted_before_any_item(harness: Harness) -> None:
    """DR-01 (E2E-33): the reconcile step adopts the hub's position while the wiped outbox is still empty; the items
    written afterwards continue at `last_origin_seq + 1` and the hub stores them without a gap or a conflict."""

    async def body() -> None:
        async with harness.env() as env:
            for seq in range(1, 8):  # pushed before the wipe
                env.hub.stored[seq] = canonical({"origin_seq": seq})
            await adopt(env, 7)  # reconcile: GET /v1/device/state before the first item
            assert await env.enqueue("metrics", METRICS) == 8
            assert await env.enqueue("quota", fixture_body("quota")) == 9
            assert await env.sync.pusher.drain() == PushOutcome(waiting="idle")
            assert [i["origin_seq"] for i in posts(env)[0]["items"]] == [8, 9]
            assert sorted(env.hub.stored) == list(range(1, 10))
            async with env.pool.connection() as conn:
                assert await outbox.pushed_origin_seq(conn) == 9
                assert await outbox.sync_fatal(conn) is None

    run(body)


def test_pending_device_sends_nothing(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            pending = CONTRACTS / "fixtures/api/device-state.response/valid/pending.json"
            env.hub.device_state = json.loads(pending.read_text())
            await env.enqueue("metrics", METRICS)
            assert await env.sync.pusher.push_once() == PushOutcome(waiting="position_unknown")
            assert posts(env) == []
            env.hub.scripted.append((200, {"state": "active", "last_origin_seq": -1}, {}))
            assert await env.sync.pusher.push_once() == PushOutcome(waiting="position_unknown")

    run(body)


def test_run_retries_with_backoff_and_retry_after(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            await adopt(env)
            await env.enqueue("metrics", METRICS)
            env.hub.tamper.extend(["none", "none", "unsigned", "unsigned"])
            env.hub.scripted.append((429, {"error": "rate_limited"}, {"Retry-After": "7"}))
            env.hub.scripted.append((403, {"error": "forbidden"}, {}))

            def stop_when_pushed(seconds: float) -> None:
                if seconds == IDLE_POLL_S:  # idle again after the success
                    env.context.stopping.set()

            env.clock.on_sleep = stop_when_pushed
            await env.sync.pusher.run(env.context.stopping)
            assert sorted(env.hub.stored) == [1]
            # 429 waits Retry-After, the other failures back off 1 s, 2 s, 4 s (jitter factor 1.0 here)
            assert env.clock.sleeps == [7.0, 1.0, 2.0, 4.0, IDLE_POLL_S]
            async with env.pool.connection() as conn:
                assert await outbox.pushed_origin_seq(conn) == 1
            log = env.log()
            assert "push_refused status=429 error=rate_limited" in log
            assert "push_refused status=403 error=forbidden" in log
            assert log.count("push_failed reason=response_unsigned") == 2

    run(body)


def test_new_items_do_not_cut_retry_after_or_the_backoff_short(harness: Harness) -> None:
    """KOR-13: every outbox write notifies the loop; a refused or failed push still waits its whole `Retry-After` or
    backoff, so a busy device does not retry every interval while the hub refuses or is away."""

    async def body() -> None:
        async with harness.env() as env:
            await adopt(env)
            await env.enqueue("metrics", METRICS)
            pusher = env.sync.pusher
            env.hub.scripted.append((429, {"error": "rate_limited"}, {"Retry-After": "7"}))
            env.hub.tamper.extend(["none", "unsigned"])
            waited: list[float] = []
            sleep = pusher.sleep

            async def busy(seconds: float) -> None:
                if seconds == IDLE_POLL_S:
                    env.context.stopping.set()
                    return await sleep(seconds)
                pusher.notify()  # an item is written while the push waits
                await asyncio.sleep(0.02)
                await sleep(seconds)
                waited.append(seconds)

            request = pusher.client.request
            attempts: list[list[float]] = []

            async def counted(method: str, target: str, body: object | None = None) -> dict[str, object]:
                if target == "/v1/device/outbox":
                    attempts.append(list(waited))
                return await request(method, target, body)

            pusher.sleep = busy
            pusher.client.request = counted  # type: ignore[method-assign]
            await pusher.run(env.context.stopping)
            assert attempts == [[], [7.0], [7.0, 1.0]]
            assert sorted(env.hub.stored) == [1]

    run(body)


def test_a_batch_that_empties_during_the_wait_sends_nothing(harness: Harness) -> None:
    """The batch read again after the wait may be empty (its items moved to a scope queue meanwhile): no empty push."""

    async def body() -> None:
        async with harness.env() as env:
            await enqueue_many(env, "metrics", [METRICS])
            pusher = env.sync.pusher
            await pusher.push_once()
            await enqueue_many(env, "metrics", [METRICS])
            prepare = pusher._prepare
            calls: list[int] = []

            async def emptied() -> tuple[int, Any]:
                calls.append(1)
                pushed, batch = await prepare()
                if len(calls) == 2:
                    batch.wire, batch.waiting = [], "idle"
                return pushed, batch

            pusher._prepare = emptied  # type: ignore[method-assign]
            assert await pusher.push_once() == PushOutcome(waiting="idle")
            assert len(calls) == 2
            assert len(posts(env)) == 1

    run(body)


def test_run_polls_while_the_position_is_unknown_and_wakes_on_notify(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            pending = CONTRACTS / "fixtures/api/device-state.response/valid/pending.json"
            env.hub.device_state = json.loads(pending.read_text())
            env.clock.on_sleep = lambda _s: env.context.stopping.set()
            await env.sync.pusher.run(env.context.stopping)
            assert env.clock.sleeps == [IDLE_POLL_S]
            # a notification ends the wait early
            env.context.stopping.clear()
            env.sync.pusher.notify()
            await env.sync.pusher._wait(None)
            # the loop clears the flag before its next attempt, not the wait
            assert env.sync.pusher.wake.is_set()
            assert env.clock.sleeps == [IDLE_POLL_S, IDLE_POLL_S]

    run(body)


def test_refusal_from_the_hub_propagates_from_push_once(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            await adopt(env)
            await env.enqueue("metrics", METRICS)
            env.hub.scripted.append((413, {"error": "size_exceeded"}, {}))
            with pytest.raises(HubRefusal, match="size_exceeded"):
                await env.sync.pusher.push_once()

    run(body)


def test_a_position_adopted_meanwhile_is_kept(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            await adopt(env, 3)
            async with env.pool.connection() as conn:
                assert await env.sync.pusher._fetch_position(conn) is True
                assert await outbox.pushed_origin_seq(conn) == 3
            assert "position_adopted" not in env.log()

    run(body)


def test_a_corrupt_outbox_row_is_fatal(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            await adopt(env)
            await env.enqueue("metrics", METRICS)
            async with env.pool.connection() as conn:
                await conn.execute("UPDATE runner.outbox SET body_ct = 'x'::bytea")
            with pytest.raises(SyncFatal, match="outbox_corrupt"):
                await env.sync.pusher.push_once()
            assert "runner.alert outbox_corrupt\n" in env.log()

    run(body)


def test_repeating_batch_refusals_raise_an_alert(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            await adopt(env)
            await env.enqueue("metrics", METRICS)
            env.hub.scripted.append((400, {"error": "request_invalid"}, {}))
            env.clock.on_sleep = lambda seconds: env.context.stopping.set() if seconds == IDLE_POLL_S else None
            await env.sync.pusher.run(env.context.stopping)
            assert "runner.alert push_refused_400\n" in env.log()
            assert sorted(env.hub.stored) == [1]

    run(body)


@pytest.mark.parametrize(
    "result",
    [
        {"origin_seq": 1, "status": "accepted"},
        {"origin_seq": 1, "status": "reseal"},  # reseal must be the first item after acked_through
    ],
)
def test_unknown_statuses_and_misplaced_reseals_are_transport_errors(harness: Harness, result: dict[str, Any]) -> None:
    async def body() -> None:
        async with harness.env() as env:
            await adopt(env)
            await env.enqueue("metrics", METRICS)
            env.hub.scripted.append((200, {"acked_through": 1, "results": [result]}, {}))
            with pytest.raises(HubTransportError, match="response_invalid"):
                await env.sync.pusher.push_once()

    run(body)


def test_a_rejection_code_outside_the_list_is_not_logged_verbatim(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            await adopt(env)
            await env.enqueue("metrics", METRICS)
            forged = "x\nrunner.alert origin_seq_conflict"
            answer = {"acked_through": 1, "results": [{"origin_seq": 1, "status": "rejected", "error": forged}]}
            env.hub.scripted.append((200, answer, {}))
            await env.sync.pusher.push_once()
            assert "item_rejected origin_seq=1 item_kind=metrics error=invalid\n" in env.log()
            assert "runner.alert" not in env.log()

    run(body)


@pytest.mark.parametrize("mode", ["unsigned", "other_key", "stale_time", "body_changed"])
def test_a_forged_conflict_answer_is_not_acted_upon(harness: Harness, mode: str) -> None:
    async def body() -> None:
        async with harness.env() as env:
            await adopt(env)
            await env.enqueue("metrics", METRICS)
            env.hub.scripted.append((409, {"error": "origin_seq_conflict"}, {}))
            env.hub.tamper.append(mode)
            with pytest.raises(HubTransportError):
                await env.sync.pusher.push_once()
            assert env.sync.pusher.fatal is None
            async with env.pool.connection() as conn:
                assert await outbox.sync_fatal(conn) is None
                assert await outbox.pushed_origin_seq(conn) == 0
            assert (await env.sync.pusher.push_once()).acked_through == 1

    run(body)


def test_an_active_state_without_a_position_is_logged(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            env.hub.scripted.append((200, {"state": "active"}, {}))
            assert await env.sync.pusher.push_once() == PushOutcome(waiting="position_unknown")
            assert "runner.sync position_unavailable state=active\n" in env.log()

    run(body)
