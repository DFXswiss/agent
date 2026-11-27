"""U-RUN-github a38: `github.comment` with `a38_review` (FIX-02) against a mocked GitHub - the guard's parser decides
before anything is posted, every refusal names the guard's reasons, transient failures stay retryable, and the step
durations reach the result and the log."""

from __future__ import annotations

import asyncio
import copy
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from agent_cli.a38_errors import GuardError
from agent_cli.a38_review import REVIEW_BEGIN, REVIEW_END, render_review_record
from agent_guest.schema import SchemaSet
from agent_runner.crypto.ids import uuid7
from agent_runner.github import a38
from agent_runner.github.client import GitHubError, GitHubOptions
from agent_runner.github.executors import EXECUTORS
from agent_runner.operations.extensions import OperationContext, OperationFailed

from ..credentials.kit import ALICE, Env, github_body, kit_env, make_record
from ..sync.fakehub import CONTRACTS

SCHEMAS = SchemaSet(CONTRACTS)
HEAD = "a" * 40
BASE = "e" * 40
REPO = "o/r"
AUTHOR = 10
Handler = Callable[[httpx.Request], httpx.Response]


def review_body(*, head: str = HEAD, defects: int = 0, second: bool = False) -> str:
    """A review comment in the shape the guard counts (v1, no REVIEW.md at the base): the first run, or the second
    run without defects."""
    ids = ("conformity-b", "logic-b") if second else ("conformity-a", "logic-a")
    lanes = [{"id": lane, "result": "pass", "status": "complete"} for lane in ids]
    payload: dict[str, Any] = {"schema": "a38-review/v1", "head": head, "passes": 2, "defects": defects, "lanes": lanes}
    shown = copy.deepcopy(payload)
    shown["lanes"] = [
        {**lane, "provider": "Acme", "model": "Acme model", "model_number": "acme-1", "prompt": "Review this in full."}
        for lane in lanes
    ]
    visible = (
        "EN:\nSecond review with Acme model\nNo defects.\n\nDE:\nZweiter Review mit Acme model\nKeine Mängel.\n\n"
        if second
        else "EN:\nFirst review with Acme model\nReady after 2 review passes.\nThe change is covered.\n\n"
        "DE:\nErster Review mit Acme model\nBereit nach 2 Review-Durchläufen.\nDie Änderung ist abgedeckt.\n\n"
    )
    return (
        visible
        + "<details>\n<summary>Details</summary>\n\n"
        + render_review_record(shown, rules_bound=False)
        + f"{REVIEW_BEGIN}\n```json\n{json.dumps(payload)}\n```\n{REVIEW_END}\n"
    )


def first_run_body(*, head: str = HEAD, defects: int = 0) -> str:
    return review_body(head=head, defects=defects)


class GitHub:
    """A mocked GitHub: the posting account, the pull request, its base without REVIEW.md, commit times and files."""

    def __init__(self) -> None:
        self.user: httpx.Response = httpx.Response(200, json={"id": AUTHOR, "login": "octo-user"})
        self.pull: httpx.Response = httpx.Response(
            200, json={"head": {"sha": HEAD}, "base": {"sha": BASE}, "user": {"id": AUTHOR}}
        )
        self.files: httpx.Response = httpx.Response(200, json=[{"status": "modified", "filename": "docs/a.md"}])
        self.posted: list[str] = []
        self.seen: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.seen.append(f"{request.method} {path}")
        if request.method == "POST" and path == f"/repos/{REPO}/issues/1/comments":
            self.posted.append(json.loads(request.content)["body"])
            return httpx.Response(201, json={"id": 77, "html_url": f"https://github.example/{REPO}/pull/1#c77"})
        if path == f"/repos/{REPO}/contents/REVIEW.md":
            assert request.url.params["ref"] == BASE
        routes = {
            "/user": self.user,
            f"/repos/{REPO}/pulls/1": self.pull,
            f"/repos/{REPO}/commits/{BASE}": commit_at("2026-07-01T00:00:00Z"),
            f"/repos/{REPO}/commits/{HEAD}": commit_at("2026-09-01T00:00:00Z"),
            f"/repos/{REPO}/pulls/1/files": self.files,
        }
        return routes.get(path, httpx.Response(404, json={"message": "Not Found"}))


def commit_at(date: str) -> httpx.Response:
    return httpx.Response(200, json={"commit": {"committer": {"date": date}}})


