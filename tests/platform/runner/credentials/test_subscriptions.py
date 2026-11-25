"""U-RUN-credentials (KOR-69): Claude subscription records fetched by ID and listed per owner, verified like every
record of the owner, cached without rollback, purged on `404` or when the list no longer names them, written by a
login of this device (keeping name, note and use) and re-sealed under the newest epoch (ADR-090 sections 1 and 2,
03-api.md section 6.1)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

from agent_runner.credentials import subscriptions
from agent_runner.credentials.cache import CredentialUnavailable, RecordRefused
from agent_runner.sync.http import HubRefusal

from .kit import ALICE, BOB, Env, ck, kit_env, make_record, seal_request
from .subscription_kit import KIND, SubscriptionHub, subscription_body

Body = Callable[[Env, SubscriptionHub], Awaitable[None]]


def run(dsn: str, body: Body) -> None:
    async def main() -> None:
        async with kit_env(dsn) as env:
            await body(env, SubscriptionHub(env.hub).install())

    asyncio.run(main())


def _login_request(env: Env, user: str = "alice") -> str:
    return env.hub.add_request(seal_request("claude.login", {"mode": "manual"}, user=user))


def test_a_record_is_verified_cached_opened_and_purged(store_dsn: str) -> None:
    async def body(env: Env, hub: SubscriptionHub) -> None:
        await env.add_ck()
        record_id = hub.add(subscription_body(name="Team"), owner=ALICE)
        found = await subscriptions.fetch(env.context, record_id)
        assert found is not None
        assert (found.id, found.owner, found.primary, found.version, found.epoch) == (record_id, ALICE, False, 1, 1)
        async with subscriptions.opened(env.context, found) as opened:
            assert opened["name"] == "Team"
            kept = opened
        assert kept == {}
        async with env.pool.connection() as conn:
            assert await subscriptions.cached(conn, record_id) == found
            assert await subscriptions.cached_of(conn, ALICE) == [found]
        del hub.served[record_id]
        assert await subscriptions.fetch(env.context, record_id) is None
        async with env.pool.connection() as conn:
            assert await subscriptions.cached(conn, record_id) is None
        assert await subscriptions.purge(env.context, record_id) is False
        assert f"runner.subscriptions purged id={record_id}" in env.log()
        hub.refusals[record_id] = (500, {"error": "internal"})
        with pytest.raises(HubRefusal):
            await subscriptions.fetch(env.context, record_id)

    run(store_dsn, body)


def test_a_refused_record_is_not_cached_and_audited(store_dsn: str) -> None:
    async def body(env: Env, hub: SubscriptionHub) -> None:
        await env.add_ck()
        # the header names another record
        other = hub.add(subscription_body(), owner=ALICE)
        record_id = "01930099-0000-7000-8000-000000000099"
        hub.served[record_id] = hub.served[other]
        with pytest.raises(RecordRefused, match="header_invalid"):
            await subscriptions.fetch(env.context, record_id)
        # the answer names no owner or no primary mark
        for answer in ({"owner_id": "x", "primary": False}, {"owner_id": ALICE, "primary": "no"}):
            hub.refusals[other] = (200, {**answer, "record": hub.served[other].record})
            with pytest.raises(RecordRefused, match="header_invalid"):
                await subscriptions.fetch(env.context, other)
        del hub.refusals[other]
        # a rollback below the cached version
        newer = hub.add(subscription_body(), owner=ALICE, version=2, record_id=other)
        assert (await subscriptions.fetch(env.context, newer)) is not None
        hub.add(subscription_body(), owner=ALICE, version=1, record_id=other)
        with pytest.raises(RecordRefused, match="version_conflict"):
            await subscriptions.fetch(env.context, other)
        assert f"record_refused id={other} code=version_conflict" in env.log()
        assert [item["action"] for item in await env.audits()].count("runner.inbound_rejected") == 4

    run(store_dsn, body)


def test_the_list_fetches_new_versions_and_purges_gone_records(store_dsn: str) -> None:
    async def body(env: Env, hub: SubscriptionHub) -> None:
        await env.add_ck()
        first = hub.add(subscription_body(), owner=ALICE)
        second = hub.add(subscription_body(), owner=ALICE)
        assert await subscriptions.sync_owner(env.context, ALICE) is True
        async with env.pool.connection() as conn:
            assert sorted(item.id for item in await subscriptions.cached_of(conn, ALICE)) == sorted([first, second])
        hub.asked.clear()
        assert await subscriptions.sync_owner(env.context, ALICE) is True
        assert hub.asked == []  # nothing newer
        del hub.served[first]
        hub.add(subscription_body(), owner=ALICE, version=2, record_id=second)
        hub.refusals[second] = (500, {"error": "internal"})
        assert await subscriptions.sync_owner(env.context, ALICE) is True
        assert f"fetch_failed id={second} error=HubRefusal code=internal" in env.log()
        async with env.pool.connection() as conn:
            assert [item.id for item in await subscriptions.cached_of(conn, ALICE)] == [second]
        del hub.refusals[second]
        hub.served[second] = hub.served[second].__class__(BOB, False, make_record(subscription_body(), owner=BOB))
        hub.refusals[second] = (500, {"error": "internal"})
        # a list naming another owner is unusable; a refused list and a transport error purge nothing
        hub.raw_lists[ALICE] = {"subscriptions": [{"id": second, "owner_id": BOB, "version": 1}]}
        assert await subscriptions.sync_owner(env.context, ALICE) is False
        del hub.raw_lists[ALICE]
        hub.list_refusals[ALICE] = (404, {"error": "not_found"})
        assert await subscriptions.sync_owner(env.context, ALICE) is False
        assert f"list_failed owner={ALICE} error=HubRefusal" in env.log()
        async with env.pool.connection() as conn:
            assert [item.id for item in await subscriptions.cached_of(conn, ALICE)] == [second]

    run(store_dsn, body)


def test_a_record_that_does_not_open_is_unavailable(store_dsn: str) -> None:
    async def body(env: Env, hub: SubscriptionHub) -> None:
        await env.add_ck()
        record_id = hub.add(subscription_body(), owner=ALICE)
        found = await subscriptions.fetch(env.context, record_id)
        assert found is not None
        broken = found.__class__(found.id, BOB, False, 1, found.record)  # verified against another owner
        with pytest.raises(CredentialUnavailable, match="invalid"):
            async with subscriptions.opened(env.context, broken):
                pass
        wrong_key = make_record(subscription_body(), owner=ALICE, kind=KIND, record_id=record_id, key=ck(BOB))
        with pytest.raises(CredentialUnavailable, match="invalid"):
            async with subscriptions.opened(env.context, found.__class__(record_id, ALICE, False, 1, wrong_key)):
                pass
        listed = make_record(["not", "an", "object"], owner=ALICE, kind=KIND, record_id=record_id)  # type: ignore[arg-type]
        with pytest.raises(CredentialUnavailable, match="invalid"):
            async with subscriptions.opened(env.context, found.__class__(record_id, ALICE, False, 1, listed)):
                pass

    run(store_dsn, body)


def test_a_login_creates_a_record_or_keeps_name_note_and_use(store_dsn: str) -> None:
    async def body(env: Env, hub: SubscriptionHub) -> None:
        await env.add_ck()
        request = _login_request(env)
        new_id = "01930099-0000-7000-8000-0000000000a1"
        fields = {"token": "sk-ant-oat01-new", "email": None, "kind": "setup_token", "name": "Spare"}
        assert await subscriptions.store_login(env.context, ALICE, new_id, fields, request) == 1
        assert hub.opened(new_id) == fields
        assert hub.puts[-1]["request_id"] == request
        use = {"pool": True, "orgs": []}
        existing = hub.add(subscription_body(token=None, name="Team", note="for reviews", use=use), owner=ALICE)
        login = {"token": "sk-ant-oat01-again", "email": "x@example.com", "kind": "setup_token", "note": "new"}
        assert await subscriptions.store_login(env.context, ALICE, existing, login, request) == 2
        assert hub.opened(existing) == {**login, "name": "Team", "use": use}
        async with env.pool.connection() as conn:
            cached = await subscriptions.cached(conn, existing)
        assert cached is not None
        assert cached.version == 2
        assert "sk-ant-oat01" not in env.log()

    run(store_dsn, body)


def test_a_login_never_writes_the_primary_or_anothers_record(store_dsn: str) -> None:
    async def body(env: Env, hub: SubscriptionHub) -> None:
        await env.add_ck()
        await env.add_ck(BOB)
        request = _login_request(env)
        primary = hub.add(subscription_body(), owner=ALICE, primary=True)
        fields = {"token": "t", "email": None, "kind": "setup_token"}
        with pytest.raises(CredentialUnavailable, match="refused") as caught:
            await subscriptions.store_login(env.context, ALICE, primary, fields, request)
        assert caught.value.code == "primary"
        theirs = hub.add(subscription_body(), owner=BOB, author="bob")
        with pytest.raises(CredentialUnavailable, match="refused") as caught:
            await subscriptions.store_login(env.context, ALICE, theirs, fields, request)
        assert caught.value.code == "owner"

    run(store_dsn, body)


def test_a_login_retries_a_concurrent_write_and_reports_refusals(store_dsn: str) -> None:
    async def body(env: Env, hub: SubscriptionHub) -> None:
        request = _login_request(env)
        record_id = "01930099-0000-7000-8000-0000000000a2"
        fields = {"token": "t", "email": None, "kind": "setup_token"}
        with pytest.raises(CredentialUnavailable, match="key_missing"):
            await subscriptions.store_login(env.context, ALICE, record_id, fields, request)
        await env.add_ck()
        conflict = (409, {"error": "version_conflict", "detail": {"expected": 0}})
        hub.put_refusals[:] = [conflict, conflict]
        assert await subscriptions.store_login(env.context, ALICE, record_id, fields, request) == 1
        hub.put_refusals[:] = [conflict, conflict, conflict]
        with pytest.raises(CredentialUnavailable) as caught:
            await subscriptions.store_login(env.context, ALICE, record_id, fields, request)
        assert (caught.value.reason, caught.value.code) == ("refused", "version_conflict")
        hub.put_refusals[:] = [(403, {"error": "forbidden"})]
        with pytest.raises(CredentialUnavailable) as caught:
            await subscriptions.store_login(env.context, ALICE, record_id, fields, request)
        assert caught.value.code == "forbidden"
        hub.put_refusals[:] = [(200, {"version": 0})]
        with pytest.raises(CredentialUnavailable) as caught:
            await subscriptions.store_login(env.context, ALICE, record_id, fields, request)
        assert caught.value.code == "response_invalid"

    run(store_dsn, body)


def test_a_record_under_an_older_epoch_is_resealed(store_dsn: str) -> None:
    async def body(env: Env, hub: SubscriptionHub) -> None:
        await env.add_ck()
        record_id = hub.add(subscription_body(name="Team"), owner=ALICE)
        found = await subscriptions.fetch(env.context, record_id)
        assert found is not None
        assert await subscriptions.reseal(env.context, found) is False  # already under the newest epoch
        await env.add_ck(epoch=2)
        env.hub.ck_epoch = 2
        primary = found.__class__(found.id, ALICE, True, found.version, found.record)
        assert await subscriptions.reseal(env.context, primary) is False
        hub.put_refusals[:] = [(409, {"error": "version_conflict", "detail": {"expected": 3}})]
        assert await subscriptions.reseal(env.context, found) is False
        hub.put_refusals[:] = [(403, {"error": "forbidden"})]
        assert await subscriptions.reseal(env.context, found) is False
        assert f"reseal_failed id={record_id} reason=refused" in env.log()
        assert await subscriptions.reseal(env.context, found) is True
        assert hub.opened(record_id) == subscription_body(name="Team")
        assert hub.served[record_id].record["header"]["epoch"] == 2
        assert "request_id" not in hub.puts[-1]
        assert f"resealed id={record_id} epoch=2" in env.log()
        broken = found.__class__(found.id, BOB, False, found.version, found.record)
        assert await subscriptions.reseal(env.context, broken) is False

    run(store_dsn, body)


def test_a_lost_answer_is_a_transport_error(store_dsn: str) -> None:
    async def body(env: Env, hub: SubscriptionHub) -> None:
        await env.add_ck()
        record_id = hub.add(subscription_body(), owner=ALICE)

        def lost(_method: str, _target: str, _body: Any) -> tuple[int, dict[str, Any]]:
            raise httpx.ReadError("connection lost")

        hub.hub.route = lost  # type: ignore[method-assign,assignment]
        assert await subscriptions.sync_owner(env.context, ALICE) is False
        assert f"list_failed owner={ALICE}" in env.log()
        del record_id

    run(store_dsn, body)
