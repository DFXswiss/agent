"""U-RUN-model (KOR-50, ADR-080 section 2): a subscription access is found used up from the gateway's `usage`
records - confirmed by the provider's readable quota at the first `429`, else by a streak of three `429` within five
minutes -, the DFX key of an org device by a `402`; the mark of the owner's own subscription reaches the hub as
`quota.limited`.

Real session starts against the fake guest and the credential hub, the records through the real journal handlers,
the usage endpoint is the local double (one quota document per token)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent_runner.model import exhaustion, limits, operator
from agent_runner.model.limits import EXHAUSTED, Limit
from agent_runner.quota.reader import Reading
from agent_runner.sync.outbox import open_item

from ..quota.double import Answer
from ..sessions.kit import desired, session_id
from .fallback_kit import AT, HOUR, T0, FKit, entry, fallback_kit, state, usage_document
from .kit import ALICE, TOKEN, model_env
from .test_operator import pool_dir, token

OWN = limits.own_key(ALICE)
REFUSED = Answer(401, b'{"error":{"type":"authentication_error"}}')
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


def test_a_full_readable_quota_marks_the_own_subscription_at_the_first_429(store_dsn: str) -> None:
    async def body(found: FKit) -> None:
        assert (await found.start(model_access_pref="subscription")).started
        found.quotas.documents[TOKEN] = usage_document(five=100.0)
        found.emit(429)
        await found.consume()
        assert await found.limit(OWN) == (EXHAUSTED, AT + HOUR, True, None)
        assert f"runner.model access_exhausted key={OWN}" in found.log()
        [item] = await items(found, "quota")
        assert item["owner_id"] == ALICE
        assert item["windows"][0]["used_percent"] == 100.0
        assert (item["limited"]["at"], item["limited"]["readable"]) == ("2026-11-18T11:06:40.000Z", True)
        # the endpoint is read at most once a minute per access; a success ends nothing that is marked
        reads = len(found.quotas.requests)
        found.emit(429)
        found.emit(200)
        await found.consume()
        assert len(found.quotas.requests) == reads
        await found.env.sync.pusher.drain()  # the contract-checking hub takes the item

    run(store_dsn, None, body, desired_state=state(entry(own_to_dfx=False)))


def test_without_a_readable_quota_a_streak_of_three_marks_it_until_the_probe_time(store_dsn: str) -> None:
    async def body(found: FKit) -> None:
        assert (await found.start(model_access_pref="subscription")).started
        found.quotas.documents[TOKEN] = REFUSED  # a setup token the usage endpoint does not accept (KOR-40)
        found.emit(429)
        found.emit(429)
        found.emit(200)  # a success in between: the streak starts again
        found.emit(429)
        await found.consume()
        assert await found.limit(OWN) is None
        found.wall.now += limits.CHECK_S
        found.emit(429)
        await found.consume()
        assert await found.limit(OWN) is None
        found.emit(429)
        await found.consume()
        now = found.wall.now
        assert await found.limit(OWN) == (EXHAUSTED, now + limits.PROBE_S, False, None)
        [item] = await items(found, "quota")
        assert (item["unavailable"], item["limited"]["readable"]) == (True, False)
        # marked already: a further streak marks nothing
        for _ in range(3):
            found.emit(429)
        await found.consume()
        assert await found.limit(OWN) == (EXHAUSTED, now + limits.PROBE_S, False, None)
        await found.env.sync.pusher.drain()

    run(store_dsn, None, body)


def test_an_older_hub_gets_no_limited_item_and_a_reading_below_100_percent_waits_for_the_streak(
    store_dsn: str,
) -> None:
    async def body(found: FKit) -> None:
        assert (await found.start(model_access_pref="subscription")).started
        for _ in range(3):
            found.emit(429)
        await found.consume()  # the quota reads 10 %: the figures lag, the streak decides
        assert (await found.limit(OWN) or ())[2:3] == (False,)
        assert await items(found, "quota") == []

    run(store_dsn, None, body, desired_state=state(api=False))


def test_an_operator_file_is_marked_from_its_own_quota_and_the_streak_counts_against_the_session(
    store_dsn: str, tmp_path: Path
) -> None:
    async def body(found: FKit) -> None:
        assert (await found.start()).started  # no own record: the pool
        found.clock.now += operator.GRACE_S
        assert found.pool.bindings == {session_id(): "a"}
        found.quotas.documents[token(1)] = usage_document(five=100.0, five_resets=AT + 2 * HOUR)
        found.emit(429)
        await found.consume()
        key = limits.operator_key("a")
        assert await found.limit(key) == (EXHAUSTED, AT + 2 * HOUR, True, None)
        assert found.pool.state("a") == operator.EXHAUSTED
        assert found.pool.quota["a"] == 100.0
        assert found.pool.choose() == "b"
        # a refusal of the usage endpoint forgets the room of the file
        exhaustion.note_quota(found.context, key, Reading(readable=False, fetched_at=T0))
        assert "a" not in found.pool.quota
        exhaustion.note_quota(found.context, limits.own_key(ALICE), Reading(readable=False))

    run(store_dsn, tmp_path, body, own=False)


def test_an_operator_streak_of_the_session_that_deferred_the_file_marks_it(store_dsn: str, tmp_path: Path) -> None:
    async def body(found: FKit) -> None:
        assert (await found.start()).started
        found.clock.now += operator.GRACE_S
        found.quotas.documents[token(1)] = REFUSED
        for _ in range(3):
            found.emit(429)
        await found.consume()
        assert found.pool.state("a") == operator.EXHAUSTED
        assert (await found.limit(limits.operator_key("a")) or ())[2] is False
        assert "operator_file_deferred file=a" in "\n".join(found.lines)

    run(store_dsn, tmp_path, body, own=False)


def test_a_402_of_the_dfx_key_of_an_org_device_marks_it_for_two_minutes(store_dsn: str) -> None:
    async def body(found: FKit) -> None:
        assert (await found.start(model_access_pref="platform")).started
        found.emit(402)
        found.emit(429)  # the DFX key's 429 is a rate limit, never a mark
        await found.consume()
        key = limits.dfx_key(found.kit.session_env.device_id)
        assert await found.limit(key) == (EXHAUSTED, T0 + limits.DFX_KEY_S, False, None)
        assert f"access_exhausted key={key}" in found.log()

    run(store_dsn, None, body, device_kind="org", key="dfx-key-SECRETMARKER-0123456789abcdef", own=False)


def test_accesses_that_cannot_run_out_and_records_without_context_change_nothing(store_dsn: str) -> None:
    async def body(found: FKit) -> None:
        assert (await found.start(model_access_pref="platform")).started  # the platform key of a platform device
        found.emit(429)
        found.emit(402)
        await found.consume()
        assert await found.env.rows("SELECT * FROM runner.access_limit") == []
        session = await found.stored(session_id())
        assert exhaustion.access_key(found.kit.session_env, session, None) is None
        env = SimpleNamespace(context=None)
        exhaustion.observe(env, 429, OWN)
        exhaustion.observe(found.kit.session_env, "429", OWN)
        exhaustion.observe(found.kit.session_env, 429, None)
        assert exhaustion.watch_for(found.context).streaks == {}

    run(store_dsn, None, body)


def test_tokens_quota_items_and_failing_checks(store_dsn: str, tmp_path: Path) -> None:
    async def body(found: FKit) -> None:
        context = found.context
        assert await exhaustion.token_of(context, limits.own_key(desired().task_id)) is None  # no record
        assert await exhaustion.token_of(context, OWN) is not None
        found.pool.scan()
        assert await exhaustion.token_of(context, limits.operator_key("gone")) is None
        assert await exhaustion.token_of(context, limits.operator_key("a")) == token(1)
        limit = Limit(OWN, EXHAUSTED, None, False, T0)
        assert exhaustion.limited_body(limit) == {"at": "2026-11-18T11:06:40.000Z", "until": None, "readable": False}
        assert exhaustion.quota_body(ALICE, None, limit) is None
        assert exhaustion.quota_body(ALICE, Reading(True, (), T0), limit) is None
        offline = SimpleNamespace(context=SimpleNamespace(identity=None), desired=state())
        await exhaustion.report_own(offline, ALICE, limit)  # not enrolled: nothing to send

        async def broken() -> None:
            raise RuntimeError("check")

        exhaustion.schedule(context, broken())
        await exhaustion.settle(context)
        assert "runner.model exhaustion_check_failed error=RuntimeError" in found.log()
        assert exhaustion.watch_for(context) is exhaustion.watch_for(context)

    run(store_dsn, tmp_path, body)


def test_without_a_pool_an_operator_key_has_no_token(store_dsn: str) -> None:
    async def body(found: FKit) -> None:
        assert await exhaustion.token_of(found.context, limits.operator_key("a")) is None

    run(store_dsn, None, body)


def test_an_own_subscription_that_cannot_be_read_is_marked_by_its_streak_without_an_item(store_dsn: str) -> None:
    async def body(found: FKit) -> None:
        key = limits.own_key(desired().task_id)  # an owner without a record on this device: no token
        senv = found.kit.session_env
        for _ in range(3):
            exhaustion.observe(senv, 429, key)
        await exhaustion.settle(found.context)
        assert await found.limit(key) == (EXHAUSTED, T0 + limits.PROBE_S, False, None)
        assert await items(found, "quota") == []

    run(store_dsn, None, body)