def context_for(env: Env, params: dict[str, Any]) -> OperationContext:
    operation = SimpleNamespace(
        id=uuid7(),
        type="github.comment",
        actor_id=ALICE,
        created_at=datetime.now(UTC) - timedelta(milliseconds=250),
    )
    return OperationContext(
        context=env.context,
        operation=operation,  # type: ignore[arg-type]
        session=SimpleNamespace(owner_id=ALICE),  # type: ignore[arg-type]
        params=params,
        data={},
        attempt=1,
        env=None,
    )


def with_github(dsn: str, github: GitHub, body: Callable[[Env], Any]) -> None:
    async def main() -> None:
        async with kit_env(dsn, github_url="https://api.mock") as env:
            env.context.github_options = GitHubOptions(transport=httpx.MockTransport(github))
            env.hub.records[(ALICE, "github")] = make_record(github_body())
            await env.add_ck()
            await body(env)

    asyncio.run(main())


def params(body: str, **extra: Any) -> dict[str, Any]:
    return {"repo": REPO, "number": 1, "body": body, "a38_review": True, **extra}


async def fails(env: Env, values: dict[str, Any]) -> OperationFailed:
    with pytest.raises(OperationFailed) as failed:
        await EXECUTORS["github.comment"](context_for(env, values))
    return failed.value


def test_a_comment_the_guard_counts_is_posted_with_its_timings(store_dsn: str) -> None:
    github = GitHub()

    async def body(env: Env) -> None:
        text = first_run_body()
        result = await EXECUTORS["github.comment"](context_for(env, params(text)))
        assert SCHEMAS.errors("ops/github.comment.result.json", result) == []
        assert result["comment_id"] == 77
        assert result["a38"]["run"] == "first"
        assert result["a38"]["head"] == HEAD
        assert "signal" not in result["a38"]
        timings = result["a38"]["timings_ms"]
        assert set(timings) == {"queued", "account", "check", "post", "total"}
        assert timings["queued"] >= 250
        assert timings["total"] >= timings["queued"]
        assert github.posted == [text]
        assert github.seen[0] == "GET /user"
        assert github.seen[-1] == f"POST /repos/{REPO}/issues/1/comments"
        line = next(entry for entry in env.log().splitlines() if "runner.github a38_review" in entry)
        assert "posted=true run=first reasons=0" in line
        assert re.search(r"queued_ms=\d+ account_ms=\d+ check_ms=\d+ post_ms=\d+ total_ms=\d+$", line)

    with_github(store_dsn, github, body)


def test_a_second_run_is_posted_with_its_merge_signal(store_dsn: str) -> None:
    github = GitHub()

    async def body(env: Env) -> None:
        text = review_body(second=True)
        result = await EXECUTORS["github.comment"](context_for(env, params(text)))
        assert SCHEMAS.errors("ops/github.comment.result.json", result) == []
        assert (result["a38"]["run"], result["a38"]["signal"]) == ("second", "clear")
        assert github.posted == [text]
        assert "posted=true run=second reasons=0" in env.log()

    with_github(store_dsn, github, body)


def test_a_comment_the_guard_would_not_count_is_not_posted(store_dsn: str) -> None:
    github = GitHub()

    async def body(env: Env) -> None:
        failed = await fails(env, params(first_run_body(defects=1)))
        assert (failed.code, failed.retryable) == ("params_invalid", False)
        assert failed.message is not None
        assert failed.message.startswith(a38.REFUSED)
        assert "review defects must be 0" in failed.message
        assert re.search(r"Check took \d+ ms\.$", failed.message)
        assert github.posted == []
        assert "posted=false run=first reasons=" in env.log()
        github.user = httpx.Response(200, json={"id": AUTHOR + 1, "login": "someone-else"})
        other = await fails(env, params(first_run_body()))
        assert "review comment author is not the pull request author" in str(other.message)
        github.user = httpx.Response(200, json={"id": AUTHOR, "login": "octo-user"})
        github.pull = httpx.Response(404, json={"message": "Not Found"})
        gone = await fails(env, params(first_run_body()))
        assert "pull request unavailable" in str(gone.message)
        github.pull = httpx.Response(200, content=b"{not json")
        unreadable = await fails(env, params(first_run_body()))
        assert "pull request unavailable" in str(unreadable.message)
        assert github.posted == []

    with_github(store_dsn, github, body)


