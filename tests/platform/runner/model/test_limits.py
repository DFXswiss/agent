"""U-RUN-model (KOR-50, ADR-080 sections 1, 2 and 6): the used-up accesses of `runner.access_limit` (kept across
restarts, released when their time passed), the rule of a readable quota, the fallback settings of the device state
and one read of the usage endpoint against the local double."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx

from agent_runner.model import limits, policy
from agent_runner.model.limits import EXHAUSTED, Limit
from agent_runner.quota import reader
from agent_runner.quota.reader import QuotaOptions, Reading

from ..quota.double import USAGE_PATH, UsageDouble
from .fallback_kit import AT, HOUR, T0, Wall, usage_document
from .kit import ALICE, TOKEN, Env, model_env

OWN = limits.own_key(ALICE)
Body = Callable[[Env], Awaitable[None]]


def run(dsn: str, body: Body) -> None:
    async def main() -> None:
        async with model_env(dsn) as env:
            limits.limits_for(env.context).wall = Wall()
            await body(env)

    asyncio.run(main())


def window(name: str, used: float, resets: float | None) -> dict[str, Any]:
    return {
        "window": name,
        "used_percent": used,
        "resets_at": None if resets is None else datetime.fromtimestamp(resets, UTC).strftime("%Y-%m-%dT%H:%M:00.000Z"),
        "severity": None,
    }


def test_access_keys_and_the_rule_of_a_readable_quota() -> None:
    assert (limits.own_key("o"), limits.operator_key("a"), limits.dfx_key("d")) == ("own:o", "operator:a", "dfxkey:d")
    later, latest = AT + 600, AT + 7200
    full = [window("five_hour", 100, later), window("seven_day", 120, latest), window("weekly:Opus", 100, T0 + 9999)]
    assert limits.full_until(full, T0) == latest  # the latest release of the full windows of the whole subscription
    assert limits.full_until([window("weekly:Opus", 100, later)], T0) is None  # a per-model window does not count
    assert limits.full_until([window("five_hour", 99.9, later)], T0) is None
    assert limits.full_until([window("five_hour", 100, T0 - 1)], T0) is None  # released already
    assert limits.full_until([window("five_hour", 100, None)], T0) is None
    assert limits.full_until([], T0) is None
    assert limits.fullest(full) == 120
    assert limits.fullest([window("weekly:Opus", 50, None)]) is None
    assert (
        limits.resets({"resets_at": "2026-11-21T12:00:00.000Z"}) == datetime(2026, 11, 21, 12, tzinfo=UTC).timestamp()
    )
    assert limits.resets({}) is None


def test_the_memory_answers_active_limits_and_blocks() -> None:
    holder = SimpleNamespace()
    found = limits.limits_for(holder)
    assert limits.limits_for(holder) is found
    found.wall = Wall()
    found.rows = {
        "own:o": Limit("own:o", EXHAUSTED, T0 + 10, True, T0),
        "own:p": Limit("own:p", EXHAUSTED, T0 - 10, True, T0 - 100),
        "operator:a": Limit("operator:a", limits.BLOCKED, None, False, T0, "1:2:3:4"),
    }
    assert found.active("own:o") is found.rows["own:o"]
    assert found.active("own:o", T0 + 10) is None
    assert found.active("own:p") is None
    assert found.active("operator:a") is None  # a block is no exhaustion
    assert found.active("own:x") is None
    assert found.blocks() == {"a": "1:2:3:4"}


def test_limits_survive_a_restart_and_passed_ones_are_released(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        context = env.context
        await limits.mark(context, Limit(OWN, EXHAUSTED, T0 + HOUR, True, T0))
        await limits.mark(context, Limit("operator:a", EXHAUSTED, T0 + 60, False, T0))
        await limits.mark(context, Limit("operator:b", limits.BLOCKED, None, False, T0, "1:2:3:4"))
        await limits.mark(context, Limit(OWN, EXHAUSTED, T0 + 2 * HOUR, False, T0 + 1))  # replaced
        await limits.release(context, ["own:nobody"])
        # a new daemon: read once, a row whose time passed is released
        wall = Wall(T0 + 120)
        delattr(context, limits.ATTRIBUTE)
        found = limits.limits_for(context)
        found.wall = wall
        loaded = await limits.load(context)
        assert loaded is found
        assert await limits.load(context) is found
        assert sorted(found.rows) == ["operator:b", OWN]
        assert found.rows[OWN] == Limit(OWN, EXHAUSTED, T0 + 2 * HOUR, False, T0 + 1)
        assert found.rows["operator:b"].identity == "1:2:3:4"
        rows = await env.rows("SELECT access_key FROM runner.access_limit ORDER BY 1")
        assert rows == [("operator:b",), (OWN,)]
        wall.now = T0 + 3 * HOUR
        await limits.prune(context)
        assert sorted(found.rows) == ["operator:b"]
        await limits.release(context, ["operator:b"])
        assert await env.rows("SELECT access_key FROM runner.access_limit") == []

    run(store_dsn, body)


def test_a_reading_marks_and_releases_by_the_rule(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        context = env.context
        full = (window("five_hour", 100, AT + HOUR),)
        assert await limits.apply(context, OWN, Reading(readable=False, fetched_at=T0)) is None
        marked = await limits.apply(context, OWN, Reading(True, full, T0))
        assert marked == Limit(OWN, EXHAUSTED, AT + HOUR, True, T0)
        assert await limits.apply(context, OWN, Reading(True, full, T0 + 5)) is None  # the same release: unchanged
        assert limits.limits_for(context).readings[OWN] == Reading(True, full, T0 + 5)
        free = (window("five_hour", 40, AT + HOUR),)
        assert await limits.apply(context, OWN, Reading(True, free, T0 + 10)) is None
        assert OWN not in limits.limits_for(context).rows  # a readable mark is released by a readable quota
        streak = Limit(OWN, EXHAUSTED, T0 + 900, False, T0)
        await limits.mark(context, streak)
        assert await limits.apply(context, OWN, Reading(True, free, T0 + 20)) is None
        assert limits.limits_for(context).rows[OWN] == streak  # the figures may lag: a streak keeps its probe time
        later = (window("five_hour", 100, AT + 2 * HOUR),)
        assert (await limits.apply(context, OWN, Reading(True, later, T0 + 30))) is not None
        assert limits.limits_for(context).rows[OWN].readable

    run(store_dsn, body)


def test_the_fallback_settings_of_the_device_state() -> None:
    assert not policy.api(None)
    assert not policy.api({"model_fallback": []})
    assert not policy.api({"model_fallback": {"api": True}})
    assert not policy.api({"model_fallback": {"api": 0}})
    assert policy.api({"model_fallback": {"api": 2}})
    assert policy.settings(None, "s") == policy.Settings(False, True, False)
    assert policy.entry({"sessions": "x"}, "s") is None
    assert policy.entry({"sessions": ["x", {"session_id": "s"}]}, "s") == {"session_id": "s"}
    listed = {"sessions": [{"session_id": "s", "fallback": {"own_to_dfx": True, "operator_to_platform": False}}]}
    assert policy.settings(listed, "s") == policy.Settings(True, False, False)
    malformed = {"sessions": [{"session_id": "s", "fallback": "on", "model_consent": "yes"}]}
    assert policy.settings(malformed, "s") == policy.Settings()
    consent = policy.settings({"sessions": [{"session_id": "s", "model_consent": True}]}, "s")
    assert consent.crossing
    assert not policy.Settings().crossing
    assert policy.Settings(own_to_dfx=True).crossing


def test_one_read_of_the_usage_endpoint() -> None:
    async def main() -> None:
        with UsageDouble(TOKEN, offset=5) as double:
            context = SimpleNamespace(config=SimpleNamespace(anthropic_usage_url=double.url + USAGE_PATH))
            assert reader.client_of(context) is reader.default_client
            context.quota_options = QuotaOptions()
            assert reader.client_of(context) is reader.default_client
            found = await reader.read(context, TOKEN, T0)
            assert found is not None
            assert (found.readable, found.fetched_at, [w["window"] for w in found.windows][:2]) == (
                True,
                T0,
                ["five_hour", "seven_day"],
            )
            assert double.requests[-1][1]["anthropic-beta"] == reader.OAUTH_BETA
            assert await reader.read(context, "wrong-token", T0) == Reading(readable=False, fetched_at=T0)
            double.queue(USAGE_PATH, 500, {})
            assert await reader.read(context, TOKEN, T0) is None
            double.queue(USAGE_PATH, 200, b"not json")
            assert await reader.read(context, TOKEN, T0) is None
            double.queue(USAGE_PATH, 200, {"five_hour": "x"})
            assert await reader.read(context, TOKEN, T0) is None
            double.queue(USAGE_PATH, 200, usage_document(five=100.0))
            full = await reader.read(context, TOKEN, T0)
            assert full is not None
            assert limits.full_until(full.windows, T0) == AT + HOUR

            def broken() -> httpx.AsyncClient:
                return httpx.AsyncClient(transport=httpx.MockTransport(fail))

            def fail(_request: httpx.Request) -> httpx.Response:
                raise httpx.ConnectError("down")

            context.quota_options = QuotaOptions(client=broken)
            assert await reader.read(context, TOKEN, T0) is None

    asyncio.run(main())
