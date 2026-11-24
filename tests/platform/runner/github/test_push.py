"""U-RUN-github: `github.push` (05-runner.md section 12.1, 03-api.md section 9) through the operation pipeline against
the GitHub double: a new branch is created, a fast-forward is a `PATCH` with `force:false`, an identical branch is left
alone, a diverged or unrelated branch is `non_fast_forward`, a commit of another task is refused by the formal
checks; and with a mock transport: a lost race on create/update, unexpected compare answers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from agent_guest.schema import SchemaSet
from agent_runner.github import checkout, commit, push
from agent_runner.github.client import GitHubOptions
from agent_runner.github.executors import EXECUTORS
from agent_runner.github.op_prechecks import PRECHECKS

from ..sync.fakehub import CONTRACTS
from .conftest import PUBLIC, REPO, GitHub
from .run10_kit import R10, MockDouble, refused, run10, world

SCHEMAS = SchemaSet(CONTRACTS)


def refs(github: GitHub) -> dict[str, str]:
    repo = github.double.world.repo("octo-org", "hello")
    assert repo is not None
    found: dict[str, str] = repo.refs
    return found


async def committed(r: R10, sid: str, branch: str, text: str, *, checkout: bool = True) -> str:
    if checkout:
        assert (await r.run(sid, "github.checkout", {"repo": REPO})).state == "succeeded"
    root = r.guest.work(sid) / "repos" / "octo-org" / "hello"
    (root / "README.md").write_text(text)
    op = await r.run(sid, "github.commit", {"repo": REPO, "branch": branch, "message": "M."})
    assert op.result is not None, op.answer
    sha: str = op.result["commit_sha"]
    return sha


def test_push_creates_then_fast_forwards_then_leaves_alone(store_dsn: str, start_github: Any, tmp_path: Path) -> None:
    github = start_github(world=world())

    async def body(r: R10) -> None:
        sid = await r.session()
        first = await committed(r, sid, "work", "one\n")
        start = len(r.journal())
        created = await r.run(sid, "github.push", {"repo": REPO, "branch": "work", "commit_sha": first})
        assert created.state == "succeeded", created.answer
        assert created.result == {"branch": "work", "sha": first, "url": f"{PUBLIC}/{REPO}/tree/work"}
        assert SCHEMAS.errors("ops/github.push.result.json", created.result) == []
        assert r.journal()[start:] == [
            f"GET /repos/{REPO}",
            f"GET /repos/{REPO}/git/ref/heads/work",
            f"POST /repos/{REPO}/git/refs",
        ]
        assert refs(github)["refs/heads/work"] == first
        second = await committed(r, sid, "work", "two\n", checkout=False)
        start = len(r.journal())
        forward = await r.run(sid, "github.push", {"repo": REPO, "branch": "refs/heads/work", "commit_sha": second})
        assert forward.state == "succeeded", forward.answer
        assert r.journal()[start:] == [
            f"GET /repos/{REPO}",
            f"GET /repos/{REPO}/git/ref/heads/work",
            f"GET /repos/{REPO}/compare/{first}...{second}",
            f"PATCH /repos/{REPO}/git/refs/heads/work",
        ]
        assert refs(github)["refs/heads/work"] == second
        start = len(r.journal())
        same = await r.run(sid, "github.push", {"repo": REPO, "branch": "work", "commit_sha": second})
        assert same.state == "succeeded"
        assert r.journal()[start:] == [f"GET /repos/{REPO}", f"GET /repos/{REPO}/git/ref/heads/work"]
        start = len(r.journal())
        back = await r.run(sid, "github.push", {"repo": REPO, "branch": "work", "commit_sha": first})
        assert (back.state, back.error_code) == ("failed", "non_fast_forward")
        assert r.journal()[start:][-1] == f"GET /repos/{REPO}/compare/{second}...{first}"
        assert refs(github)["refs/heads/work"] == second

    run10(store_dsn, github, tmp_path, body)


def test_push_of_a_diverged_commit_and_of_a_foreign_commit(store_dsn: str, start_github: Any, tmp_path: Path) -> None:
    github = start_github(world=world())

    async def body(r: R10) -> None:
        sid = await r.session()
        topic = refs(github)["refs/heads/topic"]
        mine = await committed(r, sid, "feature-x", "mine\n")  # parent: main (branch missing, from the checkout)
        diverged = await r.run(sid, "github.push", {"repo": REPO, "branch": "feature", "commit_sha": mine})
        assert (diverged.state, diverged.error_code, diverged.retryable) == ("failed", "non_fast_forward", False)
        foreign = await r.run(sid, "github.push", {"repo": REPO, "branch": "feature", "commit_sha": topic})
        assert refused(foreign) == "not_found"
        protected = await r.run(sid, "github.push", {"repo": REPO, "branch": "main", "commit_sha": mine})
        assert refused(protected) == "branch_protected"

    run10(store_dsn, github, tmp_path, body)


def mocked(
    store_dsn: str, tmp_path: Path, handle: Any, expect: tuple[str, str | None], *, html_url: str = PUBLIC + "/o/r"
) -> None:
    """A push of a commit recorded as created by this task, against `handle` (None: a repository answer)."""
    sha = "c" * 40

    def route(request: httpx.Request) -> httpx.Response:
        answer: httpx.Response | None = handle(request)
        if answer is not None:
            return answer
        return httpx.Response(200, json={"html_url": html_url})

    async def body(r: R10) -> None:
        r.ok.env.context.github_options = GitHubOptions(transport=httpx.MockTransport(route))
        sid = await r.session()
        op_id = await r.ok.request(sid, "github.commit", {"repo": REPO, "branch": "work", "message": "M."})
        async with r.ok.env.pool.connection() as conn:
            await conn.execute(
                "UPDATE runner.operation SET state = 'succeeded', result = jsonb_build_object('commit_sha', %s::text)"
                " WHERE id = %s",
                (sha, op_id),
            )
        op = await r.run(sid, "github.push", {"repo": REPO, "branch": "work", "commit_sha": sha})
        assert (op.state, op.error_code) == expect, op.answer

    run10(store_dsn, MockDouble(), tmp_path, body)


def ref_answer(sha: str) -> httpx.Response:
    return httpx.Response(200, json={"object": {"sha": sha}})


@pytest.mark.parametrize("winner", ["c" * 40, "e" * 40])
def test_a_lost_race_on_create_is_settled_by_reading_the_ref(store_dsn: str, tmp_path: Path, winner: str) -> None:
    reads: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response | None:
        if request.url.path.endswith("/git/ref/heads/work"):
            reads.append(1)
            return httpx.Response(404, json={}) if len(reads) == 1 else ref_answer(winner)
        if request.method == "POST":
            return httpx.Response(422, json={"message": "Reference already exists"})
        return None

    expect = ("succeeded", None) if winner == "c" * 40 else ("failed", "non_fast_forward")
    mocked(store_dsn, tmp_path, handle, expect)


def test_a_lost_race_on_update_and_other_refusals(store_dsn: str, tmp_path: Path) -> None:
    def handle(request: httpx.Request) -> httpx.Response | None:
        if request.url.path.endswith("/git/ref/heads/work"):
            return ref_answer("b" * 40)
        if "/compare/" in request.url.path:
            return httpx.Response(200, json={"status": "ahead"})
        if request.method == "PATCH":
            return httpx.Response(422, json={"message": "Update is not a fast forward"})
        return None

    mocked(store_dsn, tmp_path, handle, ("failed", "non_fast_forward"))


@pytest.mark.parametrize(
    ("compare", "expect"),
    [
        (httpx.Response(200, json={"status": "identical"}), ("succeeded", None)),
        (httpx.Response(200, json={"status": "behind"}), ("failed", "non_fast_forward")),
        (httpx.Response(404, json={"message": "No common ancestor"}), ("failed", "non_fast_forward")),
        (httpx.Response(200, json={"status": "weird"}), ("failed", "upstream_error")),
        (httpx.Response(403, json={"message": "Forbidden"}), ("failed", "upstream_error")),
    ],
)
def test_compare_answers(
    store_dsn: str, tmp_path: Path, compare: httpx.Response, expect: tuple[str, str | None]
) -> None:
    def handle(request: httpx.Request) -> httpx.Response | None:
        if request.url.path.endswith("/git/ref/heads/work"):
            return ref_answer("b" * 40)
        if "/compare/" in request.url.path:
            return compare
        return None

    mocked(store_dsn, tmp_path, handle, expect)


def test_refusals_of_the_ref_read_and_of_the_write(store_dsn: str, tmp_path: Path) -> None:
    def broken_ref(request: httpx.Request) -> httpx.Response | None:
        if request.url.path.endswith("/git/ref/heads/work"):
            return httpx.Response(403, json={"message": "Forbidden"})
        return None

    mocked(store_dsn, tmp_path, broken_ref, ("failed", "upstream_error"))

    def broken_write(request: httpx.Request) -> httpx.Response | None:
        if request.url.path.endswith("/git/ref/heads/work"):
            return httpx.Response(404, json={})
        if request.method == "POST":
            return httpx.Response(403, json={"message": "Resource not accessible by integration"})
        return None

    # the token lacks the right to write: the push ends with the one next step (KOR-65)
    mocked(store_dsn, tmp_path, broken_write, ("failed", "repo_not_accessible"))


def test_a_repository_answer_without_an_https_url_refuses_the_push(store_dsn: str, tmp_path: Path) -> None:
    def handle(request: httpx.Request) -> httpx.Response | None:
        return None

    mocked(store_dsn, tmp_path, handle, ("failed", "upstream_error"), html_url="http://plain")


def test_the_area_registers_executors_and_prechecks_of_part_two() -> None:
    for op_type, module in (("github.checkout", checkout), ("github.commit", commit), ("github.push", push)):
        assert EXECUTORS[op_type] is module.execute
        assert PRECHECKS[op_type] is module.check
