"""U-RUN-credentials: the credential cache (05-runner.md section 2.6, 04-crypto.md section 4.4, 03-api.md
section 6.1)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from agent_runner.credentials import cache
from agent_runner.credentials.cache import CredentialUnavailable, RecordRefused
from agent_runner.credentials.hints import HANDLERS as HINTS
from agent_runner.credentials.reconcile import STEPS
from agent_runner.orgs import audit
from agent_runner.reconcile.wire import ResponseInvalid
from agent_runner.sync.http import HubRefusal

from .kit import ALICE, BOB, Env, github_body, kit_env, make_record, pid, seal_request

Body = Callable[[Env], Awaitable[None]]


def run(dsn: str, body: Body, **kwargs: Any) -> None:
    async def main() -> None:
        async with kit_env(dsn, **kwargs) as env:
            await body(env)

    asyncio.run(main())


def test_fetch_verifies_and_caches_the_record_and_opened_decrypts_it_only_for_the_block(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        record = make_record(github_body())
        env.hub.records[(ALICE, "github")] = record
        await env.add_ck()
        assert await cache.fetch(env.context, ALICE, "github") == record
        rows = await env.cache_rows()
        assert [(owner, kind, version) for owner, kind, version, _r in rows] == [(ALICE, "github", 1)]
        assert rows[0][3] == record  # the record as the hub stores it: encrypted under CK
        async with cache.opened(env.context, ALICE, "github") as opened:
            assert opened["token"] == "double-user-token"
            kept = opened
        assert kept == {}  # wiped after use
        assert "double-user-token" not in env.log()

    run(store_dsn, body)


def test_opened_fetches_a_missing_record_on_demand(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        env.hub.records[(ALICE, "github")] = make_record(github_body())
        await env.add_ck()
        async with cache.opened(env.context, ALICE, "github") as opened:
            assert opened["login"] == "octo-user"
        assert len(await env.cache_rows()) == 1
        targets = [r.target for r in env.hub.requests if "/credentials/" in r.target]
        assert targets == [f"/v1/device/credentials/{ALICE}/github"]

    run(store_dsn, body)


def test_a_deleted_record_is_purged_and_the_listeners_are_told(store_dsn: str) -> None:
    told: list[tuple[str, str]] = []

    async def listener(owner: str, kind: str) -> None:
        told.append((owner, kind))

    async def broken(_owner: str, _kind: str) -> None:
        raise RuntimeError("listener")

    async def body(env: Env) -> None:
        env.hub.records[(ALICE, "github")] = make_record(github_body())
        await cache.fetch(env.context, ALICE, "github")
        cache.subscribe(env.context, broken)
        cache.subscribe(env.context, listener)
        del env.hub.records[(ALICE, "github")]
        assert await cache.fetch(env.context, ALICE, "github") is None
        assert await env.cache_rows() == []
        assert told == [(ALICE, "github")]
        assert f"runner.credentials purged owner={ALICE} kind=github" in env.log()
        assert "listener_failed error=RuntimeError" in env.log()
        # nothing cached: a second 404 purges nothing and tells nobody
        assert await cache.purge(env.context, ALICE, "github") is False
        assert told == [(ALICE, "github")]

    run(store_dsn, body)


@pytest.mark.parametrize(
    ("make", "code"),
    [
        (lambda: {**make_record(github_body()), "sig": make_record(github_body())["sig"]}, "signature_invalid"),
        (lambda: make_record(github_body(), owner=BOB, key=b"\x01" * 32), "header_invalid"),
        (lambda: make_record(github_body(), author="bob"), "wrap_unauthorized"),
    ],
)
def test_a_record_that_fails_verification_is_refused_not_cached_and_counted(
    store_dsn: str, make: Callable[[], Any], code: str
) -> None:
    async def body(env: Env) -> None:
        env.hub.records[(ALICE, "github")] = make()
        before = audit.counters(env.context)[audit.VERIFY_FAILURES]
        with pytest.raises(RecordRefused) as refused:
            await cache.fetch(env.context, ALICE, "github")
        assert refused.value.code == code
        assert await env.cache_rows() == []
        assert audit.counters(env.context)[audit.VERIFY_FAILURES] == before + 1
        assert f"record_refused owner={ALICE} kind=github code={code}" in env.log()
        # audited like every refused inbound item (KOR-07; contract source `credential`), metadata only
        assert await env.audits() == [
            {
                "action": "runner.inbound_rejected",
                "result": "rejected",
                "target_kind": "credential",
                "target_id": f"{ALICE}:github",
                "detail": {"source": "credential", "code": code},
            }
        ]

    run(store_dsn, body)


@pytest.mark.parametrize("record", [{"header": {"v": 2}, "ct": "x", "sig": "y"}, None])
def test_a_malformed_record_is_refused(store_dsn: str, record: object) -> None:
    async def body(env: Env) -> None:
        env.hub.scripted.append((200, {"record": record}, {}))  # past the hub's contract check
        with pytest.raises(RecordRefused) as refused:
            await cache.fetch(env.context, ALICE, "github")
        assert refused.value.code == "header_invalid"

    run(store_dsn, body)


def test_a_record_of_an_owner_outside_the_acting_orgs_has_no_known_author_key(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        zed = pid("zed")
        env.hub.records[(zed, "github")] = make_record(github_body(), owner=zed, author="zed")
        with pytest.raises(RecordRefused) as refused:
            await cache.fetch(env.context, zed, "github")
        assert refused.value.code == "key_unknown"

    run(store_dsn, body)


def test_a_record_written_by_this_device_verifies_with_the_device_key(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        env.hub.records[(ALICE, "github")] = make_record(github_body(), author="rdev")
        await env.add_ck()
        async with cache.opened(env.context, ALICE, "github") as opened:
            assert opened["login"] == "octo-user"

    run(store_dsn, body)


def test_an_older_version_than_the_cached_one_is_refused_as_a_rollback(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        env.hub.records[(ALICE, "github")] = make_record(github_body(), version=2)
        await cache.fetch(env.context, ALICE, "github")
        env.hub.records[(ALICE, "github")] = make_record(github_body(), version=1)
        with pytest.raises(RecordRefused) as refused:
            await cache.fetch(env.context, ALICE, "github")
        assert refused.value.code == "version_conflict"
        assert [row[2] for row in await env.cache_rows()] == [2]

    run(store_dsn, body)


def test_refresh_owner_reads_every_kind_and_continues_after_a_failure(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        env.hub.records[(ALICE, "github")] = make_record(github_body())
        env.hub.records[(ALICE, "mailbox")] = make_record({"x": 1}, kind="mailbox", author="bob")
        env.hub.get_refusals.append((403, {"error": "forbidden"}))
        await cache.refresh_owner(env.context, ALICE)
        targets = [r.target.rsplit("/", 1)[1] for r in env.hub.requests if "/credentials/" in r.target]
        assert targets == ["anthropic_api_key", "claude_subscription", "github", "mailbox"]
        assert "refresh_failed owner=" + ALICE + " kind=anthropic_api_key error=HubRefusal code=forbidden" in env.log()
        assert "kind=mailbox error=RecordRefused code=wrap_unauthorized" in env.log()
        assert [row[1] for row in await env.cache_rows()] == ["github"]
        env.hub.tamper.append("bad_signature")
        await cache.refresh_owner(env.context, ALICE)
        assert "kind=anthropic_api_key error=HubTransportError" in env.log()

    run(store_dsn, body)


def test_opened_refuses_what_cannot_be_used(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        with pytest.raises(CredentialUnavailable) as missing:
            async with cache.opened(env.context, ALICE, "github"):
                pass
        assert missing.value.reason == "missing"
        env.hub.records[(ALICE, "github")] = make_record(github_body())
        with pytest.raises(CredentialUnavailable) as no_key:
            async with cache.opened(env.context, ALICE, "github"):
                pass
        assert no_key.value.reason == "key_missing"
        await env.add_ck(key=b"\x02" * 32)
        with pytest.raises(CredentialUnavailable) as wrong_key:
            async with cache.opened(env.context, ALICE, "github"):
                pass
        assert wrong_key.value.reason == "invalid"

    run(store_dsn, body)


def test_opened_refuses_a_body_that_is_not_an_object_and_a_cached_record_that_no_longer_verifies(
    store_dsn: str,
) -> None:
    async def body(env: Env) -> None:
        await env.add_ck()
        env.hub.records[(ALICE, "mailbox")] = make_record([1, 2], kind="mailbox")  # type: ignore[arg-type]
        with pytest.raises(CredentialUnavailable) as not_object:
            async with cache.opened(env.context, ALICE, "mailbox"):
                pass
        assert not_object.value.reason == "invalid"
        forged = make_record(github_body(), author="bob")
        async with env.pool.connection() as conn:
            await cache._store(conn, ALICE, "github", 1, forged)
        with pytest.raises(CredentialUnavailable) as stale:
            async with cache.opened(env.context, ALICE, "github"):
                pass
        assert stale.value.reason == "invalid"

    run(store_dsn, body)


def test_put_creates_then_updates_the_record_as_this_device(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        request_id = env.hub.add_request(seal_request("github.connect", {}))
        await env.add_ck()
        assert await cache.put(env.context, ALICE, "github", github_body(), request_id) == 1
        first = env.hub.records[(ALICE, "github")]
        assert first["header"]["author"] == pid("rdev")
        assert await cache.put(env.context, ALICE, "github", github_body(token="t2"), request_id) == 2
        second = env.hub.records[(ALICE, "github")]
        assert second["header"]["id"] == first["header"]["id"]
        async with cache.opened(env.context, ALICE, "github") as opened:
            assert opened["token"] == "t2"
        assert [row[2] for row in await env.cache_rows()] == [2]
        assert "t2" not in env.log()

    run(store_dsn, body)


def test_put_uses_the_newest_ck_epoch_and_retries_a_version_conflict(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        request_id = env.hub.add_request(seal_request("github.connect", {}))
        await env.add_ck(epoch=1)
        await env.add_ck(epoch=2)
        env.hub.ck_epoch = 2
        env.hub.put_refusals.append((409, {"error": "version_conflict", "detail": {"expected": 0}}))
        assert await cache.put(env.context, ALICE, "github", github_body(), request_id) == 1
        assert env.hub.records[(ALICE, "github")]["header"]["epoch"] == 2
        puts = [r for r in env.hub.requests if r.method == "PUT"]
        assert len(puts) == 2

    run(store_dsn, body)


@pytest.mark.parametrize(
    ("refusals", "reason", "code"),
    [
        ([(409, {"error": "version_conflict"})] * 3, "refused", "version_conflict"),
        ([(400, {"error": "epoch_stale"})], "refused", "epoch_stale"),
        ([(200, {"version": True})], "refused", "response_invalid"),
    ],
)
def test_put_reports_refusals(store_dsn: str, refusals: list[tuple[int, Any]], reason: str, code: str) -> None:
    async def body(env: Env) -> None:
        request_id = env.hub.add_request(seal_request("github.connect", {}))
        await env.add_ck()
        env.hub.put_refusals.extend(refusals)
        with pytest.raises(CredentialUnavailable) as failed:
            await cache.put(env.context, ALICE, "github", github_body(), request_id)
        assert (failed.value.reason, failed.value.code) == (reason, code)

    run(store_dsn, body)


def test_put_needs_a_ck_but_not_a_verifiable_current_record(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        request_id = env.hub.add_request(seal_request("github.connect", {}))
        with pytest.raises(CredentialUnavailable) as no_key:
            await cache.put(env.context, ALICE, "github", github_body(), request_id)
        assert no_key.value.reason == "key_missing"
        await env.add_ck()
        # written by a device that was removed since: its author no longer verifies, a reconnect still replaces it
        stale = make_record(github_body(), author="bob", version=3)
        env.hub.records[(ALICE, "github")] = stale
        assert await cache.put(env.context, ALICE, "github", github_body(token="new"), request_id) == 4
        assert env.hub.records[(ALICE, "github")]["header"]["id"] == stale["header"]["id"]
        env.hub.scripted.append((200, {"record": {"header": {"v": 9}}}, {}))
        with pytest.raises(CredentialUnavailable) as invalid:
            await cache.put(env.context, ALICE, "github", github_body(), request_id)
        assert invalid.value.reason == "invalid"
        env.hub.get_refusals.append((403, {"error": "forbidden"}))
        with pytest.raises(HubRefusal):
            await cache.put(env.context, ALICE, "github", github_body(), request_id)

    run(store_dsn, body)


def test_a_device_that_is_not_enrolled_has_no_credentials(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        env.context.identity = None
        env.context.sync_runtime = None
        with pytest.raises(CredentialUnavailable) as unenrolled:
            await cache.fetch(env.context, ALICE, "github")
        assert unenrolled.value.reason == "unenrolled"

    run(store_dsn, body, verified=False)


def test_the_credentials_hint_refreshes_the_owner_and_drops_a_malformed_one(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        env.hub.records[(ALICE, "github")] = make_record(github_body())
        await HINTS["credentials"](env.context, {"type": "credentials", "owner_id": "not-a-uuid"})
        assert "runner.credentials hint_invalid" in env.log()
        await HINTS["credentials"](env.context, {"type": "credentials", "owner_id": ALICE})
        assert [row[1] for row in await env.cache_rows()] == ["github"]

    run(store_dsn, body)


def test_the_reconcile_step_refreshes_every_changed_owner(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        env.hub.records[(ALICE, "github")] = make_record(github_body())
        await cache.fetch(env.context, ALICE, "github")
        del env.hub.records[(ALICE, "github")]
        env.hub.changed = [ALICE]
        (step,) = STEPS
        assert (step.order, step.name) == (70, "credentials")
        await step.run(await env.desired())
        assert await env.cache_rows() == []
        run_without = await env.desired()
        run_without.desired = None
        await step.run(run_without)
        broken = await env.desired()
        broken.desired = {"credentials_changed": ["x"]}
        with pytest.raises(ResponseInvalid):
            await step.run(broken)

    run(store_dsn, body)


def test_opened_refuses_a_record_fetched_on_demand_that_does_not_verify(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        await env.add_ck()
        env.hub.records[(ALICE, "github")] = make_record(github_body(), author="bob")
        with pytest.raises(CredentialUnavailable) as refused:
            async with cache.opened(env.context, ALICE, "github"):
                pass
        assert refused.value.reason == "invalid"

    run(store_dsn, body)


def test_the_cache_never_goes_back_to_an_older_version(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        newer, older = make_record(github_body(), version=3), make_record(github_body(), version=2)
        async with env.pool.connection() as conn:
            await cache._store(conn, ALICE, "github", 3, newer)
            await cache._store(conn, ALICE, "github", 2, older)  # a concurrent, slower fetch
        assert [row[2] for row in await env.cache_rows()] == [3]

    run(store_dsn, body)
