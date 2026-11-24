"""U-RUN-credentials (KOR-47): GitHub identity records fetched by ID, verified with the author rule of their owner's
kind (a user's `CK` readers, an organization's active admins), cached without rollback, purged on `404`, refreshed on
the `credentials` hint and opened with the owner's `cred` key (ADR-076 section 1, 03-api.md section 6.1)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from agent_runner.credentials import cache, identities
from agent_runner.credentials.cache import CredentialUnavailable, RecordRefused, subscribe_stored
from agent_runner.credentials.hints import HANDLERS as HINTS
from agent_runner.credentials.reconcile import STEPS
from agent_runner.orgs import audit
from agent_runner.sync.http import HubRefusal

from .identity_kit import ORG_A, IdentityHub, choose, identity_body
from .kit import ALICE, BOB, Env, github_body, kit_env, make_record, pid

ORG_Z = pid("z/org")
Body = Callable[[Env, IdentityHub], Awaitable[None]]


def run(dsn: str, body: Body, **kwargs: Any) -> None:
    async def main() -> None:
        async with kit_env(dsn, **kwargs) as env:
            await body(env, IdentityHub(env.hub).install())

    asyncio.run(main())


def test_a_users_record_is_verified_cached_and_opened_only_for_the_block(store_dsn: str) -> None:
    stored: list[tuple[str, str]] = []

    async def listener(owner: str, kind: str) -> None:
        stored.append((owner, kind))

    async def body(env: Env, hub: IdentityHub) -> None:
        subscribe_stored(env.context, listener)
        record_id = hub.add(identity_body(), owner=ALICE, primary=True)
        await env.add_ck()
        found = await identities.fetch(env.context, record_id)
        assert found is not None
        assert (found.owner, found.owner_kind, found.primary, found.version) == (ALICE, "user", True, 1)
        assert stored == [(ALICE, "github")]
        async with identities.opened(env.context, found) as opened:
            assert opened["login"] == "octo-user"
            kept = opened
        assert kept == {}
        # meta: the cached row, no second request
        assert await identities.meta(env.context, record_id) == found
        assert hub.asked == [record_id]
        async with env.pool.connection() as conn:
            assert await identities.cached_ids(conn) == [record_id]
            assert await identities.cached(conn, record_id) == found
        assert "double-user-token" not in env.log()

    run(store_dsn, body)


def test_a_company_record_needs_an_active_admin_as_author_and_opens_with_the_orgs_cred_key(store_dsn: str) -> None:
    async def body(env: Env, hub: IdentityHub) -> None:
        good = hub.add(identity_body(login="octo-org"), owner=ORG_A, kind="org", author="alice", primary=True)
        await env.add_ck(owner=ORG_A)
        found = await identities.meta(env.context, good)
        assert found is not None
        assert (found.owner_kind, found.primary) == ("org", False)  # a company record is never primary
        async with identities.opened(env.context, found) as opened:
            assert opened["login"] == "octo-org"
        # a member who is no admin, and an organization this device does not belong to
        by_member = hub.add(identity_body(), owner=ORG_A, kind="org", author="bob")
        foreign = hub.add(identity_body(), owner=ORG_Z, kind="org", author="zed")
        for record_id in (by_member, foreign):
            with pytest.raises(RecordRefused) as refused:
                await identities.fetch(env.context, record_id)
            assert refused.value.code == "wrap_unauthorized"
        # an organization's record claimed as a user's
        hub.served[good].owner_kind = "user"
        with pytest.raises(RecordRefused) as refused:
            await identities.fetch(env.context, good)
        assert refused.value.code == "header_invalid"

    run(store_dsn, body)


@pytest.mark.parametrize(
    "answer",
    [
        {"owner_id": "not-a-uuid", "owner_kind": "user", "primary": False},
        {"owner_id": ALICE, "owner_kind": "team", "primary": False},
        {"owner_id": ALICE, "owner_kind": "user", "primary": "yes"},
        {"owner_id": BOB, "owner_kind": "user", "primary": False},  # the header names another owner
        {"owner_id": ALICE, "owner_kind": "user", "primary": False, "record": "x"},
    ],
)
def test_answers_that_do_not_fit_are_refused_audited_and_not_cached(store_dsn: str, answer: dict[str, Any]) -> None:
    async def body(env: Env, hub: IdentityHub) -> None:
        record_id = hub.add(identity_body(), owner=ALICE)
        hub.raw[record_id] = {"record": hub.served[record_id].record, **answer}
        before = audit.counters(env.context)[audit.VERIFY_FAILURES]
        with pytest.raises(RecordRefused) as refused:
            await identities.fetch(env.context, record_id)
        assert refused.value.code == "header_invalid"
        assert audit.counters(env.context)[audit.VERIFY_FAILURES] == before + 1
        assert f"identity_refused id={record_id} code=header_invalid" in env.log()
        assert (await env.audits())[-1]["target_id"] == record_id
        async with env.pool.connection() as conn:
            assert await identities.cached_ids(conn) == []

    run(store_dsn, body)


def test_a_record_with_another_id_or_kind_is_refused(store_dsn: str) -> None:
    async def body(env: Env, hub: IdentityHub) -> None:
        record_id = hub.add(identity_body(), owner=ALICE)
        hub.served[record_id].record = make_record(identity_body(), owner=ALICE)  # another record ID
        other_kind = hub.add(identity_body(), owner=ALICE)
        hub.served[other_kind].record = make_record({"to": "x"}, owner=ALICE, kind="mailbox", record_id=other_kind)
        for found in (record_id, other_kind):
            with pytest.raises(RecordRefused) as refused:
                await identities.fetch(env.context, found)
            assert refused.value.code == "header_invalid"
        tampered = hub.add(identity_body(), owner=ALICE)
        hub.served[tampered].record = {**hub.served[tampered].record, "sig": hub.served[record_id].record["sig"]}
        with pytest.raises(RecordRefused) as refused:
            await identities.fetch(env.context, tampered)
        assert refused.value.code == "signature_invalid"

    run(store_dsn, body)


def test_404_purges_rollback_is_refused_and_other_refusals_propagate(store_dsn: str) -> None:
    async def body(env: Env, hub: IdentityHub) -> None:
        record_id = hub.add(identity_body(), owner=ALICE, version=2)
        await identities.fetch(env.context, record_id)
        older = make_record(identity_body(), owner=ALICE, version=1, record_id=record_id)
        hub.served[record_id].record = older
        with pytest.raises(RecordRefused) as refused:
            await identities.fetch(env.context, record_id)
        assert refused.value.code == "version_conflict"
        hub.refusals[record_id] = (403, {"error": "forbidden"})
        with pytest.raises(HubRefusal):
            await identities.fetch(env.context, record_id)
        del hub.refusals[record_id]
        del hub.served[record_id]
        assert await identities.fetch(env.context, record_id) is None
        assert f"runner.credentials identity_purged id={record_id}" in env.log()
        assert await identities.purge(env.context, record_id) is False
        assert await identities.meta(env.context, record_id) is None

    run(store_dsn, body)


def test_opened_refuses_a_record_without_key_with_a_bad_cipher_or_a_body_that_is_no_object(store_dsn: str) -> None:
    async def body(env: Env, hub: IdentityHub) -> None:
        no_key = await identities.fetch(env.context, hub.add(identity_body(), owner=ALICE))
        assert no_key is not None
        with pytest.raises(CredentialUnavailable) as missing:
            async with identities.opened(env.context, no_key):
                pass
        assert missing.value.reason == "key_missing"
        await env.add_ck(key=b"\x02" * 32)  # a wrong key under the right slot
        with pytest.raises(CredentialUnavailable) as invalid:
            async with identities.opened(env.context, no_key):
                pass
        assert invalid.value.reason == "invalid"
        await env.add_ck(owner=BOB)
        listed = hub.add({"x": 1}, owner=BOB, author="bob")
        hub.served[listed].record = make_record([1], owner=BOB, author="bob", record_id=listed)  # type: ignore[arg-type]
        found = await identities.fetch(env.context, listed)
        assert found is not None
        with pytest.raises(CredentialUnavailable) as not_object:
            async with identities.opened(env.context, found):
                pass
        assert not_object.value.reason == "invalid"
        forged = identities.Identity(listed, BOB, "user", False, 1, {**found.record, "sig": "AAAA"})
        with pytest.raises(CredentialUnavailable) as refused:
            async with identities.opened(env.context, forged):
                pass
        assert refused.value.reason == "invalid"

    run(store_dsn, body)


def test_the_hint_and_the_reconcile_step_refresh_the_owners_identity_records(store_dsn: str) -> None:
    async def body(env: Env, hub: IdentityHub) -> None:
        own = hub.add(identity_body(), owner=ALICE)
        company = hub.add(identity_body(login="octo-org"), owner=ORG_A, kind="org")
        broken = hub.add(identity_body(), owner=ORG_A, kind="org")
        for record_id in (own, company, broken):
            await identities.fetch(env.context, record_id)
        hub.served[own].record = make_record(identity_body(), owner=ALICE, version=2, record_id=own)
        del hub.served[company]
        hub.served[broken].record = make_record(identity_body(), owner=ORG_A, version=2, record_id=broken)
        hub.refusals[broken] = (403, {"error": "forbidden"})  # listed in a newer version, refused when read
        env.hub.records[(ALICE, "github")] = make_record(github_body())
        await HINTS["credentials"](env.context, {"type": "credentials", "owner_id": ALICE})
        await HINTS["credentials"](env.context, {"type": "credentials", "owner_id": ORG_A})
        async with env.pool.connection() as conn:
            assert sorted(await identities.cached_ids(conn)) == sorted([own, broken])
            refreshed = await identities.cached(conn, own)
        assert refreshed is not None
        assert refreshed.version == 2
        assert [row[1] for row in await env.cache_rows()] == ["github"]  # the legacy kinds of the user
        assert f"identity_refresh_failed id={broken} error=HubRefusal code=forbidden" in env.log()
        # an organization of the device has no legacy records: none is asked for
        assert not [r.target for r in env.hub.requests if f"/credentials/{ORG_A}/" in r.target]
        # the reconcile step: `credentials_changed` names the organization
        del hub.refusals[broken]
        del hub.served[broken]
        env.hub.changed = [ORG_A]
        (step,) = STEPS
        await step.run(await env.desired())
        async with env.pool.connection() as conn:
            assert await identities.cached_ids(conn) == [own]

    run(store_dsn, body)


def test_primary_id_is_the_record_id_of_the_cached_primary_record(store_dsn: str) -> None:
    async def body(env: Env, _hub: IdentityHub) -> None:
        record = make_record(github_body())
        env.hub.records[(ALICE, "github")] = record
        async with env.pool.connection() as conn:
            assert await identities.primary_id(conn, ALICE) is None
        await cache.fetch(env.context, ALICE, "github")
        async with env.pool.connection() as conn:
            assert await identities.primary_id(conn, ALICE) == record["header"]["id"]

    run(store_dsn, body)


def test_the_generation_changes_with_every_github_record_choice_and_default(store_dsn: str) -> None:
    async def body(env: Env, hub: IdentityHub) -> None:
        seen: list[int | None] = []

        async def note() -> None:
            async with env.pool.connection() as conn:
                seen.append(await identities.generation(conn))

        await note()
        async with env.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO runner.credential_cache (owner_id, kind, version, record) VALUES (%s, 'github', 1, %s)",
                (ALICE, Jsonb({})),
            )
            await conn.execute(
                "INSERT INTO runner.credential_cache (owner_id, kind, version, record) VALUES (%s, 'mailbox', 1, %s)",
                (ALICE, Jsonb({})),
            )
        await note()
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.credential_cache SET version = 2 WHERE kind = 'mailbox'")
        await note()
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.credential_cache SET version = 2 WHERE kind = 'github'")
        await note()
        await identities.fetch(env.context, hub.add(identity_body(), owner=ALICE))
        await note()
        await choose(env.pool, task_choice=pid("x"))
        await note()
        await choose(env.pool, task_choice=pid("x"), defaults=[("org", ORG_A, pid("y"))])
        await note()
        assert seen[0] is None
        assert seen[1] == seen[2]  # another kind does not count
        values = [seen[1], *seen[3:]]
        assert len(set(values)) == len(values)
        assert all(isinstance(value, int) and 1 <= value < 2**31 for value in values)

    run(store_dsn, body)


def test_the_owners_list_brings_new_records_purges_gone_ones_and_skips_known_versions(store_dsn: str) -> None:
    async def body(env: Env, hub: IdentityHub) -> None:
        first = hub.add(identity_body(), owner=ALICE)
        second = hub.add(identity_body(login="alice-two", user_id=201), owner=ALICE)
        hub.add(identity_body(), owner=BOB, author="bob")  # another owner's record is not listed for alice
        found = await identities.known(env.context, ALICE)
        assert sorted(item.id for item in found) == sorted([first, second])
        assert hub.listed == [ALICE]
        assert await identities.known(env.context, ALICE) == found  # cached: no list again
        assert hub.listed == [ALICE]
        del hub.served[second]
        asked = len(hub.asked)
        assert await identities.sync_owner(env.context, ALICE) is True
        assert len(hub.asked) == asked  # the known version is not read again
        async with env.pool.connection() as conn:
            assert await identities.cached_ids(conn) == [first]

    run(store_dsn, body)


@pytest.mark.parametrize(
    "refusal",
    [
        (404, None),
        (400, None),
        (200, {"identities": [{"id": "x", "owner_id": ALICE, "version": 1}]}),
        (200, {"identities": [{"id": pid("other"), "owner_id": BOB, "version": 1}]}),
        (200, {"identities": "x"}),
        (0, None),
    ],
)
def test_without_a_usable_list_nothing_is_purged_and_cached_records_are_read_by_id(
    store_dsn: str, refusal: tuple[int, dict[str, Any] | None]
) -> None:
    async def body(env: Env, hub: IdentityHub) -> None:
        kept = hub.add(identity_body(), owner=ALICE)
        failing = hub.add(identity_body(), owner=ALICE)
        for record_id in (kept, failing):
            await identities.fetch(env.context, record_id)
        hub.served[kept].record = make_record(identity_body(), owner=ALICE, version=2, record_id=kept)
        hub.refusals[failing] = (403, {"error": "forbidden"})
        status, answer = refusal
        if status == 0:
            env.hub.tamper.append("bad_signature")  # the list answer does not verify
        elif answer is None:
            hub.list_refusals[ALICE] = (status, {"error": "not_found" if status == 404 else "request_invalid"})
        else:
            hub.raw_lists[ALICE] = answer
        assert await identities.sync_owner(env.context, ALICE) is False
        assert f"identity_list_failed owner={ALICE}" in env.log()
        await identities.refresh(env.context, ALICE)  # falls back to reading every cached record by its ID
        async with env.pool.connection() as conn:
            found = await identities.cached(conn, kept)
        assert found is not None
        assert found.version == 2

    run(store_dsn, body)
