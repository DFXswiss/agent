"""U-RUN-github (KOR-25): the issue executors, the pull-request list and the connection request against the GitHub
double (issues with pull requests mixed into the list, pages, a repository without issues, rate limits, SSO, a revoked
token) and against mocked answers that break the contract or exceed the result budget. Every result is checked against
`contracts/ops/<type>.result.json`; the double's journal shows the exact REST requests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from agent_runner.crypto.encoding import body_json
from agent_runner.github import connection, issues, ops, pages, precheck, pull_list
from agent_runner.github.client import GitHubOptions
from agent_runner.github.executors import EXECUTORS
from agent_runner.github.op_prechecks import PRECHECKS
from agent_runner.operations.extensions import CheckFailed

from ..credentials.kit import ALICE, Env, github_body, kit_env, make_record
from .conftest import OTHER_TOKEN, REPO, TOKEN, GitHub
from .test_executors import execute, fails, own, precheck_request, refused, result_ok, with_env

QUIET = "octo-org/quiet"
SSO_TOKEN = "double-sso-token"  # not a secret: the double only compares it
LIST_QUERY = "state=open&sort=created&direction=desc&per_page=30&page=1"
SCAN_QUERY = "state=open&sort=created&direction=desc&per_page=100&page=1"


def world() -> dict[str, Any]:
    """`octo-org/hello` with issues #1 (two comments), #2 (closed, not planned), #3 (newest issue) and pull request #4
    (the newest item, so GitHub lists it first); `octo-org/quiet` without issues; `octo-user` also holds a classic
    token whose organisation `octo-org` requires SSO authorisation."""
    return {
        "users": [
            {
                "login": "octo-user",
                "id": 101,
                "name": "Octo User",
                "email": None,
                "tokens": [TOKEN, {"token": SSO_TOKEN, "kind": "classic", "scopes": ["repo"], "sso": ["octo-org"]}],
            },
            {"login": "other-user", "id": 102, "name": None, "email": None, "tokens": [OTHER_TOKEN]},
        ],
        "repositories": [
            {
                "full_name": REPO,
                "branches": [
                    {"name": "main", "commits": [{"files": {"README.md": "hello\n"}}]},
                    {"name": "feature", "from": "main", "commits": [{"files": {"a.txt": "a\n"}}]},
                    {"name": "develop", "from": "main", "commits": [{"files": {"b.txt": "b\n"}}]},
                ],
                "issues": [
                    {
                        "title": "Login fails",
                        "body": "Steps to reproduce.",
                        "user": "other-user",
                        "labels": ["bug"],
                        "assignees": ["octo-user"],
                    },
                    {"title": "Old idea", "user": "octo-user", "state": "closed", "state_reason": "not_planned"},
                    {"title": "Newest issue", "user": "octo-user", "labels": ["bug", "help wanted"]},
                ],
                "pulls": [{"title": "Feature", "head": "feature", "base": "main", "user": "octo-user"}],
                "comments": [
                    {"number": 1, "body": "Seen it too.", "user": "octo-user"},
                    {"number": 1, "body": "Fixed on main?", "user": "other-user"},
                    {"number": 4, "body": "Looks good.", "user": "other-user"},
                ],
            },
            {
                "full_name": QUIET,
                "has_issues": False,
                "branches": [{"name": "main", "commits": [{}]}],
            },
        ],
        "installations": [{"id": 31, "account": "octo-org", "repositories": [REPO, QUIET], "users": ["octo-user"]}],
    }


@pytest.fixture
def gh(start_github: Callable[..., GitHub]) -> GitHub:
    return start_github(world=world())


def numbers(result: dict[str, Any], key: str = "issues") -> list[int]:
    return [entry["number"] for entry in result[key]]


# --- github.list_issues -------------------------------------------------------------------------------------------


def test_list_issues_lists_open_issues_newest_first_without_pull_requests(store_dsn: str, gh: GitHub) -> None:
    async def body(env: Env) -> None:
        result = await execute(env, "github.list_issues", {"repo": REPO})
        assert (numbers(result), result["next_page"], result["truncated"]) == ([3, 1], None, False)
        first = result["issues"][1]
        assert {key: first[key] for key in ("title", "state", "state_reason", "author", "labels", "assignees")} == {
            "title": "Login fails",
            "state": "open",
            "state_reason": None,
            "author": "other-user",
            "labels": ["bug"],
            "assignees": ["octo-user"],
        }
        assert (first["comments"], first["closed_at"]) == (2, None)
        assert first["url"] == f"https://github.example/{REPO}/issues/1"
        assert gh.journal() == [f"GET /repos/{REPO}/issues?{SCAN_QUERY}"]

    with_env(store_dsn, gh, body)


def test_the_newest_issue_pages_past_a_pull_request(store_dsn: str, gh: GitHub) -> None:
    async def body(env: Env) -> None:
        # The newest item is pull request #4: pages count issues, so the first page of one holds the newest issue.
        first = await execute(env, "github.list_issues", {"repo": REPO, "per_page": 1})
        assert (numbers(first), first["next_page"], first["truncated"]) == ([3], 2, False)
        assert gh.journal()[-1] == f"GET /repos/{REPO}/issues?{SCAN_QUERY}"
        second = await execute(env, "github.list_issues", {"repo": REPO, "per_page": 1, "page": 2})
        assert (numbers(second), second["next_page"]) == ([1], None)
        assert "note" not in second
        beyond = await execute(env, "github.list_issues", {"repo": REPO, "per_page": 1, "page": 3})
        assert (beyond["issues"], beyond["next_page"], beyond["truncated"]) == ([], None, False)

    with_env(store_dsn, gh, body)


def busy_world(busy: str, pulls: int, issues: int) -> dict[str, Any]:
    """`busy`: `issues` old issues, then `pulls` newer pull requests (GitHub lists the pull requests first)."""
    return {
        "repositories": [
            {
                "full_name": busy,
                "branches": [
                    {"name": "main", "commits": [{"files": {"README.md": "x\n"}}]},
                    *({"name": f"f{n}", "from": "main", "commits": [{}]} for n in range(pulls)),
                ],
                "issues": [{"title": f"Old issue {n}", "user": "octo-user"} for n in range(issues)],
                "pulls": [
                    {"title": f"Change {n}", "head": f"f{n}", "base": "main", "user": "octo-user"} for n in range(pulls)
                ],
            }
        ],
        "installations": [{"id": 32, "account": "octo-org", "repositories": [busy], "users": ["octo-user"]}],
    }


def test_a_first_page_lists_issues_even_when_the_newest_entries_are_pull_requests(
    store_dsn: str, gh: GitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HOT-09: the newest entries of the repository are all pull requests; `per_page: 5` still lists five issues, and
    paging lists every issue exactly once (GitHub pages of 4 entries here, so issues straddle GitHub's pages)."""
    busy = "octo-org/busy"
    gh.double.seed(busy_world(busy, pulls=12, issues=12))
    monkeypatch.setattr(issues, "SCAN_PER_PAGE", 4)

    async def body(env: Env) -> None:
        first = await execute(env, "github.list_issues", {"repo": busy, "per_page": 5})
        assert (len(first["issues"]), first["next_page"], first["truncated"]) == (5, 2, False)
        seen = numbers(first)
        page: int | None = first["next_page"]
        while page is not None:
            result = await execute(env, "github.list_issues", {"repo": busy, "per_page": 5, "page": page})
            assert result["issues"] != []
            seen += numbers(result)
            page = result["next_page"]
        assert seen == list(range(12, 0, -1))
        empty = await execute(env, "github.list_issues", {"repo": busy, "per_page": 5, "page": 4})
        assert (empty["issues"], empty["next_page"], empty["truncated"]) == ([], None, False)

    with_env(store_dsn, gh, body)


