"""U-RUN-credentials (KOR-47 on HOT-14): the one place that opens personal connections (`credentials.acting`) also opens
every GitHub identity record for an operation - a personal record only of the acting user, never a foreign copy of
the operator's token - names co-authors by account ID and login only, and lists the humans who instructed a task."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from agent_runner.credentials import acting, cache, identities
from agent_runner.credentials.cache import CredentialUnavailable

from .identity_kit import ORG_A, IdentityHub, deliver, identity_body, operation_of
from .kit import ALICE, BOB, Env, kit_env, make_record, pid

Body = Callable[[Env, IdentityHub], Awaitable[None]]
OPERATOR_TOKEN = "operator-double-token-1"  # not a secret: compared only


def run(dsn: str, body: Body) -> None:
    async def main() -> None:
        async with kit_env(dsn) as env:
            await body(env, IdentityHub(env.hub).install())

    asyncio.run(main())


def operator_file(tmp_path: Path) -> Path:
    directory = tmp_path / "operator"
    directory.mkdir(mode=0o700)
    found = directory / "github.token"
    found.write_text(OPERATOR_TOKEN + "\n")
    found.chmod(0o600)
    return found


def test_identity_records_open_only_for_the_acting_user_and_never_as_a_foreign_copy(
    store_dsn: str, tmp_path: Path
) -> None:
    async def body(env: Env, hub: IdentityHub) -> None:
        await env.add_ck()
        await env.add_ck(owner=ORG_A)
        own = await identities.meta(env.context, hub.add(identity_body(), owner=ALICE))
        company = await identities.meta(env.context, hub.add(identity_body(login="octo-org"), owner=ORG_A, kind="org"))
        copy = await identities.meta(env.context, hub.add(identity_body(token=OPERATOR_TOKEN), owner=ALICE))
        assert own is not None
        assert company is not None
        assert copy is not None
        async with acting.opened_identity(env.context, operation_of(), own) as body_:
            assert body_["login"] == "octo-user"
        async with acting.opened_identity(env.context, operation_of(BOB), company) as body_:
            assert body_["login"] == "octo-org"  # a company record is no personal connection
        reasons = []
        for operation, record in ((operation_of(None), own), (operation_of(BOB), own)):
            with pytest.raises(CredentialUnavailable) as refused:
                async with acting.opened_identity(env.context, operation, record):
                    pass
            reasons.append(refused.value.reason)
        assert reasons == ["no_actor", "not_actor"]
        with pytest.raises(ValueError, match="personal connection"):
            async with acting.opened_identity(env.context, operation_of(op_type="task.set_title"), own):
                pass
        env.context.config.operator_github_token_file = operator_file(tmp_path)
        env.context.config.operator_github_users = frozenset({BOB})
        with pytest.raises(CredentialUnavailable) as shared:
            async with acting.opened_identity(env.context, operation_of(), copy):
                pass
        assert shared.value.reason == "shared"
        async with env.pool.connection() as conn:
            assert await acting.primary_record_id(conn, operation_of(None)) is None
            assert await acting.primary_record_id(conn, operation_of()) is None

    run(store_dsn, body)


def test_attribution_names_a_person_by_account_and_login_only(store_dsn: str, tmp_path: Path) -> None:
    async def body(env: Env, _hub: IdentityHub) -> None:
        await env.add_ck()
        await env.add_ck(owner=BOB)
        env.hub.records[(ALICE, "github")] = make_record(identity_body())
        assert await acting.attribution(env.context, ALICE) == (101, "octo-user")
        env.hub.records[(BOB, "github")] = make_record(identity_body(source="operator"), owner=BOB, author="bob")
        assert await acting.attribution(env.context, BOB) is None  # a copy of the operator's account
        assert await acting.attribution(env.context, pid("zed")) is None  # no record
        env.hub.get_refusals.append((403, {"error": "forbidden"}))
        assert await acting.attribution(env.context, pid("carol")) is None  # refused by the hub
        for bad in ({"login": "a b", "user_id": 1}, {"login": "x", "user_id": True}, {"login": "x", "user_id": 0}):
            assert acting._account(bad) is None
        env.context.config.operator_github_token_file = operator_file(tmp_path)
        env.context.config.operator_github_users = frozenset({ALICE})
        env.hub.records[(BOB, "github")] = make_record(
            identity_body(token=OPERATOR_TOKEN), owner=BOB, author="bob", version=2
        )
        await cache.fetch(env.context, BOB, "github")
        assert await acting.attribution(env.context, BOB) is None  # a foreign copy of the operator's token

    run(store_dsn, body)


def test_instructors_are_the_authors_of_the_tasks_delivered_prompts(store_dsn: str) -> None:
    async def body(env: Env, _hub: IdentityHub) -> None:
        task = pid("task/instructors")
        await deliver(env.pool, ALICE, 1, task_id=task)
        await deliver(env.pool, BOB, 2, task_id=task, state="typed")
        await deliver(env.pool, ALICE, 3, task_id=task)
        await deliver(env.pool, BOB, 5, task_id=task)
        await deliver(env.pool, pid("zed"), 4, task_id=task, state="pending")  # not delivered
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.prompt_event SET author_id = NULL WHERE scope_seq = 1")
            assert await acting.instructors(conn, task) == [BOB, ALICE]

    run(store_dsn, body)
