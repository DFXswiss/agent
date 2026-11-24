"""U-RUN-github (KOR-47): which identities the acting user may use (`identity.usable`), the resolution of the identity
of a `github.*` operation in its precedence (ADR-078 sections 2 and 3) with the acting user stamped at the receipt
(HOT-14, ADR-070), forged choices, defaults that hold, the account check of the token (`GET /user`), and the clients
the executors open for the resolved identity (`ops.identity_client`)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from agent_runner.credentials import acting, cache, identities
from agent_runner.github import identity, ops, precheck, push
from agent_runner.github.client import GitHubError
from agent_runner.github.op_prechecks import PRECHECKS
from agent_runner.operations.extensions import CheckFailed, PrecheckRequest, PrecheckResult
from agent_runner.operations.settings import DEFAULTS

from ..credentials.identity_kit import (
    ORG_A,
    ORG_B,
    WG_A,
    IdentityHub,
    choose,
    identity_body,
    operation_of,
    session_of,
    two_orgs,
)
from ..credentials.kit import ALICE, BOB, Env, kit_env, make_record, pid
from .conftest import OTHER_TOKEN, REPO, TOKEN, GitHub

ZED = pid("zed")
ORG_Z = pid("z/org")
Body = Callable[[Env, IdentityHub], Awaitable[None]]


def run(dsn: str, body: Body, github: GitHub | None = None) -> None:
    async def main() -> None:
        url = "http://127.0.0.1:9" if github is None else github.url
        async with kit_env(dsn, two_orgs(), github_url=url) as env:
            await body(env, IdentityHub(env.hub).install())

    asyncio.run(main())


def request_of(env: Env, actor: str | None = ALICE, params: dict[str, Any] | None = None, **kw: Any) -> PrecheckRequest:
    session: Any = session_of(**kw)
    return PrecheckRequest(env.context, operation_of(actor), session, DEFAULTS, params or {})


async def resolve(env: Env, params: dict[str, Any] | None = None, actor: str | None = ALICE) -> identity.Resolved:
    return await identity.resolve(request_of(env, actor, params))


async def refused(env: Env, params: dict[str, Any] | None = None, actor: str | None = ALICE) -> str:
    with pytest.raises(CheckFailed) as failed:
        await resolve(env, params, actor)
    return failed.value.code


async def legacy(env: Env, owner: str = ALICE, author: str = "alice", **body: Any) -> str:
    """A primary record of `owner` at the hub (legacy endpoint); its record ID."""
    record = make_record(identity_body(**body), owner=owner, author=author)
    env.hub.records[(owner, "github")] = record
    await env.add_ck(owner=owner)
    return str(record["header"]["id"])


# --- who may use what ------------------------------------------------------------------------------------------


def test_usable_own_task_org_and_operator_never_another_persons_or_orgs(store_dsn: str) -> None:
    async def body(env: Env, _hub: IdentityHub) -> None:
        book = await identity.loaded(env.context)

        def use(owner: str, kind: str, user: str) -> str | None:
            return identity.usable(env.context, book, owner, kind, user=user, org_id=ORG_A)

        assert use(ALICE, "user", ALICE) == "own"
        assert use(BOB, "user", ALICE) is None
        assert use(ORG_A, "org", BOB) == "task_org"
        assert use(ORG_A, "org", ZED) is None  # no member of the task's organization
        assert use(ORG_B, "org", ALICE) is None  # another organization
        assert use(ORG_Z, "org", ALICE) is None
        env.context.config.operator_github_org = ORG_B
        assert use(ORG_B, "org", ALICE) == "operator"
        assert use(ORG_B, "org", ZED) is None
        assert use(ORG_Z, "org", ALICE) is None
        assert identity.member(book, ORG_Z, ZED) is False  # not an organization of this device
        assert identity.member(book, ORG_A, pid("rdev")) is False  # a device is no human

    run(store_dsn, body)


# --- the resolution ----------------------------------------------------------------------------------------------


def test_the_resolution_takes_parameter_session_task_defaults_primary_operator_in_this_order(store_dsn: str) -> None:
    async def body(env: Env, hub: IdentityHub) -> None:
        primary = await legacy(env)
        await env.add_ck(owner=ORG_A)
        await env.add_ck(owner=ORG_B)
        second = hub.add(identity_body(login="alice-two", user_id=201), owner=ALICE)
        wg_default = hub.add(identity_body(login="octo-org"), owner=ORG_A, kind="org")
        org_default = hub.add(identity_body(login="octo-org-two"), owner=ORG_A, kind="org")
        operator = hub.add(identity_body(login="op-account", source="operator"), owner=ORG_B, kind="org")
        env.context.config.operator_github_org = ORG_B
        defaults = [("wg", WG_A, wg_default), ("org", ORG_A, org_default)]
        await choose(env.pool, session_choice=wg_default, task_choice=org_default, defaults=defaults)

        async def picked(params: dict[str, Any] | None = None) -> tuple[str, str, bool]:
            found = await resolve(env, params)
            assert found.acting_user == ALICE
            return found.identity_id, found.source, found.primary

        assert await picked({"github_identity": second}) == (second, "own", False)
        assert await picked() == (wg_default, "task_org", False)  # the session's
        await choose(env.pool, task_choice=org_default, defaults=defaults)
        assert await picked() == (org_default, "task_org", False)  # the task's
        await choose(env.pool, defaults=defaults)
        assert await picked() == (wg_default, "task_org", False)  # the workgroup's default
        await choose(env.pool, defaults=[("org", ORG_A, org_default)])
        assert await picked() == (org_default, "task_org", False)  # the organization's default
        await choose(env.pool)
        assert await picked() == (primary, "own", True)  # the own primary record
        assert (await resolve(env)).data() == {
            "github_identity": primary,
            "identity_owner": ALICE,
            "identity_kind": "user",
            "identity_primary": True,
            "acting_user": ALICE,
        }
        del env.hub.records[(ALICE, "github")]
        await cache.purge(env.context, ALICE, "github")
        assert await picked() == (operator, "operator", False)  # the operator's account (listed at the hub)
        env.context.config.operator_github_org = None
        assert await refused(env) == "github_not_connected"

    run(store_dsn, body)


def test_without_acting_user_nothing_is_resolved_and_the_hub_is_not_asked(store_dsn: str) -> None:
    async def body(env: Env, hub: IdentityHub) -> None:
        await legacy(env)  # the owner is connected
        await choose(env.pool, task_choice=pid("chosen"))
        asked = len(env.hub.requests)
        with pytest.raises(CheckFailed) as failed:
            await resolve(env, actor=None)
        assert (failed.value.code, failed.value.message) == ("github_not_connected", acting.NO_ACTOR)
        assert len(env.hub.requests) == asked
        assert hub.asked == []

    run(store_dsn, body)


def test_forged_choices_are_forbidden_and_missing_records_are_not_connected(store_dsn: str) -> None:
    async def body(env: Env, hub: IdentityHub) -> None:
        await legacy(env)
        await env.add_ck(owner=BOB)
        await env.add_ck(owner=ORG_A)
        await env.add_ck(owner=ORG_B)
        bobs = hub.add(identity_body(login="bob-user", user_id=301), owner=BOB, author="bob")
        org_b = hub.add(identity_body(login="b-org"), owner=ORG_B, kind="org")
        org_z = hub.add(identity_body(login="z-org"), owner=ORG_Z, kind="org", author="zed")
        hub.refusals[org_z] = (403, {"error": "forbidden"})  # this device is no reader of org z
        by_member = hub.add(identity_body(), owner=ORG_A, kind="org", author="bob")
        no_token = hub.add({"login": "octo-user", "user_id": 101}, owner=ALICE)
        no_key = hub.add(identity_body(), owner=ALICE, epoch=2)
        for forged in (bobs, org_b, org_z):
            assert await refused(env, {"github_identity": forged}) == "identity_forbidden"
        for missing in (pid("unknown"), by_member, no_token, no_key):
            assert await refused(env, {"github_identity": missing}) == "github_not_connected"
        hub.refusals[pid("unknown")] = (400, {"error": "request_invalid"})
        assert await refused(env, {"github_identity": pid("unknown")}) == "github_not_connected"
        # an explicit choice that is not usable is never replaced by a default or the own record
        await choose(env.pool, session_choice=bobs)
        assert await refused(env) == "identity_forbidden"
        await choose(env.pool, task_choice=org_b)
        assert await refused(env) == "identity_forbidden"

    run(store_dsn, body)


def test_defaults_the_actor_may_not_use_are_skipped_unreadable_ones_hold(store_dsn: str) -> None:
    async def body(env: Env, hub: IdentityHub) -> None:
        primary = await legacy(env)
        await env.add_ck(owner=ORG_A)
        await env.add_ck(owner=ORG_B)
        personal = hub.add(identity_body(login="alice-two", user_id=201), owner=ALICE)
        org_b = hub.add(identity_body(login="b-org"), owner=ORG_B, kind="org")
        for defaults in ([("wg", WG_A, personal)], [("org", ORG_A, org_b)]):
            await choose(env.pool, defaults=defaults)  # a personal account is never a default; another org's
            assert (await resolve(env)).identity_id == primary, defaults
        no_key = hub.add(identity_body(), owner=ORG_A, kind="org", epoch=3)  # the epoch's wrap did not arrive yet
        by_member = hub.add(identity_body(), owner=ORG_A, kind="org", author="bob")
        hub.refusals[pid("refused")] = (403, {"error": "forbidden"})
        for unreadable in (no_key, by_member, pid("gone"), pid("refused")):
            await choose(env.pool, defaults=[("wg", WG_A, unreadable)])
            assert await refused(env) == "github_not_connected", unreadable  # held, never the personal account
        # a company record of the task's organization for somebody who is no member of it: skipped
        usable = hub.add(identity_body(login="octo-org"), owner=ORG_A, kind="org")
        await choose(env.pool, defaults=[("org", ORG_A, usable)])
        assert await refused(env, actor=ZED) == "github_not_connected"
        assert (await resolve(env)).identity_id == usable

    run(store_dsn, body)


def test_a_foreign_humans_instruction_never_acts_with_the_owners_personal_account(store_dsn: str) -> None:
    async def body(env: Env, hub: IdentityHub) -> None:
        await legacy(env)  # the owner alice is connected
        assert await refused(env, actor=BOB) == "github_not_connected"  # held for bob's connection
        alices = hub.add(identity_body(login="alice-two", user_id=201), owner=ALICE)
        await choose(env.pool, task_choice=alices)
        assert await refused(env, actor=BOB) == "identity_forbidden"
        await choose(env.pool)
        bobs = await legacy(env, owner=BOB, author="bob", login="bob-user", user_id=301)
        found = await resolve(env, actor=BOB)
        assert (found.identity_id, found.owner, found.acting_user) == (bobs, BOB, BOB)

    run(store_dsn, body)


def test_the_operators_account_needs_membership_the_operator_source_and_is_the_newest(store_dsn: str) -> None:
    async def body(env: Env, hub: IdentityHub) -> None:
        await env.add_ck(owner=ORG_B)
        env.context.config.operator_github_org = ORG_B
        hub.add(identity_body(login="b-org"), owner=ORG_B, kind="org", ts=1_795_000_200.0)  # not the operator's token
        assert await refused(env) == "github_not_connected"
        older = hub.add(identity_body(login="op-old", source="operator"), owner=ORG_B, kind="org", ts=1_795_000_000.0)
        newer = hub.add(identity_body(login="op-new", source="operator"), owner=ORG_B, kind="org", ts=1_795_000_100.0)
        await identities.sync_owner(env.context, ORG_B)
        found = await resolve(env)
        assert (found.identity_id, found.source) == (newer, "operator")
        assert older != newer
        assert await refused(env, actor=ZED) == "github_not_connected"  # no member of the operator's organization
        # the operator's organization is the task's: the record counts as a company record of it
        await env.add_ck(owner=ORG_A)
        env.context.config.operator_github_org = ORG_A
        own_org = hub.add(identity_body(login="op-a", source="operator"), owner=ORG_A, kind="org")
        found = await resolve(env)
        assert (found.identity_id, found.source) == (own_org, "task_org")

    run(store_dsn, body)


def test_a_subagent_session_waits_for_its_own_identity(store_dsn: str) -> None:
    async def body(env: Env, _hub: IdentityHub) -> None:
        await legacy(env)
        child = request_of(env, session_id=pid("child"), depth=1, parent_id=pid("parent"))
        with pytest.raises(CheckFailed) as failed:
            await identity.resolve(child)
        assert failed.value.code == "github_not_connected"

    run(store_dsn, body)


# --- the pre-check wrapper and the account check -----------------------------------------------------------------


def test_the_resolved_precheck_checks_the_account_once_and_passes_the_identity_on(
    store_dsn: str, github: GitHub
) -> None:
    async def body(env: Env, hub: IdentityHub) -> None:
        seen: list[identity.Resolved] = []

        async def inner(_request: PrecheckRequest) -> PrecheckResult:
            seen.append(identity.current())
            return PrecheckResult(dedupe={"x": 1}, data={"own": True})

        check = identity.resolved(inner)
        assert check.__wrapped__ is inner  # type: ignore[attr-defined]
        primary = await legacy(env, token=TOKEN, user_id=101)
        found = await check(request_of(env))
        assert found.dedupe == {"x": 1}
        assert found.data == {"own": True, **seen[0].data()}
        assert seen[0].identity_id == primary
        await check(request_of(env))
        assert github.paths() == ["GET /user"]  # checked once for this version
        with pytest.raises(LookupError):
            identity.current()  # reset after the check
        await env.add_ck(owner=ORG_A)
        cases = [
            (identity_body(token=TOKEN, user_id=999), "identity_unavailable"),  # the token is another account's
            ({"token": TOKEN, "login": "octo-user"}, "identity_unavailable"),  # no account ID in the record
            (identity_body(token="double-revoked-token"), "github_not_connected"),  # 401
        ]
        for record_body, code in cases:
            chosen = hub.add(record_body, owner=ORG_A, kind="org")
            with pytest.raises(CheckFailed) as failed:
                await check(request_of(env, params={"github_identity": chosen}))
            assert failed.value.code == code
        github.faults({"route": "GET /user", "kind": "error", "status": 503, "times": 1})
        fresh = hub.add(identity_body(token=OTHER_TOKEN, user_id=102), owner=ORG_A, kind="org")
        with pytest.raises(CheckFailed) as failed:
            await check(request_of(env, params={"github_identity": fresh}))
        assert (failed.value.code, failed.value.message) == ("upstream_error", identity.ACCOUNT_FAILED)
        await cache.purge(env.context, ALICE, "github")
        env.hub.tamper.append("bad_signature")
        with pytest.raises(CheckFailed) as failed:
            await check(request_of(env))
        assert (failed.value.code, failed.value.message) == ("upstream_error", identity.HUB_FAILED)

    run(store_dsn, body, github)


def test_the_account_check_maps_a_record_that_goes_away(store_dsn: str) -> None:
    async def body(env: Env, _hub: IdentityHub) -> None:
        data = {"github_identity": pid("gone"), "identity_owner": ORG_A, "identity_primary": False}
        with pytest.raises(CheckFailed) as failed:
            await identity.check_account(env.context, operation_of(), data, 1)
        assert failed.value.code == "github_not_connected"

    run(store_dsn, body)


def test_the_connection_request_answers_for_the_acting_users_own_record(
    store_dsn: str, github: GitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def body(env: Env, _hub: IdentityHub) -> None:
        await legacy(env)  # the owner is connected
        bobs = await legacy(env, owner=BOB, author="bob", token=OTHER_TOKEN, login="other-user", user_id=102)
        request = request_of(env, BOB)
        found = await PRECHECKS["github.request_connection"](request)
        assert (found.data["github_identity"], found.data["acting_user"]) == (bobs, BOB)
        assert github.paths() == ["GET /user"]
        await legacy(env, owner=BOB, author="bob", token=OTHER_TOKEN, login="other-user", user_id=7)
        await cache.purge(env.context, BOB, "github")
        with pytest.raises(CheckFailed) as other:
            await precheck.verified_connection(request)
        assert other.value.code == "identity_unavailable"  # the token is another account than the record says

        async def gone(_context: Any, _operation: Any) -> None:
            return None

        monkeypatch.setattr(identity, "primary", gone)  # the record went away after GET /user
        with pytest.raises(CheckFailed) as missing:
            await precheck.verified_connection(request)
        assert missing.value.code == "github_not_connected"

    run(store_dsn, body, github)


def test_a_primary_record_purged_while_it_was_read_is_not_used(store_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    async def body(env: Env, _hub: IdentityHub) -> None:
        await legacy(env)

        async def purged(_conn: Any, _operation: Any) -> None:
            return None

        await legacy(env, owner=BOB, author="bob", token="has space")  # no usable token
        assert await identity.primary(env.context, operation_of(BOB)) is None
        monkeypatch.setattr(acting, "primary_record_id", purged)
        assert await identity.primary(env.context, operation_of()) is None

    run(store_dsn, body)


# --- the clients of the resolved identity ------------------------------------------------------------------------


def test_identity_client_opens_exactly_the_resolved_record_for_the_acting_user(store_dsn: str) -> None:
    async def body(env: Env, hub: IdentityHub) -> None:
        primary = await legacy(env)
        await env.add_ck(owner=ORG_A)
        await env.add_ck(owner=BOB)
        company = hub.add(identity_body(token="double-org-token"), owner=ORG_A, kind="org")
        bobs = hub.add(identity_body(token="double-bob-token"), owner=BOB, author="bob")
        operation = operation_of()

        def data(identity_id: str, owner: str, primary_record: bool = False) -> dict[str, Any]:
            return {"github_identity": identity_id, "identity_owner": owner, "identity_primary": primary_record}

        async with ops.identity_body(env.context, operation, data(company, ORG_A)) as opened:
            assert opened["token"] == "double-org-token"
        async with ops.identity_client(env.context, operation, data(primary, ALICE, True)) as client:
            assert client is not None
        async with ops.data_client(env.context, operation, {}) as client:
            assert client is not None
        for wrong in (
            data(pid("other"), ALICE, True),  # the primary record changed
            data(primary, BOB, True),  # the primary record of somebody else than the acting user
            data(pid("gone"), ALICE),  # no record
            data(company, BOB),  # another owner than resolved
            data(bobs, BOB),  # another person's personal record: never opened for alice's instruction
        ):
            with pytest.raises(ops.NotConnected):
                async with ops.identity_body(env.context, operation, wrong):
                    pass
        bad = hub.add({"token": "", "login": "x"}, owner=ORG_A, kind="org")
        with pytest.raises(ops.NotConnected):
            async with ops.identity_client(env.context, operation, data(bad, ORG_A)):
                pass
        refused_id = hub.add(identity_body(), owner=ORG_A, kind="org", author="bob")
        hub.refusals[pid("forbidden")] = (403, {"error": "forbidden"})
        for failing in (refused_id, pid("forbidden")):
            with pytest.raises(ops.NotConnected):
                async with ops.identity_body(env.context, operation, data(failing, ORG_A)):
                    pass
        env.hub.tamper.append("bad_signature")
        with pytest.raises(GitHubError) as unreachable:
            async with ops.identity_body(env.context, operation, data(pid("new"), ORG_A)):
                pass
        assert unreachable.value.retryable

    run(store_dsn, body)


def test_a_resolved_record_that_goes_away_before_use_is_not_connected(store_dsn: str, github: GitHub) -> None:
    async def body(env: Env, hub: IdentityHub) -> None:
        gone = identity.Resolved(pid("gone"), ORG_A, "org", False, "task_org", ALICE)
        request = request_of(env, params={"repo": REPO})
        token = identity.CURRENT.set(gone)
        try:
            for check in (precheck.connection, precheck.repo_access, push.check):
                with pytest.raises(CheckFailed) as failed:
                    await check(request)
                assert failed.value.code == "github_not_connected"
            env.hub.tamper.append("bad_signature")  # the hub cannot be asked for the record
            with pytest.raises(CheckFailed) as failed:
                await precheck.connection(request)
            assert failed.value.code == "upstream_error"
        finally:
            identity.CURRENT.reset(token)
        # a token GitHub refuses after the account check (revoked in between): not connected
        await env.add_ck(owner=ORG_A)
        revoked = hub.add(identity_body(token="double-revoked-token"), owner=ORG_A, kind="org")
        token = identity.CURRENT.set(identity.Resolved(revoked, ORG_A, "org", False, "task_org", ALICE))
        try:
            for check in (precheck.repo_access, push.check):
                with pytest.raises(CheckFailed) as failed:
                    await check(request)
                assert failed.value.code == "github_not_connected"
        finally:
            identity.CURRENT.reset(token)

    run(store_dsn, body, github)


# --- the formal check's cheap answer -------------------------------------------------------------------------------


def test_available_sees_records_choices_and_defaults(store_dsn: str) -> None:
    async def body(env: Env, _hub: IdentityHub) -> None:
        async def available(actor: str | None = ALICE) -> bool:
            async with env.pool.connection() as conn:
                return await identity.available(conn, operation_of(actor), session_of())

        assert await available() is False
        async with env.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO runner.credential_cache (owner_id, kind, version, record) VALUES (%s, 'github', 1, %s)",
                (BOB, Jsonb({})),
            )
        assert await available() is False  # another person's record
        assert await available(BOB) is True  # the acting user's
        async with env.pool.connection() as conn:
            await conn.execute("DELETE FROM runner.credential_cache")
        await choose(env.pool, task_choice=pid("x"))
        assert await available() is True
        await choose(env.pool, session_choice=pid("x"), task_id=pid("other-task"), session_id=pid("other-session"))
        assert await available() is False  # another task's choice
        await choose(env.pool, defaults=[("wg", WG_A, pid("x"))])
        assert await available() is True
        await choose(env.pool, defaults=[("org", ORG_A, pid("x"))])
        assert await available() is True
        await choose(env.pool, defaults=[("org", ORG_B, pid("x"))])
        assert await available() is False
        async with env.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO runner.github_identity_cache (id, owner_id, owner_kind, is_primary, version, record)"
                " VALUES (%s, %s, 'org', false, 1, '{}')",
                (pid("x"), ORG_B),
            )
        assert await available() is True

    run(store_dsn, body)
