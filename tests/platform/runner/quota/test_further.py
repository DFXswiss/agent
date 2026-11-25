"""U-RUN-quota (KOR-69, ADR-090 section 4): further subscriptions of the polled owners are listed at the hub, re-sealed
when they lie under an older epoch and measured against the local usage double; `quota` items carry
`subscription_id` and go only to a hub that announces `subscriptions.api`."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from agent_runner.quota.further import FurtherPoller, api, owners
from agent_runner.sync.outbox import open_item

from ..credentials.subscription_kit import SubscriptionHub
from ..model.kit import ALICE, TOKEN, Env, model_env
from .double import USAGE_PATH, UsageDouble

NOW = 1_795_000_000.0
DESIRED = {"quota_poll_owners": [ALICE], "subscriptions": {"api": 1}}
Body = Callable[[Env, SubscriptionHub, UsageDouble], Awaitable[None]]


def run(dsn: str, body: Body) -> None:
    async def main() -> None:
        async with model_env(dsn) as env:
            hub = SubscriptionHub(env.hub).install()
            with UsageDouble(TOKEN) as double:
                await body(env, hub, double)

    asyncio.run(main())


def poller(env: Env, double: UsageDouble) -> FurtherPoller:
    return FurtherPoller(context=env.context, url=double.url + USAGE_PATH)


def further(token: str | None = TOKEN) -> dict[str, Any]:
    body: dict[str, Any] = {"email": None, "kind": "setup_token", "name": "Team"}
    if token is not None:
        body["token"] = token
    return body


async def quota_items(env: Env) -> list[dict[str, Any]]:
    rows = await env.rows("SELECT origin_seq, item_kind, body_ct FROM runner.outbox ORDER BY origin_seq")
    return [open_item(env.sync.storage_key, seq, kind, bytes(blob)) for seq, kind, blob in rows if kind == "quota"]


def test_the_hub_must_announce_the_api_and_owners_must_be_well_formed() -> None:
    assert api(DESIRED) is True
    for desired in (None, {}, {"subscriptions": None}, {"subscriptions": {"api": True}}, {"subscriptions": {"api": 0}}):
        assert api(desired) is False
    assert owners({"quota_poll_owners": [ALICE, ALICE, "x", 3]}) == [ALICE]
    assert owners({"quota_poll_owners": "x"}) == []
    assert owners(None) == []


def test_further_subscriptions_are_listed_measured_and_reported(store_dsn: str) -> None:
    async def body(env: Env, hub: SubscriptionHub, double: UsageDouble) -> None:
        record = hub.add(further(), owner=ALICE)
        hub.add(further(), owner=ALICE, primary=True)
        polls = poller(env, double)
        await polls.tick({"quota_poll_owners": [ALICE]}, NOW)  # an older hub: nothing at all
        assert double.requests == []
        await polls.tick(DESIRED, NOW)
        [item] = await quota_items(env)
        assert (item["owner_id"], item["subscription_id"]) == (ALICE, record)
        assert [w["window"] for w in item["windows"]] == ["five_hour", "seven_day", "weekly:Opus"]
        assert "limited" not in item
        assert len(double.requests) == 1  # the primary record is the other poller's
        await polls.tick(DESIRED, NOW + 899)
        assert len(double.requests) == 1
        await polls.tick(DESIRED, NOW + 900)
        assert len(double.requests) == 2
        assert f"runner.quota reported id={record} windows=3" in env.log()
        assert TOKEN not in env.log()
        # the owner leaves the list: its schedules go
        await polls.tick({**DESIRED, "quota_poll_owners": []}, NOW + 2000)
        assert (polls.listed, polls.records) == ({}, {})

    run(store_dsn, body)


def test_a_refused_token_is_reported_unavailable_until_a_new_login(store_dsn: str) -> None:
    async def body(env: Env, hub: SubscriptionHub, double: UsageDouble) -> None:
        record = hub.add(further(), owner=ALICE)
        polls = poller(env, double)
        double.queue(USAGE_PATH, 403, {"error": {"type": "permission_error"}})
        await polls.tick(DESIRED, NOW)
        [item] = await quota_items(env)
        assert (item["subscription_id"], item["unavailable"]) == (record, True)
        await polls.tick(DESIRED, NOW + 1000)  # listed again, same version: not polled
        assert len(double.requests) == 1
        hub.add(further(), owner=ALICE, version=2, record_id=record)
        await polls.tick(DESIRED, NOW + 2000)
        assert len(double.requests) == 2

    run(store_dsn, body)


def test_unusable_answers_are_logged_and_polled_again_later(store_dsn: str) -> None:
    async def body(env: Env, hub: SubscriptionHub, double: UsageDouble) -> None:
        record = hub.add(further(), owner=ALICE)
        polls = poller(env, double)
        double.queue(USAGE_PATH, 429, {}, {"retry-after": "1200"})
        await polls.tick(DESIRED, NOW)
        assert f"rate_limited id={record}" in env.log()
        await polls.tick(DESIRED, NOW + 1100)
        assert len(double.requests) == 1
        for status, answer in ((500, {}), (200, b"not json"), (200, {"five_hour": "x"}), (200, {})):
            double.queue(USAGE_PATH, status, answer)
        for step in range(4):
            await polls.tick(DESIRED, NOW + 1200 + step * 900)
        log = env.log()
        assert f"poll_failed id={record} status=500" in log
        assert f"usage_invalid id={record} field=json" in log
        assert f"usage_invalid id={record} field=five_hour" in log
        assert f"no_windows id={record}" in log
        assert await quota_items(env) == []

    run(store_dsn, body)


def test_a_transport_error_a_logged_out_or_unopenable_record_reports_nothing(store_dsn: str) -> None:
    async def body(env: Env, hub: SubscriptionHub, double: UsageDouble) -> None:
        out = hub.add(further(token=None), owner=ALICE)
        polls = poller(env, double)
        await polls.tick(DESIRED, NOW)
        assert f"subscription_logged_out id={out}" in env.log()
        assert double.requests == []
        del hub.served[out]
        record = hub.add(further(), owner=ALICE)
        polls.url = "http://127.0.0.1:9" + USAGE_PATH
        await polls.tick(DESIRED, NOW + 900)
        assert f"poll_failed id={record} error=ConnectError" in env.log()
        async with env.pool.connection() as conn:
            await conn.execute(
                "UPDATE runner.subscription_cache SET record = jsonb_set(record, '{ct}', '\"AAAA\"') WHERE id = %s",
                (record,),
            )
        polls.listed.clear()
        hub.list_refusals[ALICE] = (404, {"error": "not_found"})
        await polls.tick(DESIRED, NOW + 1800)
        assert f"subscription_unusable id={record} reason=invalid" in env.log()
        assert await quota_items(env) == []

    run(store_dsn, body)


def test_a_record_under_an_older_epoch_is_resealed_before_it_is_measured(store_dsn: str) -> None:
    async def body(env: Env, hub: SubscriptionHub, double: UsageDouble) -> None:
        record = hub.add(further(), owner=ALICE)
        await env.add_ck(epoch=2)
        env.hub.ck_epoch = 2
        polls = poller(env, double)
        await polls.tick(DESIRED, NOW)
        assert hub.served[record].record["header"]["epoch"] == 2
        assert double.requests == []
        polls.listed.clear()
        await polls.tick(DESIRED, NOW + 1)
        assert len(double.requests) == 1
        [item] = await quota_items(env)
        assert item["subscription_id"] == record

    run(store_dsn, body)
