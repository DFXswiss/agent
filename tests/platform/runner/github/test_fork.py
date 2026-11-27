"""U-RUN-github (HOT-33): an identity that may only read a repository (the roles read and triage: `permissions.push`
false) writes through its fork, by itself, and a repository it can write through neither gets the exact refusal at
once.

Through the operation pipeline against the GitHub double (`github.create_branch`, `github.commit`, `github.push`,
`github.create_pull_request` as a session requests them, naming the repository and never the fork):

- an identity with pull and triage and a fork under another name: the branch lands in the fork, which is first
  brought to the repository's state (merge-upstream); commit and push go to the fork, the pull request comes from it;
  every result names the fork; nothing is written to the repository and nothing is held;
- no fork yet and forks allowed: the fork is created (at execution, after the registration) and used;
- forks not allowed: `repo_read_only` at once with the missing right and the next step, never held, nothing written;
- an identity with the right to push: unchanged, directly in the repository.

Unit part (`agent_runner.github.fork` against mocked answers): the other forms of GitHub's answers.
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent_guest.schema import SchemaSet
from agent_runner.github import fork
from agent_runner.github.client import GitHubClient, GitHubError, RateGate
from agent_runner.operations.extensions import CheckFailed, OperationFailed
from agent_runner.subagents import guest_features

from ..sync.fakehub import CONTRACTS
from .conftest import PUBLIC, REPO, GitHub
from .run10_kit import R10, refused, run10, world

SCHEMAS = SchemaSet(CONTRACTS)
READER = "ghp_double-reader"  # not a secret: the double only compares it
FORK = "octo-user/hello-mine"
NEW_FORK = "octo-user/hello"
README = "README.md"


def reader_world(*, fork_exists: bool = True, allow: bool = True) -> dict[str, Any]:
    """The RUN-10 world in which `octo-user` has the role triage on `octo-org/hello` (a classic token with `repo`:
    the account may read, not push) and, with `fork_exists`, its fork `octo-user/hello-mine` (another name)."""
    data = copy.deepcopy(world())
    user = data["users"][0]
    user["readonly"] = [REPO]
    user["tokens"].append({"token": READER, "kind": "classic", "scopes": ["repo"]})
    data["repositories"][0]["allow_forking"] = allow
    if fork_exists:
        data["repositories"].append({"full_name": FORK, "fork_of": REPO})
    return data


def refs(github: GitHub, name: str) -> dict[str, str]:
    owner, repo = name.split("/")
    found = github.double.world.repo(owner, repo)
    return {} if found is None else dict(found.refs)


def advance(github: GitHub, repo: str, branch: str, files: dict[str, str]) -> str:
    """A new commit on `branch` of `repo` (the base moves on after the fork was made)."""
    body = {"repo": repo, "branch": branch, "commit": {"message": "Upstream.", "files": files}}
    answer = github.control.request("POST", "/_double/commits", body)
    assert answer.status == 200, answer.body
    sha: str = answer.json()["sha"]
    return sha


def writes(github: GitHub) -> list[str]:
    return [path for path in github.paths() if not path.startswith("GET ")]


def ok(op: Any, op_type: str) -> dict[str, Any]:
    assert op.state == "succeeded", op.answer
    assert (op.held_until, op.held_reason) == (None, None)
    assert op.result is not None
    assert SCHEMAS.errors(f"ops/{op_type}.result.json", op.result) == []
    result: dict[str, Any] = op.result
    return result


async def checked_out(r: R10, sid: str) -> Path:
    op = await r.run(sid, "github.checkout", {"repo": REPO})
    assert op.state == "succeeded", op.answer
    return r.guest.work(sid) / "repos" / "octo-org" / "hello"


# -- the four cases of HOT-33 ----------------------------------------------------------------------------------------


def test_pull_and_triage_write_through_the_existing_fork_and_the_pull_request_comes_from_it(
    store_dsn: str, start_github: Callable[..., GitHub], tmp_path: Path
) -> None:
    github = start_github(world=reader_world())
    upstream = advance(github, REPO, "main", {"upstream.txt": "new\n"})  # the fork is behind now
    assert refs(github, FORK)["refs/heads/main"] != upstream

    async def body(r: R10) -> None:
        sid = await r.session()
        made = await r.run(sid, "github.create_branch", {"repo": REPO, "branch": "work", "from": "main"})
        branch = ok(made, "github.create_branch")
        assert branch == {"branch": "work", "sha": upstream, "fork": FORK}
        assert refs(github, FORK)["refs/heads/main"] == upstream  # merge-upstream first
        assert refs(github, FORK)["refs/heads/work"] == upstream
        assert "refs/heads/work" not in refs(github, REPO)
        assert writes(github) == [f"POST /repos/{FORK}/merge-upstream", f"POST /repos/{FORK}/git/refs"]

        root = await checked_out(r, sid)
        (root / README).write_text("changed in the fork\n")
        committed = await r.run(sid, "github.commit", {"repo": REPO, "branch": "work", "message": "Change."})
        commit = ok(committed, "github.commit")
        assert (commit["parent_sha"], commit["fork"]) == (upstream, FORK)
        pushed = await r.run(sid, "github.push", {"repo": REPO, "branch": "work", "commit_sha": commit["commit_sha"]})
        assert ok(pushed, "github.push") == {
            "branch": "work",
            "sha": commit["commit_sha"],
            "url": f"{PUBLIC}/{FORK}/tree/work",
            "fork": FORK,
        }
        assert refs(github, FORK)["refs/heads/work"] == commit["commit_sha"]

        params = {"repo": REPO, "head": "work", "base": "main", "title": "Change from the fork"}
        pull = ok(await r.run(sid, "github.create_pull_request", params), "github.create_pull_request")
        assert pull["fork"] == FORK
        found = github.double.world.repo("octo-org", "hello")
        assert found is not None
        opened = found.pulls[pull["number"]]
        assert (opened.head, opened.source is not None and opened.source.full_name) == ("work", FORK)
        assert not [path for path in writes(github) if path.startswith(f"POST /repos/{REPO}/git")]
        assert [line for line in r.ok.lines if "held operation_id" in line] == []

    run10(store_dsn, github, tmp_path, body, token=READER)


def test_without_a_fork_one_is_created_when_forks_are_allowed(
    store_dsn: str, start_github: Callable[..., GitHub], tmp_path: Path
) -> None:
    github = start_github(world=reader_world(fork_exists=False))
    main = refs(github, REPO)["refs/heads/main"]

    async def body(r: R10) -> None:
        sid = await r.session()
        # the commit is the first write: the fork is created at its execution, the parent is the checkout's commit
        root = await checked_out(r, sid)
        (root / README).write_text("first\n")
        committed = await r.run(sid, "github.commit", {"repo": REPO, "branch": "work", "message": "First."})
        commit = ok(committed, "github.commit")
        assert (commit["parent_sha"], commit["fork"]) == (main, NEW_FORK)
        assert writes(github)[0] == f"POST /repos/{REPO}/forks"
        assert github.double.world.repo("octo-user", "hello") is not None
        # the next operation finds the fork by its name
        pushed = await r.run(sid, "github.push", {"repo": REPO, "branch": "work", "commit_sha": commit["commit_sha"]})
        assert ok(pushed, "github.push")["fork"] == NEW_FORK
        assert refs(github, NEW_FORK)["refs/heads/work"] == commit["commit_sha"]
        # a branch from a commit needs no merge-upstream
        made = await r.run(sid, "github.create_branch", {"repo": REPO, "branch": "other", "from": main})
        assert ok(made, "github.create_branch") == {"branch": "other", "sha": main, "fork": NEW_FORK}
        assert f"POST /repos/{NEW_FORK}/merge-upstream" not in writes(github)
        assert writes(github).count(f"POST /repos/{REPO}/forks") == 1

    run10(store_dsn, github, tmp_path, body, token=READER)


def test_a_branch_of_a_new_fork_is_created_after_the_fork(
    store_dsn: str, start_github: Callable[..., GitHub], tmp_path: Path
) -> None:
    github = start_github(world=reader_world(fork_exists=False))

    async def body(r: R10) -> None:
        sid = await r.session()
        branch = await r.run(sid, "github.create_branch", {"repo": REPO, "branch": "work", "from": "main"})
        assert ok(branch, "github.create_branch")["fork"] == NEW_FORK
        assert writes(github) == [
            f"POST /repos/{REPO}/forks",
            f"POST /repos/{NEW_FORK}/merge-upstream",
            f"POST /repos/{NEW_FORK}/git/refs",
        ]

    run10(store_dsn, github, tmp_path, body, token=READER)


def test_forks_not_allowed_refuse_at_once_with_the_missing_right_and_the_next_step(
    store_dsn: str, start_github: Callable[..., GitHub], tmp_path: Path
) -> None:
    github = start_github(world=reader_world(fork_exists=False, allow=False))

    async def body(r: R10) -> None:
        sid = await r.session()
        op = await r.run(sid, "github.create_branch", {"repo": REPO, "branch": "work", "from": "main"})
        assert refused(op) == "repo_read_only"
        assert (op.held, op.held_until, op.held_reason) == (False, None, None)  # no wait of 15 minutes
        assert op.answer is not None
        message = op.answer["error"]["message"]
        assert message.startswith(f"The GitHub account octo-user may only read {REPO} (its rights there: triage, pull)")
        assert "does not allow forks" in message
        assert f"ask an owner of {REPO} to give octo-user write access or to allow forks ({PUBLIC}/{REPO})" in message
        root = await checked_out(r, sid)
        (root / README).write_text("x\n")
        commit = await r.run(sid, "github.commit", {"repo": REPO, "branch": "work", "message": "X."})
        assert refused(commit) == "repo_read_only"
        assert writes(github) == []
        waiting = [kind for kind, item in await r.ok.outbox() if kind == "event" and "waiting_for" in item["body"]]
        assert waiting == []
        # a guest before HOT-33 gets the code its contracts know, with the same sentence
        assert r.guest.answers()[-1][1]["result"]["error"]["code"] == "repo_read_only"
        async with r.ok.env.pool.connection() as conn:
            await conn.execute("UPDATE runner.golden SET guest_version = '0.7.0'")
        again = await r.run(sid, "github.create_branch", {"repo": REPO, "branch": "other", "from": "main"})
        assert refused(again) == "repo_read_only"
        assert again.answer is not None
        delivered = r.guest.answers()[-1][1]["result"]
        assert delivered["error"] == {**again.answer["error"], "code": "repo_not_accessible"}

    run10(store_dsn, github, tmp_path, body, token=READER)


def test_an_identity_with_the_right_to_push_writes_to_the_repository_as_before(
    store_dsn: str, start_github: Callable[..., GitHub], tmp_path: Path
) -> None:
    github = start_github(world=world())

    async def body(r: R10) -> None:
        sid = await r.session()
        branch = await r.run(sid, "github.create_branch", {"repo": REPO, "branch": "work", "from": "topic"})
        assert "fork" not in ok(branch, "github.create_branch")
        assert "refs/heads/work" in refs(github, REPO)
        params = {"repo": REPO, "head": "work", "base": "main", "title": "Direct"}
        pull = ok(await r.run(sid, "github.create_pull_request", params), "github.create_pull_request")
        assert "fork" not in pull
        assert [path for path in github.paths() if "fork" in path or "merge-upstream" in path] == []
        assert writes(github) == [f"POST /repos/{REPO}/git/refs", f"POST /repos/{REPO}/pulls"]

    run10(store_dsn, github, tmp_path, body)


def test_a_pull_request_of_a_reader_without_the_head_in_a_fork_stays_in_the_repository(
    store_dsn: str, start_github: Callable[..., GitHub], tmp_path: Path
) -> None:
    """The head branch is a branch of the repository (someone with the right made it): no fork, or a fork without
    it, leaves the pull request as it was."""
    github = start_github(world=reader_world())
    mine = github.double.world.repo("octo-user", "hello-mine")
    assert mine is not None
    del mine.refs["refs/heads/topic"]  # the fork was made before the branch

    async def body(r: R10) -> None:
        sid = await r.session()
        params = {"repo": REPO, "head": "topic", "base": "main", "title": "Topic"}
        pull = ok(await r.run(sid, "github.create_pull_request", params), "github.create_pull_request")
        assert "fork" not in pull
        found = github.double.world.repo("octo-org", "hello")
        assert found is not None
        assert found.pulls[pull["number"]].source is None

    run10(store_dsn, github, tmp_path, body, token=READER)


# -- unit: the other answers of GitHub -------------------------------------------------------------------------------

Answer = tuple[int, object]


def client(answers: dict[tuple[str, str], Answer | list[Answer]]) -> GitHubClient:
    """A classic token's client; `answers` by `(method, path)` (a list: one answer per request, the last repeats)."""

    def handle(request: httpx.Request) -> httpx.Response:
        found = answers.get((request.method, request.url.path), (404, {"message": "Not Found"}))
        if isinstance(found, list):
            found = found.pop(0) if len(found) > 1 else found[0]
        status, data = found
        headers = {"x-github-sso": "required"} if status == 403 and data == "sso" else {}
        return httpx.Response(status, json={"message": str(data)} if isinstance(data, str) else data, headers=headers)

    async def sleep(_seconds: float) -> None:
        return None

    return GitHubClient(
        api_url="https://api.github.com",
        web_url="https://github.com",
        token="ghp_mock",
        wall=lambda: 0.0,
        sleep=sleep,
        gate=RateGate(),
        transport=httpx.MockTransport(handle),
    )


