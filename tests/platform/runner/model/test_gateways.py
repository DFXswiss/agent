"""U-RUN-model: renewal and withdrawal of the gateway credentials of running sessions, budget refusal of new work
(05-runner.md sections 2.6, 10.2, 13; 03-api.md sections 6.1 and 7.1).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from agent_runner.credentials import cache
from agent_runner.crypto.ids import uuid7
from agent_runner.guestlink import cursor
from agent_runner.guestlink.errors import GuestRefused, GuestUnreachable
from agent_runner.model import access, gateways, uncounted
from agent_runner.model.access import provisions
from agent_runner.model.reconcile import STEPS
from agent_runner.model.uncounted import usd_of
from agent_runner.orgs.book import book_for
from agent_runner.reconcile.engine import Run
from agent_runner.sessions import records
from agent_runner.sessions.start import Starter
from agent_runner.vm import records as vm_records

from ..sessions.kit import desired, session_id
from ..sync.fakehub import SCHEMAS
from .kit import (
    API_KEY,
    Env,
    Kit,
    api_key_body,
    attach,
    credentials,
    model_env,
    model_kit,
    store_record,
    subscription_body,
)

Body = Callable[[Env], Awaitable[None]]
NEW_KEY = "test-api-key-NEWKEY-0123456789abcdef"


def run(dsn: str, body: Body) -> None:
    async def main() -> None:
        async with model_env(dsn) as env:
            await body(env)

    asyncio.run(main())


async def running(env: Env, preference: str, **kit_options: Any) -> Kit:
    store_record(env, "anthropic_api_key", api_key_body())
    store_record(env, "claude_subscription", subscription_body())
    found = await model_kit(env, **kit_options)
    assert (await Starter(found.session_env).start(desired(model_access_pref=preference))).started
    found.guest.calls.clear()
    return found


async def step(found: Kit) -> None:
    run_ = Run(context=found.env.context, sync=found.env.sync, book=book_for(found.env.context))
    run_.desired = dict(found.session_env.desired or {})
    await STEPS[0].run(run_)


def test_a_new_record_version_is_provisioned_again_and_an_unchanged_one_is_not(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        found = await running(env, "api_key")
        attach(found)
        await step(found)
        assert credentials(found) == []  # nothing changed since the start
        store_record(env, "anthropic_api_key", api_key_body(NEW_KEY), version=2)
        await cache.refresh_owner(env.context, desired().owner_id)
        await step(found)
        [(sid, credential)] = credentials(found)
        assert (sid, credential["auth_value"], credential["access"]) == (session_id(), NEW_KEY, "api_key")
        assert provisions(env.context).stamps[sid].version == 2
        assert f"credential_renewed session_id={sid} access=api_key" in "\n".join(found.lines)
        await step(found)
        assert len(credentials(found)) == 1

    run(store_dsn, body)


def test_a_changed_platform_budget_reaches_the_running_gateway(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        org = desired().org_id
        found = await running(env, "platform")
        attach(found)
        found.session_env.desired = {"platform_budget": {"usd_per_org_day": 5, "spent_today_by_org": {org: 1.25}}}
        await step(found)
        [(_sid, credential)] = credentials(found)
        assert credential["budget"] == {"usd_per_day": 5.0, "spent_usd": 1.25}
        await step(found)
        assert len(credentials(found)) == 1  # same budget: no new provisioning

    run(store_dsn, body)


def _usage(session: str, micro: int, basis: str = "provider") -> dict[str, Any]:
    counts = dict.fromkeys(("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"), 1)
    delta = {"requests": 1, **counts, "thinking_tokens": 0, "cost_micro_usd": micro}
    return {
        "session_id": session,
        "delta": delta,
        "cost_basis": basis,
        "context_used_tokens": 3,
        "context_window_tokens": 128000,
        "last_provider": None,
    }


async def _other_session(env: Env, org: str, access: str | None) -> str:
    other = str(uuid7())
    async with env.pool.connection() as conn, conn.transaction():
        await records.insert(
            conn, session_id=other, task_id=str(uuid7()), org_id=org, wg_id=str(uuid7()),
            parent_id=None, depth=0, owner_id=str(uuid7()), created_at=datetime.now(UTC),
        )  # fmt: skip
        await records.update(conn, other, model_access=access)
    return other


def test_the_budget_adds_the_cost_the_hub_has_not_counted_and_names_the_journal_position(store_dsn: str) -> None:
    """KOR-10 (AUD-02 NA-10): the hub's amount contains this device's platform `usage` items up to `last_origin_seq`
    of the same device state. The items above it that the hub will count for the organization are added (platform
    access, or no known access and provider cost); items of other organizations, of own-credential sessions and rows
    that do not open are not. `journal_through` names the journal position of the session's VM (0 without one). The
    sum is made in micro-USD like the hub's, so an item that the hub counts meanwhile leaves the same number, and
    within one hub day the amount never falls."""

    async def body(env: Env) -> None:
        org = desired().org_id
        found = await running(env, "platform")
        attach(found)
        sid = session_id()
        stored = await found.stored(sid)
        assert stored.vm_id is not None
        foreign = await _other_session(env, str(uuid7()), "platform")
        own_key = await _other_session(env, org, "api_key")
        unknown = await _other_session(env, org, None)
        async with env.pool.connection() as conn, conn.transaction():
            counted = await env.sync.enqueue(conn, "usage", _usage(sid, 100_000))
            first = await env.sync.enqueue(conn, "usage", _usage(sid, 250_000))
            await env.sync.enqueue(conn, "usage", _usage(foreign, 4_000_000))
            await env.sync.enqueue(conn, "usage", _usage(own_key, 3_000_000, "list_price"))
            await env.sync.enqueue(conn, "usage", _usage(unknown, 50_000))
            await env.sync.enqueue(conn, "usage", _usage(unknown, 70_000, "list_price"))
            broken = await env.sync.enqueue(conn, "usage", _usage(sid, 9_000_000))
            last = await env.sync.enqueue(conn, "usage", _usage(sid, 500_000))
            await conn.execute("UPDATE runner.outbox SET body_ct = 'x'::bytea WHERE origin_seq = %s", (broken,))
        day = "2026-11-22T12:00:00.000Z"
        budget = {"usd_per_org_day": 5, "spent_today_by_org": {org: 1.0}}
        found.session_env.desired = {"last_origin_seq": counted, "platform_budget": budget, "hub_time": day}
        await step(found)
        [(_sid, credential)] = credentials(found)
        assert credential["budget"] == {"usd_per_day": 5.0, "spent_usd": 1.8, "journal_through": 0}
        assert SCHEMAS.errors("guest/session-credential.request.json", credential) == []
        await cursor.save_cursor(env.pool, stored.vm_id, 17)
        await step(found)
        assert len(credentials(found)) == 1  # only a new journal position: no new provisioning
        # the hub counted the next item: the same amount, to the last digit, no new provisioning
        moved = {"usd_per_org_day": 5, "spent_today_by_org": {org: usd_of(1_250_000)}}
        found.session_env.desired = {"last_origin_seq": first, "platform_budget": moved, "hub_time": day}
        await step(found)
        assert len(credentials(found)) == 1
        # the hub rejected the rest: its amount stays, the items are gone - within the day the amount does not fall
        found.session_env.desired = {"last_origin_seq": last, "platform_budget": moved, "hub_time": day}
        await step(found)
        assert len(credentials(found)) == 1
        later = {"usd_per_org_day": 5, "spent_today_by_org": {org: 2.0}}
        found.session_env.desired = {"last_origin_seq": last, "platform_budget": later, "hub_time": day}
        await step(found)
        assert credentials(found)[-1][1]["budget"] == {"usd_per_day": 5.0, "spent_usd": 2.0, "journal_through": 17}
        # a new day starts over; without a position the hub's amount alone
        found.session_env.desired = {"platform_budget": budget, "hub_time": "2026-11-23T00:00:01.000Z"}
        await step(found)
        assert credentials(found)[-1][1]["budget"] == {"usd_per_day": 5.0, "spent_usd": 1.0}
        found.session_env.desired = {"platform_budget": {"usd_per_org_day": 5, "spent_today_by_org": {org: 0.5}}}
        await step(found)
        assert credentials(found)[-1][1]["budget"] == {"usd_per_day": 5.0, "spent_usd": 0.5}

    run(store_dsn, body)


def test_an_older_guest_gets_the_budget_without_the_journal_position(store_dsn: str) -> None:
    """KOR-10 (review): a guest built from contracts before KOR-10 refuses `journal_through` (closed `budget` object,
    `400 request_invalid`); the renewal is sent again without it, so the gateway keeps its old bound."""

    async def body(env: Env) -> None:
        org = desired().org_id
        found = await running(env, "platform")
        attach(found)
        record = found.guest.put_credential

        async def older(sid: str, credential: Mapping[str, Any]) -> None:
            if "journal_through" in credential.get("budget", {}):
                raise GuestRefused("credential", 400, "request_invalid")
            await record(sid, credential)

        found.guest.put_credential = older  # type: ignore[method-assign]
        budget = {"usd_per_org_day": 5, "spent_today_by_org": {org: 1.0}}
        found.session_env.desired = {"last_origin_seq": 0, "platform_budget": budget}
        await step(found)
        assert credentials(found)[-1][1]["budget"] == {"usd_per_day": 5.0, "spent_usd": 1.0}
        assert f"credential_without_journal_through session_id={session_id()}" in "\n".join(found.lines)

    run(store_dsn, body)


def test_a_store_that_cannot_be_read_leaves_the_hubs_amount(store_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    async def body(env: Env) -> None:
        org = desired().org_id
        found = await running(env, "platform")
        attach(found)

        async def broken(*_args: object) -> int:
            raise psycopg.OperationalError("gone")

        monkeypatch.setattr(uncounted, "uncounted_micro", broken)
        budget = {"usd_per_org_day": 5, "spent_today_by_org": {org: 1.0}}
        found.session_env.desired = {"last_origin_seq": 0, "platform_budget": budget}
        await step(found)
        assert credentials(found)[-1][1]["budget"] == {"usd_per_day": 5.0, "spent_usd": 1.0}
        assert f"runner.model uncounted_unavailable org_id={org} error=OperationalError" in "\n".join(found.lines)

    run(store_dsn, body)


def test_micro_usd_survive_the_round_trip_through_the_hubs_float() -> None:
    for micro in (0, 1, 7, 300_001, 1_234_567, 99_999_999, 123_456_789_012, 2**40 + 3):
        assert uncounted.micro_of(uncounted.usd_of(micro)) == micro
        assert uncounted.usd_of(micro) == float(Decimal(micro) / Decimal(1_000_000))  # the hub's conversion


def test_the_uncounted_items_are_read_up_to_a_limit(store_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    async def body(env: Env) -> None:
        found = await running(env, "platform")
        sid = session_id()
        async with env.pool.connection() as conn, conn.transaction():
            await env.sync.enqueue(conn, "usage", _usage(sid, 1))
            await env.sync.enqueue(conn, "usage", _usage(sid, 2))
        monkeypatch.setattr(uncounted, "MAX_ITEMS", 1)
        async with env.pool.connection() as conn:
            assert await uncounted.uncounted_micro(conn, env.sync.storage_key, desired().org_id, 0) == 1
            await conn.execute(
                "INSERT INTO runner.usage_pending (session_id, item, first_wall) VALUES (%s, %s, 0)",
                (sid, Jsonb(_usage(sid, 4))),
            )
            # a pending merged item is counted although the stored items reach the limit
            assert await uncounted.uncounted_micro(conn, env.sync.storage_key, desired().org_id, 0) == 5
        assert uncounted.marks(None).keep("org", "2026-11-22", 5) == 5
        assert found is not None

    run(store_dsn, body)


def test_a_deleted_credential_is_withdrawn_at_once(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        found = await running(env, "subscription")
        attach(found)
        await step(found)  # registers the purge listener
        await step(found)  # once only
        await gateways.on_purge(env.context, desired().owner_id, "anthropic_api_key")  # not this session's kind
        assert credentials(found) == []
        del env.hub.records[(desired().owner_id, "claude_subscription")]
        await cache.refresh_owner(env.context, desired().owner_id)  # 404 -> purge -> listener
        [(sid, credential)] = credentials(found)
        assert credential["models_allowed"] == []
        assert "withdrawn" in credential["auth_value"]
        assert ("control", (sid, "stop")) in found.guest.calls
        stored = await found.stored(sid)
        assert stored.state == "failed"
        assert (await found.transitions())[-1] == ("failed", "model_access_unavailable")
        statuses = [b for k, b in await found.outbox() if k == "event"]
        assert statuses  # the session.status event of the failure
        assert sid not in provisions(env.context).stamps
        assert "access_withdrawn" in "\n".join(found.lines)
        # withdrawing a session that already ended stops nothing more
        found.guest.calls.clear()
        await gateways.withdraw(found.session_env, stored, "again")
        assert [name for name, _ in found.guest.calls] == ["put_credential"]
        # an API key of the same owner is unaffected; an unrelated kind is ignored
        await gateways.on_purge(env.context, desired().owner_id, "github")

    run(store_dsn, body)


def test_the_renewal_withdraws_what_is_gone(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        found = await running(env, "api_key")
        attach(found)
        async with env.pool.connection() as conn:
            await conn.execute("DELETE FROM runner.credential_cache")  # purged without a listener
        await step(found)
        assert credentials(found)[-1][1]["models_allowed"] == []
        assert (await found.stored(session_id())).state == "failed"

    run(store_dsn, body)


def test_a_platform_key_that_cannot_be_read_now_keeps_the_provisioning(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        found = await running(env, "platform")
        attach(found)
        key = found.session_env.platform_key
        found.session_env.platform_key = lambda *_: None
        await step(found)
        assert credentials(found) == []
        assert (await found.stored(session_id())).state == "agent_starting"
        assert f"platform_key_unusable session_id={session_id()}" in "\n".join(found.lines)
        found.session_env.platform_key = key
        await step(found)
        assert credentials(found) == []  # the same key and budget as at the start

    run(store_dsn, body)


def test_a_record_that_does_not_open_now_keeps_the_provisioning(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        found = await running(env, "api_key")
        attach(found)
        store_record(env, "anthropic_api_key", {"key": "two words"}, version=2)
        await cache.refresh_owner(env.context, desired().owner_id)
        await step(found)
        assert credentials(found) == []
        assert (await found.stored(session_id())).state == "agent_starting"
        store_record(env, "anthropic_api_key", api_key_body(NEW_KEY), version=3)
        await cache.refresh_owner(env.context, desired().owner_id)
        await step(found)
        assert credentials(found)[-1][1]["auth_value"] == NEW_KEY

    run(store_dsn, body)


def test_a_withdrawn_session_is_never_renewed_and_a_resuming_one_only_withdrawn(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        org = desired().org_id
        found = await running(env, "platform")
        attach(found)
        provisions(env.context).withdrawn.add(session_id())
        found.session_env.desired = {"platform_budget": {"usd_per_org_day": 5, "spent_today_by_org": {org: 1}}}
        await gateways.refresh(found.session_env, await found.stored(session_id()))
        assert credentials(found) == []
        provisions(env.context).withdrawn.clear()
        async with env.pool.connection() as conn:
            await records.update(conn, session_id(), state="resuming")
        await step(found)
        assert credentials(found) == []  # a resume provisions itself
        # a platform session can be withdrawn too (its upstream is OpenRouter)
        await gateways.withdraw(found.session_env, await found.stored(session_id()), "test")
        assert credentials(found)[-1][1]["upstream_base_url"] == "https://openrouter.ai/api"
        assert credentials(found)[-1][1]["models_allowed"] == []
        assert (await found.stored(session_id())).state == "failed"
        await step(found)  # the ended session's state is dropped
        assert session_id() not in provisions(env.context).locks
        assert session_id() not in provisions(env.context).withdrawn

    run(store_dsn, body)


def test_a_purge_reaches_a_resuming_session(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        found = await running(env, "subscription")
        attach(found)
        await step(found)
        async with env.pool.connection() as conn:
            await records.update(conn, session_id(), state="resuming")
        del env.hub.records[(desired().owner_id, "claude_subscription")]
        await cache.refresh_owner(env.context, desired().owner_id)
        assert (await found.stored(session_id())).state == "failed"

    run(store_dsn, body)


def test_a_version_that_changes_while_the_record_is_opened_is_not_claimed(
    store_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def body(env: Env) -> None:
        found = await running(env, "api_key")
        real = cache.opened
        changes: list[str] = []

        @contextlib.asynccontextmanager
        async def opened(context: Any, owner: str, kind: str) -> AsyncIterator[dict[str, Any]]:
            async with real(context, owner, kind) as opened_body:
                # the credential step stores a new version (or purges) while the record is open
                async with env.pool.connection() as conn:
                    await conn.execute(changes.pop(0))
                yield opened_body

        monkeypatch.setattr(cache, "opened", opened)
        changes.append("UPDATE runner.credential_cache SET version = 2")
        found_secret = await access.owner_secret(found.session_env, desired().owner_id, "anthropic_api_key")
        assert found_secret == (API_KEY, access.UNKNOWN_VERSION)
        changes.append("DELETE FROM runner.credential_cache")
        found_secret = await access.owner_secret(found.session_env, desired().owner_id, "anthropic_api_key")
        assert found_secret == (API_KEY, access.UNKNOWN_VERSION)

    run(store_dsn, body)


@pytest.mark.parametrize(
    ("failure", "line"),
    [
        (GuestUnreachable("x"), "credential_unreachable"),
        (GuestRefused("credential", 404, "not_found"), "credential_refused"),
    ],
)
def test_a_guest_that_does_not_take_the_renewal_is_tried_again(store_dsn: str, failure: Any, line: str) -> None:
    async def body(env: Env) -> None:
        org = desired().org_id
        found = await running(env, "platform")
        attach(found)
        found.session_env.desired = {"platform_budget": {"usd_per_org_day": 5, "spent_today_by_org": {org: 1}}}
        found.guest.fail["put_credential"] = failure
        await step(found)
        assert line in "\n".join(found.lines)
        del found.guest.fail["put_credential"]
        await step(found)
        assert credentials(found)[-1][1]["budget"]["spent_usd"] == 1.0

    run(store_dsn, body)


def test_sessions_without_a_running_vm_starting_or_ended_are_left_alone(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        found = await running(env, "platform")
        service = attach(found)
        stamps = provisions(env.context).stamps
        stamps["01930005-0000-7000-8000-0000000000ee"] = stamps[session_id()]  # an ended session
        org = desired().org_id
        found.session_env.desired = {"platform_budget": {"usd_per_org_day": 5, "spent_today_by_org": {org: 1}}}
        await vm_records.update(env.pool, "vm-00000001", state="stopped")
        await step(found)
        assert credentials(found) == []
        assert "01930005-0000-7000-8000-0000000000ee" not in stamps
        await vm_records.update(env.pool, "vm-00000001", state="running")
        never: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        service.tasks[session_id()] = asyncio.ensure_future(never)  # a start or stop is running
        await step(found)
        assert credentials(found) == []
        service.tasks[session_id()].cancel()
        del service.tasks[session_id()]

    run(store_dsn, body)


def test_a_failing_refresh_is_logged_and_the_others_go_on(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        found = await running(env, "platform")
        attach(found)

        def broken(*_args: object) -> str:
            raise RuntimeError("key")

        found.session_env.platform_key = broken
        await step(found)
        assert f"refresh_failed session_id={session_id()} error=RuntimeError" in "\n".join(found.lines)

    run(store_dsn, body)


def test_the_step_needs_a_desired_state_and_an_enrolled_device(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        found = await running(env, "platform")
        run_ = Run(context=env.context, sync=env.sync, book=book_for(env.context))
        await STEPS[0].run(run_)  # no desired state: nothing
        assert not provisions(env.context).subscribed
        context = SimpleNamespace(identity=None)
        unenrolled = Run(context=context, sync=env.sync, book=book_for(env.context))
        unenrolled.desired = {}
        await STEPS[0].run(unenrolled)
        assert found.guest.calls == []

    run(store_dsn, body)


def test_budget_spent_and_the_purge_listener_without_a_session_service(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        found = await running(env, "platform")
        org = desired().org_id
        assert gateways.platform_budget_spent(env.context, org) is False  # no service yet
        await gateways.on_purge(env.context, desired().owner_id, "anthropic_api_key")
        attach(found)
        found.session_env.desired = {"platform_budget": {"usd_per_org_day": 1, "spent_today_by_org": {org: 1}}}
        assert gateways.platform_budget_spent(env.context, org) is True
        assert gateways.platform_budget_spent(env.context, "other-org") is False
        assert API_KEY not in "\n".join(found.lines)

    run(store_dsn, body)
