"""U-RUN-outbox (HOT-25): an `origin_seq_conflict` after a power cut repairs itself, real conflicts stay fatal.

A power cut can cost the runner store its newest commits (macOS `fsync` without `F_FULLFSYNC`, `store.cluster`): the
hub already holds items the store no longer has, and the store numbers new items the same. The first push after the
restart meets `409 origin_seq_conflict`. When the hub's position confirms a short lost tail, the runner takes the
hub's position and pushes on (`outbox.adopt_after_loss`); two devices with one identity and a store from an old backup
stay fatal (05-runner.md section 2.3).
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_runner.sync import outbox
from agent_runner.sync.http import HubTransportError
from agent_runner.sync.push import LOSS_LIMIT, Pusher, PushOutcome, SyncFatal

from .conftest import Env, Harness, run
from .fakehub import canonical
from .test_outbox import fixture_body
from .test_push import METRICS, Scope, adopt, enqueue_many

AUDIT = fixture_body("audit")


def restart(env: Env, max_items: int = 500) -> Pusher:
    """A new push loop on the same store, hub and keys: the daemon after the restart."""
    old = env.sync.pusher
    fresh = Pusher(
        pool=old.pool,
        client=old.client,
        storage_key=old.storage_key,
        sealer=old.sealer,
        log=old.log,
        clock=old.clock,
        sleep=old.sleep,
        hedge_min=None,
        max_items=max_items,
    )
    env.sync.pusher = fresh
    return fresh


async def power_cut(env: Env, keep: int) -> None:
    """The store loses every item after `keep` and the acknowledgement of them; the hub keeps them."""
    async with env.pool.connection() as conn, conn.transaction():
        await conn.execute("DELETE FROM runner.outbox WHERE origin_seq > %s", (keep,))
        await outbox.set_state(conn, outbox.PUSHED, str(keep))


async def state(env: Env) -> tuple[int | None, str | None]:
    async with env.pool.connection() as conn:
        return await outbox.pushed_origin_seq(conn), await outbox.sync_fatal(conn)


def test_a_store_that_lost_its_last_items_takes_the_hub_position_and_pushes_on(harness: Harness) -> None:
    """The unclean shutdown: items 3 and 4 reached the hub, the store lost them with their acknowledgement and gave
    their numbers to new items. After the restart the runner moves its new items behind the hub's position, reports
    the two lost items (log and audit) and pushes on - no `sync_fatal`, nothing of the hub overwritten."""

    async def body() -> None:
        async with harness.env() as env:
            scope = Scope()
            await scope.install(env)
            await adopt(env)
            await enqueue_many(env, "metrics", [METRICS] * 3)
            await env.enqueue("event", scope.draft("vor dem Ausfall"))
            await env.sync.pusher.drain()
            before = dict(env.hub.stored)
            assert sorted(before) == [1, 2, 3, 4]
            await power_cut(env, keep=2)
            pusher = restart(env)
            await env.enqueue("audit", AUDIT)
            await env.enqueue("event", scope.draft("nach dem Ausfall"))
            await env.enqueue("metrics", METRICS)
            assert await pusher.push_once() == PushOutcome(more=True)
            assert "runner.alert outbox_conflict_recovered pushed=2 hub=4 lost=2\n" in env.log()
            assert await state(env) == (4, None)
            await pusher.drain()
            assert {seq: env.hub.stored[seq] for seq in before} == before  # the hub's items stay
            items = env.hub.items()
            assert [i["item_kind"] for i in items[4:]] == ["audit", "event", "metrics", "audit"]
            assert items[4]["body"] == AUDIT
            assert items[-1]["body"] == {
                "action": "runner.alert",
                "result": "failed",
                "target_kind": "outbox",
                "target_id": "origin_seq_conflict",
                "detail": {"alert": "outbox_conflict_recovered", "value": 2},
            }
            assert await state(env) == (8, None)
            assert pusher.fatal is None
            # a second conflict in the same process is real
            env.hub.stored[9] = canonical({"origin_seq": 9, "item_kind": "metrics", "body": {"other": 1}})
            await env.enqueue("audit", AUDIT)
            with pytest.raises(SyncFatal, match="origin_seq_conflict"):
                await pusher.push_once()
            assert "runner.sync conflict_fatal reason=after_answer\n" in env.log()

    run(body)


def test_the_repair_forgets_the_pending_reseals_of_the_old_numbers(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            await adopt(env)
            await enqueue_many(env, "metrics", [METRICS] * 3)
            await env.sync.pusher.drain()
            await power_cut(env, keep=1)
            pusher = restart(env)
            await env.enqueue("audit", AUDIT)
            pusher.reseal[2] = 4
            assert await pusher.push_once() == PushOutcome(more=True)
            assert pusher.reseal == {}

    run(body)


async def lost_tail(env: Env, max_items: int = 500) -> Pusher:
    """Items 1 and 2 pushed, the store lost item 2, a new item 2 waits, the daemon restarted."""
    await adopt(env)
    await enqueue_many(env, "metrics", [METRICS] * 2)
    await env.sync.pusher.drain()
    await power_cut(env, keep=1)
    pusher = restart(env, max_items)
    await env.enqueue("audit", AUDIT)
    return pusher


@pytest.mark.parametrize(
    ("hub_last", "reason"),
    [(1, "hub_behind"), (1 + LOSS_LIMIT + 1, "too_far")],
)
def test_a_conflict_the_hub_position_does_not_explain_is_fatal(harness: Harness, hub_last: int, reason: str) -> None:
    """A hub behind the disputed item cannot hold a lost item; a hub far ahead is a store from an old backup."""

    async def body() -> None:
        async with harness.env() as env:
            pusher = await lost_tail(env)
            env.hub.device_state = {**env.hub.active_state(), "last_origin_seq": hub_last}
            env.hub.scripted.append((409, {"error": "origin_seq_conflict"}, {}))
            with pytest.raises(SyncFatal, match="origin_seq_conflict"):
                await pusher.push_once()
            assert f"runner.sync conflict_fatal reason={reason}\n" in env.log()
            assert await state(env) == (1, "origin_seq_conflict")

    run(body)


def test_a_store_with_a_hole_after_its_position_is_fatal(harness: Harness) -> None:
    """The first item after `pushed_origin_seq` must be `pushed_origin_seq + 1`; anything else is not a lost tail."""

    async def body() -> None:
        async with harness.env() as env:
            pusher = await lost_tail(env)
            await env.enqueue("audit", AUDIT)
            async with env.pool.connection() as conn:
                await conn.execute("DELETE FROM runner.outbox WHERE origin_seq = 2")
            env.hub.scripted.append((409, {"error": "origin_seq_conflict"}, {}))
            asked = [r.target for r in env.hub.requests].count("/v1/device/state")
            with pytest.raises(SyncFatal, match="origin_seq_conflict"):
                await pusher.push_once()
            assert "runner.sync conflict_fatal reason=not_contiguous\n" in env.log()
            assert [r.target for r in env.hub.requests].count("/v1/device/state") == asked  # decided locally

    run(body)


def test_a_conflict_settled_meanwhile_is_fatal(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    """`adopt_after_loss` looks again under the outbox lock; when the store no longer looks like a lost tail there,
    the conflict is treated as real."""

    async def nothing(conn: Any, storage_key: bytes, hub_last: int) -> None:
        return None

    async def body() -> None:
        async with harness.env() as env:
            pusher = await lost_tail(env)
            monkeypatch.setattr(outbox, "adopt_after_loss", nothing)
            with pytest.raises(SyncFatal, match="origin_seq_conflict"):
                await pusher.push_once()
            assert "runner.sync conflict_fatal reason=not_contiguous\n" in env.log()

    run(body)


def test_an_unconfirmed_hub_position_is_retried_without_changing_anything(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            pusher = await lost_tail(env)
            env.hub.scripted.append((409, {"error": "origin_seq_conflict"}, {}))
            env.hub.scripted.append((200, {"state": "active"}, {}))  # the position is missing
            with pytest.raises(HubTransportError, match="position_unconfirmed"):
                await pusher.push_once()
            assert "runner.sync conflict_unconfirmed\n" in env.log()
            assert await state(env) == (1, None)
            assert await pusher.push_once() == PushOutcome(more=True)
            await pusher.drain()
            assert await state(env) == (4, None)  # the audit item, then the alert

    run(body)


def test_a_corrupt_row_met_by_the_repair_is_fatal(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            pusher = await lost_tail(env, max_items=1)  # batch: item 2 alone; item 3 is first opened by the repair
            await env.enqueue("audit", AUDIT)
            async with env.pool.connection() as conn:
                await conn.execute("UPDATE runner.outbox SET body_ct = 'x'::bytea WHERE origin_seq = 3")
            with pytest.raises(SyncFatal, match="outbox_corrupt"):
                await pusher.push_once()
            assert await state(env) == (1, "outbox_corrupt")
            async with env.pool.connection() as conn:
                assert await outbox.first_origin_seq(conn) == 1  # rolled back, nothing moved
                assert await outbox.last_origin_seq(conn) == 3

    run(body)


def test_adopt_after_loss_only_moves_a_lost_tail(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            key = env.sync.storage_key
            async with env.pool.connection() as conn:
                assert await outbox.adopt_after_loss(conn, key, 5) is None  # position unknown
            await adopt(env, 3)
            async with env.pool.connection() as conn:
                assert await outbox.adopt_after_loss(conn, key, 5) is None  # nothing after the position
                assert await outbox.adopt_after_loss(conn, key, 3) is None  # the hub is not ahead
            await env.enqueue("metrics", METRICS)
            async with env.pool.connection() as conn, conn.transaction():
                assert await outbox.adopt_after_loss(conn, key, 6) == 3
                assert await outbox.first_origin_seq(conn) == 7
                assert await outbox.last_origin_seq(conn) == 8  # the item and the alert
                assert await outbox.pushed_origin_seq(conn) == 6

    run(body)