USER = ("GET", "/user")
LOGIN = (200, {"login": "octo-user", "id": 101})
READ_ONLY = {"permissions": {"pull": True, "triage": True, "push": False}}
A_FORK = {
    "full_name": FORK,
    "fork": True,
    "parent": {"full_name": "Octo-Org/Hello"},
    "html_url": f"https://github.com/{FORK}",
    "owner": {"login": "octo-user"},
    "permissions": {"push": True},
}


def run(body: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(body())


def test_a_same_named_repository_that_is_no_fork_is_passed_over_for_the_fork_list() -> None:
    answers: dict[tuple[str, str], Answer | list[Answer]] = {
        USER: LOGIN,
        ("GET", "/repos/octo-user/hello"): (200, {"full_name": "octo-user/hello", "fork": False}),
        ("GET", f"/repos/{REPO}/forks"): (
            200,
            ["junk", {"owner": None}, {"owner": {"login": "someone"}, "full_name": "someone/hello"}, A_FORK],
        ),
        ("GET", f"/repos/{FORK}"): (200, A_FORK),
    }

    async def body() -> None:
        async with client(answers) as made:
            routed = await fork.route(made, REPO, READ_ONLY)
            assert routed == {
                "repo": FORK,
                "owner": "octo-user",
                "create": False,
                "html_url": f"https://github.com/{FORK}",
                "rights": "triage, pull",
            }

    run(body)


def test_a_fork_the_connection_may_not_write_either_is_read_only() -> None:
    answers: dict[tuple[str, str], Answer | list[Answer]] = {
        USER: LOGIN,
        ("GET", "/repos/octo-user/hello"): (200, {**A_FORK, "permissions": {"push": False}}),
    }

    async def body() -> None:
        async with client(answers) as made:
            with pytest.raises(CheckFailed) as caught:
                await fork.route(made, REPO, {"permissions": {"pull": True, "push": False}})
            assert caught.value.code == "repo_read_only"
            assert caught.value.message is not None
            assert f"may not write its fork {FORK} either" in caught.value.message
            assert "(its rights there: pull)" in caught.value.message
            assert fork.rights({"permissions": "odd"}) == "none"

    run(body)


def test_the_own_named_repository_is_asked_and_other_failures_pass_on() -> None:
    answers: dict[tuple[str, str], Answer | list[Answer]] = {
        USER: LOGIN,
        ("GET", "/repos/octo-user/hello"): (502, "down"),
    }

    async def body() -> None:
        async with client(answers) as made:
            with pytest.raises(GitHubError):
                await fork.find(made, REPO, "octo-user")

    run(body)


def test_a_fork_github_refuses_is_read_only_and_other_refusals_pass_on() -> None:
    create = ("POST", f"/repos/{REPO}/forks")
    routed = {"repo": None, "owner": "octo-user", "create": True, "html_url": None, "rights": "pull"}

    async def body() -> None:
        async with client({create: (403, "Forking is disabled")}) as made:
            with pytest.raises(OperationFailed) as caught:
                await fork.ensure(made, REPO, routed)
            assert (caught.value.code, caught.value.retryable) == ("repo_read_only", False)
            assert caught.value.message is not None
            assert "GitHub refused to create a fork" in caught.value.message
        async with client({create: (403, "sso")}) as made:
            with pytest.raises(GitHubError):
                await fork.ensure(made, REPO, routed)
        async with client({create: (500, "down")}) as made:
            with pytest.raises(GitHubError):
                await fork.ensure(made, REPO, routed)

    run(body)


def test_a_new_fork_is_waited_for_until_its_default_branch_is_there() -> None:
    create = ("POST", f"/repos/{REPO}/forks")
    ref = ("GET", f"/repos/{FORK}/git/ref/heads/main")
    made_fork = {**A_FORK, "default_branch": "main"}
    routed = {"repo": None, "owner": "octo-user", "create": True}

    async def body() -> None:
        ready: list[Answer] = [(404, "Not Found"), (409, "Git Repository is empty."), (200, {"object": {}})]
        async with client({create: (202, made_fork), ref: ready}) as made:
            assert await fork.ensure(made, REPO, routed) == (FORK, f"https://github.com/{FORK}")
        async with client({create: (202, made_fork), ref: (404, "Not Found")}) as made:
            with pytest.raises(OperationFailed) as caught:
                await fork.ensure(made, REPO, routed)
            assert (caught.value.code, caught.value.retryable) == ("upstream_error", True)
        async with client({create: (202, made_fork), ref: (502, "down")}) as made:
            with pytest.raises(GitHubError):
                await fork.ensure(made, REPO, routed)
        # an answer without a default branch is not waited for
        async with client({create: (202, A_FORK)}) as made:
            assert (await fork.ensure(made, REPO, routed))[0] == FORK

    run(body)


def test_merge_upstream_keeps_a_diverged_or_missing_branch_and_passes_other_failures_on() -> None:
    sync = ("POST", f"/repos/{FORK}/merge-upstream")

    async def body() -> None:
        for status in (404, 409, 422):
            async with client({sync: (status, "kept")}) as made:
                await fork.sync(made, FORK, "main")
        async with client({sync: (502, "down")}) as made:
            with pytest.raises(GitHubError):
                await fork.sync(made, FORK, "main")

    run(body)


def test_a_guest_before_hot_33_gets_answers_its_contracts_allow() -> None:
    made = {"operation_id": "x", "status": "succeeded", "result": {"branch": "w", "sha": "a" * 40, "fork": FORK}}
    assert guest_features.older_answer(made) == {**made, "result": {"branch": "w", "sha": "a" * 40}}
    failed = {"operation_id": "x", "status": "failed", "error": {"code": "repo_read_only", "message": "m"}}
    assert guest_features.older_answer(failed)["error"] == {"code": "repo_not_accessible", "message": "m"}
    other = {"operation_id": "x", "status": "failed", "error": {"code": "timeout"}}
    assert guest_features.older_answer(other) == other
    assert guest_features.older_answer({"status": "pending"}) == {"status": "pending"}
    assert guest_features.older_code(None) is None


def test_the_pull_request_route_needs_a_fork_with_the_head() -> None:
    own = ("GET", "/repos/octo-user/hello")
    ref = ("GET", f"/repos/{FORK}/git/ref/heads/work")

    async def body() -> None:
        async with client({}) as made:
            assert await fork.pull_route(made, REPO, {"permissions": {"push": True}}, "work") is None
        async with client({USER: LOGIN, ("GET", f"/repos/{REPO}/forks"): (200, [])}) as made:
            assert await fork.pull_route(made, REPO, READ_ONLY, "work") is None  # no fork at all
        async with client({USER: LOGIN, own: (200, A_FORK)}) as made:
            assert await fork.pull_route(made, REPO, READ_ONLY, "work") is None  # the fork lacks the head
        async with client({USER: LOGIN, own: (200, A_FORK), ref: (200, {})}) as made:
            assert await fork.pull_route(made, REPO, READ_ONLY, "work") == {"repo": FORK, "head": "octo-user:work"}
        async with client({USER: LOGIN, own: (200, A_FORK), ref: (502, "down")}) as made:
            with pytest.raises(GitHubError):
                await fork.pull_route(made, REPO, READ_ONLY, "work")

    run(body)
