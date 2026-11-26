"""U-RUN-github (KOR-51, ADR-094 sections 4-6): how `github.merge`, `github.list_reviews` and `github.get_checks` treat
GitHub's answers (a mock transport), and the push guard of the merge rules."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from agent_runner.github import access, merge, push
from agent_runner.github.client import GitHubOptions
from agent_runner.operations.extensions import CheckFailed, PrecheckResult
from agent_runner.operations.settings import MergeCriteria

from .conftest import REPO
from .run10_kit import R10, MockDouble, known_user, refused, run10

HEAD = "a" * 40
PULL = {"state": "open", "merged": False, "draft": False, "head": {"sha": HEAD}, "base": {"ref": "main"}}
MERGE = {"repo": REPO, "number": 1, "head_sha": HEAD}


def mock(**answers: tuple[int, Any]) -> Callable[[httpx.Request], httpx.Response]:
    """Answers by the last path part (`pull`, `merge`, `reviews`, `runs`, `status`)."""

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        for key, suffix in (
            ("merge", "/pulls/1/merge"),
            ("reviews", "/pulls/1/reviews"),
            ("pull", "/pulls/1"),
            ("runs", "/check-runs"),
            ("status", "/status"),
        ):
            if path.endswith(suffix):
                status, body = answers.get(key, (200, PULL if key == "pull" else {}))
                return httpx.Response(status, json=body)
        if path.endswith(f"/repos/{REPO}"):
            return httpx.Response(200, json={"full_name": REPO, "default_branch": "main", "private": True})
        return httpx.Response(404, json={})

    return handle


def scenario(store_dsn: str, tmp_path: Path, answers: dict[str, Any], body: Callable[[R10], Any]) -> None:
    async def run(r: R10) -> None:
        await r.ok.settings([REPO], protected=[])
        r.ok.env.context.github_options = GitHubOptions(transport=known_user(mock(**answers)))
        await body(r)

    run10(store_dsn, MockDouble(), tmp_path, run)


@pytest.mark.parametrize(
    ("answers", "code"),
    [
        ({"pull": (404, {})}, "not_found"),
        ({"pull": (500, {})}, "upstream_error"),
        ({"pull": (200, {**PULL, "draft": True})}, "merge_unsupported"),
    ],
)
def test_the_pre_check(store_dsn: str, tmp_path: Path, answers: dict[str, Any], code: str) -> None:
    async def body(r: R10) -> None:
        assert refused(await r.run(await r.session(), "github.merge", MERGE)) == code

    scenario(store_dsn, tmp_path, answers, body)


def test_a_protected_base(store_dsn: str, tmp_path: Path) -> None:
    async def body(r: R10) -> None:
        await r.ok.settings([REPO], protected=["main"])
        assert refused(await r.run(await r.session(), "github.merge", MERGE)) == "branch_protected"

    scenario(store_dsn, tmp_path, {}, body)


@pytest.mark.parametrize(
    ("merged", "code"),
    [
        ((409, {"message": "Head branch was modified."}), "merge_outdated"),
        ((405, {"message": "not mergeable"}), "rights_denied"),
        ((422, {}), "upstream_error"),
        ((200, {"merged": False, "sha": HEAD}), "upstream_error"),
    ],
)
def test_github_answers_the_merge(store_dsn: str, tmp_path: Path, merged: tuple[int, Any], code: str) -> None:
    async def body(r: R10) -> None:
        op = await r.run(await r.session(), "github.merge", MERGE)
        assert op.state == "failed"
        assert op.answer is not None
        assert op.answer["error"]["code"] == code

    scenario(store_dsn, tmp_path, {"merge": merged}, body)


def test_malformed_reviews_and_checks(store_dsn: str, tmp_path: Path) -> None:
    reviews = [{"id": 5, "user": {"login": "x"}, "state": "APPROVED", "commit_id": HEAD, "body": None}, {"id": "bad"}]
    answers = {"reviews": (200, reviews), "runs": (200, {"check_runs": "x"}), "status": (200, {"statuses": []})}

    async def body(r: R10) -> None:
        sid = await r.session()
        listed = await r.run(sid, "github.list_reviews", {"repo": REPO, "number": 1})
        assert listed.result is not None, listed.answer
        assert [item["id"] for item in listed.result["reviews"]] == [5]
        checks = await r.run(sid, "github.get_checks", {"repo": REPO, "ref": HEAD})
        assert checks.state == "failed"

    scenario(store_dsn, tmp_path, answers, body)


def test_the_push_guard_of_the_merge_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    async def checked(_request: Any) -> PrecheckResult:
        return PrecheckResult(dedupe={"x": 1})

    class Client:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_exc: object) -> None:
            return None

    async def repository(_github: object, _repo: str) -> dict[str, Any]:
        return {"default_branch": "main"}

    monkeypatch.setattr(push, "check", checked)
    monkeypatch.setattr(access, "client", lambda _request: Client())
    monkeypatch.setattr(access, "repository", repository)

    def request(branch: str, rules: MergeCriteria, separate: bool = False) -> Any:
        settings = SimpleNamespace(merge_criteria=rules, separate_duties=separate)
        return SimpleNamespace(params={"repo": REPO, "branch": branch}, settings=settings)

    assert asyncio.run(merge.guarded_push(request("main", MergeCriteria()))).dedupe == {"x": 1}
    assert asyncio.run(merge.guarded_push(request("feature", MergeCriteria(passed_verdicts=1)))).dedupe == {"x": 1}
    with pytest.raises(CheckFailed) as failed:
        asyncio.run(merge.guarded_push(request("main", MergeCriteria(), separate=True)))
    assert failed.value.code == "branch_protected"
