"""U-RUN-quota (KOR-50, ADR-080 sections 2, 3 and 6): the quotas of the operator's subscriptions - every token file
read every 900 s (a renewed file at once), a full window marks the file used up and a free one releases it, the pool
learns the room of each file - and the snapshot `operator_quota` sent on change, at most every minute, only to a hub
with `model_fallback.api`; never a token."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from agent_runner.credentials import cache
from agent_runner.model import limits, operator
from agent_runner.model.limits import Limit
from agent_runner.quota import operator as operator_quota
from agent_runner.quota import tasks
from agent_runner.quota.subscription import SubscriptionPoller
from agent_runner.sync.outbox import open_item

from ..model.fallback_kit import AT, HOUR, T0, FKit, fallback_kit, state, usage_document
from ..model.kit import ALICE, TOKEN, model_env
from ..model.test_operator import MARKER, pool_dir, put, token
from ..quota.double import USAGE_PATH, Answer

API = state()
Body = Callable[[FKit], Awaitable[None]]


def run(dsn: str, tmp_path: Path | None, body: Body, **options: Any) -> None:
    async def main() -> None:
        async with model_env(dsn) as env:
            directory = None if tmp_path is None else pool_dir(tmp_path)
            async with fallback_kit(env, directory, **options) as found:
                await body(found)

    asyncio.run(main())


async def items(found: FKit, kind: str) -> list[dict[str, Any]]:
    rows = await found.env.rows("SELECT origin_seq, item_kind, body_ct FROM runner.outbox ORDER BY origin_seq")
    key = found.env.sync.storage_key
    return [open_item(key, seq, item, bytes(blob)) for seq, item, blob in rows if item == kind]


def reads(found: FKit) -> int:
    return sum(1 for path, _headers in found.quotas.requests if path == USAGE_PATH)


def test_every_file_is_polled_and_marked_or_released(store_dsn: str, tmp_path: Path) -> None:
    async def body(found: FKit) -> None:
        directory = found.pool.directory
        put(directory, "bad", token(9), mode=0o644)  # ignored: never read
        found.quotas.documents[token(1)] = usage_document(five=100.0)
        found.quotas.documents[token(2)] = Answer(500, b"{}")
        poller = operator_quota.OperatorPoller(context=found.context)
        await poller.tick(T0)
        assert reads(found) == 2
        assert await found.limit(limits.operator_key("a")) == ("exhausted", AT + HOUR, True, None)
        assert found.pool.quota == {"a": 100.0}
        assert found.pool.state("a") == operator.EXHAUSTED
        await poller.tick(T0 + 60)  # not due
        assert reads(found) == 2
        put(directory, "a", token(1))  # renewed: read again at once
        found.quotas.documents[token(1)] = usage_document(five=20.0)
        await poller.tick(T0 + 120)
        assert reads(found) == 3
        assert await found.limit(limits.operator_key("a")) is None
        (directory / "b.token").unlink()
        await poller.tick(T0 + operator_quota.POLL_S)
        assert sorted(poller.next_at) == ["a"]

    run(store_dsn, tmp_path, body, own=False)


def test_without_a_pool_nothing_is_polled_or_reported(store_dsn: str) -> None:
    async def body(found: FKit) -> None:
        await operator_quota.OperatorPoller(context=found.context).tick(T0)
        assert reads(found) == 0
        assert operator_quota.snapshot(found.context) is None
        assert not await operator_quota.report(found.context, API, T0)

    run(store_dsn, None, body)


def test_the_snapshot_is_sent_on_change_at_most_every_minute(store_dsn: str, tmp_path: Path) -> None:
    async def body(found: FKit) -> None:
        context = found.context
        found.pool.scan()
        assert not await operator_quota.report(context, state(api=False), T0)  # an older hub
        assert await operator_quota.report(context, API, T0)
        assert not await operator_quota.report(context, API, T0 + 120)  # unchanged
        await operator_quota.OperatorPoller(context=context).tick(T0)
        assert not await operator_quota.report(context, API, T0 + 30)  # changed, but within the minute
        held = found.pool.current("b")
        assert held is not None
        found.pool.blocked["b"] = held[1]
        await limits.mark(context, Limit(limits.operator_key("b"), "blocked", None, False, T0, "1:2:3:4"))
        await limits.mark(context, Limit(limits.operator_key("a"), "exhausted", AT + HOUR, True, T0))
        assert await operator_quota.report(context, API, T0 + 60)
        first, second = await items(found, "operator_quota")
        assert first["subscriptions"][0] == {
            "name": "a",
            "state": "usable",
            "readable": False,
            "windows": [],
            "limited_at": None,
            "until": None,
            "fetched_at": None,
        }
        a, b = second["subscriptions"]
        assert (a["state"], a["readable"], a["until"], a["limited_at"]) == (
            "exhausted",
            True,
            "2026-11-18T12:07:00.000Z",
            "2026-11-18T11:06:40.000Z",
        )
        assert len(a["windows"]) == 2
        assert (b["state"], b["until"], b["limited_at"]) == ("blocked", None, "2026-11-18T11:06:40.000Z")
        assert second["at"] == "2026-11-18T11:07:40.000Z"
        assert MARKER not in repr(second)
        await found.env.sync.pusher.drain()  # the contract-checking hub takes both items

    run(store_dsn, tmp_path, body, own=False)


def test_a_device_that_is_not_enrolled_reports_nothing(store_dsn: str, tmp_path: Path) -> None:
    async def body(found: FKit) -> None:
        identity = found.context.identity
        sync = found.context.sync_runtime
        del found.context.sync_runtime
        found.context.identity = None
        try:
            assert not await operator_quota.report(found.context, API, T0)
        finally:
            found.context.identity = identity
            found.context.sync_runtime = sync

    run(store_dsn, tmp_path, body, own=False)


def test_the_owners_quota_items_carry_the_latest_limit(store_dsn: str) -> None:
    async def body(found: FKit) -> None:
        await cache.fetch(found.context, ALICE, "claude_subscription")
        polls = SubscriptionPoller(context=found.context, url=found.quotas.url + USAGE_PATH)
        found.quotas.documents[TOKEN] = usage_document(five=100.0)
        await polls.tick({**API, "quota_poll_owners": [ALICE]}, T0)
        [item] = await items(found, "quota")
        assert item["limited"] == {
            "at": "2026-11-18T11:06:40.000Z",
            "until": "2026-11-18T12:07:00.000Z",
            "readable": True,
        }
        assert limits.limits_for(found.context).active(limits.own_key(ALICE)) is not None
        # an older hub: no `limited`; a refusal of the token releases nothing
        found.quotas.documents[TOKEN] = Answer(401, b"{}")
        await polls.tick({"quota_poll_owners": [ALICE]}, T0 + 900)
        last = (await items(found, "quota"))[-1]
        assert (last["unavailable"], "limited" in last) == (True, False)
        assert limits.limits_for(found.context).active(limits.own_key(ALICE)) is not None
        await found.env.sync.pusher.drain()

    run(store_dsn, None, body)


def test_the_quota_task_polls_the_operator_files_too(store_dsn: str, tmp_path: Path) -> None:
    async def body(found: FKit) -> None:
        found.context.config.openrouter_key_file = None
        polls = SubscriptionPoller(context=found.context, url=found.quotas.url + USAGE_PATH)
        await tasks.tick(found.context, polls, lambda: T0)
        await tasks.tick(found.context, polls, lambda: T0 + 30)
        assert reads(found) == 2  # the two files, once
        assert len(await items(found, "operator_quota")) == 1

    run(store_dsn, tmp_path, body, own=False)