def test_the_flood_limit_ends_the_reading_with_a_note(
    store_dsn: str, gh: GitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    busy = "octo-org/flood"
    gh.double.seed(busy_world(busy, pulls=7, issues=3))
    monkeypatch.setattr(issues, "SCAN_PER_PAGE", 2)
    monkeypatch.setattr(issues, "SCAN_PAGES", 3)

    async def body(env: Env) -> None:
        # GitHub pages of two: PR PR | PR PR | PR PR | PR issue | issue issue - three pages hold no issue at all.
        start = len(gh.journal())
        cut = await execute(env, "github.list_issues", {"repo": busy, "per_page": 2})
        assert (cut["issues"], cut["next_page"], cut["truncated"]) == ([], None, True)
        assert cut["note"] == issues.SCAN_NOTE.format(rows=6)
        assert len(gh.journal()) - start == 3
        # the other way round the issues come first; a complete page at the limit still names the next page
        oldest = await execute(env, "github.list_issues", {"repo": busy, "per_page": 3, "direction": "asc"})
        assert (len(oldest["issues"]), oldest["next_page"], oldest["truncated"]) == (3, 2, False)
        # three issues only, but the limit ends the reading before the list does: that is not known to be all
        short = await execute(env, "github.list_issues", {"repo": busy, "per_page": 6, "direction": "asc"})
        assert (len(short["issues"]), short["next_page"], short["truncated"]) == (3, None, True)
        assert "note" in short
        monkeypatch.setattr(issues, "SCAN_PER_PAGE", 1)
        monkeypatch.setattr(issues, "SCAN_PAGES", 2)
        full = await execute(env, "github.list_issues", {"repo": busy, "per_page": 2, "direction": "asc"})
        assert (len(full["issues"]), full["next_page"], full["truncated"], "note" in full) == (2, 2, False, False)

    with_env(store_dsn, gh, body)


def test_list_issues_filters_by_state_and_labels_and_sorts(store_dsn: str, gh: GitHub) -> None:
    async def body(env: Env) -> None:
        closed = await execute(env, "github.list_issues", {"repo": REPO, "state": "closed"})
        assert numbers(closed) == [2]
        assert (closed["issues"][0]["state_reason"], closed["issues"][0]["closed_at"] is not None) == (
            "not_planned",
            True,
        )
        labelled = await execute(
            env, "github.list_issues", {"repo": REPO, "state": "all", "labels": ["bug", "help wanted"]}
        )
        assert numbers(labelled) == [3]
        assert gh.journal()[-1] == (
            f"GET /repos/{REPO}/issues?state=all&sort=created&direction=desc&per_page=100&page=1"
            "&labels=bug%2Chelp+wanted"
        )
        everything = await execute(env, "github.list_issues", {"repo": REPO, "state": "all", "labels": []})
        assert numbers(everything) == [3, 2, 1]
        assert "labels" not in gh.journal()[-1]
        oldest = await execute(env, "github.list_issues", {"repo": REPO, "direction": "asc"})
        assert numbers(oldest) == [1, 3]
        talked = await execute(env, "github.list_issues", {"repo": REPO, "sort": "comments"})
        assert numbers(talked) == [1, 3]

    with_env(store_dsn, gh, body)


def test_list_issues_failures(store_dsn: str, gh: GitHub) -> None:
    async def body(env: Env) -> None:
        disabled = await fails(env, "github.list_issues", {"repo": QUIET})
        assert (disabled.code, disabled.message, disabled.retryable) == ("not_found", ops.ISSUES_DISABLED, False)
        missing = await fails(env, "github.list_issues", {"repo": "octo-org/nothing"})
        assert (missing.code, missing.message) == ("not_found", None)
        route = "GET /repos/{owner}/{repo}/issues"
        gh.faults({"route": route, "kind": "retry_after", "retry_after": 7})
        limited = await fails(env, "github.list_issues", {"repo": REPO})
        assert (limited.code, limited.retryable, limited.retry_after) == ("upstream_error", True, 7.0)
        gh.faults({"route": route, "kind": "secondary_rate_limit"})
        secondary = await fails(env, "github.list_issues", {"repo": REPO})
        assert (secondary.code, secondary.retryable, secondary.retry_after) == ("upstream_error", True, 60.0)
        gh.faults({"route": route, "kind": "error", "status": 502, "times": 1})
        server = await fails(env, "github.list_issues", {"repo": REPO})
        assert (server.code, server.retryable) == ("upstream_error", True)

    with_env(store_dsn, gh, body)


def test_a_revoked_token_is_not_connected(store_dsn: str, gh: GitHub) -> None:
    async def revoked(env: Env) -> None:
        gh.double.world.revoke(TOKEN)
        for op_type, params in (
            ("github.list_issues", {"repo": REPO}),
            ("github.get_issue", {"repo": REPO, "number": 1}),
            ("github.create_issue", {"repo": REPO, "title": "T"}),
            ("github.update_issue", {"repo": REPO, "number": 1, "state": "closed"}),
            ("github.list_pull_requests", {"repo": REPO}),
        ):
            failed = await fails(env, op_type, params)
            assert (failed.code, failed.message) == ("github_not_connected", ops.NOT_CONNECTED)

    with_env(store_dsn, gh, revoked)


def test_an_organisation_that_requires_sso_is_named_without_its_url(store_dsn: str, gh: GitHub) -> None:
    async def sso(env: Env) -> None:
        # the executor names the one step (KOR-65): authorise the token for the organisation, at GitHub's page of it
        failed = await fails(env, "github.list_issues", {"repo": REPO})
        assert (failed.code, failed.retryable) == ("repo_not_accessible", False)
        assert failed.message is not None
        assert "requires SAML single sign-on" in failed.message
        # the pre-check names the one next step instead (KOR-65): authorise the token for the organisation
        assert await refused(env, "github.get_issue", {"repo": REPO}) == "repo_not_accessible"
        with pytest.raises(CheckFailed) as caught:
            await PRECHECKS["github.get_issue"](precheck_request(env, "github.get_issue", {"repo": REPO}))
        assert caught.value.next_step is not None
        assert caught.value.next_step["action"] == "authorize_sso"

    with_env(store_dsn, gh, sso, token=SSO_TOKEN)


# --- github.get_issue ---------------------------------------------------------------------------------------------


def test_get_issue_reads_the_issue_with_one_page_of_comments(store_dsn: str, gh: GitHub) -> None:
    async def body(env: Env) -> None:
        result = await execute(env, "github.get_issue", {"repo": REPO, "number": 1})
        assert (result["number"], result["title"], result["body"], result["author"]) == (
            1,
            "Login fails",
            "Steps to reproduce.",
            "other-user",
        )
        assert (result["pull_request"], result["comments_total"], result["comments_next_page"]) == (False, 2, None)
        assert [(c["author"], c["body"]) for c in result["comments"]] == [
            ("octo-user", "Seen it too."),
            ("other-user", "Fixed on main?"),
        ]
        assert result["truncated"] is False
        assert gh.journal() == [
            f"GET /repos/{REPO}/issues/1",
            f"GET /repos/{REPO}/issues/1/comments?per_page=100&page=1",
        ]
        paged = await execute(env, "github.get_issue", {"repo": REPO, "number": 1, "comments_per_page": 1})
        assert ([c["body"] for c in paged["comments"]], paged["comments_next_page"]) == (["Seen it too."], 2)
        last = await execute(
            env, "github.get_issue", {"repo": REPO, "number": 1, "comments_per_page": 1, "comments_page": 2}
        )
        assert ([c["body"] for c in last["comments"]], last["comments_next_page"]) == (["Fixed on main?"], None)
        empty = await execute(env, "github.get_issue", {"repo": REPO, "number": 2})
        assert (empty["body"], empty["comments"], empty["state_reason"]) == (None, [], "not_planned")
        pull = await execute(env, "github.get_issue", {"repo": REPO, "number": 4})
        assert (pull["pull_request"], pull["comments_total"]) == (True, 1)
        assert (await fails(env, "github.get_issue", {"repo": REPO, "number": 99})).code == "not_found"
        disabled = await fails(env, "github.get_issue", {"repo": QUIET, "number": 1})
        assert (disabled.code, disabled.message) == ("not_found", ops.ISSUES_DISABLED)

    with_env(store_dsn, gh, body)


# --- github.create_issue and github.update_issue ------------------------------------------------------------------


def test_create_issue_opens_an_issue_and_answers_number_url_and_id(store_dsn: str, gh: GitHub) -> None:
    async def body(env: Env) -> None:
        params = {"repo": REPO, "title": "Crash on start", "body": "Trace.", "labels": ["bug", "crash"]}
        created = await execute(env, "github.create_issue", params)
        assert created["number"] == 5
        assert created["url"] == f"https://github.example/{REPO}/issues/5"
        stored = next(issue for issue in gh.repo()["issues"] if issue["number"] == 5)
        assert (stored["title"], stored["body"], stored["labels"], stored["user"]) == (
            "Crash on start",
            "Trace.",
            ["bug", "crash"],
            "octo-user",
        )
        minimal = await execute(env, "github.create_issue", {"repo": REPO, "title": "Only a title"})
        assert minimal["number"] == 6
        assert gh.journal() == [f"POST /repos/{REPO}/issues"] * 2
        disabled = await fails(env, "github.create_issue", {"repo": QUIET, "title": "T"})
        assert (disabled.code, disabled.message) == ("not_found", ops.ISSUES_DISABLED)

    with_env(store_dsn, gh, body)


def test_update_issue_closes_reopens_renames_and_relabels(store_dsn: str, gh: GitHub) -> None:
    async def body(env: Env) -> None:
        closed = await execute(
            env, "github.update_issue", {"repo": REPO, "number": 1, "state": "closed", "state_reason": "not_planned"}
        )
        assert closed == {
            "number": 1,
            "url": f"https://github.example/{REPO}/issues/1",
            "state": "closed",
            "state_reason": "not_planned",
            "title": "Login fails",
            "labels": ["bug"],
        }
        reopened = await execute(env, "github.update_issue", {"repo": REPO, "number": 1, "state": "open"})
        assert (reopened["state"], reopened["state_reason"]) == ("open", "reopened")
        renamed = await execute(
            env, "github.update_issue", {"repo": REPO, "number": 1, "title": "Login fails on Safari", "labels": []}
        )
        assert (renamed["title"], renamed["labels"], renamed["state"]) == ("Login fails on Safari", [], "open")
        texted = await execute(env, "github.update_issue", {"repo": REPO, "number": 1, "body": ""})
        assert texted["number"] == 1
        assert gh.journal() == [f"GET /repos/{REPO}/issues/1", f"PATCH /repos/{REPO}/issues/1"] * 4
        assert (await fails(env, "github.update_issue", {"repo": REPO, "number": 99, "title": "x"})).code == (
            "not_found"
        )
        assert gh.journal()[-1] == f"GET /repos/{REPO}/issues/99"

    with_env(store_dsn, gh, body)


def test_update_issue_never_changes_a_pull_request(store_dsn: str, gh: GitHub) -> None:
    async def body(env: Env) -> None:
        changes: list[dict[str, Any]] = [{"state": "closed"}, {"title": "Renamed"}, {"labels": []}]
        for params in changes:
            failed = await fails(env, "github.update_issue", {"repo": REPO, "number": 4, **params})
            assert (failed.code, failed.message, failed.retryable) == ("not_found", issues.PULL_REQUEST, False)
        assert gh.journal() == [f"GET /repos/{REPO}/issues/4"] * 3
        pull = next(p for p in gh.repo()["pulls"] if p["number"] == 4)
        assert (pull["state"], pull["title"]) == ("open", "Feature")
        disabled = await fails(env, "github.update_issue", {"repo": QUIET, "number": 1, "title": "x"})
        assert (disabled.code, disabled.message) == ("not_found", ops.ISSUES_DISABLED)

    with_env(store_dsn, gh, body)


def test_create_and_update_send_exactly_the_given_fields(store_dsn: str) -> None:
    sent: list[tuple[str, str, Any]] = []
    issue = {
        "number": 8,
        "id": 80,
        "title": "T",
        "state": "closed",
        "state_reason": "completed",
        "user": {"login": "u"},
        "labels": [{"name": "bug"}],
        "assignees": [],
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "closed_at": "2026-01-02T00:00:00Z",
        "html_url": "https://github.com/o/r/issues/8",
    }

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        return httpx.Response(200, json=issue)

    async def main() -> None:
        async with mocked_env(store_dsn, handle) as env:
            await execute(env, "github.create_issue", {"repo": "o/r", "title": "T", "body": ""})
            await execute(env, "github.create_issue", {"repo": "o/r", "title": "T", "labels": []})
            result = await execute(
                env, "github.update_issue", {"repo": "o/r", "number": 8, "state": "closed", "state_reason": "completed"}
            )
            assert result["labels"] == ["bug"]
            await execute(env, "github.update_issue", {"repo": "o/r", "number": 8, "labels": ["a", "b"]})
            assert sent == [
                ("POST", "/repos/o/r/issues", {"title": "T", "body": ""}),
                ("POST", "/repos/o/r/issues", {"title": "T", "labels": []}),
                ("GET", "/repos/o/r/issues/8", None),
                ("PATCH", "/repos/o/r/issues/8", {"state": "closed", "state_reason": "completed"}),
                ("GET", "/repos/o/r/issues/8", None),
                ("PATCH", "/repos/o/r/issues/8", {"labels": ["a", "b"]}),
            ]
            # answers that break the contract are upstream errors, never passed on
            issue["html_url"] = "http://github.com/o/r/issues/8"
            assert (await fails(env, "github.create_issue", {"repo": "o/r", "title": "T"})).code == "upstream_error"
            issue.update(html_url="https://github.com/o/r/issues/8", state="merged")
            update = {"repo": "o/r", "number": 8, "title": "T"}
            assert (await fails(env, "github.update_issue", update)).code == "upstream_error"

    asyncio.run(main())


# --- github.list_pull_requests ------------------------------------------------------------------------------------


def test_list_pull_requests_lists_one_page_with_filters(store_dsn: str, gh: GitHub) -> None:
    async def body(env: Env) -> None:
        result = await execute(env, "github.list_pull_requests", {"repo": REPO})
        assert result == {
            "pull_requests": [
                {
                    **{key: result["pull_requests"][0][key] for key in ("created_at", "updated_at")},
                    "number": 4,
                    "title": "Feature",
                    "state": "open",
                    "draft": False,
                    "author": "octo-user",
                    "head": "feature",
                    "base": "main",
                    "url": f"https://github.example/{REPO}/pull/4",
                }
            ],
            "next_page": None,
            "truncated": False,
        }
        assert gh.journal() == [f"GET /repos/{REPO}/pulls?{LIST_QUERY}"]
        other_base = await execute(env, "github.list_pull_requests", {"repo": REPO, "base": "develop"})
        assert other_base["pull_requests"] == []
        assert gh.journal()[-1].endswith("&base=develop")
        closed = await execute(env, "github.list_pull_requests", {"repo": REPO, "state": "closed", "sort": "updated"})
        assert closed["pull_requests"] == []
        missing = await fails(env, "github.list_pull_requests", {"repo": "octo-org/nothing"})
        assert missing.code == "not_found"

    with_env(store_dsn, gh, body)


# --- github.request_connection ------------------------------------------------------------------------------------


def test_request_connection_answers_the_login_of_the_owners_credential(store_dsn: str, gh: GitHub) -> None:
    async def body(env: Env) -> None:
        assert await execute(env, "github.request_connection", {}) == {"login": "octo-user"}
        assert gh.journal() == []  # no GitHub request
        assert await PRECHECKS["github.request_connection"](precheck_request(env, "github.request_connection", {}))
        assert await refused(env, "github.request_connection", {}, owner=None) == "github_not_connected"
        none = await fails(env, "github.request_connection", {}, owner=None)
        assert (none.code, none.message) == ("github_not_connected", ops.NOT_CONNECTED)

    with_env(store_dsn, gh, body)


def test_request_connection_without_a_usable_credential(store_dsn: str, gh: GitHub) -> None:
    async def body(env: Env) -> None:
        assert (await fails(env, "github.request_connection", {})).code == "github_not_connected"
        assert await refused(env, "github.request_connection", {}) == "github_not_connected"
        env.hub.records[(ALICE, "github")] = make_record({**github_body(), "login": "bad login"})
        await env.add_ck()
        assert (await fails(env, "github.request_connection", {})).code == "github_not_connected"
        async with env.pool.connection() as conn:
            await conn.execute("DELETE FROM runner.credential_cache")
        env.hub.tamper.append("bad_signature")
        unreachable = await fails(env, "github.request_connection", {})
        assert (unreachable.code, unreachable.retryable) == ("upstream_error", True)

    with_env(store_dsn, gh, body, token=None)


# --- pre-checks ---------------------------------------------------------------------------------------------------


def test_the_new_types_check_the_connection_and_the_repository(store_dsn: str, gh: GitHub) -> None:
    new = ("github.list_issues", "github.get_issue", "github.create_issue", "github.update_issue")

    async def body(env: Env) -> None:
        for op_type in (*new, "github.list_pull_requests"):
            assert PRECHECKS[op_type].__wrapped__ is precheck.repo_access  # type: ignore[attr-defined]
            found = await PRECHECKS[op_type](precheck_request(env, op_type, {"repo": REPO}))
            assert (found.dedupe, found.data) == ({}, await own(env))
            assert await refused(env, op_type, {"repo": "octo-org/nothing"}) == "repo_not_accessible"
            assert await refused(env, op_type, {"repo": REPO}, owner=None) == "github_not_connected"
        assert {
            "github.list_issues": issues.execute_list,
            "github.get_issue": issues.execute_get,
            "github.create_issue": issues.execute_create,
            "github.update_issue": issues.execute_update,
            "github.list_pull_requests": pull_list.execute,
            "github.request_connection": connection.execute,
        }.items() <= EXECUTORS.items()

    with_env(store_dsn, gh, body)


# --- answers outside the contract and the result budget (mocked GitHub) --------------------------------------------


@asynccontextmanager
async def mocked_env(dsn: str, handle: Callable[[httpx.Request], httpx.Response]) -> AsyncIterator[Env]:
    """`kit_env` with a mocked GitHub transport and the owner's credential."""
    async with kit_env(dsn, github_url="https://api.mock") as env:
        env.context.github_options = GitHubOptions(transport=httpx.MockTransport(handle))
        env.hub.records[(ALICE, "github")] = make_record(github_body())
        await env.add_ck()
        yield env


def issue_item(number: int, **changes: Any) -> dict[str, Any]:
    item = {
        "number": number,
        "title": f"Issue {number}",
        "state": "open",
        "state_reason": None,
        "user": {"login": "octo-user"},
        "labels": [{"name": "bug"}],
        "assignees": [{"login": "octo-user"}],
        "comments": 0,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00.5Z",
        "closed_at": None,
        "html_url": f"https://github.com/o/r/issues/{number}",
        "body": None,
    }
    item.update(changes)
    return item


def test_list_issues_leaves_out_entries_outside_the_contract_and_cuts_texts(store_dsn: str) -> None:
    page: list[Any] = [
        issue_item(1),
        issue_item(2, title="t" * 2000),
        issue_item(3, labels=[{"name": "l" * 101}, {"name": "ok"}, {"name": None}, "plain"]),
        issue_item(4, assignees=[{"login": "bad login"}, {"login": "dependabot[bot]"}, "x"]),
        issue_item(5, state="closed", state_reason="weird", closed_at="2026-01-02T00:00:00Z"),
        issue_item(6, user=None),
        issue_item(7, comments=-1),
        issue_item(8, labels="bug"),
        issue_item(9, assignees=None),
        issue_item(10, state="merged"),
        issue_item(11, closed_at="yesterday"),
        issue_item(12, comments=True),
        issue_item(13, pull_request={"url": "x"}),
        "not an object",
    ]
    links = ['<https://api.mock/repos/o/r/issues?page=2&per_page=100>; rel="next"']

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.params["page"] != "1":
            return httpx.Response(200, json=[issue_item(99)])
        return httpx.Response(200, json=page, headers={"link": links[0]} if links else {})

    async def main() -> None:
        async with mocked_env(store_dsn, handle) as env:
            result = await execute(env, "github.list_issues", {"repo": "o/r"})
            assert numbers(result) == [1, 2, 3, 4, 5, 99]
            assert (result["next_page"], result["truncated"]) == (None, True)
            by_number = {entry["number"]: entry for entry in result["issues"]}
            assert len(by_number[2]["title"]) == 1024
            assert by_number[3]["labels"] == ["ok", "plain"]
            assert by_number[4]["assignees"] == ["dependabot[bot]"]
            assert by_number[5]["state_reason"] is None
            # entries left out still take their place: the page of four starts with #1 and ends before #6 (left out)
            small = await execute(env, "github.list_issues", {"repo": "o/r", "per_page": 4, "page": 2})
            assert (numbers(small), small["next_page"], small["truncated"]) == ([5], 3, True)
            page[:] = [issue_item(1)]
            links.clear()
            clean = await execute(env, "github.list_issues", {"repo": "o/r"})
            assert (numbers(clean), clean["next_page"], clean["truncated"]) == ([1], None, False)
            page[:] = [issue_item(n) for n in range(1, 106)]
            many = await execute(env, "github.list_issues", {"repo": "o/r", "per_page": 100, "page": 1000})
            assert (many["issues"], many["next_page"], many["truncated"]) == ([], None, False)
            last = await execute(env, "github.list_issues", {"repo": "o/r", "per_page": 100})
            assert (len(last["issues"]), last["next_page"], last["truncated"]) == (100, 2, False)
            page[:] = [issue_item(n) for n in range(1, 1002)]
            deepest = await execute(env, "github.list_issues", {"repo": "o/r", "per_page": 1, "page": 1000})
            assert (numbers(deepest), deepest["next_page"]) == ([1000], None)  # no page beyond the contract's range
            page[:] = [issue_item(1, labels=[{"name": f"l{n}"} for n in range(101)])]
            labelled = await execute(env, "github.list_issues", {"repo": "o/r"})
            assert (len(labelled["issues"][0]["labels"]), labelled["truncated"]) == (100, True)
            page[:] = [issue_item(1, assignees=[{"login": f"u{n}"} for n in range(101)])]
            assigned = await execute(env, "github.list_issues", {"repo": "o/r"})
            assert (len(assigned["issues"][0]["assignees"]), assigned["truncated"]) == (100, True)

    asyncio.run(main())


def test_a_listing_answer_that_is_not_a_list_is_an_upstream_error(store_dsn: str) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": "not a list"})

    async def main() -> None:
        async with mocked_env(store_dsn, handle) as env:
            for op_type in ("github.list_issues", "github.list_pull_requests"):
                failed = await fails(env, op_type, {"repo": "o/r"})
                assert (failed.code, failed.retryable) == ("upstream_error", False)

    asyncio.run(main())


@pytest.mark.parametrize(
    ("link", "page"),
    [
        (None, None),
        ('<https://api.mock/x?page=2>; rel="prev"', None),
        ('<https://api.mock/x?per_page=5>; rel="next"', None),
        ('<https://api.mock/x?page=2&page=3>; rel="next"', None),
        ('<https://api.mock/x?page=two>; rel="next"', None),
        ('<https://api.mock/x?page=1>; rel="next"', None),
        ('<https://api.mock/x?page=1001>; rel="next"', None),
        ('<https://api.mock/x?page=1000>; rel="next"', 1000),
        ('<https://elsewhere.example/x?page=7>; rel="next", <https://api.mock/x?page=9>; rel="last"', 7),
    ],
)
def test_the_next_page_is_only_a_number_in_the_contract_range(link: str | None, page: int | None) -> None:
    assert pages.next_page(httpx.Headers({} if link is None else {"link": link})) == page


def test_listings_stay_within_the_result_budget(store_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pages, "RESULT_BUDGET", 4000)
    pulls_page = [pull_item(n, title="p" * 900) for n in range(1, 11)]
    issues_page = [issue_item(n, title="t" * 900) for n in range(1, 11)]

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pulls_page if request.url.path.endswith("/pulls") else issues_page)

    async def main() -> None:
        async with mocked_env(store_dsn, handle) as env:
            listed = await execute(env, "github.list_issues", {"repo": "o/r"})
            assert 0 < len(listed["issues"]) < 10
            assert listed["truncated"] is True
            assert listed["note"] == issues.FIT_NOTE  # next_page goes on after the issues left out
            assert len(body_json(listed)) <= 4000
            pulled = await execute(env, "github.list_pull_requests", {"repo": "o/r"})
            assert 0 < len(pulled["pull_requests"]) < 10
            assert pulled["truncated"] is True
            assert len(body_json(pulled)) <= 4000

    asyncio.run(main())


