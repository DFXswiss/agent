"""U-RUN-github (HOT-30): `github.update_pull_request` - a task changes the title, the text, the draft state and the
labels of its pull request - against the GitHub double (the exact REST and GraphQL requests in its journal) and against
mocked answers that break the contract; through the RUN-07 pipeline (formal checks with the workgroup's repository
list, pre-check, registration at the contract-checking hub); and the limits of the type: another repository is refused,
and nothing of it merges, closes or approves a pull request."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from agent_runner.github import precheck, pull_update
from agent_runner.github.client import graphql_url
from agent_runner.github.executors import EXECUTORS
from agent_runner.github.op_prechecks import PRECHECKS

from ..credentials.kit import Env
from ..operations.kit import OKit
from .conftest import REPO, GitHub
from .test_executors import execute, fails, own, precheck_request, refused, with_env
from .test_issues import mocked_env
from .test_pipeline import pipeline_run

PULL = f"/repos/{REPO}/pulls/1"
NEVER = ("PUT", "/merge", "/reviews", "/git/refs")
"""Requests `github.update_pull_request` must never send: a merge, a review (approve) or a moved branch."""


def pull_of(github: GitHub, number: int = 1) -> dict[str, Any]:
    return next(p for p in github.repo()["pulls"] if p["number"] == number)


def never_merges(github: GitHub) -> None:
    for line in github.paths():
        assert not line.startswith("PUT "), line
        assert not any(part in line for part in NEVER[1:]), line


# --- the executor against the double ------------------------------------------------------------------------------


def test_a_task_changes_the_title_and_the_text_of_its_pull_request(store_dsn: str, github: GitHub) -> None:
    async def body(env: Env) -> None:
        params = {"repo": REPO, "number": 1, "title": "Fix the login", "body": "Closes #17.\n\nTested on Safari."}
        result = await execute(env, "github.update_pull_request", params)
        assert result == {
            "number": 1,
            "url": f"https://github.example/{REPO}/pull/1",
            "title": "Fix the login",
            "state": "open",
            "draft": False,
            "labels": [],
        }
        stored = pull_of(github)
        assert (stored["title"], stored["body"], stored["state"]) == (
            "Fix the login",
            "Closes #17.\n\nTested on Safari.",
            "open",
        )
        assert github.journal() == [f"GET {PULL}", f"PATCH {PULL}", f"GET {PULL}"]
        sent = [r for r in github.double.journal.records() if r["method"] == "PATCH"]
        assert [r["body"] for r in sent] == [{"title": "Fix the login", "body": "Closes #17.\n\nTested on Safari."}]
        # only the text: the title stays
        texted = await execute(env, "github.update_pull_request", {"repo": REPO, "number": 1, "body": ""})
        assert texted["title"] == "Fix the login"
        assert pull_of(github)["body"] == ""
        never_merges(github)

    with_env(store_dsn, github, body)


def test_draft_and_ready_go_through_graphql_and_labels_through_the_issue(store_dsn: str, github: GitHub) -> None:
    async def body(env: Env) -> None:
        drafted = await execute(env, "github.update_pull_request", {"repo": REPO, "number": 1, "draft": True})
        assert drafted["draft"] is True
        assert pull_of(github)["draft"] is True
        assert github.journal() == [f"GET {PULL}", "POST /graphql", f"GET {PULL}"]
        sent = [r["body"] for r in github.double.journal.records() if r["path"] == "/graphql"]
        assert "convertPullRequestToDraft" in sent[0]["query"]
        assert set(sent[0]["variables"]) == {"id"}
        # the same state again sends no mutation
        start = len(github.journal())
        again = await execute(env, "github.update_pull_request", {"repo": REPO, "number": 1, "draft": True})
        assert (again["draft"], github.journal()[start:]) == (True, [f"GET {PULL}", f"GET {PULL}"])
        ready = await execute(
            env, "github.update_pull_request", {"repo": REPO, "number": 1, "draft": False, "labels": ["bug", "ui"]}
        )
        assert (ready["draft"], ready["labels"]) == (False, ["bug", "ui"])
        assert github.journal()[-4:] == [f"GET {PULL}", f"PATCH /repos/{REPO}/issues/1", "POST /graphql", f"GET {PULL}"]
        last = [r["body"] for r in github.double.journal.records() if r["path"] == "/graphql"][-1]
        assert "markPullRequestReadyForReview" in last["query"]
        stored = pull_of(github)
        assert (stored["draft"], stored["labels"], stored["state"]) == (False, ["bug", "ui"], "open")
        cleared = await execute(env, "github.update_pull_request", {"repo": REPO, "number": 1, "labels": []})
        assert cleared["labels"] == []
        never_merges(github)

    with_env(store_dsn, github, body)


def test_unknown_and_closed_pull_requests_fail_without_a_change(store_dsn: str, github: GitHub) -> None:
    async def body(env: Env) -> None:
        missing = await fails(env, "github.update_pull_request", {"repo": REPO, "number": 99, "title": "x"})
        assert (missing.code, missing.retryable) == ("not_found", False)
        assert github.journal() == [f"GET /repos/{REPO}/pulls/99"]
        with github.double.world.lock:
            github.double.world.repo("octo-org", "hello").pulls[1].state = "closed"  # type: ignore[union-attr]
        closed = await fails(env, "github.update_pull_request", {"repo": REPO, "number": 1, "draft": True})
        assert (closed.code, closed.retryable) == ("upstream_error", False)
        assert pull_of(github)["draft"] is False
        never_merges(github)

    with_env(store_dsn, github, body)


def test_prechecks_check_the_connection_and_the_repository(store_dsn: str, github: GitHub) -> None:
    op_type = "github.update_pull_request"

    async def body(env: Env) -> None:
        assert PRECHECKS[op_type].__wrapped__ is precheck.repo_access  # type: ignore[attr-defined]
        found = await PRECHECKS[op_type](precheck_request(env, op_type, {"repo": REPO}))
        assert (found.dedupe, found.data) == ({}, await own(env))
        # a repository the identity cannot reach and an owner without a connection
        assert await refused(env, op_type, {"repo": "octo-org/hidden"}) == "repo_not_accessible"
        assert await refused(env, op_type, {"repo": REPO}, owner=None) == "github_not_connected"
        assert EXECUTORS[op_type] is pull_update.execute

    with_env(store_dsn, github, body)


# --- answers outside the contract (mocked GitHub) -----------------------------------------------------------------


def pull_answer(**changes: Any) -> dict[str, Any]:
    answer = {
        "number": 8,
        "node_id": "PR_kwDOAbc123",
        "title": "T",
        "state": "open",
        "draft": True,
        "labels": [{"name": "bug"}, {"name": "x" * 101}],
        "html_url": "https://github.com/o/r/pull/8",
    }
    answer.update(changes)
    return answer


def test_answers_outside_the_contract_and_graphql_refusals(store_dsn: str) -> None:
    answers: dict[str, Any] = {"pull": pull_answer(), "graphql": {"data": {"x": {}}}, "graphql_status": 200}
    sent: list[tuple[str, str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        if request.url.path == "/graphql":
            return httpx.Response(answers["graphql_status"], json=answers["graphql"])
        return httpx.Response(200, json=answers["pull"])

    async def main() -> None:
        async with mocked_env(store_dsn, handle) as env:
            params = {"repo": "o/r", "number": 8, "draft": False}
            result = await execute(env, "github.update_pull_request", params)
            assert result == {
                "number": 8,
                "url": "https://github.com/o/r/pull/8",
                "title": "T",
                "state": "open",
                "draft": True,
                "labels": ["bug"],
            }
            assert sent[1] == ("POST", "/graphql", {"query": pull_update.TO_READY, "variables": {"id": "PR_kwDOAbc123"}})
            answers["pull"] = pull_answer(title="t" * 2000)
            assert len((await execute(env, "github.update_pull_request", {**params, "draft": True}))["title"]) == 1024
            refusals = [
                ({"errors": [{"type": "NOT_FOUND", "message": "Could not resolve"}]}, "not_found"),
                ({"errors": [{"type": "FORBIDDEN", "message": "Resource not accessible"}]}, "upstream_error"),
                ({"errors": [{"message": "Pull request is closed"}, "x"]}, "upstream_error"),
                ({"errors": "broken"}, "upstream_error"),
                ({"data": None}, "upstream_error"),
                (["not", "an", "object"], "upstream_error"),
            ]
            for answer, code in refusals:
                answers["graphql"] = answer
                assert (await fails(env, "github.update_pull_request", params)).code == code, answer
            answers["graphql"] = {"data": {"x": {}}}
            for broken in (
                {"node_id": "not a node id"},
                {"state": "merged"},
                {"draft": None},
                {"labels": "bug"},
                {"html_url": "http://github.com/o/r/pull/8"},
            ):
                answers["pull"] = pull_answer(**broken)
                assert (await fails(env, "github.update_pull_request", params)).code == "upstream_error", broken
            answers["pull"] = pull_answer()
            answers["graphql_status"] = 502
            server = await fails(env, "github.update_pull_request", params)
            assert (server.code, server.retryable) == ("upstream_error", True)

    asyncio.run(main())


@pytest.mark.parametrize(
    ("api_url", "endpoint"),
    [
        ("https://api.github.com", "https://api.github.com/graphql"),
        ("https://api.github.com/", "https://api.github.com/graphql"),
        ("https://ghe.example/api/v3", "https://ghe.example/api/graphql"),
        ("http://127.0.0.1:9000", "http://127.0.0.1:9000/graphql"),
    ],
)
def test_the_graphql_endpoint_of_an_api_url(api_url: str, endpoint: str) -> None:
    assert graphql_url(api_url) == endpoint


# --- through the pipeline: the task's repositories only, never a merge --------------------------------------------


def test_through_the_pipeline_a_task_changes_title_and_text(store_dsn: str, github: GitHub) -> None:
    async def body(kit: OKit) -> None:
        sid = await kit.session()
        params = {"repo": REPO, "number": 1, "title": "Fix the login", "body": "Closes #17."}
        op = await kit.run(sid, "github.update_pull_request", params)
        assert (op.state, op.attempt) == ("succeeded", 1)
        assert op.answer is not None
        assert op.answer["status"] == "succeeded"
        assert op.result is not None
        assert (op.result["title"], op.result["draft"]) == ("Fix the login", False)
        assert github.paths() == ["GET /user", f"GET /repos/{REPO}", f"GET {PULL}", f"PATCH {PULL}", f"GET {PULL}"]
        assert (pull_of(github)["title"], pull_of(github)["body"]) == ("Fix the login", "Closes #17.")
        # registered at the hub like every operation (audited there); no dedupe fields: setting values is idempotent
        assert kit.hub.requests[0]["dedupe"] is None

    pipeline_run(store_dsn, github, body)


def test_a_repository_outside_the_workgroups_list_is_refused(store_dsn: str, github: GitHub) -> None:
    async def body(kit: OKit) -> None:
        sid = await kit.session()
        kit.hub.decisions.append({"state": "rejected", "error_code": "repo_not_allowed"})
        # `octo-org/second` is reachable with the connection but not in the workgroup's repository list
        op = await kit.run(sid, "github.update_pull_request", {"repo": "octo-org/second", "number": 1, "title": "x"})
        assert (op.state, op.error_code) == ("rejected", "repo_not_allowed")
        (registration,) = kit.hub.requests
        assert (registration["local_check"], registration["error_code"]) == ("rejected", "repo_not_allowed")
        assert github.paths() == []

    pipeline_run(store_dsn, github, body)


@pytest.mark.parametrize(
    "extra",
    [
        {"merge": True},
        {"state": "closed"},
        {"merge_method": "squash"},
        {"event": "APPROVE"},
        {"head_sha": "a" * 40},
    ],
)
def test_the_type_cannot_merge_close_or_approve(store_dsn: str, github: GitHub, extra: dict[str, Any]) -> None:
    async def body(kit: OKit) -> None:
        sid = await kit.session()
        kit.hub.decisions.append({"state": "rejected", "error_code": "params_invalid"})
        op = await kit.run(sid, "github.update_pull_request", {"repo": REPO, "number": 1, "title": "x", **extra})
        assert (op.state, op.error_code) == ("rejected", "params_invalid")
        assert op.answer is not None
        assert op.answer["error"]["pointers"] == [f"/params/{next(iter(extra))}"]
        assert github.paths() == []
        assert pull_of(github)["state"] == "open"

    pipeline_run(store_dsn, github, body)


def test_an_update_that_changes_nothing_is_refused(store_dsn: str, github: GitHub) -> None:
    async def body(kit: OKit) -> None:
        sid = await kit.session()
        kit.hub.decisions.append({"state": "rejected", "error_code": "params_invalid"})
        op = await kit.run(sid, "github.update_pull_request", {"repo": REPO, "number": 1})
        assert (op.state, op.error_code) == ("rejected", "params_invalid")
        assert github.paths() == []

    pipeline_run(store_dsn, github, body)
