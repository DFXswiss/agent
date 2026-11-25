"""U-RUN-github: executors I and pre-checks against the GitHub double (05-runner.md section 12.1, 03-api.md section 9):
results checked against `contracts/ops/<type>.result.json`, the double's journal with the exact REST sequence, and the
failure forms (5xx, 429, secondary limit, 404, 409/422 `branch_exists`, revoked token, missing connection)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from agent_guest.schema import SchemaSet
from agent_runner.credentials import identities
from agent_runner.crypto.encoding import body_json
from agent_runner.crypto.ids import uuid7
from agent_runner.github import branches, comments, precheck, pulls, repos
from agent_runner.github.client import GitHubOptions
from agent_runner.github.executors import EXECUTORS
from agent_runner.github.op_prechecks import PRECHECKS
from agent_runner.operations.extensions import CheckFailed, OperationContext, OperationFailed, PrecheckRequest
from agent_runner.operations.settings import DEFAULTS

from ..credentials.identity_kit import session_of
from ..credentials.kit import ALICE, Env, github_body, kit_env, make_record
from ..sync.fakehub import CONTRACTS
from .conftest import OTHER_TOKEN, REPO, TOKEN, GitHub

SCHEMAS = SchemaSet(CONTRACTS)
Executor = Callable[[OperationContext], Awaitable[dict[str, Any]]]


def context_for(env: Env, params: dict[str, Any], **kw: Any) -> OperationContext:
    return OperationContext(
        context=env.context,
        operation=SimpleNamespace(id=uuid7(), type="github.test", actor_id=kw.get("owner", ALICE)),  # type: ignore[arg-type]
        session=SimpleNamespace(owner_id=kw.get("owner", ALICE)),  # type: ignore[arg-type]
        params=params,
        data=kw.get("data", {}),
        attempt=kw.get("attempt", 1),
        env=None,
    )


def with_env(dsn: str, github: GitHub, body: Callable[[Env], Awaitable[None]], *, token: str | None = TOKEN) -> None:
    async def main() -> None:
        async with kit_env(dsn, github_url=github.url, github_wall=github.clock) as env:
            if token is not None:
                env.hub.records[(ALICE, "github")] = make_record(github_body(token=token))
                await env.add_ck()
            await body(env)

    asyncio.run(main())
    assert github.failures() == []


def result_ok(op_type: str, result: dict[str, Any]) -> None:
    assert SCHEMAS.errors(f"ops/{op_type}.result.json", result) == []


async def execute(env: Env, op_type: str, params: dict[str, Any], **kw: Any) -> dict[str, Any]:
    result: dict[str, Any] = await EXECUTORS[op_type](context_for(env, params, **kw))
    result_ok(op_type, result)
    return result


async def fails(env: Env, op_type: str, params: dict[str, Any], **kw: Any) -> OperationFailed:
    with pytest.raises(OperationFailed) as failed:
        await EXECUTORS[op_type](context_for(env, params, **kw))
    return failed.value


def test_list_repositories_lists_every_installation_repository_once_with_allowed(
    store_dsn: str, github: GitHub
) -> None:
    async def body(env: Env) -> None:
        data = {"allowed": ["octo-org/hello"]}
        result = await execute(env, "github.list_repositories", {}, data=data)
        assert result == {
            "repositories": [
                {"repo": REPO, "default_branch": "main", "private": True, "allowed": True},
                {"repo": "octo-org/second", "default_branch": "main", "private": False, "allowed": False},
            ]
        }
        assert github.journal() == [
            "GET /user/installations?per_page=100",
            "GET /user/installations/31/repositories?per_page=100",
        ]

    with_env(store_dsn, github, body)


def test_list_repositories_of_unreadable_settings_allow_nothing(store_dsn: str, github: GitHub) -> None:
    async def body(env: Env) -> None:
        # restricted without a list (stored settings that no longer open): fail closed, every entry not allowed
        result = await execute(env, "github.list_repositories", {}, data={"allowed": [], "restricted": True})
        assert [entry["allowed"] for entry in result["repositories"]] == [False, False]
        # no restriction: every entry allowed
        result = await execute(env, "github.list_repositories", {}, data={"allowed": [], "restricted": False})
        assert [entry["allowed"] for entry in result["repositories"]] == [True, True]

    with_env(store_dsn, github, body)


def test_list_repositories_leaves_out_entries_outside_the_contract(store_dsn: str) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/user/installations":
            return httpx.Response(200, json={"installations": [{"id": 1}, {"id": 2}]})
        if request.url.path == "/user/installations/1/repositories":
            good = {"full_name": "o/a", "default_branch": "main", "private": False}
            return httpx.Response(
                200,
                json={
                    "repositories": [
                        good,
                        {**good, "full_name": "bad name"},
                        {**good, "default_branch": "-x"},
                        {**good, "private": "no"},
                        "x",
                    ]
                },
            )
        return httpx.Response(
            200, json={"repositories": [{"full_name": "O/A", "default_branch": "dev", "private": True}]}
        )

    async def main() -> None:
        async with kit_env(store_dsn, github_url="https://api.mock") as env:
            env.context.github_options = GitHubOptions(transport=httpx.MockTransport(handle))
            env.hub.records[(ALICE, "github")] = make_record(github_body())
            await env.add_ck()
            ctx = context_for(env, {})
            result = await EXECUTORS["github.list_repositories"](ctx)
            # no allow-list in the pre-check data: every listed repository is allowed (KOR-30)
            assert result == {
                "repositories": [{"repo": "o/a", "default_branch": "main", "private": False, "allowed": True}]
            }
            assert f"runner.github repositories_skipped operation_id={ctx.operation.id} count=4" in env.log()

    asyncio.run(main())


def test_get_pull_request_returns_the_pull_request_files_and_comments(store_dsn: str, github: GitHub) -> None:
    async def body(env: Env) -> None:
        result = await execute(env, "github.get_pull_request", {"repo": REPO, "number": 1})
        assert (result["number"], result["title"], result["state"], result["draft"]) == (1, "Feature", "open", False)
        assert result["head"]["ref"] == "feature"
        assert result["base"]["ref"] == "main"
        assert result["url"] == "https://github.example/octo-org/hello/pull/1"
        assert [(f["path"], f["status"]) for f in result["files"]] == [("src/app.py", "modified")]
        assert "patch" in result["files"][0]
        assert [(c["author"], c["body"]) for c in result["comments"]] == [("other-user", "Looks good.")]
        assert result["truncated"] is False
        assert (result["mergeable"], result["mergeable_state"]) == (True, "clean")
        assert github.journal() == [
            f"GET /repos/{REPO}/pulls/1",  # mergeable null: GitHub starts computing it
            f"GET /repos/{REPO}/pulls/1",
            f"GET /repos/{REPO}/pulls/1/files?per_page=100",
            f"GET /repos/{REPO}/issues/1/comments?per_page=100",
        ]
        assert (await fails(env, "github.get_pull_request", {"repo": REPO, "number": 99})).code == "not_found"

    with_env(store_dsn, github, body)


def test_get_pull_request_cuts_to_the_contract_and_the_event_budget(
    store_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    pull = {
        "number": 5,
        "title": "t" * 2000,
        "body": "b" * 300000,
        "state": "closed",
        "draft": True,
        "head": {"ref": "h", "sha": "a" * 40},
        "base": {"ref": "main", "sha": "b" * 40},
        "html_url": "https://github.com/o/r/pull/5",
    }
    files = [
        {"filename": "a.txt", "status": "added", "additions": 1, "deletions": 0, "patch": "+a"},
        {"filename": "big.txt", "status": "modified", "additions": 1, "deletions": 1, "patch": "x" * 5000},
        {"filename": "bin", "status": "modified", "additions": 0, "deletions": 0},
        {"filename": "/abs", "status": "added", "additions": 1, "deletions": 0},
        {"filename": "odd", "status": "weird", "additions": 1, "deletions": 0},
        {"filename": "neg", "status": "added", "additions": -1, "deletions": 0},
        "x",
    ]
    comments_page = [
        {"id": 1, "user": {"login": "dependabot[bot]"}, "body": "c" * 300000, "created_at": "2026-01-01T00:00:00Z"},
        {"id": 2, "user": None, "body": "x", "created_at": "2026-01-01T00:00:00Z"},
        {"id": 3, "user": {"login": "u"}, "body": None, "created_at": "2026-01-01T00:00:00Z"},
    ]

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/files"):
            return httpx.Response(200, json=files)
        if path.endswith("/comments"):
            return httpx.Response(200, json=comments_page)
        return httpx.Response(200, json=pull)

    monkeypatch.setattr(pulls, "RESULT_BUDGET", 600_000)
    monkeypatch.setattr(pulls, "PATCH_LIMIT", 100)
    monkeypatch.setattr(pulls, "MAX_FILES", 5)

    async def main() -> None:
        async with kit_env(store_dsn, github_url="https://api.mock") as env:
            env.context.github_options = GitHubOptions(transport=httpx.MockTransport(handle))
            env.hub.records[(ALICE, "github")] = make_record(github_body())
            await env.add_ck()
            result = await EXECUTORS["github.get_pull_request"](context_for(env, {"repo": "o/r", "number": 5}))
            result_ok("github.get_pull_request", result)
            assert len(result["title"]) == 1024
            assert len(result["body"]) == 262144
            assert [f["path"] for f in result["files"]] == ["a.txt", "big.txt", "bin"]
            assert [f.get("patch") for f in result["files"]] == ["+a", None, None]
            assert [c["id"] for c in result["comments"]] == [1, 3]
            assert result["comments"][1]["body"] == ""
            assert result["truncated"] is True
            pull.update(state="merged")
            with pytest.raises(OperationFailed) as invalid:
                await EXECUTORS["github.get_pull_request"](context_for(env, {"repo": "o/r", "number": 5}))
            assert invalid.value.code == "upstream_error"
            pull.update(state="open", head={"ref": "-bad", "sha": "a" * 40})
            with pytest.raises(OperationFailed):
                await EXECUTORS["github.get_pull_request"](context_for(env, {"repo": "o/r", "number": 5}))

    asyncio.run(main())


def test_get_pull_request_fits_a_large_pull_request_into_one_event(store_dsn: str) -> None:
    pull = {
        "number": 9,
        "title": "Large",
        "body": "ü" * 150000,
        "state": "open",
        "draft": False,
        "head": {"ref": "h", "sha": "a" * 40},
        "base": {"ref": "main", "sha": "b" * 40},
        "html_url": "https://github.com/o/r/pull/9",
    }
    files = [
        {"filename": f"f{n}.txt", "status": "modified", "additions": 1, "deletions": 1, "patch": "p" * 2000}
        for n in range(300)
    ]
    comments_page = [
        {"id": n, "user": {"login": "u"}, "body": "c" * 1000, "created_at": "2026-01-01T00:00:00Z"}
        for n in range(1, 301)
    ]

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/files"):
            return httpx.Response(200, json=files)
        if path.endswith("/comments"):
            return httpx.Response(200, json=comments_page)
        return httpx.Response(200, json=pull)

    async def main() -> None:
        async with kit_env(store_dsn, github_url="https://api.mock") as env:
            env.context.github_options = GitHubOptions(transport=httpx.MockTransport(handle))
            env.hub.records[(ALICE, "github")] = make_record(github_body())
            await env.add_ck()
            result = await execute(env, "github.get_pull_request", {"repo": "o/r", "number": 9})
            assert len(body_json(result)) <= pulls.RESULT_BUDGET
            assert result["truncated"] is True
            assert result["comments"] == []  # newest comments go first, then files, then the body
            assert len(result["files"]) == 300
            assert 0 < len(result["body"]) < 150000
            comments_page[:] = comments_page[:20]
            pull["body"] = "short"
            fitted = await execute(env, "github.get_pull_request", {"repo": "o/r", "number": 9})
            assert len(body_json(fitted)) <= pulls.RESULT_BUDGET
            assert len(fitted["comments"]) == 20
            assert 0 < sum("patch" in f for f in fitted["files"]) < 300
            pull["title"] = "t" * 1000
            pull["body"] = "b" * 262144
            files[:] = [{**f, "filename": "x" * 4000 + str(n)} for n, f in enumerate(files[:60])]
            squeezed = await execute(env, "github.get_pull_request", {"repo": "o/r", "number": 9})
            assert len(body_json(squeezed)) <= pulls.RESULT_BUDGET

    asyncio.run(main())


def test_create_branch_from_a_branch_or_sha_and_branch_exists(store_dsn: str, github: GitHub) -> None:
    async def body(env: Env) -> None:
        main_sha = github.repo()["refs"]["refs/heads/main"]
        result = await execute(env, "github.create_branch", {"repo": REPO, "branch": "work", "from": "main"})
        assert result == {"branch": "work", "sha": main_sha}
        assert github.repo()["refs"]["refs/heads/work"] == main_sha
        assert github.journal() == [f"GET /repos/{REPO}/git/ref/heads/main", f"POST /repos/{REPO}/git/refs"]
        assert (await execute(env, "github.create_branch", {"repo": REPO, "branch": "w2", "from": main_sha}))[
            "sha"
        ] == main_sha
        short = await execute(env, "github.create_branch", {"repo": REPO, "branch": "w3", "from": main_sha[:7]})
        assert short["sha"] == main_sha
        exists = await fails(env, "github.create_branch", {"repo": REPO, "branch": "work", "from": "feature"})
        assert (exists.code, exists.retryable) == ("branch_exists", False)
        missing = await fails(env, "github.create_branch", {"repo": REPO, "branch": "w4", "from": "nope"})
        assert missing.code == "not_found"
        # a retry that finds the branch it created itself succeeds
        again = await execute(env, "github.create_branch", {"repo": REPO, "branch": "work", "from": "main"}, attempt=2)
        assert again == {"branch": "work", "sha": main_sha}
        other = await fails(env, "github.create_branch", {"repo": REPO, "branch": "work", "from": "feature"}, attempt=2)
        assert other.code == "branch_exists"
        github.faults({"route": "POST /repos/{owner}/{repo}/git/refs", "kind": "error", "status": 500, "times": 1})
        server = await fails(env, "github.create_branch", {"repo": REPO, "branch": "w5", "from": "main"})
        assert (server.code, server.retryable) == ("upstream_error", True)
        github.faults({"route": "POST /repos/{owner}/{repo}/git/refs", "kind": "retry_after", "retry_after": 3})
        limited = await fails(env, "github.create_branch", {"repo": REPO, "branch": "w5", "from": "main"})
        assert (limited.code, limited.retryable, limited.retry_after) == ("upstream_error", True, 3.0)
        github.faults({"route": "POST /repos/{owner}/{repo}/git/refs", "kind": "secondary_rate_limit"})
        secondary = await fails(env, "github.create_branch", {"repo": REPO, "branch": "w5", "from": "main"})
        assert (secondary.retryable, secondary.retry_after) == (True, 60.0)
        bad_sha = await fails(env, "github.create_branch", {"repo": REPO, "branch": "w6", "from": "0" * 40})
        assert bad_sha.code == "upstream_error"

    with_env(store_dsn, github, body)


def test_create_branch_maps_a_409_to_branch_exists(store_dsn: str) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(409, json={"message": "Conflict"})
        if request.url.path.endswith("/heads/b"):
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(200, json={"object": {"sha": "a" * 40}})

    async def main() -> None:
        async with kit_env(store_dsn, github_url="https://api.mock") as env:
            env.context.github_options = GitHubOptions(transport=httpx.MockTransport(handle))
            env.hub.records[(ALICE, "github")] = make_record(github_body())
            await env.add_ck()
            failed = await fails(env, "github.create_branch", {"repo": "o/r", "branch": "b", "from": "main"})
            assert failed.code == "branch_exists"
            unreadable = await fails(
                env, "github.create_branch", {"repo": "o/r", "branch": "b", "from": "main"}, attempt=2
            )
            assert unreadable.code == "branch_exists"
            assert branches.ref_path("feature/x y") == "feature/x%20y"

    asyncio.run(main())


def test_create_pull_request_opens_a_draft_or_returns_the_existing_one(store_dsn: str, github: GitHub) -> None:
    async def body(env: Env) -> None:
        params = {"repo": REPO, "head": "topic", "base": "main", "title": "Topic", "body": "Text.", "draft": True}
        created = await execute(env, "github.create_pull_request", params)
        assert created["draft"] is True
        assert "existing" not in created
        assert created["url"] == f"https://github.example/{REPO}/pull/{created['number']}"
        assert github.journal() == [
            f"GET /repos/{REPO}/pulls?head=octo-org%3Atopic&base=main&state=open",
            f"POST /repos/{REPO}/pulls",
        ]
        existing = await execute(env, "github.create_pull_request", {**params, "head": "feature", "draft": False})
        assert (existing["number"], existing["existing"]) == (1, True)
        assert github.paths()[-1] == f"GET /repos/{REPO}/pulls"
        without_body = {k: v for k, v in params.items() if k != "body"}
        same = await execute(env, "github.create_pull_request", without_body)
        assert same == {**created, "existing": True}
        no_commits = {**params, "head": "main", "base": "topic"}
        assert (await fails(env, "github.create_pull_request", no_commits)).code == "upstream_error"
        unknown = {**params, "head": "nope"}
        assert (await fails(env, "github.create_pull_request", unknown)).code == "not_found"

    with_env(store_dsn, github, body)


def test_create_pull_request_answers_a_pull_request_opened_meanwhile(store_dsn: str) -> None:
    lists: list[list[Any]] = [[], [{"number": 7, "html_url": "https://github.com/o/r/pull/7", "id": 70, "draft": True}]]

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                422,
                json={
                    "message": "Validation Failed",
                    "errors": [{"message": "A pull request already exists for o:h."}],
                },
            )
        return httpx.Response(200, json=lists.pop(0) if lists else {"not": "a list"})

    async def main() -> None:
        async with kit_env(store_dsn, github_url="https://api.mock") as env:
            env.context.github_options = GitHubOptions(transport=httpx.MockTransport(handle))
            env.hub.records[(ALICE, "github")] = make_record(github_body())
            await env.add_ck()
            params = {"repo": "o/r", "head": "h", "base": "main", "title": "T", "draft": True}
            found = await execute(env, "github.create_pull_request", params)
            assert found == {
                "number": 7,
                "url": "https://github.com/o/r/pull/7",
                "id": 70,
                "draft": True,
                "existing": True,
            }
            assert (await fails(env, "github.create_pull_request", params)).code == "upstream_error"

    asyncio.run(main())


def test_create_pull_request_with_a_race_that_left_nothing_open(store_dsn: str) -> None:
    posts = [
        httpx.Response(422, json={"message": "A pull request already exists for o:h."}),
        httpx.Response(502, json={"message": "Bad Gateway"}),
    ]

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return posts.pop(0)
        return httpx.Response(200, json=[])

    async def main() -> None:
        async with kit_env(store_dsn, github_url="https://api.mock") as env:
            env.context.github_options = GitHubOptions(transport=httpx.MockTransport(handle))
            env.hub.records[(ALICE, "github")] = make_record(github_body())
            await env.add_ck()
            params = {"repo": "o/r", "head": "h", "base": "main", "title": "T", "draft": True}
            assert (await fails(env, "github.create_pull_request", params)).code == "upstream_error"
            server = await fails(env, "github.create_pull_request", params)
            assert (server.code, server.retryable) == ("upstream_error", True)

    asyncio.run(main())


def test_comment_posts_once_and_unknown_numbers_are_not_found(store_dsn: str, github: GitHub) -> None:
    async def body(env: Env) -> None:
        result = await execute(env, "github.comment", {"repo": REPO, "number": 1, "body": "Done."})
        assert result["url"].startswith(f"https://github.example/{REPO}/pull/1#issuecomment-")
        assert github.journal() == [f"POST /repos/{REPO}/issues/1/comments"]
        assert [c["body"] for c in github.repo()["comments"]][-1] == "Done."
        assert (await fails(env, "github.comment", {"repo": REPO, "number": 42, "body": "x"})).code == "not_found"

    with_env(store_dsn, github, body)


def test_answers_with_non_https_urls_are_upstream_errors(store_dsn: str, start_github: Any) -> None:
    github = start_github(public=None)

    async def body(env: Env) -> None:
        failed = await fails(env, "github.comment", {"repo": REPO, "number": 1, "body": "x"})
        assert (failed.code, failed.retryable) == ("upstream_error", False)

    with_env(store_dsn, github, body)


def test_operations_need_the_owners_usable_connection(store_dsn: str, github: GitHub) -> None:
    async def body(env: Env) -> None:
        params = {"repo": REPO, "number": 1, "body": "x"}
        assert (await fails(env, "github.comment", params)).code == "github_not_connected"
        assert (await fails(env, "github.comment", params, owner=None)).code == "github_not_connected"
        env.hub.records[(ALICE, "github")] = make_record(github_body(token="revoked-token"))
        await env.add_ck()
        revoked = await fails(env, "github.comment", params)
        assert (revoked.code, revoked.retryable) == ("github_not_connected", False)
        assert revoked.message is not None
        async with env.pool.connection() as conn:
            await conn.execute("DELETE FROM runner.credential_cache")
        env.hub.records[(ALICE, "github")] = make_record({**github_body(), "token": "has space"})
        assert (await fails(env, "github.comment", params)).code == "github_not_connected"
        async with env.pool.connection() as conn:
            await conn.execute("DELETE FROM runner.credential_cache")
        env.hub.tamper.append("bad_signature")
        unreachable = await fails(env, "github.comment", params)
        assert (unreachable.code, unreachable.retryable) == ("upstream_error", True)

    with_env(store_dsn, github, body, token=None)


def precheck_request(env: Env, op_type: str, params: dict[str, Any], owner: str | None = ALICE) -> PrecheckRequest:
    settings = type(DEFAULTS)(
        repositories=frozenset({REPO, "octo-org/hidden"}), protected_branches=(), pr_draft_default=True
    )
    return PrecheckRequest(
        context=env.context,
        operation=SimpleNamespace(id=uuid7(), type=op_type, actor_id=owner),  # type: ignore[arg-type]
        session=session_of(owner),  # type: ignore[arg-type]
        settings=settings,
        params=params,
    )


async def own(env: Env, owner: str = ALICE) -> dict[str, Any]:
    """The pre-check data of an operation that acts with the owner's primary record (KOR-47)."""
    async with env.pool.connection() as conn:
        record_id = await identities.primary_id(conn, owner)
    return {
        "github_identity": record_id,
        "identity_owner": owner,
        "identity_kind": "user",
        "identity_primary": True,
        "acting_user": owner,
    }