def comment_item(number: int, size: int) -> dict[str, Any]:
    return {"id": number, "user": {"login": "u"}, "body": "c" * size, "created_at": "2026-01-01T00:00:00Z"}


def test_get_issue_fits_a_large_issue_into_one_event(store_dsn: str) -> None:
    issue = issue_item(9, body="ü" * 300000, title="t" * 2000, comments=150)
    comments: list[Any] = [comment_item(n, 1000) for n in range(1, 101)]
    answers: dict[str, Any] = {"comments": comments}

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/comments"):
            return httpx.Response(200, json=answers["comments"])
        return httpx.Response(200, json=issue)

    async def main() -> None:
        async with mocked_env(store_dsn, handle) as env:
            result = await execute(env, "github.get_issue", {"repo": "o/r", "number": 9})
            assert len(body_json(result)) <= pages.RESULT_BUDGET
            assert result["truncated"] is True
            assert result["comments"] == []  # the newest comments go first, then the body
            assert 0 < len(result["body"]) < 262144
            assert len(result["title"]) == 1024
            assert result["comments_total"] == 150
            issue.update(body="short", title="Short")
            comments[:] = [comment_item(1, 300000), comment_item(2, 10)]
            cut = await execute(env, "github.get_issue", {"repo": "o/r", "number": 9})
            assert (cut["comments"], cut["body"], cut["truncated"]) == ([], "short", True)
            comments[:] = [comment_item(1, 10), {"id": 2, "user": None}, comment_item(3, 10), "x"]
            left_out = await execute(env, "github.get_issue", {"repo": "o/r", "number": 9})
            assert ([c["id"] for c in left_out["comments"]], left_out["truncated"]) == ([1, 3], True)
            comments[:] = [comment_item(1, 10)]
            issue.update(body="b" * 200000, title="t" * 1024)
            tight = await execute(env, "github.get_issue", {"repo": "o/r", "number": 9})
            assert tight["truncated"] is False
            assert len(body_json(tight)) <= pages.RESULT_BUDGET
            comments[:] = [comment_item(1, 10)]
            issue["comments"] = "many"
            assert (await fails(env, "github.get_issue", {"repo": "o/r", "number": 9})).code == "upstream_error"
            issue.update(comments=1, number=0)
            assert (await fails(env, "github.get_issue", {"repo": "o/r", "number": 9})).code == "upstream_error"
            issue["number"] = 9
            answers["comments"] = {"not": "a list"}
            assert (await fails(env, "github.get_issue", {"repo": "o/r", "number": 9})).code == "upstream_error"

    asyncio.run(main())


