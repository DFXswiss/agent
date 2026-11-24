"""U-RUN-billing (usage): towards a hub with `billing.api` the `usage` item names the model, the access origin
(`platform`, `operator`, `own`; AUD-04 W10) and the request time; towards an older hub it stays as it was (KOR-31,
ADR-062 section 3)."""

from __future__ import annotations

import pytest

from ..model.kit import FLASH, OPUS, Env
from ..model.test_usage import TOKENS, consume, record, run, started
from ..sessions.kit import session_id

BILLING = {"billing": {"api": 1, "active": True, "orgs": {}}}


@pytest.mark.parametrize(
    ("preference", "model", "operator", "origin"),
    [
        ("platform", FLASH, False, "platform"),
        ("api_key", OPUS, False, "own"),
        ("subscription", OPUS, False, "own"),
        ("subscription", OPUS, True, "operator"),
    ],
)
def test_the_origin_is_reported_to_a_billing_hub(
    store_dsn: str, *, preference: str, model: str, operator: bool, origin: str
) -> None:
    async def body(env: Env) -> None:
        found, reader = await started(env, preference)
        found.session_env.desired = BILLING
        sid = session_id()
        if operator:
            async with env.pool.connection() as conn:
                # the binding may already be pruned (the session ended): the kept origin counts
                await conn.execute("INSERT INTO runner.operator_origin (session_id) VALUES (%s)", (sid,))
        found.guest.emit(sid, "usage", record(model, **TOKENS))
        found.guest.emit(sid, "usage", record(model, **TOKENS), ts="not a time")
        await consume(found, reader)
        first, second = await found.of_kind("usage")
        assert (first["model"], first["access_origin"], first["at"]) == (model, origin, "2026-11-21T12:00:01.000Z")
        assert "at" not in second
        await env.sync.pusher.drain()  # the hub checks the items against contracts/outbox/usage.json

    run(store_dsn, body)


def test_an_older_hub_gets_no_billing_fields(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        found, reader = await started(env, "platform")
        found.guest.emit(session_id(), "usage", record(FLASH, **TOKENS))
        await consume(found, reader)
        (item,) = await found.of_kind("usage")
        assert not {"model", "access_origin", "at"} & set(item)

    run(store_dsn, body)