def test_transient_github_failures_stay_retryable_and_post_nothing(store_dsn: str) -> None:
    github = GitHub()

    async def body(env: Env) -> None:
        github.pull = httpx.Response(502, json={"message": "Bad Gateway"})
        server = await fails(env, params(first_run_body()))
        assert (server.code, server.retryable) == ("upstream_error", True)
        github.pull = httpx.Response(200, json={"head": {"sha": HEAD}, "base": {"sha": BASE}, "user": {"id": AUTHOR}})
        github.files = httpx.Response(429, json={"message": "slow down"}, headers={"retry-after": "3"})
        limited = await fails(env, params(first_run_body()))
        assert (limited.code, limited.retryable) == ("upstream_error", True)
        assert github.posted == []

    with_github(store_dsn, github, body)


@pytest.mark.parametrize(
    ("answer", "code", "retryable"),
    [
        (httpx.Response(403, json={"message": "Resource not accessible by integration"}), "upstream_error", False),
        (httpx.Response(404, json={"message": "Not Found"}), "upstream_error", False),
        (httpx.Response(401, json={"message": "Bad credentials"}), "github_not_connected", False),
        (httpx.Response(503, json={"message": "unavailable"}), "upstream_error", True),
        (httpx.Response(200, json={"id": True}), "upstream_error", False),
    ],
)
def test_the_posting_account_must_be_readable(
    store_dsn: str, answer: httpx.Response, code: str, retryable: bool
) -> None:
    github = GitHub()
    github.user = answer

    async def body(env: Env) -> None:
        failed = await fails(env, params(first_run_body()))
        assert (failed.code, failed.retryable) == (code, retryable)
        if answer.status_code in (403, 404):
            assert failed.message == a38.NO_ACCOUNT
        assert github.posted == []

    with_github(store_dsn, github, body)


def test_without_a38_review_the_comment_is_posted_unchecked(store_dsn: str) -> None:
    github = GitHub()

    async def body(env: Env) -> None:
        result = await EXECUTORS["github.comment"](context_for(env, params("Done.", a38_review=False)))
        assert "a38" not in result
        assert github.seen == [f"POST /repos/{REPO}/issues/1/comments"]

    with_github(store_dsn, github, body)


def test_guard_api_pages_and_bounds_and_maps_failures() -> None:
    pages = {
        "/repos/o/r/pulls/1/files": [
            httpx.Response(
                200,
                json=[{"n": 1}, {"n": 2}],
                headers={"link": '<https://api.mock/repos/o/r/pulls/1/files?page=2>; rel="next"'},
            ),
            httpx.Response(200, json=[{"n": 3}]),
        ]
    }

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/broken":
            return httpx.Response(200, content=b"{not json")
        if request.url.path == "/missing":
            return httpx.Response(404, json={"message": "Not Found"})
        if request.url.path == "/down":
            return httpx.Response(500, json={"message": "down"})
        page = 2 if request.url.params.get("page") == "2" else 1
        return pages[request.url.path][page - 1]

    async def main() -> None:
        settings = SimpleNamespace(
            config=SimpleNamespace(
                github_api_url="https://api.mock", github_web_url="https://web.mock", github_client_id="c"
            ),
            github_options=GitHubOptions(transport=httpx.MockTransport(handle)),
            host=SimpleNamespace(sleep=asyncio.sleep),
        )
        from agent_runner.github.client import settings_for  # noqa: PLC0415

        async with settings_for(settings).client("t0ken-value-1234567890") as client:
            api = a38.GuardApi(client, asyncio.get_running_loop())
            items = await asyncio.to_thread(api.paginate, "/repos/o/r/pulls/1/files", hard_limit=10)
            assert items == [{"n": 1}, {"n": 2}, {"n": 3}]
            with pytest.raises(GuardError):
                await asyncio.to_thread(api.paginate, "/repos/o/r/pulls/1/files", hard_limit=2)
            assert await asyncio.to_thread(api.request, "GET", "/missing") == (404, None, {})
            with pytest.raises(GuardError):
                await asyncio.to_thread(api.request, "GET", "/broken")
            with pytest.raises(a38.Transient) as transient:
                await asyncio.to_thread(api.request, "GET", "/down")
            assert isinstance(transient.value.error, GitHubError)
            assert transient.value.error.retryable is True

    asyncio.run(main())


def test_refusal_message_is_printable_ascii_within_the_limit() -> None:
    short = a38.refusal(["review result missing", "Mängel"], 12)
    assert short == a38.REFUSED + "review result missing; M?ngel. Check took 12 ms."
    long = a38.refusal(["x" * 2000], 5)
    assert len(long) == a38.MESSAGE_LIMIT
    assert long.endswith("...")