async def refused(env: Env, op_type: str, params: dict[str, Any], **kw: Any) -> str:
    with pytest.raises(CheckFailed) as failed:
        await PRECHECKS[op_type](precheck_request(env, op_type, params, **kw))
    assert failed.value.error()["code"] == failed.value.code
    return failed.value.code


def test_prechecks_check_connection_and_repository_access(store_dsn: str, github: GitHub) -> None:
    async def body(env: Env) -> None:
        assert set(PRECHECKS) == set(EXECUTORS)
        listed = await PRECHECKS["github.list_repositories"](precheck_request(env, "github.list_repositories", {}))
        assert listed.data == {"allowed": ["octo-org/hello", "octo-org/hidden"], "restricted": True, **await own(env)}
        assert github.journal() == ["GET /user"]  # the account check of the token, once per record version
        for op_type in (
            "github.get_pull_request",
            "github.create_branch",
            "github.create_pull_request",
            "github.comment",
        ):
            found = await PRECHECKS[op_type](precheck_request(env, op_type, {"repo": REPO}))
            assert (found.dedupe, found.data) == ({}, await own(env))
        assert github.paths() == ["GET /user"] + [f"GET /repos/{REPO}"] * 4
        assert await refused(env, "github.comment", {"repo": "octo-org/hidden"}) == "repo_not_accessible"
        assert await refused(env, "github.comment", {"repo": REPO}, owner=None) == "github_not_connected"
        assert await refused(env, "github.list_repositories", {}, owner=None) == "github_not_connected"
        github.faults({"route": "GET /repos/{owner}/{repo}", "kind": "error", "status": 503, "times": 1})
        assert await refused(env, "github.comment", {"repo": REPO}) == "upstream_error"
        env.hub.records[(ALICE, "github")] = make_record(github_body(token=OTHER_TOKEN + "-revoked"), version=2)
        async with env.pool.connection() as conn:
            await conn.execute("DELETE FROM runner.credential_cache")
        assert await refused(env, "github.comment", {"repo": REPO}) == "github_not_connected"
        async with env.pool.connection() as conn:
            await conn.execute("DELETE FROM runner.credential_cache")
        env.hub.tamper.append("bad_signature")
        assert await refused(env, "github.list_repositories", {}) == "upstream_error"

    with_env(store_dsn, github, body)


def test_the_modules_expose_their_executors() -> None:
    # part I; RUN-10 adds checkout, commit and push (asserted in its own suites)
    assert {
        "github.list_repositories": repos.execute,
        "github.get_pull_request": pulls.execute_get,
        "github.create_branch": branches.execute,
        "github.create_pull_request": pulls.execute_create,
        "github.comment": comments.execute,
    }.items() <= EXECUTORS.items()
    assert PRECHECKS["github.comment"].__wrapped__ is precheck.repo_access  # type: ignore[attr-defined]
