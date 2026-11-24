"""U-RUN-github: the GitHub operations through the RUN-07 pipeline (formal checks, the pre-checks of this area,
registration at the contract-checking hub, executor retries, result and answer) against the GitHub double: the exact
REST sequence per operation, retryable 5xx within one operation, an existing pull request answered `duplicate`,
`repo_not_accessible` and a missing connection (03-api.md section 9, 05-runner.md section 12, E2E-22 at unit level)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from agent_runner.credentials.cache import _store
from agent_runner.github.client import GitHubOptions
from agent_runner.github.executors import EXECUTORS
from agent_runner.github.op_prechecks import PRECHECKS
from agent_runner.orgs.db import session
from agent_runner.orgs.keys import store_scope_key

from ..credentials.kit import ALICE, CLIENT_ID, ck, github_body, make_record
from ..operations.kit import OKit, setup
from ..reconcile.rig import Rig, standard_world
from .conftest import REPO, TOKEN, GitHub


def pipeline_run(dsn: str, github: GitHub, body: Callable[[OKit], Awaitable[None]], *, connected: bool = True) -> None:
    async def main() -> None:
        async with Rig(dsn).env(standard_world()) as env:
            env.context.config.github_client_id = CLIENT_ID
            env.context.config.github_api_url = github.url
            env.context.config.github_web_url = github.url
            env.context.github_options = GitHubOptions(wall=github.clock)
            found = await setup(env, executors=EXECUTORS, prechecks=PRECHECKS)
            await found.settings([REPO, "octo-org/hidden"])
            if connected:
                async with session(env.pool) as conn:
                    await _store(conn, ALICE, "github", 1, make_record(github_body(token=TOKEN)))
                    await store_scope_key(conn, env.sync.storage_key, ("cred", ALICE, 1), ck(), source="wrap")
            await body(found)

    asyncio.run(main())
    assert github.failures() == []


def test_a_comment_runs_precheck_then_one_post(store_dsn: str, github: GitHub) -> None:
    async def body(kit: OKit) -> None:
        sid = await kit.session()
        op = await kit.run(sid, "github.comment", {"repo": REPO, "number": 1, "body": "Done."})
        assert (op.state, op.attempt) == ("succeeded", 1)
        assert op.answer is not None
        assert op.answer["status"] == "succeeded"
        assert op.result is not None
        assert op.result["url"].startswith("https://github.example/")
        assert github.paths() == ["GET /user", f"GET /repos/{REPO}", f"POST /repos/{REPO}/issues/1/comments"]
        assert kit.hub.requests[0]["dedupe"] is not None

    pipeline_run(store_dsn, github, body)


def test_a_retryable_5xx_is_retried_within_the_same_operation(store_dsn: str, github: GitHub) -> None:
    github.faults({"route": "POST /repos/{owner}/{repo}/issues/{number}/comments", "kind": "error", "times": 1})

    async def body(kit: OKit) -> None:
        sid = await kit.session()
        op = await kit.run(sid, "github.comment", {"repo": REPO, "number": 1, "body": "Done."})
        assert (op.state, op.attempt) == ("succeeded", 2)
        assert github.paths() == [
            "GET /user",
            f"GET /repos/{REPO}",
            f"POST /repos/{REPO}/issues/1/comments",
            f"POST /repos/{REPO}/issues/1/comments",
        ]
        assert 2.0 in kit.env.clock.sleeps

    pipeline_run(store_dsn, github, body)


def test_an_open_pull_request_at_github_succeeds_as_existing_and_is_answered_duplicate(
    store_dsn: str, github: GitHub
) -> None:
    async def body(kit: OKit) -> None:
        sid = await kit.session()
        params = {"repo": REPO, "head": "feature", "base": "main", "title": "Feature"}
        op = await kit.run(sid, "github.create_pull_request", params)
        assert op.state == "succeeded"
        assert op.answer is not None
        assert op.answer["status"] == "duplicate"
        assert op.answer["result"]["existing"] is True
        assert op.answer["result"]["number"] == 1
        assert github.journal() == [
            "GET /user",
            f"GET /repos/{REPO}",
            f"GET /repos/{REPO}/pulls?head=octo-org%3Afeature&base=main&state=open",
        ]

    pipeline_run(store_dsn, github, body)


def test_an_allowed_repository_the_owner_cannot_reach_is_held_with_a_next_step(store_dsn: str, github: GitHub) -> None:
    async def body(kit: OKit) -> None:
        sid = await kit.session()
        op = await kit.run(sid, "github.create_branch", {"repo": "octo-org/hidden", "branch": "x", "from": "main"})
        assert (op.state, op.held_reason) == ("received", "github_access")
        assert kit.hub.requests == []
        assert github.paths() == ["GET /user", "GET /repos/octo-org/hidden"]

    pipeline_run(store_dsn, github, body)


def test_list_repositories_marks_the_allowed_ones(store_dsn: str, github: GitHub) -> None:
    async def body(kit: OKit) -> None:
        sid = await kit.session()
        op = await kit.run(sid, "github.list_repositories", {})
        assert op.state == "succeeded"
        assert op.result == {
            "repositories": [
                {"repo": REPO, "default_branch": "main", "private": True, "allowed": True},
                {"repo": "octo-org/second", "default_branch": "main", "private": False, "allowed": False},
            ]
        }

    pipeline_run(store_dsn, github, body)


def test_without_a_connection_nothing_reaches_github(store_dsn: str, github: GitHub) -> None:
    async def body(kit: OKit) -> None:
        sid = await kit.session()
        op = await kit.run(sid, "github.comment", {"repo": REPO, "number": 1, "body": "x"})
        # held for the owner's connection (KOR-25): not registered, nothing sent to GitHub
        assert (op.state, op.held, op.registration) == ("received", True, None)
        assert kit.hub.requests == []
        assert github.paths() == []

    pipeline_run(store_dsn, github, body, connected=False)
