"""U-RUN-github: operations held for the task owner's GitHub connection against the GitHub double (KOR-25;
05-runner.md section 12 step 3): held without a connection, the owner's `github` credential arrives at the device
(fetched from the hub, as after the hint `credentials {owner_id}`), the pipeline wakes and the held operation is
registered and executed with no new request of the session; `github.request_connection` held while not connected,
answered `{login}` once connected."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from agent_runner.credentials import cache, identities
from agent_runner.github.client import GitHubOptions
from agent_runner.github.executors import EXECUTORS
from agent_runner.github.op_prechecks import PRECHECKS
from agent_runner.operations.pipeline import install
from agent_runner.orgs.db import session
from agent_runner.orgs.keys import store_scope_key
from agent_runner.sessions import records as session_records

from ..credentials.kit import ALICE, CLIENT_ID, Env, ck, github_body, make_record
from ..operations.kit import OKit, setup
from ..reconcile.rig import Rig, standard_world
from .conftest import REPO, TOKEN, GitHub
from .test_executors import precheck_request, refused, with_env

TARGET = f"/v1/device/credentials/{ALICE}/github"


def held_run(dsn: str, github: GitHub, body: Callable[[OKit, dict[str, Any]], Awaitable[None]]) -> None:
    """A pipeline installed in the daemon context, the owner's `CK` on the device and no `github` credential; the
    hub serves the record put into the returned mapping (`404` while empty)."""

    async def main() -> None:
        async with Rig(dsn).env(standard_world()) as env:
            env.context.config.github_client_id = CLIENT_ID
            env.context.config.github_api_url = github.url
            env.context.config.github_web_url = github.url
            env.context.github_options = GitHubOptions(wall=github.clock)
            found = await setup(env, executors=EXECUTORS, prechecks=PRECHECKS)
            await found.settings([REPO])
            async with session(env.pool) as conn:
                await store_scope_key(conn, env.sync.storage_key, ("cred", ALICE, 1), ck(), source="wrap")
            install(env.context, found.pipeline)
            served: dict[str, Any] = {}
            original = env.hub.route

            def route(method: str, target: str, data: Any) -> tuple[int, dict[str, Any]]:
                if (method, target) == ("GET", TARGET):
                    return (200, {"record": served["record"]}) if served else (404, {"error": "not_found"})
                return original(method, target, data)

            env.hub.route = route  # type: ignore[method-assign,assignment]
            await body(found, served)

    asyncio.run(main())
    assert github.failures() == []


async def arrive(kit: OKit, served: dict[str, Any]) -> None:
    """The owner connected: the hub holds the record and the device fetches it (hint `credentials`)."""
    served["record"] = make_record(github_body(token=TOKEN))
    kit.pipeline.trigger.clear()
    await cache.fetch(kit.env.context, ALICE, "github")
    assert kit.pipeline.trigger.is_set()


def test_a_held_comment_runs_against_github_once_the_credential_arrives(store_dsn: str, github: GitHub) -> None:
    async def body(kit: OKit, served: dict[str, Any]) -> None:
        sid = await kit.session()
        op_id = await kit.request(sid, "github.comment", {"repo": REPO, "number": 1, "body": "Done."})
        assert await kit.pipeline.scan() == [op_id]
        await kit.pipeline.settle()
        op = await kit.op(op_id)
        assert (op.state, op.held) == ("received", True)
        assert github.paths() == []
        assert kit.hub.requests == []
        statuses = [i["body"] for k, i in await kit.outbox() if k == "event"]
        assert [s["waiting_for"] for s in statuses] == ["github_connection"]
        await arrive(kit, served)
        journaled = kit.seq
        await kit.pipeline.scan()
        await kit.pipeline.settle()
        op = await kit.op(op_id)
        assert (op.state, op.held) == ("succeeded", False)
        assert kit.seq == journaled  # no new request of the session
        # the account check of the token (KOR-47), the access check, the comment
        assert github.paths() == ["GET /user", f"GET /repos/{REPO}", f"POST /repos/{REPO}/issues/1/comments"]
        assert kit.answers() == [op.answer]
        await kit.kit.pushed_events()

    held_run(store_dsn, github, body)


def test_request_connection_is_held_while_not_connected_and_answers_the_login(store_dsn: str, github: GitHub) -> None:
    async def body(kit: OKit, served: dict[str, Any]) -> None:
        sid = await kit.session()
        held = await kit.run(sid, "github.request_connection", {})
        assert (held.state, held.held, held.held_repo) == ("received", True, None)
        ((_kind, event),) = [(k, i) for k, i in await kit.outbox() if k == "event"]
        assert event["body"]["operation_type"] == "github.request_connection"
        assert "repo" not in event["body"]
        await arrive(kit, served)
        await kit.pipeline.process(held.id)
        op = await kit.op(held.id)
        assert (op.state, op.result) == ("succeeded", {"login": "octo-user"})
        # connected: a second request is answered at once, never held
        again = await kit.run(sid, "github.request_connection", {})
        assert (again.state, again.held_until, again.result) == ("succeeded", None, {"login": "octo-user"})
        assert github.paths() == ["GET /user", "GET /user"]
        await kit.kit.pushed_events()

    held_run(store_dsn, github, body)


def test_a_held_issue_of_a_stopped_session_never_reaches_github(store_dsn: str, github: GitHub) -> None:
    async def body(kit: OKit, served: dict[str, Any]) -> None:
        sid = await kit.session()
        op_id = await kit.request(sid, "github.create_issue", {"repo": REPO, "title": "Bug"})
        await kit.pipeline.process(op_id)
        assert (await kit.op(op_id)).held
        async with kit.env.pool.connection() as conn:
            await session_records.update(conn, sid, state="stopped")  # the task was closed
        await arrive(kit, served)
        kit.hub.decisions.append({"state": "rejected", "error_code": "not_found"})
        await kit.pipeline.process(op_id)
        op = await kit.op(op_id)
        assert (op.state, op.error_code) == ("rejected", "not_found")
        assert github.paths() == []

    held_run(store_dsn, github, body)


def test_a_revoked_token_is_sent_to_github_once_per_credential_version(store_dsn: str, github: GitHub) -> None:
    async def body(kit: OKit, served: dict[str, Any]) -> None:
        served["record"] = make_record(github_body(token="revoked-token"))
        await cache.fetch(kit.env.context, ALICE, "github")
        sid = await kit.session()
        op_id = await kit.request(sid, "github.comment", {"repo": REPO, "number": 1, "body": "Done."})
        for _scan in range(10):
            await kit.pipeline.scan()
            await kit.pipeline.settle()
        assert (await kit.op(op_id)).held
        assert github.paths() == ["GET /user"]  # refused once (`401`), not asked again for this version
        served["record"] = make_record(github_body(token=TOKEN), version=2)
        await cache.fetch(kit.env.context, ALICE, "github")
        await kit.pipeline.scan()
        await kit.pipeline.settle()
        assert (await kit.op(op_id)).state == "succeeded"
        assert github.paths() == ["GET /user"] * 2 + [f"GET /repos/{REPO}", f"POST /repos/{REPO}/issues/1/comments"]

    held_run(store_dsn, github, body)


def test_request_connection_with_a_revoked_token_asks_to_connect_again(store_dsn: str, github: GitHub) -> None:
    async def body(kit: OKit, served: dict[str, Any]) -> None:
        served["record"] = make_record(github_body(token="revoked-token"))
        await cache.fetch(kit.env.context, ALICE, "github")
        sid = await kit.session()
        held = await kit.run(sid, "github.request_connection", {})
        assert (held.state, held.held) == ("received", True)
        async with kit.env.pool.connection() as conn:
            assert held.held_credential == await identities.generation(conn)
        statuses = [i["body"] for k, i in await kit.outbox() if k == "event"]
        assert [s["waiting_for"] for s in statuses] == ["github_connection"]
        assert github.paths() == ["GET /user"]
        served["record"] = make_record(github_body(token=TOKEN), version=2)
        await cache.fetch(kit.env.context, ALICE, "github")
        await kit.pipeline.process(held.id)
        op = await kit.op(held.id)
        assert (op.state, op.result) == ("succeeded", {"login": "octo-user"})
        assert github.paths() == ["GET /user", "GET /user"]

    held_run(store_dsn, github, body)


def test_the_request_connection_precheck_verifies_the_token(store_dsn: str, github: GitHub) -> None:
    async def body(env: Env) -> None:
        check = PRECHECKS["github.request_connection"]
        assert await check(precheck_request(env, "github.request_connection", {})) is not None
        assert await refused(env, "github.request_connection", {}, owner=None) == "github_not_connected"
        github.faults({"route": "GET /user", "kind": "error", "status": 503, "times": 1})
        assert await refused(env, "github.request_connection", {}) == "upstream_error"
        env.hub.records[(ALICE, "github")] = make_record(github_body(token="revoked-token"), version=2)
        async with env.pool.connection() as conn:
            await conn.execute("DELETE FROM runner.credential_cache")
        assert await refused(env, "github.request_connection", {}) == "github_not_connected"
        assert github.paths() == ["GET /user"] * 3

    with_env(store_dsn, github, body)
