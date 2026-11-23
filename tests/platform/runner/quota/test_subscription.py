"""U-RUN-quota: subscription quota polling against the local usage double (05-runner.md section 10.6, 03-api.md
section 6.1 `quota`, `quota_poll_owners`; ADR-010).

The token comes from a real `claude_subscription` record (credential hub of RUN-09, decrypted by the cache); quota
items are pushed to the contract-checking hub. The double refuses a request without exactly the bearer token and the
OAuth beta header, so every `200` proves the headers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from agent_runner.credentials import cache
from agent_runner.quota import subscription
from agent_runner.quota.subscription import SubscriptionPoller, retry_after
from agent_runner.sessions import records
from agent_runner.sync.outbox import open_item

from ..credentials.kit import make_record, pid
from ..model.kit import ALICE, TOKEN, Env, model_env, store_record, subscription_body
from .double import USAGE_PATH, UsageDouble

NOW = 1_795_000_000.0
DESIRED = {"quota_poll_owners": [ALICE]}
SESSION = "01930005-0000-7000-8000-000000000001"
Body = Callable[[Env, UsageDouble], Awaitable[None]]


def run(dsn: str, body: Body, *, session: bool = True, cached: bool = True, offset: int = 0) -> None:
    async def main() -> None:
        async with model_env(dsn) as env:
            store_record(env, "claude_subscription", subscription_body())
            if cached:
                await cache.fetch(env.context, ALICE, "claude_subscription")
            if session:
                await add_session(env, SESSION)
            with UsageDouble(TOKEN, offset=offset) as double:
                await body(env, double)

    asyncio.run(main())


async def add_session(env: Env, sid: str, *, ended: float | None = None, owner: str = ALICE) -> None:
    async with env.pool.connection() as conn:
        await records.insert(
            conn,
            session_id=sid,
            task_id="01930004-0000-7000-8000-000000000001",
            org_id="01930001-0000-7000-8000-000000000001",
            wg_id="01930002-0000-7000-8000-000000000001",
            parent_id=None,
            depth=0,
            owner_id=owner,
            created_at=datetime.fromtimestamp(NOW - 3 * 86400, UTC),
        )
        if ended is not None:
            await records.update(conn, sid, state="stopped", ended_at=datetime.fromtimestamp(ended, UTC))


def poller(env: Env, double: UsageDouble) -> SubscriptionPoller:
    return SubscriptionPoller(context=env.context, url=double.url + USAGE_PATH)


async def quota_items(env: Env) -> list[dict[str, Any]]:
    rows = await env.rows("SELECT origin_seq, item_kind, body_ct FROM runner.outbox ORDER BY origin_seq")
    return [open_item(env.sync.storage_key, seq, kind, bytes(blob)) for seq, kind, blob in rows if kind == "quota"]


def test_a_due_owner_is_polled_and_its_windows_reported(store_dsn: str) -> None:
    async def body(env: Env, double: UsageDouble) -> None:
        polls = poller(env, double)
        await polls.tick(DESIRED, NOW)
        [item] = await quota_items(env)
        assert item["owner_id"] == ALICE
        assert item["fetched_at"] == "2026-11-18T11:06:40.000Z"
        assert [w["window"] for w in item["windows"]] == ["five_hour", "seven_day", "weekly:Opus"]
        [(path, headers)] = double.requests
        assert path == USAGE_PATH
        assert headers["authorization"] == f"Bearer {TOKEN}"
        assert headers["anthropic-beta"] == "oauth-2025-04-20"
        assert headers["accept"] == "application/json"
        # not due again before 900 s
        await polls.tick(DESIRED, NOW + 899)
        assert len(double.requests) == 1
        await polls.tick(DESIRED, NOW + 900)
        assert len(double.requests) == 2
        await env.sync.pusher.drain()  # the hub checks contracts/outbox/quota.json
        assert [i["item_kind"] for i in env.hub.items()].count("quota") == 2
        assert TOKEN not in env.log()
        assert "reported owner=" in env.log()

    run(store_dsn, body)


@pytest.mark.parametrize("status", [401, 403])
def test_the_first_refusal_marks_the_credential_unavailable_for_a_day(store_dsn: str, status: int) -> None:
    async def body(env: Env, double: UsageDouble) -> None:
        polls = poller(env, double)
        double.queue(USAGE_PATH, status, {"error": "x"})
        await polls.tick(DESIRED, NOW)
        assert f"quota_unavailable owner={ALICE} status={status}" in env.log()
        # the hub learns it (KOR-05, B-18): it drops the older windows, the web shows "not available"
        assert await quota_items(env) == [
            {"owner_id": ALICE, "unavailable": True, "fetched_at": "2026-11-18T11:06:40.000Z"}
        ]
        await env.sync.pusher.drain()  # the hub checks contracts/outbox/quota.json
        assert [i["item_kind"] for i in env.hub.items()].count("quota") == 1
        await polls.tick(DESIRED, NOW + 900)
        await polls.tick(DESIRED, NOW + 86399)
        assert len(double.requests) == 1
        await polls.tick(DESIRED, NOW + 86400)  # retried after 24 h
        assert len(double.requests) == 2
        # the retry after 24 h answered with windows: reported as such, after the earlier marker
        items = await quota_items(env)
        assert [("unavailable" in item, item["fetched_at"]) for item in items] == [
            (True, "2026-11-18T11:06:40.000Z"),
            (False, "2026-11-19T11:06:40.000Z"),
        ]
        # a new login (record version) is polled at once
        double.queue(USAGE_PATH, status)
        await polls.tick(DESIRED, NOW + 87300)
        assert len(double.requests) == 3
        store_record(env, "claude_subscription", subscription_body(), version=2)
        await cache.refresh_owner(env.context, ALICE)
        await polls.tick(DESIRED, NOW + 87301)
        assert len(double.requests) == 4

    run(store_dsn, body)


def test_a_wrong_token_is_refused_by_the_double_and_detected(store_dsn: str) -> None:
    async def body(env: Env, double: UsageDouble) -> None:
        double.token = "another-token"
        await poller(env, double).tick(DESIRED, NOW)
        assert "quota_unavailable" in env.log()
        assert [item.get("unavailable") for item in await quota_items(env)] == [True]

    run(store_dsn, body)


@pytest.mark.parametrize(
    ("headers", "wait"),
    [({"Retry-After": "1200"}, 1200), ({"Retry-After": "10"}, 300), ({}, 300)],
)
def test_rate_limits_wait_at_least_five_minutes(store_dsn: str, headers: dict[str, str], wait: int) -> None:
    async def body(env: Env, double: UsageDouble) -> None:
        polls = poller(env, double)
        double.queue(USAGE_PATH, 429, {"error": "rate"}, headers)
        await polls.tick(DESIRED, NOW)
        assert "rate_limited" in env.log()
        await polls.tick(DESIRED, NOW + wait - 1)
        assert len(double.requests) == 1
        await polls.tick(DESIRED, NOW + wait)
        assert len(double.requests) == 2

    run(store_dsn, body)


@pytest.mark.parametrize(
    ("status", "body_", "line"),
    [
        (500, {"error": "x"}, "poll_failed owner={owner} status=500"),
        (302, {}, "poll_failed owner={owner} status=302"),  # redirects are never followed
        (200, b"not json", "usage_invalid owner={owner} field=json"),
        (200, {"five_hour": "x"}, "usage_invalid owner={owner} field=five_hour"),
        (200, {"five_hour": None}, "no_windows owner={owner}"),
    ],
)
def test_unusable_answers_are_logged_and_polled_again_later(
    store_dsn: str, status: int, body_: object, line: str
) -> None:
    async def body(env: Env, double: UsageDouble) -> None:
        polls = poller(env, double)
        double.queue(USAGE_PATH, status, body_, {"Location": "https://elsewhere.test/"} if status == 302 else None)
        await polls.tick(DESIRED, NOW)
        assert line.format(owner=ALICE) in env.log()
        assert await quota_items(env) == []
        assert len(double.requests) == 1
        await polls.tick(DESIRED, NOW + 900)
        assert len(await quota_items(env)) == 1

    run(store_dsn, body)


def test_a_transport_error_is_logged(store_dsn: str) -> None:
    async def body(env: Env, double: UsageDouble) -> None:
        polls = SubscriptionPoller(context=env.context, url="http://127.0.0.1:9/api/oauth/usage")
        await polls.tick(DESIRED, NOW)
        assert f"poll_failed owner={ALICE} error=ConnectError" in env.log()

    run(store_dsn, body)


def test_only_owners_the_hub_names_are_polled(store_dsn: str) -> None:
    async def body(env: Env, double: UsageDouble) -> None:
        polls = poller(env, double)
        for desired in (None, {}, {"quota_poll_owners": "x"}, {"quota_poll_owners": ["not-a-uuid", 5]}):
            await polls.tick(desired, NOW)
        assert double.requests == []
        # the hub's election is authoritative: no session of the owner on this device is needed (ACC-FINAL)
        await polls.tick(DESIRED, NOW)
        assert len(double.requests) == 1
        assert ALICE in polls.owners
        # an owner the hub no longer names is forgotten
        await polls.tick({"quota_poll_owners": []}, NOW + 900)
        assert polls.owners == {}

    run(store_dsn, body, session=False)


def test_a_named_owner_without_cached_subscription_is_fetched_once_per_interval(store_dsn: str) -> None:
    async def body(env: Env, double: UsageDouble) -> None:
        polls = poller(env, double)
        await polls.tick(DESIRED, NOW)
        assert len(double.requests) == 1  # fetched from the hub, verified, cached, polled
        assert await records_cached(env) == 1
        # a purged record (404) is not polled and fetched again only after the interval
        env.hub.records.clear()
        async with env.pool.connection() as conn:
            await conn.execute("DELETE FROM runner.credential_cache")
        await polls.tick(DESIRED, NOW + 900)
        await polls.tick(DESIRED, NOW + 1000)
        assert len(double.requests) == 1
        assert polls.owners == {}
        store_record(env, "claude_subscription", subscription_body())
        await polls.tick(DESIRED, NOW + 1100)
        assert len(double.requests) == 1  # still inside the interval of the last fetch
        await polls.tick(DESIRED, NOW + 1800)
        assert len(double.requests) == 2
        # an owner the hub no longer names is forgotten, its fetch interval too
        env.hub.records.clear()
        async with env.pool.connection() as conn:
            await conn.execute("DELETE FROM runner.credential_cache")
        await polls.tick(DESIRED, NOW + 2700)
        assert ALICE in polls.fetched
        await polls.tick({"quota_poll_owners": []}, NOW + 2800)
        assert polls.fetched == {}

    run(store_dsn, body, session=False, cached=False)


def test_a_failed_fetch_is_logged_and_the_owner_skipped(store_dsn: str) -> None:
    async def body(env: Env, double: UsageDouble) -> None:
        env.hub.get_refusals.append((503, {"error": "unavailable"}))
        await poller(env, double).tick(DESIRED, NOW)
        assert double.requests == []
        assert f"credential_fetch_failed owner={ALICE} error=HubRefusal" in env.log()

    run(store_dsn, body, cached=False)


def test_the_record_of_an_owner_the_log_copy_does_not_show_yet_is_fetched_after_a_refresh(store_dsn: str) -> None:
    # A newly elected owner (here a user who just joined) may be ahead of the device's log copy: the logs are
    # refreshed before the record is judged, so a rightful record is never refused and audited (KOR-17 rule).
    carol = pid("carol")

    async def body(env: Env, double: UsageDouble) -> None:
        spec = env.world.orgs[pid("a/org")]
        spec.log.invite_user(spec.founder, "carol", wgs={})
        spec.log.accept("carol")
        env.hub.records[(carol, "claude_subscription")] = make_record(
            subscription_body(), kind="claude_subscription", owner=carol, author="carol"
        )
        await poller(env, double).tick({"quota_poll_owners": [carol]}, NOW)
        assert [a for a in await env.audits() if a["action"] == "runner.inbound_rejected"] == []
        assert await records_cached(env) == 1

    run(store_dsn, body, session=False, cached=False)


def test_no_record_is_fetched_while_the_logs_cannot_be_refreshed(store_dsn: str) -> None:
    async def body(env: Env, double: UsageDouble) -> None:
        serve = env.hub.state_answer
        env.hub.state_answer = lambda: (503, {"error": "unavailable"})  # type: ignore[method-assign]
        polls = poller(env, double)
        await polls.tick(DESIRED, NOW)
        assert await records_cached(env) == 0
        assert "logs_refresh_failed" in env.log()
        assert polls.fetched == {}  # tried again on the next tick
        env.hub.state_answer = serve  # type: ignore[method-assign]
        await polls.tick(DESIRED, NOW + 30)
        assert len(double.requests) == 1

    run(store_dsn, body, session=False, cached=False)


def test_one_refresh_serves_every_owner_of_a_tick_and_a_halted_device_fetches_nothing(store_dsn: str) -> None:
    async def body(env: Env, double: UsageDouble) -> None:
        polls = poller(env, double)
        before = len(env.hub.targets("/v1/device/state"))
        await polls.tick({"quota_poll_owners": [ALICE, pid("bob")]}, NOW)  # bob has no record (404)
        assert len(env.hub.targets("/v1/device/state")) == before + 1
        assert len(double.requests) == 1
        env.hub.records.clear()
        async with env.pool.connection() as conn:
            await conn.execute("DELETE FROM runner.credential_cache")
        env.world.state = "pending_certification"  # the device is halted: nothing is judged
        await polls.tick(DESIRED, NOW + 900)
        assert polls.fetched == {ALICE: NOW}

    run(store_dsn, body, session=False, cached=False)


async def records_cached(env: Env) -> int:
    rows = await env.rows("SELECT count(*) FROM runner.credential_cache")
    return int(rows[0][0])


def test_a_token_that_does_not_open_is_not_sent(store_dsn: str) -> None:
    async def body(env: Env, double: UsageDouble) -> None:
        async with env.pool.connection() as conn:
            await conn.execute("DELETE FROM runner.scope_key")  # no CK on this device any more
        await poller(env, double).tick(DESIRED, NOW)
        assert double.requests == []
        assert f"credential_unusable owner={ALICE} reason=key_missing" in env.log()

    run(store_dsn, body)


def test_report_without_sync_runtime_is_dropped(store_dsn: str) -> None:
    async def body(env: Env, double: UsageDouble) -> None:
        polls = poller(env, double)
        polls.context = type("Unenrolled", (), {"identity": None, "pool": env.pool, "log": env.context.log})()
        await polls.report(ALICE, [{"window": "five_hour"}], NOW)
        assert await quota_items(env) == []
        await polls.tick(DESIRED, NOW)  # nothing is fetched without a hub to refresh the logs from
        assert polls.fetched == {}
        assert double.requests == []

    run(store_dsn, body, cached=False)


def test_retry_after_forms() -> None:
    assert retry_after(None, NOW) == 0.0
    assert retry_after(" 120 ", NOW) == 120.0
    assert retry_after("Wed, 18 Nov 2026 11:16:40 GMT", NOW) == 600.0
    assert retry_after("Wed, 18 Nov 2020 08:36:40 GMT", NOW) == 0.0
    assert retry_after("soon", NOW) == 0.0
    assert retry_after("\u00b2", NOW) == 0.0  # a Unicode digit is no delta-seconds value
    assert retry_after("999999999", NOW) == 86400.0  # capped at 24 h
    assert retry_after("Wed, 18 Nov 2099 11:16:40 GMT", NOW) == 86400.0


def test_the_default_client_never_follows_redirects() -> None:
    async def main() -> None:
        async with subscription.default_client() as client:
            assert client.follow_redirects is False
            assert client.timeout == httpx.Timeout(8.0)

    asyncio.run(main())
