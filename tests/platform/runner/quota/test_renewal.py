"""U-RUN-quota (renewal, KOR-71, ADR-091 section 2): the device that measures an owner renews the full login of a
further subscription before its access token expires and after a `401` of the usage endpoint, against the token
endpoint of the auth-page double (rotation: the used refresh token dies). The new tokens are stored sealed before
they are written to the hub; a crash or a refused write keeps them for the next tick (the token endpoint is not called
again); a concurrent change of the owner is merged, a newer login wins; `invalid_grant` marks the record "log in
again"; a device that does not measure the owner never renews; no token ever reaches a log or the store in
plaintext."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from agent_doubles.login_cli import codes
from agent_doubles.login_cli.authpage import AuthPageDouble, Scenario
from agent_runner.quota import renewal
from agent_runner.quota.further import FurtherPoller
from agent_runner.sessions.records import time_text

from ..credentials.subscription_kit import SubscriptionHub
from ..model.kit import ALICE, TOKEN, Env, model_env
from ..sync.fakehub import SCHEMAS
from .double import USAGE_PATH, UsageDouble
from .test_further import DESIRED, NOW, quota_items

SEED = "renewal"
REFRESH = "refresh-of-the-first-login-0123456789"
LIFETIME = codes.FULL_LIFETIME
DUE = NOW + LIFETIME - renewal.RENEW_BEFORE_S
Body = Callable[[Env, SubscriptionHub, UsageDouble, AuthPageDouble], Awaitable[None]]


def run(dsn: str, body: Body) -> None:
    async def main() -> None:
        async with model_env(dsn) as env:
            hub = SubscriptionHub(env.hub).install()
            auth = AuthPageDouble(scenario=Scenario(seed=SEED)).start()
            try:
                env.context.config.claude_token_url = auth.page_url + codes.TOKEN_PATH
                env.context.config.claude_oauth_client_id = codes.CLIENT_ID
                with UsageDouble(TOKEN) as double:
                    await body(env, hub, double, auth)
            finally:
                auth.stop()
            assert auth.state()["failures"] == []

    asyncio.run(main())


def login(token: str = TOKEN, refresh: str | None = REFRESH, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "token": token,
        "expires_at": time_text(NOW + LIFETIME),
        "scopes": ["user:inference", "user:profile"],
        "email": "team@example.com",
        "kind": "oauth_login",
        "name": "Team",
        **extra,
    }
    if refresh is not None:
        body["refresh_token"] = refresh
    return body


def poller(env: Env, double: UsageDouble) -> FurtherPoller:
    return FurtherPoller(context=env.context, url=double.url + USAGE_PATH)


def renewed(n: int) -> tuple[str, str]:
    """The n-th access and refresh token the auth-page double hands out."""
    return codes.access_token(SEED, n), codes.refresh_token(SEED, n)


def grant(auth: AuthPageDouble, refresh: str = REFRESH) -> None:
    """The refresh token of a login the provider knows."""
    auth.run.refresh[refresh] = "user:inference user:profile"


async def pending_rows(env: Env) -> list[tuple[Any, ...]]:
    return await env.rows(
        "SELECT id::text, base_version, record::text, base_record::text FROM runner.subscription_renewal"
    )


def test_the_measuring_device_renews_before_the_expiry_and_measures_with_the_new_token(store_dsn: str) -> None:
    async def body(env: Env, hub: SubscriptionHub, double: UsageDouble, auth: AuthPageDouble) -> None:
        grant(auth)
        record = hub.add(login(), owner=ALICE)
        polls = poller(env, double)
        await polls.tick(DESIRED, NOW)
        [item] = await quota_items(env)
        assert (item["subscription_id"], len(item["windows"])) == (record, 3)  # measured with the first token
        assert hub.puts == []
        await polls.tick(DESIRED, DUE - 1)  # not yet due
        assert hub.puts == []
        await polls.tick(DESIRED, DUE)
        access, refresh = renewed(1)
        stored = hub.opened(record)
        assert stored == {
            **login(access, refresh),
            "expires_at": time_text(DUE + LIFETIME),
            "scopes": ["user:inference", "user:profile"],
        }
        assert SCHEMAS.errors("credentials/claude_subscription.json", stored) == []
        assert [put.get("request_id") for put in hub.puts] == [None]
        assert await pending_rows(env) == []
        assert REFRESH not in auth.run.refresh  # the provider rotated it
        # measured again with the new token at the next tick, nothing renewed twice
        double.token = access
        await polls.tick(DESIRED, DUE + 900)
        before = len(await quota_items(env))
        await polls.tick(DESIRED, DUE + 1800)
        items = await quota_items(env)
        assert len(items) == before + 1
        assert "windows" in items[-1]
        assert len(hub.puts) == 1
        log = env.log()
        assert f"runner.renewal renewal_stored id={record} base=1" in log
        assert f"runner.renewal renewed id={record} version=2" in log
        for secret in (TOKEN, REFRESH, access, refresh):
            assert secret not in log

    run(store_dsn, body)


def test_a_refused_usage_read_renews_at_once(store_dsn: str) -> None:
    async def body(env: Env, hub: SubscriptionHub, double: UsageDouble, auth: AuthPageDouble) -> None:
        grant(auth)
        record = hub.add(login("expired-access-token"), owner=ALICE)
        polls = poller(env, double)
        await polls.tick(DESIRED, NOW)  # the usage double refuses the token: 401
        assert hub.opened(record)["token"] == renewed(1)[0]
        assert await quota_items(env) == []  # not reported as unreadable
        double.token = renewed(1)[0]
        await polls.tick(DESIRED, NOW + 30)
        [item] = await quota_items(env)
        assert "windows" in item

    run(store_dsn, body)


def test_a_new_login_written_before_the_crash_is_written_after_the_restart(store_dsn: str) -> None:
    async def body(env: Env, hub: SubscriptionHub, double: UsageDouble, auth: AuthPageDouble) -> None:
        grant(auth)
        record = hub.add(login(), owner=ALICE)
        hub.put_refusals = [(503, {"error": "unavailable"}), (403, {"error": "forbidden"})]
        polls = poller(env, double)
        await polls.tick(DESIRED, DUE)
        [row] = await pending_rows(env)
        access, refresh = renewed(1)
        assert row[:2] == (record, 1)
        for secret in (access, refresh, REFRESH, TOKEN):
            assert secret not in row[2]
            assert secret not in row[3]  # ciphertext only
        assert hub.served[record].record["header"]["version"] == 1
        # the daemon restarts (nothing in memory) and is not the measuring device any more: the row is still written
        polls = poller(env, double)
        await polls.tick({**DESIRED, "quota_poll_owners": []}, DUE + 1)
        assert len(hub.puts) == 2  # refused again (forbidden), kept
        assert len(await pending_rows(env)) == 1
        await polls.tick({**DESIRED, "quota_poll_owners": []}, DUE + 1 + renewal.RETRY_S)
        assert hub.opened(record)["token"] == access
        assert await pending_rows(env) == []
        assert auth.run.counters["refresh"] == 1  # the token endpoint was asked once

    run(store_dsn, body)


def test_an_owner_change_meanwhile_is_merged_and_a_newer_login_wins(store_dsn: str) -> None:
    async def body(env: Env, hub: SubscriptionHub, double: UsageDouble, auth: AuthPageDouble) -> None:
        grant(auth)
        renamed = hub.add(login(), owner=ALICE)
        hub.put_refusals = [(503, {"error": "unavailable"})]
        polls = poller(env, double)
        await polls.tick(DESIRED, DUE)
        hub.add(login(name="Renamed", use={"pool": True, "orgs": []}), owner=ALICE, version=2, record_id=renamed)
        await polls.tick(DESIRED, DUE + renewal.RETRY_S)
        merged = hub.opened(renamed)
        assert (merged["name"], merged["use"], merged["token"]) == (
            "Renamed",
            {"pool": True, "orgs": []},
            renewed(1)[0],
        )
        assert hub.served[renamed].record["header"]["version"] == 3
        # a newer login replaced the record meanwhile: the renewal is dropped
        del hub.served[renamed]  # removed by its owner: listed no more
        grant(auth, "second-refresh-token-0123456789")
        relogged = hub.add(login(refresh="second-refresh-token-0123456789"), owner=ALICE)
        hub.put_refusals = [(503, {"error": "unavailable"})]
        await polls.tick(DESIRED, DUE + 900)  # listed anew: the new record is due at once
        fresh = login("fresh-login-token", refresh="fresh-login-refresh", expires_at=time_text(DUE + 2 * LIFETIME))
        hub.add(fresh, owner=ALICE, version=2, record_id=relogged)
        await polls.tick(DESIRED, DUE + 900 + renewal.RETRY_S)
        assert hub.served[relogged].record["header"]["author"] != env.context.identity.device.device_id
        assert await pending_rows(env) == []
        assert f"pending_dropped id={relogged} reason=newer_login" in env.log()

    run(store_dsn, body)


def test_a_refused_refresh_token_marks_the_record_log_in_again(store_dsn: str) -> None:
    async def body(env: Env, hub: SubscriptionHub, double: UsageDouble, auth: AuthPageDouble) -> None:
        record = hub.add(login(refresh="unknown-to-the-provider"), owner=ALICE)  # never granted: invalid_grant
        polls = poller(env, double)
        await polls.tick(DESIRED, DUE)
        marked = hub.opened(record)
        assert "token" not in marked
        assert "refresh_token" not in marked
        assert marked["relogin"] == "renewal_refused"
        assert marked["name"] == "Team"
        assert SCHEMAS.errors("credentials/claude_subscription.json", marked) == []
        await polls.tick(DESIRED, DUE + 900)  # no token: neither renewed nor measured
        assert auth.run.counters.get("refresh", 0) == 0
        assert f"subscription_logged_out id={record}" in env.log()

    run(store_dsn, body)


def test_a_pending_renewal_wins_over_a_refusal_of_another_device(store_dsn: str) -> None:
    async def body(env: Env, hub: SubscriptionHub, double: UsageDouble, auth: AuthPageDouble) -> None:
        grant(auth)
        record = hub.add(login(), owner=ALICE)
        hub.put_refusals = [(503, {"error": "unavailable"})]
        polls = poller(env, double)
        await polls.tick(DESIRED, DUE)
        assert len(await pending_rows(env)) == 1
        # meanwhile another device got invalid_grant with the old refresh token and marked the record
        marked = {k: v for k, v in login().items() if k not in ("token", "refresh_token")}
        hub.add({**marked, "relogin": "renewal_refused"}, owner=ALICE, version=2, record_id=record)
        await polls.tick(DESIRED, DUE + renewal.RETRY_S)
        found = hub.opened(record)
        assert (found["token"], found["refresh_token"]) == renewed(1)
        assert "relogin" not in found
        assert hub.served[record].record["header"]["version"] == 3

    run(store_dsn, body)


def test_a_refusal_after_a_newer_login_marks_nothing(store_dsn: str) -> None:
    async def body(env: Env, hub: SubscriptionHub, double: UsageDouble, auth: AuthPageDouble) -> None:
        record = hub.add(login(refresh="unknown-to-the-provider"), owner=ALICE)
        hub.put_refusals = [(409, {"error": "version_conflict", "detail": {"expected": 2}})]
        newer = login("new-login-token", refresh="new-login-refresh")

        original = hub.answer
        calls = {"n": 0}

        def answer(record_id: str) -> tuple[int, Any]:
            calls["n"] += 1
            if calls["n"] == 3:  # the read after the conflict sees the newer login
                hub.add(newer, owner=ALICE, version=2, record_id=record)
            return original(record_id)

        hub.answer = answer  # type: ignore[method-assign]
        await poller(env, double).tick(DESIRED, DUE)
        assert [put["expected_version"] for put in hub.puts] == [1]
        assert hub.served[record].record["header"]["version"] == 2  # the newer login stays as it is

    run(store_dsn, body)


def test_a_device_that_does_not_measure_the_owner_never_renews(store_dsn: str) -> None:
    async def body(env: Env, hub: SubscriptionHub, double: UsageDouble, auth: AuthPageDouble) -> None:
        grant(auth)
        hub.add(login(), owner=ALICE)
        polls = poller(env, double)
        await polls.tick({**DESIRED, "quota_poll_owners": []}, DUE + LIFETIME)
        await polls.tick({"quota_poll_owners": [ALICE]}, DUE + LIFETIME)  # a hub without the api
        assert hub.puts == []
        assert auth.run.counters.get("refresh", 0) == 0

    run(store_dsn, body)


def test_failed_renewals_are_tried_again_later(store_dsn: str) -> None:
    async def body(env: Env, hub: SubscriptionHub, double: UsageDouble, auth: AuthPageDouble) -> None:
        grant(auth)
        record = hub.add(login(), owner=ALICE)
        polls = poller(env, double)
        token_url = env.context.config.claude_token_url
        env.context.config.claude_token_url = "http://127.0.0.1:9" + codes.TOKEN_PATH
        await polls.tick(DESIRED, DUE)
        assert f"renew_failed id={record} error=ConnectError" in env.log()
        env.context.config.claude_token_url = token_url
        auth.set_scenario(Scenario(seed=SEED, fault="token"))
        grant(auth)
        await polls.tick(DESIRED, DUE + 1)  # within RETRY_S: not asked
        await polls.tick(DESIRED, DUE + renewal.RETRY_S)
        assert f"renew_failed id={record} status=500" in env.log()
        assert hub.puts == []
        auth.set_scenario(Scenario(seed=SEED))
        grant(auth)
        await polls.tick(DESIRED, DUE + 2 * renewal.RETRY_S)
        assert hub.opened(record)["token"] == renewed(1)[0]

    run(store_dsn, body)


def test_records_that_are_no_renewable_login_are_left_alone(store_dsn: str) -> None:
    async def body(env: Env, hub: SubscriptionHub, double: UsageDouble, auth: AuthPageDouble) -> None:
        setup = hub.add({"token": "setup-token", "email": None, "kind": "setup_token"}, owner=ALICE)
        logged_out = hub.add(login(refresh=None), owner=ALICE)
        broken = hub.add({**login(), "expires_at": "not a time"}, owner=ALICE)
        polls = poller(env, double)
        await polls.tick(DESIRED, DUE + LIFETIME)
        assert hub.puts == []
        items = {item["subscription_id"]: item for item in await quota_items(env)}
        assert items[setup]["unavailable"] is True  # a setup token stays "not readable"
        assert "windows" in items[logged_out]  # measured, never renewed
        assert "windows" in items[broken]

    run(store_dsn, body)
