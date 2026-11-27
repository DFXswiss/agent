"""U-RUN-model (KOR-75, ADR-103 section 1): towards a hub whose device state has `subscriptions.api >= 2` a `usage`
item names the Claude subscription record that served the request (`model.served`): the owner's own primary record,
the record of a shared pool entry `sub-<id>`, and nothing for a token file of the operator, for a session that was
provisioned from the pool once, or for any other access; an older hub gets no `subscription_id`."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from psycopg.types.json import Jsonb

from agent_runner.crypto.ids import uuid7
from agent_runner.model import served
from agent_runner.sessions.records import LocalSession

from ..model.kit import ALICE, FLASH, Env
from ..sessions.kit import session_id
from .test_usage import TOKENS, consume, immediate_usage, record, run, started

USAGE_API: dict[str, Any] = {"subscriptions": {"api": 2, "shared": []}}
RECORD = "01930050-0000-7000-8000-000000000050"
__all__ = ["immediate_usage"]  # one `usage` item per record (KOR-35): the autouse fixture of test_usage.py


def _session(model_access: str | None = "subscription", owner: str | None = ALICE) -> LocalSession:
    return LocalSession(
        id=uuid7(),
        task_id=uuid7(),
        org_id=uuid7(),
        wg_id=uuid7(),
        vm_id=None,
        parent_id=None,
        depth=0,
        state="working",
        claude_session_id=None,
        model_access=model_access,
        model_id=None,
        owner_id=owner,
        created_at=datetime(2026, 11, 26, tzinfo=UTC),
        ready_at=None,
        ended_at=None,
    )


def test_api_needs_version_two() -> None:
    assert served.api(USAGE_API)
    assert not served.api({"subscriptions": {"api": 1}})
    assert not served.api({"subscriptions": {"api": True}})
    assert not served.api({"subscriptions": []})
    assert not served.api({})
    assert not served.api(None)


def test_usage_names_the_own_subscription(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        found, reader = await started(env, "subscription")
        found.session_env.desired = USAGE_API
        sid = session_id()
        found.guest.emit(sid, "usage", record(FLASH, **TOKENS))
        await consume(found, reader)
        found.session_env.desired = None
        found.guest.emit(sid, "usage", record(FLASH, **TOKENS))
        await consume(found, reader)
        async with env.pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT record FROM runner.credential_cache WHERE kind = 'claude_subscription' AND owner_id = %s",
                (ALICE,),
            )
            row = await cursor.fetchone()
        assert row is not None
        items = await found.of_kind("usage")
        assert [item.get("subscription_id", "absent") for item in items] == [row[0]["header"]["id"], "absent"]
        await env.sync.pusher.drain()  # the hub checks the items against contracts/outbox/usage.json

    run(store_dsn, body)


def test_usage_of_another_access_names_no_subscription(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        found, reader = await started(env, "platform")
        found.session_env.desired = USAGE_API
        found.guest.emit(session_id(), "usage", record(FLASH, **TOKENS))
        await consume(found, reader)
        assert [item["subscription_id"] for item in await found.of_kind("usage")] == [None]

    run(store_dsn, body)


def test_the_subscription_of_a_session(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        async with env.pool.connection() as conn:
            assert await served.subscription(conn, _session("platform")) is None
            # a shared pool entry, a malformed one and an operator token file
            for name, expected in ((f"sub-{RECORD}", RECORD), ("sub-x", None), ("team", None)):
                session = _session()
                await conn.execute(
                    "INSERT INTO runner.operator_binding (session_id, file_name) VALUES (%s, %s)", (session.id, name)
                )
                assert await served.subscription(conn, session) == expected
            # provisioned from the pool once, the binding ended
            session = _session()
            await conn.execute("INSERT INTO runner.operator_origin (session_id) VALUES (%s)", (session.id,))
            assert await served.subscription(conn, session) is None
            # the own subscription: nothing cached, a malformed record, then the record's ID
            assert await served.subscription(conn, _session(owner=None)) is None
            assert await served.subscription(conn, _session()) is None
            for cached, expected in (({"header": "x"}, None), ({"header": {"id": RECORD}}, RECORD)):
                await conn.execute(
                    "INSERT INTO runner.credential_cache (owner_id, kind, version, record) VALUES (%s, %s, 1, %s)"
                    " ON CONFLICT (owner_id, kind) DO UPDATE SET record = EXCLUDED.record",
                    (ALICE, "claude_subscription", Jsonb(cached)),
                )
                assert await served.subscription(conn, _session()) == expected

    run(store_dsn, body)