def pull_item(number: int, **changes: Any) -> dict[str, Any]:
    item = {
        "number": number,
        "title": f"Pull {number}",
        "state": "open",
        "draft": False,
        "user": {"login": "octo-user"},
        "head": {"ref": "feature/x", "sha": "a" * 40},
        "base": {"ref": "main", "sha": "b" * 40},
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "html_url": f"https://github.com/o/r/pull/{number}",
    }
    item.update(changes)
    return item


def test_list_pull_requests_leaves_out_entries_outside_the_contract(store_dsn: str) -> None:
    page: list[Any] = [
        pull_item(1),
        pull_item(2, title="t" * 2000),
        pull_item(3, state="merged"),
        pull_item(4, head={"ref": "-bad"}),
        pull_item(5, base=None),
        pull_item(6, draft="no"),
        pull_item(7, user={"login": "bad login"}),
        "x",
    ]

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=page)

    async def main() -> None:
        async with mocked_env(store_dsn, handle) as env:
            result = await execute(env, "github.list_pull_requests", {"repo": "o/r"})
            assert numbers(result, "pull_requests") == [1, 2]
            assert len(result["pull_requests"][1]["title"]) == 1024
            assert result["truncated"] is True
            page[:] = [pull_item(n) for n in range(1, 106)]
            many = await execute(env, "github.list_pull_requests", {"repo": "o/r"})
            assert (len(many["pull_requests"]), many["truncated"]) == (100, True)
            page[:] = [pull_item(1)]
            clean = await execute(env, "github.list_pull_requests", {"repo": "o/r"})
            assert clean["truncated"] is False
            result_ok("github.list_pull_requests", clean)

    asyncio.run(main())
