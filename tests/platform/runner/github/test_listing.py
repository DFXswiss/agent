"""U-RUN-github (KOR-47): `github.list_identities` (ADR-076 section 3) and the co-author candidates of a task
(`people`, ADR-076 section 4): which identities the acting human may use, which one applies now, which humans may be
named, and that copies of the operator's account are never co-authors."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_guest.schema import SchemaSet
from agent_runner.credentials import acting, identities
from agent_runner.crypto.encoding import b64u_encode
from agent_runner.github import identity, listing, people
from agent_runner.github.executors import EXECUTORS
from agent_runner.github.op_prechecks import PRECHECKS
from agent_runner.operations.extensions import CheckFailed, OperationContext, OperationFailed, PrecheckRequest
from agent_runner.operations.settings import DEFAULTS

from ..credentials.identity_kit import (
    ORG_A,
    ORG_B,
    WG_A,
    IdentityHub,
    choose,
    deliver,
    identity_body,
    operation_of,
    session_of,
    two_orgs,
)
from ..credentials.kit import ALICE, BOB, Env, kit_env, make_record, pid
from ..sync.fakehub import CONTRACTS
from .conftest import OTHER_TOKEN, GitHub

SCHEMAS = SchemaSet(CONTRACTS)
ZED = pid("zed")
SIGNING = {"alg": "ssh-ed25519", "seed": b64u_encode(b"\x01" * 32), "public": "ssh-ed25519 " + "A" * 68}
Body = Callable[[Env, IdentityHub], Awaitable[None]]


def run(dsn: str, body: Body, github: GitHub | None = None) -> None:
    async def main() -> None:
        url = "http://127.0.0.1:9" if github is None else github.url
        async with kit_env(dsn, two_orgs(), github_url=url) as env:
            env.context.config.operator_github_org = None
            await body(env, IdentityHub(env.hub).install())

    asyncio.run(main())


async def legacy(env: Env, owner: str, author: str, **body: Any) -> str:
    record = make_record(identity_body(**body), owner=owner, author=author)
    env.hub.records[(owner, "github")] = record
    await env.add_ck(owner=owner)
    return str(record["header"]["id"])


def request(env: Env, actor: str | None = ALICE) -> PrecheckRequest:
    session: Any = session_of()
    return PrecheckRequest(env.context, operation_of(actor, "github.list_identities"), session, DEFAULTS, {})


async def listed(env: Env, data: dict[str, Any], actor: str | None = ALICE) -> dict[str, Any]:
    session: Any = session_of()
    ctx = OperationContext(env.context, operation_of(actor, "github.list_identities"), session, {}, data, 1, None)
    result: dict[str, Any] = await EXECUTORS["github.list_identities"](ctx)
    assert SCHEMAS.errors("ops/github.list_identities.result.json", result) == []
    return result


def test_the_list_names_usable_identities_the_current_one_and_the_co_authors(store_dsn: str) -> None:
    async def body(env: Env, hub: IdentityHub) -> None:
        assert PRECHECKS["github.list_identities"] is listing.check
        assert EXECUTORS["github.list_identities"] is listing.execute
        primary = await legacy(env, ALICE, "alice")
        await legacy(env, BOB, "bob", login="other-user", user_id=102, token=OTHER_TOKEN)
        await env.add_ck(owner=ORG_A)
        await env.add_ck(owner=ORG_B)
        second = hub.add(identity_body(login="alice-two", user_id=201, signing_key=SIGNING), owner=ALICE)
        company = hub.add(identity_body(login="octo-org", user_id=401), owner=ORG_A, kind="org")
        foreign = hub.add(identity_body(login="b-org"), owner=ORG_B, kind="org")
        bobs = hub.add(identity_body(login="bob-two", user_id=302), owner=BOB, author="bob")
        no_login = hub.add({"token": "double-user-token"}, owner=ORG_A, kind="org")
        no_key = hub.add(identity_body(), owner=ORG_A, kind="org", epoch=5)
        for cached in (foreign, bobs, no_login, no_key):
            await hub_fetch(env, cached)
        await choose(
            env.pool,
            session_choice=second,
            task_choice=second,
            defaults=[("wg", WG_A, company), ("org", ORG_A, pid("gone"))],
        )
        await deliver(env.pool, BOB, 1)
        await deliver(env.pool, ZED, 2, state="typed")  # zed has no GitHub identity
        await deliver(env.pool, ALICE, 3)
        checked = await PRECHECKS["github.list_identities"](request(env))
        assert (checked.data["acting_user"], checked.data["github_identity"]) == (ALICE, second)
        result = await listed(env, dict(checked.data))
        assert result == {
            "acting_user": ALICE,
            "current": second,
            "identities": [
                entry(primary, "octo-user", "user", ALICE, primary=True, source="own"),
                entry(second, "alice-two", "user", ALICE, signing=True, source="own"),
                entry(company, "octo-org", "org", ORG_A, source="task_org"),
            ],
            "co_authors": [{"user_id": ALICE, "login": "octo-user"}, {"user_id": BOB, "login": "other-user"}],
        }

    run(store_dsn, body)


def entry(
    identity_id: str,
    login: str,
    kind: str,
    owner: str,
    *,
    primary: bool = False,
    signing: bool = False,
    source: str,
) -> dict[str, Any]:
    return {
        "id": identity_id,
        "login": login,
        "owner_kind": kind,
        "owner_id": owner,
        "primary": primary,
        "signing": signing,
        "source": source,
    }


async def hub_fetch(env: Env, identity_id: str) -> None:
    await identities.fetch(env.context, identity_id)


def test_without_any_identity_the_list_is_empty_and_current_is_null(store_dsn: str) -> None:
    async def body(env: Env, hub: IdentityHub) -> None:
        checked = await PRECHECKS["github.list_identities"](request(env))
        assert checked.data == {"acting_user": ALICE}
        assert await listed(env, dict(checked.data)) == {
            "acting_user": ALICE,
            "current": None,
            "identities": [],
            "co_authors": [],
        }
        with pytest.raises(CheckFailed) as no_actor:
            await PRECHECKS["github.list_identities"](request(env, None))
        assert (no_actor.value.code, no_actor.value.message) == ("github_not_connected", acting.NO_ACTOR)
        with pytest.raises(OperationFailed) as failed:
            await listed(env, {}, None)
        assert failed.value.code == "github_not_connected"
        env.hub.tamper.append("bad_signature")  # the hub cannot be asked for the own record
        assert (await PRECHECKS["github.list_identities"](request(env))).data == {"acting_user": ALICE}
        # a forbidden task choice: nothing applies now
        bobs = hub.add(identity_body(), owner=BOB, author="bob")
        await choose(env.pool, task_choice=bobs)
        assert (await PRECHECKS["github.list_identities"](request(env))).data == {"acting_user": ALICE}
        # an own primary record without a valid login is not listed, one that is refused neither
        await legacy(env, ALICE, "alice", login="not a login")
        assert (await listed(env, {"acting_user": ALICE}))["identities"] == []

    run(store_dsn, body)


def test_a_primary_record_that_stops_opening_is_left_out(store_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    async def body(env: Env, _hub: IdentityHub) -> None:
        await legacy(env, ALICE, "alice")
        found = await identity.primary(env.context, operation_of())
        assert found is not None

        async def known(_context: Any, _operation: Any) -> Any:
            return found

        monkeypatch.setattr(identity, "primary", known)
        env.hub.records.clear()
        await env.rows("DELETE FROM runner.credential_cache RETURNING 1")
        assert (await listed(env, {"acting_user": ALICE}))["identities"] == []

    run(store_dsn, body)


# --- people -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"login": "octo user", "user_id": 1},
        {"login": "octo-user", "user_id": True},
        {"login": "octo-user", "user_id": 0},
        {"login": "octo-user", "user_id": "1"},
        {"login": 7, "user_id": 1},
    ],
)
def test_login_of_needs_a_valid_login_and_account_id(body: dict[str, Any]) -> None:
    assert people.login_of(body) is None


def test_copies_of_the_operators_account_are_no_co_authors(store_dsn: str, github: GitHub, tmp_path: Path) -> None:
    async def body(env: Env, _hub: IdentityHub) -> None:
        await legacy(env, ALICE, "alice")
        await legacy(env, BOB, "bob", login="op-copy", source="operator")
        assert await people.account(env.context, ALICE) == (101, "octo-user")
        assert await people.account(env.context, BOB) is None  # source operator
        assert await people.account(env.context, ZED) is None  # no record
        # on a platform device the operator's account ID decides too (a copy made before KOR-47)
        directory = tmp_path / "operator"
        directory.mkdir(mode=0o700)
        token_file = directory / "github.token"
        env.context.config.operator_github_token_file = token_file
        assert await people.operator_account_id(env.context) is None  # the file is missing
        assert "operator_github file_unusable reason=missing" in env.log()
        assert "runner.github operator_account unknown" in env.log()
        token_file.write_text("double-revoked-token\n")
        token_file.chmod(0o600)
        assert await people.operator_account_id(env.context) is None  # GitHub refuses the token
        token_file.write_text(OTHER_TOKEN + "\n")
        await legacy(env, BOB, "bob", login="other-user", user_id=102, token=OTHER_TOKEN)
        async with env.pool.connection() as conn:
            await conn.execute("DELETE FROM runner.credential_cache WHERE owner_id = %s", (BOB,))
        assert await people.account(env.context, BOB) is None  # the operator's token itself (a foreign copy)
        assert await people.operator_account_id(env.context) == 102
        assert await people.operator_account_id(env.context) == 102  # remembered
        assert github.paths() == ["GET /user", "GET /user"]  # asked once per daemon after the refusal
        found = env.context.identity
        env.context.identity = replace(found, device=replace(found.device, kind="org"))  # no operator's token
        del env.context.operator_github_account
        assert await people.operator_account_id(env.context) is None

    run(store_dsn, body, github)


def test_own_and_company_records_never_cached_are_listed_at_the_hub(store_dsn: str) -> None:
    async def body(env: Env, hub: IdentityHub) -> None:
        await env.add_ck()
        await env.add_ck(owner=ORG_A)
        own = hub.add(identity_body(login="alice-two", user_id=201), owner=ALICE)
        company = hub.add(identity_body(login="octo-org", user_id=401), owner=ORG_A, kind="org")
        result = await listed(env, {"acting_user": ALICE})
        assert [item["id"] for item in result["identities"]] == sorted([own, company])
        assert sorted(hub.listed) == sorted([ALICE, ORG_A])

    run(store_dsn, body)
