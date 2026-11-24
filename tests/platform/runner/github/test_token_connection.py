"""U-RUN-github: a GitHub connection made with an existing token (KOR-25) against the GitHub double: the device checks
and stores the token (`github.connect {token}`), and `github.list_repositories` falls back to `GET /user/repos` when
the App endpoints refuse a token that is not a GitHub App user token. The workgroup's allow-list stays the boundary."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from agent_runner.credentials import cache
from agent_runner.github.client import GitHubOptions

from ..credentials.kit import ALICE, Env, github_body, kit_env, make_record
from .conftest import REPO, GitHub, seed
from .test_device_flow import connect, failed
from .test_executors import execute, fails, with_env

CLASSIC = "double-pat-classic0token"  # not secrets: the double only compares them
FINE = "double-pat-fine0grained"
LIMITED = "double-pat-limited0token"
REVOKED = "double-pat-revoked0token"
SSO = "double-pat-sso0token"
NO_REPO = "double-pat-gist0token"


def token_world() -> dict[str, Any]:
    """The world of `conftest.seed` with personal access tokens of `octo-user`: classic (`repo`), fine-grained,
    one limited to `octo-org/hidden`, a revoked one, one not authorised for the SSO of `octo-org` and one without a
    repository scope."""
    world = seed()
    user = world["users"][0]
    user["tokens"] = [
        *user["tokens"],
        {"token": CLASSIC, "kind": "classic", "scopes": ["repo", "read:org"]},
        {"token": FINE, "kind": "fine_grained"},
        {"token": LIMITED, "kind": "classic", "scopes": ["repo"], "repositories": ["octo-org/hidden"]},
        {"token": REVOKED, "kind": "classic", "scopes": ["repo"], "revoked": True},
        {"token": SSO, "kind": "classic", "scopes": ["repo"], "sso": ["octo-org"]},
        {"token": NO_REPO, "kind": "classic", "scopes": ["gist"]},
    ]
    return world


@pytest.fixture
def tokens(start_github: Any) -> GitHub:
    github: GitHub = start_github(world=token_world())
    return github


ALL = [
    {"repo": REPO, "default_branch": "main", "private": True, "allowed": True},
    {"repo": "octo-org/hidden", "default_branch": "main", "private": True, "allowed": False},
    {"repo": "octo-org/second", "default_branch": "main", "private": False, "allowed": False},
]


@pytest.mark.parametrize("token", [CLASSIC, FINE])
def test_a_personal_token_lists_the_account_repositories_marked_by_the_allow_list(
    store_dsn: str, tokens: GitHub, token: str
) -> None:
    async def body(env: Env) -> None:
        result = await execute(env, "github.list_repositories", {}, data={"allowed": [REPO]})
        assert result == {"repositories": ALL}
        assert tokens.journal() == ["GET /user/installations?per_page=100", "GET /user/repos?per_page=100"]
        assert "runner.github repositories_fallback operation_id=" in env.log()
        assert " list=user_repos\n" in env.log()
        assert token not in env.log()

    with_env(store_dsn, tokens, body, token=token)


def test_a_limited_token_lists_only_its_repositories_and_nothing_is_allowed_by_reach(
    store_dsn: str, tokens: GitHub
) -> None:
    async def body(env: Env) -> None:
        result = await execute(env, "github.list_repositories", {}, data={"allowed": [REPO]})
        assert result == {"repositories": [ALL[1]]}

    with_env(store_dsn, tokens, body, token=LIMITED)


def test_an_app_token_never_reaches_the_fallback(store_dsn: str, tokens: GitHub) -> None:
    async def body(env: Env) -> None:
        await execute(env, "github.list_repositories", {})
        assert "GET /user/repos?per_page=100" not in tokens.journal()
        assert "repositories_fallback" not in env.log()

    with_env(store_dsn, tokens, body)


def test_a_revoked_personal_token_is_not_connected_and_never_falls_back(store_dsn: str, tokens: GitHub) -> None:
    async def body(env: Env) -> None:
        assert (await fails(env, "github.list_repositories", {})).code == "github_not_connected"
        assert tokens.paths() == ["GET /user/installations"]

    with_env(store_dsn, tokens, body, token=REVOKED)


@pytest.mark.parametrize("status", [404, 403])
def test_app_refusals_by_status_fall_back_and_other_failures_do_not(store_dsn: str, status: int) -> None:
    revoked: list[bool] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if revoked:
            return httpx.Response(401, json={"message": "Bad credentials"})
        if request.url.path == "/user/installations":
            return httpx.Response(status, json={"message": "Not Found"})
        return httpx.Response(200, json=[{"full_name": "o/a", "default_branch": "main", "private": False}])

    async def main() -> None:
        async with kit_env(store_dsn, github_url="https://api.mock") as env:
            env.hub.records[(ALICE, "github")] = make_record(github_body())
            await env.add_ck()
            env.context.github_options = GitHubOptions(transport=httpx.MockTransport(handle))
            result = await execute(env, "github.list_repositories", {}, data={"allowed": ["o/a"]})
            assert result == {
                "repositories": [{"repo": "o/a", "default_branch": "main", "private": False, "allowed": True}]
            }
            revoked.append(True)
            failure = await fails(env, "github.list_repositories", {})
            assert failure.code == "github_not_connected"
            assert env.log().count("repositories_fallback") == 1

    asyncio.run(main())


def connected(dsn: str, github: GitHub, token: str) -> tuple[list[Any], Any, str]:
    found: list[Any] = []

    async def check(env: Env, request_id: str) -> None:
        record = None
        if (ALICE, "github") in env.hub.records:
            async with cache.opened(env.context, ALICE, "github") as opened:
                record = dict(opened)
            result = await execute(env, "github.list_repositories", {}, data={"allowed": [REPO]})
            found.append(result)
        found[:0] = [env.hub.bodies(request_id), record, env.log()]

    connect(dsn, github, lambda _c: None, check, client_id=None, body={"token": token})
    return found[0], found[1], found[2]


def test_a_token_connected_on_the_device_works_for_the_operations(store_dsn: str, tokens: GitHub) -> None:
    bodies, record, log = connected(store_dsn, tokens, CLASSIC)
    assert bodies == [{"step": "done", "login": "octo-user", "scopes": ["read:org", "repo"], "source": "token"}]
    assert {key: record[key] for key in ("token", "login", "user_id", "scopes", "source")} == {
        "token": CLASSIC,
        "login": "octo-user",
        "user_id": 101,
        "scopes": ["read:org", "repo"],
        "source": "token",
    }
    assert CLASSIC not in log
    assert tokens.paths() == ["GET /user", "GET /user/installations", "GET /user/repos"]


@pytest.mark.parametrize(
    ("token", "code"),
    [
        (REVOKED, "token_refused"),
        (SSO, "done"),
        (NO_REPO, "scope_missing"),
        ("double-pat-unknown0token", "token_refused"),
    ],
)
def test_tokens_the_device_refuses_store_nothing(store_dsn: str, tokens: GitHub, token: str, code: str) -> None:
    bodies, record, _log = connected(store_dsn, tokens, token)
    if code == "done":
        # GET /user is not an organisation resource: SSO is refused later, per repository
        assert bodies[0]["step"] == "done"
        return
    assert bodies == [failed(code)]
    assert record is None
