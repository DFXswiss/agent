"""U-RUN-github: every repository the account can reach is reachable, and one that is not ends with one next step
(KOR-65, rule E-29; `agent_runner.github.reach`, `operations.pipeline` step 1a `github_access`).

Unit part: token kinds from GitHub's prefixes, the checked SSO link, the next step of every refusal. Against the
GitHub double: a user with repositories in three places (own account and fork, an organisation with the GitHub App,
an organisation of a third party that restricts OAuth Apps) reaches all three with the OAuth App's token; with the
GitHub App's token the fork is held with the step "connect" and goes on by itself once the user connected again; a
restricting organisation holds the operation with the approval link and it goes on after the owner approved, without
a new request of the session; SAML single sign-on and a forbidden classic token are held with their steps; the access
is checked again at most every 30 s for an unchanged credential; the timeout names the step."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

from agent_runner.credentials import cache
from agent_runner.github import reach
from agent_runner.github.client import GitHubClient, GitHubError, GitHubOptions, RateGate, sso_link, token_kind
from agent_runner.github.executors import EXECUTORS
from agent_runner.github.op_prechecks import PRECHECKS
from agent_runner.operations.pipeline import ACCESS_RECHECK_S, HOLD_S, TIMEOUT_MESSAGES, install
from agent_runner.orgs.db import session
from agent_runner.orgs.keys import store_scope_key

from ..credentials.kit import ALICE, CLIENT_ID, ck, github_body, make_record
from ..operations.kit import OKit, setup
from ..reconcile.rig import Rig, standard_world
from .conftest import GitHub

APP = "ghu_double-app"  # not secrets: the double only compares them
OAUTH = "gho_double-oauth"
OAUTH_SSO = "gho_double-oauth-sso"
CLASSIC = "ghp_double-classic"
FINE = "github_pat_double-fine"
OAUTH_ID = "Ov23double00000000"
SLUG = "agent-dfx-swiss"
FORK = "octo-user/hello-fork"
OWN = "octo-org/hello"
THIRD = "third-org/app"
SSO_REPO = "sso-org/app"
STRICT = "strict-org/app"
TARGET = f"/v1/device/credentials/{ALICE}/github"


def world() -> dict[str, Any]:
    """`octo-user` with a token of each kind; repositories in the own account (a fork), in `octo-org` (GitHub App
    installed) and in `third-org` (restricts OAuth Apps), `sso-org` (SAML) and `strict-org` (no classic tokens)."""

    def repo(name: str) -> dict[str, Any]:
        return {"full_name": name, "branches": [{"name": "main", "commits": [{"files": {"README.md": "x\n"}}]}]}

    return {
        "users": [
            {
                "login": "octo-user",
                "id": 101,
                "name": "Octo User",
                "email": None,
                "collaborator": [OWN, THIRD, SSO_REPO, STRICT],
                "tokens": [
                    APP,
                    {"token": OAUTH, "kind": "oauth", "scopes": ["repo", "workflow"], "client": OAUTH_ID},
                    {"token": OAUTH_SSO, "kind": "oauth", "scopes": ["repo"], "client": OAUTH_ID, "sso": ["sso-org"]},
                    {"token": CLASSIC, "kind": "classic", "scopes": ["repo"]},
                    {"token": FINE, "kind": "fine_grained", "repositories": [OWN]},
                ],
            }
        ],
        "repositories": [repo(FORK), repo(OWN), repo(THIRD), repo(SSO_REPO), repo(STRICT)],
        "installations": [{"id": 31, "account": "octo-org", "repositories": [OWN], "users": ["octo-user"]}],
        "organizations": [
            {"login": "third-org", "oauth_restricted": True},
            {"login": "strict-org", "classic_forbidden": True},
        ],
    }


@pytest.fixture
def places(start_github: Callable[..., GitHub]) -> GitHub:
    return start_github(world=world())


# -- unit ------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "kind"),
    [
        (APP, "github_app"),
        (OAUTH, "oauth_app"),
        (CLASSIC, "classic"),
        (FINE, "fine_grained"),
        ("double-user-token", "unknown"),
        (None, "unknown"),
    ],
)
def test_the_token_kind_follows_github_prefixes(token: str | None, kind: str) -> None:
    assert token_kind(token) == kind


@pytest.mark.parametrize(
    ("header", "found"),
    [
        (
            "required; url=https://github.com/orgs/sso-org/sso?authorization_request=A1b2_C3-d.4~",
            "https://github.com/orgs/sso-org/sso?authorization_request=A1b2_C3-d.4~",
        ),
        ("required", None),
        ("required; url=https://evil.example/orgs/sso-org/sso?authorization_request=x", None),
        ("required; url=http://github.com/orgs/sso-org/sso?authorization_request=x", None),
        ("required; url=https://github.com/orgs/sso-org/settings?authorization_request=x", None),
        ("required; url=https://github.com/orgs/sso-org/sso?authorization_request=x&next=evil", None),
        ("required; url=https://github.com/orgs/sso-org/sso?authorization_request=x#frag", None),
    ],
)
def test_only_githubs_own_sso_page_is_kept(header: str, found: str | None) -> None:
    assert sso_link("https://github.com", header) == found


def client(token: str | None, **extra: Any) -> GitHubClient:
    async def sleep(_seconds: float) -> None:
        return None

    made = GitHubClient(
        api_url="https://api.github.com",
        web_url="https://github.com",
        token=token,
        wall=lambda: 0.0,
        sleep=sleep,
        gate=RateGate(),
    )
    for name, value in extra.items():
        setattr(made, name, value)
    return made


NOT_FOUND = GitHubError("not_found", status=404)
RESTRICTED = GitHubError(
    "forbidden", status=403, message="... the `third-org` organization has enabled OAuth App access restrictions ..."
)
CLASSIC_REFUSED = GitHubError(
    "forbidden", status=403, message="`strict-org` forbids access via a personal access token (classic)."
)
FINE_URL = (
    "https://github.com/settings/personal-access-tokens/new?name=agent.dfx.swiss&target_name=third-org"
    "&expires_in=none&contents=write&pull_requests=write&issues=write&workflows=write&metadata=read"
)
CLASSIC_URL = "https://github.com/settings/tokens/new?scopes=repo%2Cworkflow&description=agent.dfx.swiss"


@pytest.mark.parametrize(
    ("token", "extra", "error", "step"),
    [
        (APP, {"oauth_client_id": OAUTH_ID}, NOT_FOUND, {"action": "connect", "owner": "third-org"}),
        (
            APP,
            {"app_slug": SLUG},
            NOT_FOUND,
            {"action": "install_app", "owner": "third-org", "url": f"https://github.com/apps/{SLUG}/installations/new"},
        ),
        (APP, {}, NOT_FOUND, {"action": "new_token", "owner": "third-org", "url": CLASSIC_URL}),
        (FINE, {}, NOT_FOUND, {"action": "new_token", "owner": "third-org", "url": FINE_URL}),
        (
            CLASSIC,
            {},
            NOT_FOUND,
            {"action": "check_access", "owner": "third-org", "url": "https://github.com/third-org/app"},
        ),
        (
            OAUTH,
            {"source": "device_flow", "oauth_client_id": OAUTH_ID},
            RESTRICTED,
            {
                "action": "approve_app",
                "owner": "third-org",
                "url": f"https://github.com/settings/connections/applications/{OAUTH_ID}",
            },
        ),
        (
            OAUTH,
            {"source": "token", "oauth_client_id": OAUTH_ID},
            RESTRICTED,
            {"action": "approve_app", "owner": "third-org", "url": "https://github.com/settings/applications"},
        ),
        (CLASSIC, {}, CLASSIC_REFUSED, {"action": "new_token", "owner": "third-org", "url": FINE_URL}),
        (
            CLASSIC,
            {},
            GitHubError("forbidden", status=403, sso=True),
            {"action": "authorize_sso", "owner": "third-org", "url": "https://github.com/orgs/third-org/sso"},
        ),
        (
            OAUTH,
            {},
            GitHubError("forbidden", status=403, sso=True, sso_url="https://github.com/orgs/third-org/sso?a=1"),
            {"action": "authorize_sso", "owner": "third-org", "url": "https://github.com/orgs/third-org/sso?a=1"},
        ),
    ],
)
def test_every_access_refusal_has_one_next_step(
    token: str, extra: dict[str, Any], error: GitHubError, step: dict[str, str]
) -> None:
    async def main() -> None:
        made = client(token, **extra)
        assert reach.next_step(made, THIRD, error) == step
        refused = reach.refusal(made, THIRD, error)
        assert refused is not None
        assert (refused.code, refused.next_step) == ("repo_not_accessible", step)
        assert refused.message is not None
        assert refused.message.startswith("The ")
        assert step.get("url", "") in refused.message
        assert "next_step" not in refused.error()
        await made.aclose()

    asyncio.run(main())


@pytest.mark.parametrize(
    "error",
    [
        GitHubError("forbidden", status=403, message="Resource not accessible by integration"),
        GitHubError("unauthorized", status=401),
        GitHubError("server_error", status=502, retryable=True),
    ],
)
def test_other_failures_are_no_access_refusal(error: GitHubError) -> None:
    async def main() -> None:
        made = client(APP)
        assert reach.refusal(made, THIRD, error) is None
        await made.aclose()

    asyncio.run(main())


def test_a_message_never_exceeds_the_contract() -> None:
    step = {"action": "install_app", "owner": "third-org", "url": "https://github.com/" + "x" * 2000}
    text = reach.message(step, THIRD)
    assert len(text) == 1024
    assert text.isascii()
    assert text.isprintable()


# -- against the double ----------------------------------------------------------------------------------------------

Connect = Callable[..., Awaitable[None]]
Body = Callable[[OKit, Connect], Awaitable[None]]


def access_run(dsn: str, github: GitHub, token: str | None, body: Body, **config: Any) -> None:
    """A pipeline in the daemon context, the owner's `CK` on the device and the owner's `github` credential with
    `token` (none for None); `connect(token, version)` stores another one as the hub would after a new connection.
    `config` sets the GitHub ways of the device (`github_oauth_client_id`, `github_app_slug`)."""

    async def main() -> None:
        async with Rig(dsn).env(standard_world()) as env:
            env.context.config.github_client_id = CLIENT_ID
            env.context.config.github_api_url = github.url
            env.context.config.github_web_url = github.url
            env.context.config.github_oauth_client_id = config.get("oauth")
            env.context.config.github_app_slug = config.get("slug")
            env.context.github_options = GitHubOptions(wall=github.clock)
            found = await setup(env, executors=EXECUTORS, prechecks=PRECHECKS)
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

            async def connect(value: str, version: int, source: str = "device_flow") -> None:
                served["record"] = make_record({**github_body(token=value), "source": source}, version=version)
                await cache.fetch(env.context, ALICE, "github")

            if token is not None:
                await connect(token, 1)
            await body(found, connect)

    asyncio.run(main())
    assert github.failures() == []


async def statuses(kit: OKit) -> list[dict[str, Any]]:
    return [item["body"] for kind, item in await kit.outbox() if kind == "event"]


async def scan(kit: OKit) -> None:
    await kit.pipeline.scan()
    await kit.pipeline.settle()


def test_the_oauth_app_token_reaches_the_own_account_a_fork_and_every_organisation(
    store_dsn: str, places: GitHub
) -> None:
    async def body(kit: OKit, _connect: Connect) -> None:
        sid = await kit.session()
        for repo in (FORK, OWN):
            op = await kit.run(sid, "github.list_issues", {"repo": repo})
            assert op.state == "succeeded", op.answer
        third = await kit.run(sid, "github.list_issues", {"repo": "third-org/app", "state": "all"})
        assert (third.state, third.held, third.held_reason) == ("received", True, "github_access")
        assert places.paths().count(f"GET /repos/{FORK}") == 1

    access_run(store_dsn, places, OAUTH, body, oauth=OAUTH_ID)


def test_a_fork_with_the_github_apps_token_waits_for_the_connection_and_goes_on(store_dsn: str, places: GitHub) -> None:
    async def body(kit: OKit, connect: Connect) -> None:
        sid = await kit.session()
        reached = await kit.run(sid, "github.list_issues", {"repo": OWN})
        assert reached.state == "succeeded"
        op_id = await kit.request(sid, "github.list_issues", {"repo": FORK})
        await scan(kit)
        op = await kit.op(op_id)
        assert (op.state, op.held_reason, op.held_next) == (
            "received",
            "github_access",
            {"action": "connect", "owner": "octo-user"},
        )
        (status,) = await statuses(kit)
        assert status["waiting_for"] == "github_access"
        assert status["next"] == {"action": "connect", "owner": "octo-user"}
        assert status["repo"] == FORK
        journaled = kit.seq
        await connect(OAUTH, 2)  # the user connected again with the OAuth App
        await scan(kit)
        op = await kit.op(op_id)
        assert (op.state, op.held) == ("succeeded", False)
        assert kit.seq == journaled

    access_run(store_dsn, places, APP, body, oauth=OAUTH_ID)


def test_without_the_oauth_app_the_step_installs_the_github_app_and_the_check_repeats(
    store_dsn: str, places: GitHub
) -> None:
    async def body(kit: OKit, _connect: Connect) -> None:
        sid = await kit.session()
        op_id = await kit.request(sid, "github.list_issues", {"repo": THIRD})
        await scan(kit)
        op = await kit.op(op_id)
        assert op.held_next == {
            "action": "install_app",
            "owner": "third-org",
            "url": f"{places.url}/apps/{SLUG}/installations/new",
        }
        for _ in range(5):  # within ACCESS_RECHECK_S: GitHub is not asked again
            await scan(kit)
        assert places.paths().count(f"GET /repos/{THIRD}") == 1
        # the user installs the app for the organisation at GitHub; nothing reaches the runner but time
        places.control.request(
            "POST",
            "/_double/seed",
            {"installations": [{"id": 32, "account": "third-org", "repositories": [THIRD], "users": ["octo-user"]}]},
        )
        kit.env.clock.now += ACCESS_RECHECK_S
        await scan(kit)
        op = await kit.op(op_id)
        assert op.state == "succeeded", op.answer
        assert len(await statuses(kit)) == 1

    access_run(store_dsn, places, APP, body, slug=SLUG)


def test_a_restricting_organisation_holds_with_the_approval_link_and_goes_on_after_it(
    store_dsn: str, places: GitHub
) -> None:
    async def body(kit: OKit, _connect: Connect) -> None:
        sid = await kit.session()
        op_id = await kit.request(sid, "github.list_issues", {"repo": THIRD})
        await scan(kit)
        (status,) = await statuses(kit)
        assert status["next"] == {
            "action": "approve_app",
            "owner": "third-org",
            "url": f"{places.url}/settings/connections/applications/{OAUTH_ID}",
        }
        answer = places.control.request(
            "POST", "/_double/organizations", {"login": "third-org", "oauth_approved": [OAUTH_ID]}
        )
        assert answer.status == 200
        kit.env.clock.now += ACCESS_RECHECK_S
        await scan(kit)
        assert (await kit.op(op_id)).state == "succeeded"

    access_run(store_dsn, places, OAUTH, body, oauth=OAUTH_ID)


def test_sso_and_a_forbidden_classic_token_are_held_with_their_steps(store_dsn: str, places: GitHub) -> None:
    async def body(kit: OKit, connect: Connect) -> None:
        sid = await kit.session()
        sso = await kit.run(sid, "github.list_issues", {"repo": SSO_REPO})
        assert sso.held_next == {"action": "authorize_sso", "owner": "sso-org", "url": f"{places.url}/orgs/sso-org/sso"}
        await connect(CLASSIC, 2, "token")
        strict = await kit.run(sid, "github.list_issues", {"repo": STRICT})
        assert strict.held_next is not None
        assert strict.held_next["action"] == "new_token"
        assert "target_name=strict-org" in strict.held_next["url"]

    access_run(store_dsn, places, OAUTH_SSO, body, oauth=OAUTH_ID)


def test_a_connection_that_arrives_without_access_changes_the_reason_and_announces_again(
    store_dsn: str, places: GitHub
) -> None:
    async def body(kit: OKit, connect: Connect) -> None:
        sid = await kit.session()
        op_id = await kit.request(sid, "github.list_issues", {"repo": THIRD})
        await scan(kit)
        assert (await kit.op(op_id)).held_reason == "github_connection"
        await connect(APP, 1)
        await scan(kit)
        op = await kit.op(op_id)
        assert (op.held_reason, op.held_next) == ("github_access", {"action": "connect", "owner": "third-org"})
        assert [item["waiting_for"] for item in await statuses(kit)] == ["github_connection", "github_access"]
        assert any(f"hold_changed operation_id={op_id} reason=github_access" in line for line in kit.lines)

    access_run(store_dsn, places, None, body, oauth=OAUTH_ID)


def test_the_timeout_names_the_step(store_dsn: str, places: GitHub) -> None:
    async def body(kit: OKit, _connect: Connect) -> None:
        sid = await kit.session()
        op_id = await kit.request(sid, "github.list_issues", {"repo": THIRD})
        await scan(kit)
        assert (await kit.op(op_id)).held
        await kit.age(op_id, HOLD_S + 1)
        async with kit.env.pool.connection() as conn:
            await conn.execute(
                "UPDATE runner.operation SET held_until = now() - interval '1 second' WHERE id = %s", (op_id,)
            )
        kit.hub.decisions.append({"state": "rejected", "error_code": "repo_not_accessible"})
        await scan(kit)
        op = await kit.op(op_id)
        assert (op.state, op.error_code) == ("rejected", "repo_not_accessible")
        assert op.answer is not None
        message = op.answer["error"]["message"]
        assert message.startswith(TIMEOUT_MESSAGES["github_access"] + " The organisation third-org restricts")
        assert f"{places.url}/settings/connections/applications/{OAUTH_ID}" in message

    access_run(store_dsn, places, OAUTH, body, oauth=OAUTH_ID)


def test_github_answers_other_than_access_are_not_held(store_dsn: str, places: GitHub) -> None:
    async def body(kit: OKit, _connect: Connect) -> None:
        sid = await kit.session()
        places.faults({"route": "GET /repos/{owner}/{repo}", "kind": "error", "status": 502, "times": 1})
        failed = await kit.run(sid, "github.list_issues", {"repo": OWN})
        assert (failed.state, failed.held_until) == ("rejected", None)

    access_run(store_dsn, places, OAUTH, body, oauth=OAUTH_ID)


def test_the_double_refuses_the_restricted_organisation_like_github(places: GitHub) -> None:
    headers = {"Authorization": f"Bearer {OAUTH}", "Accept": "application/vnd.github+json"}
    headers["X-GitHub-Api-Version"] = "2022-11-28"
    answer = httpx.get(f"{places.url}/repos/{THIRD}", headers=headers)
    assert answer.status_code == 403
    assert "OAuth App access restrictions" in answer.json()["message"]
