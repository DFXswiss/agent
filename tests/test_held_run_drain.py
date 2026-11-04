"""Bounded draining of superseded never-started fork workflow holds."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlparse

import pytest

from agent_cli import a38_guard, held_run_drain
from agent_cli.a38_guard import GuardError
from agent_cli.held_run_drain import (
    MAX_DELETIONS,
    MAX_EXAMINED,
    MAX_REQUESTS,
    ROTATION_PERIOD_S,
    drain_held_runs,
)
from test_a38_guard import REPO, FakeAPI, _pr_guard_config

pytestmark = pytest.mark.no_pg

FORK = "contributor/fork"
BASE_SLOT = int(
    datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
) // ROTATION_PERIOD_S


def _now_for_offset(candidate_count: int, wanted_offset: int) -> datetime:
    slot = BASE_SLOT
    while (slot * MAX_EXAMINED) % candidate_count != wanted_offset:
        slot += 1
    return datetime.fromtimestamp(slot * ROTATION_PERIOD_S, tz=timezone.utc)


class FakeDrain(FakeAPI):
    """FakeAPI plus held-run inventory, jobs reads, and run deletion."""

    def __init__(
        self,
        *,
        configured: bool = True,
        enabled: bool = True,
        listing_total_override: int | None = None,
    ) -> None:
        self.requests = 0
        super().__init__()
        config = _pr_guard_config()
        if configured:
            config["held_run_drain"] = {"enabled": enabled}
        self.set_pr_guard_config(config)
        self.runs: list[dict] = []
        self.open_pull_items: list[dict] = []
        self.open_pull_lists: list[list[dict]] = []
        self.open_pull_gets = 0
        self.pull_list_queries: list[str] = []
        self.current_open_pull_items: list[dict] = []
        self.pull_overrides: dict[int, object] = {}
        self.commits_by_pull: dict[int, list[str]] = {}
        self.commits_reads: dict[int, list[list[str]]] = {}
        self.commit_read_counts: dict[int, int] = {}
        self.jobs_by_run: dict[int, list[dict]] = {}
        self.jobs_reads: dict[int, list[list[dict]]] = {}
        self.jobs_read_counts: dict[int, int] = {}
        self.jobs_404: set[int] = set()
        self.run_overrides: dict[int, dict] = {}
        self.run_reads: dict[int, list[dict]] = {}
        self.run_read_counts: dict[int, int] = {}
        self.missing_on_reread: set[int] = set()
        self.delete_responses: dict[int, tuple[int, object, dict]] = {}
        self.listing_total_override = listing_total_override
        self.listing_calls: list[str] = []
        self.jobs_gets: list[int] = []
        self.deletes: list[int] = []

    @staticmethod
    def run(**changes: object) -> dict:
        data: dict = {
            "id": 101,
            "event": "pull_request",
            "head_sha": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
            "head_repository": {"full_name": FORK},
            "head_branch": "feature",
            "status": "completed",
            "conclusion": "action_required",
            "run_attempt": 1,
            "created_at": "2000-01-01T00:00:00Z",
        }
        data.update(changes)
        return data

    def request_fn(self, method: str, url: str, body: bytes | None = None):
        self.requests += 1
        method = method.upper()
        path = urlparse(url).path
        query = parse_qs(urlparse(url).query)
        root = f"/repos/{REPO}"
        if method == "GET" and path == root:
            return 200, {"default_branch": "develop"}, {}
        if method == "GET" and path == f"{root}/pulls":
            self.pull_list_queries.append(urlparse(url).query)
            if self.open_pull_lists:
                index = min(self.open_pull_gets, len(self.open_pull_lists) - 1)
                items = self.open_pull_lists[index]
            else:
                items = self.open_pull_items
            self.open_pull_gets += 1
            head_filter = (query.get("head") or [None])[0]
            if head_filter is not None:
                owner, branch = head_filter.split(":", 1)
                items = [
                    item
                    for item in items
                    if isinstance(item.get("head"), dict)
                    and isinstance(item["head"].get("repo"), dict)
                    and isinstance(item["head"]["repo"].get("full_name"), str)
                    and (
                        item["head"]["repo"]["full_name"].split("/", 1)[0].lower()
                        == owner.lower()
                    )
                    and item["head"].get("ref") == branch
                ]
            self.current_open_pull_items = copy.deepcopy(items)
            page = int((query.get("page") or ["1"])[0])
            per_page = int((query.get("per_page") or ["100"])[0])
            start = (page - 1) * per_page
            chunk = items[start : start + per_page]
            headers: dict[str, str] = {}
            if start + per_page < len(items):
                next_query = {
                    key: list(values)
                    for key, values in query.items()
                    if key not in {"page", "per_page"}
                }
                next_query["per_page"] = [str(per_page)]
                next_query["page"] = [str(page + 1)]
                next_url = (
                    f"https://api.github.com{path}?"
                    f"{urlencode(next_query, doseq=True)}"
                )
                headers["link"] = f'<{next_url}>; rel="next"'
            return 200, copy.deepcopy(chunk), headers
        if method == "GET" and path.startswith(f"{root}/pulls/"):
            remainder = path.split("/pulls/", 1)[1]
            number = int(remainder.split("/", 1)[0])
            if remainder.endswith("/commits"):
                reads = self.commits_reads.get(number)
                if reads:
                    read_count = self.commit_read_counts.get(number, 0)
                    shas = reads[min(read_count, len(reads) - 1)]
                    self.commit_read_counts[number] = read_count + 1
                else:
                    shas = self.commits_by_pull.get(number, [])
                return 200, [
                    {"sha": sha} for sha in shas
                ], {}
            if "/" not in remainder:
                pull = self.pull_overrides.get(number)
                if pull is None:
                    pull = next(
                        item
                        for item in self.current_open_pull_items
                        if item.get("number") == number
                    )
                return 200, copy.deepcopy(pull), {}
        if method == "GET" and path == f"{root}/actions/runs":
            self.listing_calls.append(url)
            page = int((query.get("page") or ["1"])[0])
            per_page = int((query.get("per_page") or ["100"])[0])
            start = (page - 1) * per_page
            chunk = self.runs[start : start + per_page]
            return 200, {
                "total_count": (
                    len(self.runs)
                    if self.listing_total_override is None
                    else self.listing_total_override
                ),
                "workflow_runs": copy.deepcopy(chunk),
            }, {}
        if path.startswith(f"{root}/actions/runs/"):
            remainder = path.split("/actions/runs/", 1)[1]
            ident = int(remainder.split("/", 1)[0])
            if method == "GET" and remainder.endswith("/jobs"):
                self.jobs_gets.append(ident)
                if ident in self.jobs_404:
                    return 404, {"message": "missing"}, {}
                reads = self.jobs_reads.get(ident)
                if reads:
                    read_count = self.jobs_read_counts.get(ident, 0)
                    jobs = reads[min(read_count, len(reads) - 1)]
                    self.jobs_read_counts[ident] = read_count + 1
                else:
                    jobs = self.jobs_by_run.get(ident, [])
                return 200, {
                    "total_count": len(jobs),
                    "jobs": copy.deepcopy(jobs),
                }, {}
            if method == "GET" and "/" not in remainder:
                if ident in self.missing_on_reread:
                    return 404, {"message": "missing"}, {}
                reads = self.run_reads.get(ident)
                if reads:
                    read_count = self.run_read_counts.get(ident, 0)
                    run = reads[min(read_count, len(reads) - 1)]
                    self.run_read_counts[ident] = read_count + 1
                else:
                    run = self.run_overrides.get(ident)
                    if run is None:
                        run = next(item for item in self.runs if item["id"] == ident)
                return 200, copy.deepcopy(run), {}
            if method == "DELETE" and "/" not in remainder:
                self.deletes.append(ident)
                return self.delete_responses.get(ident, (204, None, {}))
        return super().request_fn(method, url, body)


def _open_pull(
    head: str,
    merge: str | None = None,
    *,
    number: int = 1,
    head_repo: str | None = FORK,
    head_ref: str = "feature",
) -> dict:
    repo = None if head_repo is None else {"full_name": head_repo}
    return {
        "number": number,
        "head": {"sha": head, "repo": repo, "ref": head_ref},
        "merge_commit_sha": merge,
    }


def test_disabled_by_omission_does_not_list_or_delete() -> None:
    fake = FakeDrain(configured=False)
    result = drain_held_runs(fake.api(), REPO)
    assert result["enabled"] is False
    assert fake.listing_calls == []
    assert fake.deletes == []


def test_explicitly_disabled_does_not_list_or_delete() -> None:
    fake = FakeDrain(enabled=False)
    result = drain_held_runs(fake.api(), REPO)
    assert result["enabled"] is False
    assert fake.listing_calls == []
    assert fake.deletes == []


def test_empty_first_listing_page_with_positive_total_fails_closed() -> None:
    fake = FakeDrain(listing_total_override=1)
    with pytest.raises(GuardError, match="incomplete"):
        drain_held_runs(fake.api(), REPO)
    assert len(fake.listing_calls) == 1
    assert fake.deletes == []


def test_fork_proof_is_case_insensitive_and_skips_same_repository_runs() -> None:
    fake = FakeDrain()
    fake.runs = [fake.run(head_repository={"full_name": REPO.upper()})]
    result = drain_held_runs(fake.api(), REPO)
    assert result["deleted"] == []
    assert result["candidates"] == 0
    assert fake.deletes == []


def test_open_pull_branch_fork_match_is_case_insensitive() -> None:
    fake = FakeDrain()
    run = fake.run(head_repository={"full_name": "CONTRIBUTOR/fork"})
    fake.runs = [run]
    fake.open_pull_items = [
        _open_pull("a" * 40, "b" * 40, head_repo="Contributor/Fork")
    ]
    fake.commits_by_pull[1] = [run["head_sha"]]
    result = drain_held_runs(fake.api(), REPO)
    assert result["deleted"] == []
    assert fake.deletes == []


def test_superseded_old_never_started_fork_hold_is_deleted() -> None:
    fake = FakeDrain()
    fake.runs = [fake.run()]
    result = drain_held_runs(fake.api(), REPO)
    assert result["deleted"] == [101]
    assert result["requests"] > 0
    assert result["aborted"] is None
    assert fake.deletes == [101]


def test_request_budget_stops_rechecks_after_preserving_deletions() -> None:
    fake = FakeDrain()
    fake.open_pull_items = [
        _open_pull(
            f"{number:040x}",
            number=number,
            head_ref=f"open-{number}",
        )
        for number in range(1, 131)
    ]
    fake.runs = [
        fake.run(
            id=ident,
            head_sha=f"{1000 + ident:040x}",
            head_branch=f"candidate-{ident}",
        )
        for ident in range(1, 31)
    ]

    result = drain_held_runs(fake.api(), REPO, now=_now_for_offset(30, 0))

    assert result["aborted"] == "budget"
    assert result["deleted"]
    assert result["deleted"] == fake.deletes
    assert result["requests"] <= MAX_REQUESTS
    assert fake.requests <= MAX_REQUESTS + 3


def test_request_budget_exhausted_during_scan_returns_without_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeDrain()
    fake.runs = [fake.run()]
    fake.open_pull_items = [
        _open_pull("a" * 40, number=1),
        _open_pull("b" * 40, number=2),
    ]
    monkeypatch.setattr(held_run_drain, "MAX_REQUESTS", 2)

    result = drain_held_runs(fake.api(), REPO)

    assert result["aborted"] == "budget"
    assert result["deleted"] == []
    assert fake.deletes == []
    assert "error" not in result


def test_multi_page_open_pull_scan_stops_at_the_request_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeDrain()
    fake.runs = [fake.run()]
    fake.open_pull_items = [
        _open_pull(
            f"{number:040x}",
            "f" * 40,
            number=number,
            head_ref=f"open-{number}",
        )
        for number in range(1, 302)
    ]
    monkeypatch.setattr(held_run_drain, "MAX_REQUESTS", 3)

    result = drain_held_runs(fake.api(), REPO)

    assert result["aborted"] == "budget"
    assert result["requests"] == held_run_drain.MAX_REQUESTS
    assert result["deleted"] == []
    assert fake.deletes == []
    assert fake.requests - 3 <= held_run_drain.MAX_REQUESTS


def test_request_budget_interruption_does_not_delete_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def configured_fake() -> FakeDrain:
        fake = FakeDrain()
        fake.runs = [fake.run()]
        fake.open_pull_items = [_open_pull("a" * 40, "b" * 40)]
        fake.commits_by_pull[1] = []
        return fake

    measured = configured_fake()
    measured_result = drain_held_runs(measured.api(), REPO, dry_run=True)
    assert measured_result["planned"] == [101]
    served_after_gate = measured_result["requests"]
    assert measured.requests == served_after_gate + 3

    monkeypatch.setattr(held_run_drain, "MAX_REQUESTS", served_after_gate - 1)
    fake = configured_fake()
    result = drain_held_runs(fake.api(), REPO)

    assert result["aborted"] == "budget"
    assert 101 not in result["deleted"]
    assert fake.deletes == []


def test_open_pull_current_head_is_protected() -> None:
    fake = FakeDrain()
    run = fake.run()
    fake.runs = [run]
    fake.open_pull_items = [_open_pull(run["head_sha"])]
    result = drain_held_runs(fake.api(), REPO)
    assert result["protected"] == 1
    assert result["deleted"] == []
    assert fake.deletes == []


def test_open_pull_merge_commit_is_protected() -> None:
    fake = FakeDrain()
    run = fake.run()
    fake.runs = [run]
    fake.open_pull_items = [_open_pull("a" * 40, run["head_sha"])]
    result = drain_held_runs(fake.api(), REPO)
    assert result["protected"] == 2
    assert result["deleted"] == []
    assert fake.deletes == []


def test_open_pull_older_commit_is_protected() -> None:
    fake = FakeDrain()
    run = fake.run()
    fake.runs = [run]
    fake.open_pull_items = [_open_pull("a" * 40, "b" * 40)]
    fake.commits_by_pull[1] = [run["head_sha"]]
    result = drain_held_runs(fake.api(), REPO)
    assert result["deleted"] == []
    assert fake.deletes == []


def test_branch_without_an_open_pull_is_deleted() -> None:
    fake = FakeDrain()
    fake.runs = [fake.run()]
    result = drain_held_runs(fake.api(), REPO)
    assert result["deleted"] == [101]
    assert fake.deletes == [101]


def test_force_push_orphan_on_open_pull_branch_is_deleted() -> None:
    fake = FakeDrain()
    fake.runs = [fake.run()]
    fake.open_pull_items = [_open_pull("a" * 40, "b" * 40)]
    fake.commits_by_pull[1] = ["c" * 40]
    result = drain_held_runs(fake.api(), REPO)
    assert result["deleted"] == [101]
    assert fake.deletes == [101]


def test_fresh_open_pull_recheck_protects_candidate_before_delete() -> None:
    fake = FakeDrain()
    run = fake.run()
    fake.runs = [run]
    fake.open_pull_lists = [[], [_open_pull(run["head_sha"])]]
    result = drain_held_runs(fake.api(), REPO)
    assert result["candidates"] == 1
    assert result["deleted"] == []
    assert fake.deletes == []


def test_fresh_commit_list_recheck_protects_a_newly_pushed_candidate() -> None:
    fake = FakeDrain()
    run = fake.run()
    fake.runs = [run]
    fake.open_pull_items = [_open_pull("a" * 40, "b" * 40)]
    fake.commits_reads[1] = [["c" * 40], [run["head_sha"]]]
    result = drain_held_runs(fake.api(), REPO)
    assert result["candidates"] == 1
    assert result["deleted"] == []
    assert fake.deletes == []


def test_recheck_queries_only_the_head_and_reads_its_fresh_commits() -> None:
    fake = FakeDrain()
    run = fake.run()
    fake.runs = [run]
    fake.open_pull_lists = [
        [],
        [_open_pull("a" * 40, "b" * 40)],
    ]
    fake.commits_by_pull[1] = [run["head_sha"]]

    result = drain_held_runs(fake.api(), REPO)

    assert result["candidates"] == 1
    assert result["deleted"] == []
    assert "head=" not in fake.pull_list_queries[0]
    assert len(fake.pull_list_queries) == 2
    assert all("head=" in query for query in fake.pull_list_queries[1:])
    assert fake.deletes == []


@pytest.mark.parametrize("head_branch", ["feature\x01", "x" * 256])
def test_invalid_candidate_head_branch_is_never_deleted(
    head_branch: str,
) -> None:
    fake = FakeDrain()
    fake.runs = [fake.run(head_branch=head_branch)]

    result = drain_held_runs(fake.api(), REPO)

    assert result["candidates"] == 1
    assert result["deleted"] == []
    assert len(fake.pull_list_queries) == 1
    assert fake.deletes == []


@pytest.mark.parametrize(
    "detail_head",
    [
        {"repo": {"full_name": "other/fork"}, "ref": "feature"},
        {"repo": {"full_name": FORK}, "ref": "other-feature"},
    ],
)
def test_recheck_ignores_detail_for_another_head(detail_head: dict) -> None:
    fake = FakeDrain()
    run = fake.run()
    listed = _open_pull("a" * 40, "b" * 40)
    fake.runs = [run]
    fake.open_pull_items = [listed]
    fake.pull_overrides[1] = {
        **listed,
        "head": {**listed["head"], **detail_head},
    }

    result = drain_held_runs(fake.api(), REPO)

    assert result["candidates"] == 1
    assert result["deleted"] == [101]
    assert fake.deletes == [101]


def test_last_single_pull_read_protects_changed_merge_sha() -> None:
    fake = FakeDrain()
    run = fake.run()
    listed = _open_pull("a" * 40, "b" * 40)
    fake.runs = [run]
    fake.open_pull_items = [listed]
    fake.pull_overrides[1] = {**listed, "merge_commit_sha": run["head_sha"]}
    result = drain_held_runs(fake.api(), REPO)
    assert result["candidates"] == 1
    assert result["deleted"] == []
    assert fake.deletes == []


@pytest.mark.parametrize("detail", [[], "invalid"])
def test_last_single_pull_read_malformed_body_fails_closed(detail: object) -> None:
    fake = FakeDrain()
    run = fake.run()
    listed = _open_pull("a" * 40, "b" * 40)
    fake.runs = [run]
    fake.open_pull_items = [listed]
    fake.pull_overrides[1] = detail
    with pytest.raises(GuardError, match="open pull request head sha missing"):
        drain_held_runs(fake.api(), REPO)
    assert fake.deletes == []


def test_last_single_pull_read_malformed_head_sha_fails_closed() -> None:
    fake = FakeDrain()
    run = fake.run()
    listed = _open_pull("a" * 40, "b" * 40)
    fake.runs = [run]
    fake.open_pull_items = [listed]
    fake.pull_overrides[1] = {
        **listed,
        "head": {**listed["head"], "sha": "not-a-sha"},
    }
    with pytest.raises(GuardError, match="open pull request head sha missing"):
        drain_held_runs(fake.api(), REPO)
    assert fake.deletes == []


def test_last_single_pull_read_malformed_merge_sha_fails_closed() -> None:
    fake = FakeDrain()
    run = fake.run()
    listed = _open_pull("a" * 40, "b" * 40)
    fake.runs = [run]
    fake.open_pull_items = [listed]
    fake.pull_overrides[1] = {**listed, "merge_commit_sha": "nope"}
    with pytest.raises(GuardError, match="open pull request merge sha invalid"):
        drain_held_runs(fake.api(), REPO)
    assert fake.deletes == []


def test_null_list_merge_sha_is_resolved_by_single_pull_read() -> None:
    fake = FakeDrain()
    run = fake.run()
    listed = _open_pull("a" * 40)
    fake.runs = [run]
    fake.open_pull_items = [listed]
    fake.pull_overrides[1] = {**listed, "merge_commit_sha": run["head_sha"]}
    result = drain_held_runs(fake.api(), REPO)
    assert result["protected"] == 2
    assert result["deleted"] == []
    assert fake.deletes == []


def test_malformed_open_pull_head_sha_fails_closed() -> None:
    fake = FakeDrain()
    fake.runs = [fake.run()]
    fake.open_pull_items = [_open_pull("not-a-sha")]
    with pytest.raises(GuardError, match="open pull request head sha missing"):
        drain_held_runs(fake.api(), REPO)
    assert fake.deletes == []


@pytest.mark.parametrize("head_repo", ["invalid", {"full_name": ""}])
def test_malformed_open_pull_head_repository_fails_closed(head_repo: object) -> None:
    fake = FakeDrain()
    pull = _open_pull("a" * 40)
    pull["head"]["repo"] = head_repo
    fake.runs = [fake.run()]
    fake.open_pull_items = [pull]
    with pytest.raises(GuardError, match="open pull request head repository invalid"):
        drain_held_runs(fake.api(), REPO)
    assert fake.deletes == []


def test_deleted_fork_pull_does_not_block_an_unrelated_candidate() -> None:
    fake = FakeDrain()
    fake.runs = [fake.run()]
    fake.open_pull_items = [_open_pull("a" * 40, "b" * 40, head_repo=None)]
    result = drain_held_runs(fake.api(), REPO)
    assert result["deleted"] == [101]
    assert fake.deletes == [101]


def test_hold_with_a_started_job_is_not_deleted() -> None:
    fake = FakeDrain()
    fake.runs = [fake.run()]
    fake.jobs_by_run[101] = [{"id": 1}]
    result = drain_held_runs(fake.api(), REPO)
    assert result["candidates"] == 1
    assert result["deleted"] == []
    assert fake.jobs_gets == [101]
    assert fake.deletes == []


def test_jobs_appearing_after_protection_reads_prevent_delete() -> None:
    fake = FakeDrain()
    fake.runs = [fake.run()]
    fake.jobs_reads[101] = [[], [{"id": 1}]]

    result = drain_held_runs(fake.api(), REPO)

    assert result["candidates"] == 1
    assert result["deleted"] == []
    assert fake.jobs_gets == [101, 101]
    assert fake.deletes == []


def test_run_becoming_queued_after_protection_reads_prevents_delete() -> None:
    fake = FakeDrain()
    run = fake.run()
    fake.runs = [run]
    fake.run_reads[101] = [
        run,
        {**run, "status": "queued", "conclusion": None},
    ]

    result = drain_held_runs(fake.api(), REPO)

    assert result["candidates"] == 1
    assert result["deleted"] == []
    assert fake.jobs_gets == [101, 101]
    assert fake.deletes == []


def test_hold_inside_the_age_floor_is_not_deleted() -> None:
    fake = FakeDrain()
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fake.runs = [fake.run(created_at=created_at)]
    result = drain_held_runs(fake.api(), REPO)
    assert result["deleted"] == []
    assert fake.jobs_gets == []
    assert fake.deletes == []


def test_base_repository_and_missing_head_repository_are_not_deleted() -> None:
    fake = FakeDrain()
    fake.runs = [
        fake.run(id=101, head_repository={"full_name": REPO}),
        fake.run(id=102, head_repository=None),
    ]
    result = drain_held_runs(fake.api(), REPO)
    assert result["deleted"] == []
    assert fake.jobs_gets == []
    assert fake.deletes == []


def test_empty_run_head_branch_is_not_deleted() -> None:
    fake = FakeDrain()
    fake.runs = [fake.run(head_branch="")]
    result = drain_held_runs(fake.api(), REPO)
    assert result["deleted"] == []
    assert fake.deletes == []


def test_truncated_pull_commit_list_protects_only_its_branch() -> None:
    fake = FakeDrain()
    fake.runs = [
        fake.run(id=101, head_branch="feature"),
        fake.run(id=102, head_branch="other-feature"),
    ]
    fake.open_pull_items = [_open_pull("a" * 40, "b" * 40)]
    fake.commits_by_pull[1] = [f"{ident:040x}" for ident in range(250)]
    result = drain_held_runs(fake.api(), REPO)
    assert result["deleted"] == [102]
    assert fake.deletes == [102]


def test_later_run_attempt_is_not_deleted() -> None:
    fake = FakeDrain()
    fake.runs = [fake.run(run_attempt=2)]
    result = drain_held_runs(fake.api(), REPO)
    assert result["deleted"] == []
    assert fake.jobs_gets == []
    assert fake.deletes == []


def test_dry_run_plans_without_deleting() -> None:
    fake = FakeDrain()
    fake.runs = [fake.run()]
    result = drain_held_runs(fake.api(), REPO, dry_run=True)
    assert result["planned"] == [101]
    assert result["deleted"] == []
    assert fake.deletes == []


def test_deletion_is_bounded_and_oldest_runs_are_deleted_first() -> None:
    fake = FakeDrain()
    start = datetime(2000, 1, 1, tzinfo=timezone.utc)
    fake.runs = [
        fake.run(
            id=ident,
            created_at=(start + timedelta(seconds=ident)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        )
        for ident in range(1, MAX_DELETIONS + 3)
    ]
    fake.runs.reverse()
    result = drain_held_runs(
        fake.api(), REPO, now=_now_for_offset(MAX_DELETIONS + 2, 0)
    )
    assert result["candidates"] == MAX_DELETIONS + 2
    assert result["deleted"] == list(range(1, MAX_DELETIONS + 1))
    assert fake.jobs_gets == [
        ident
        for ident in range(1, MAX_DELETIONS + 1)
        for _ in range(2)
    ]
    assert fake.deletes == list(range(1, MAX_DELETIONS + 1))


def test_started_job_does_not_consume_the_deletion_bound() -> None:
    fake = FakeDrain()
    fake.runs = [
        fake.run(id=ident)
        for ident in range(1, MAX_DELETIONS + 2)
    ]
    fake.jobs_by_run[1] = [{"id": 1}]
    result = drain_held_runs(
        fake.api(), REPO, now=_now_for_offset(MAX_DELETIONS + 1, 0)
    )
    assert result["candidates"] == MAX_DELETIONS + 1
    assert result["deleted"] == list(range(2, MAX_DELETIONS + 2))
    assert fake.jobs_gets == [1] + [
        ident
        for ident in range(2, MAX_DELETIONS + 2)
        for _ in range(2)
    ]
    assert fake.deletes == list(range(2, MAX_DELETIONS + 2))


def test_examination_bound_stops_jobs_reads_before_later_candidates() -> None:
    fake = FakeDrain()
    total = MAX_EXAMINED + 6
    fake.runs = [fake.run(id=ident) for ident in range(1, total + 1)]
    for ident in range(1, total):
        fake.jobs_by_run[ident] = [{"id": ident}]
    result = drain_held_runs(fake.api(), REPO, now=_now_for_offset(total, 0))
    assert result["candidates"] == MAX_EXAMINED + 6
    assert result["deleted"] == []
    assert fake.jobs_gets == list(range(1, MAX_EXAMINED + 1))
    assert not set(range(MAX_EXAMINED + 1, total + 1)) & set(fake.jobs_gets)
    assert fake.deletes == []


def test_examination_window_advances_past_a_non_deletable_prefix() -> None:
    candidate_count = MAX_EXAMINED + 5
    first_now = _now_for_offset(candidate_count, 0)

    first = FakeDrain()
    first.runs = [
        first.run(id=ident) for ident in range(1, candidate_count + 1)
    ]
    for ident in range(1, MAX_EXAMINED + 1):
        first.jobs_by_run[ident] = [{"id": ident}]
    first_result = drain_held_runs(first.api(), REPO, now=first_now)
    assert first_result["deleted"] == []
    assert first.jobs_gets == list(range(1, MAX_EXAMINED + 1))

    next_now = first_now + timedelta(seconds=ROTATION_PERIOD_S)
    second = FakeDrain()
    second.runs = [
        second.run(id=ident) for ident in range(1, candidate_count + 1)
    ]
    for ident in range(1, MAX_EXAMINED + 1):
        second.jobs_by_run[ident] = [{"id": ident}]
    second_result = drain_held_runs(second.api(), REPO, now=next_now)
    assert second_result["deleted"] == list(
        range(MAX_EXAMINED + 1, candidate_count + 1)
    )
    assert second.jobs_gets[:10] == [
        ident
        for ident in range(MAX_EXAMINED + 1, candidate_count + 1)
        for _ in range(2)
    ]


def test_wrapped_window_examines_every_candidate_below_the_bound() -> None:
    candidate_count = 7
    offset = MAX_EXAMINED % candidate_count
    fake = FakeDrain()
    fake.runs = [
        fake.run(id=ident) for ident in range(1, candidate_count + 1)
    ]
    result = drain_held_runs(
        fake.api(), REPO, now=_now_for_offset(candidate_count, offset)
    )
    expected = list(range(offset + 1, candidate_count + 1)) + list(
        range(1, offset + 1)
    )
    assert set(fake.jobs_gets) == set(range(1, candidate_count + 1))
    assert result["deleted"] == expected
    assert fake.deletes == expected


def test_ten_full_listing_pages_are_processed_as_a_window() -> None:
    fake = FakeDrain()
    fake.runs = [fake.run(id=ident) for ident in range(1, 1001)]
    result = drain_held_runs(fake.api(), REPO, now=_now_for_offset(1000, 0))
    assert result["scanned"] == 1000
    assert result["candidates"] == 1000
    assert result["deleted"] == list(range(1, MAX_DELETIONS + 1))
    assert fake.jobs_gets == [
        ident
        for ident in range(1, MAX_DELETIONS + 1)
        for _ in range(2)
    ]
    assert fake.deletes == list(range(1, MAX_DELETIONS + 1))


def test_missing_jobs_inventory_skips_run_and_deletes_next_candidate() -> None:
    fake = FakeDrain()
    fake.runs = [fake.run(id=101), fake.run(id=102)]
    fake.jobs_404.add(101)
    result = drain_held_runs(fake.api(), REPO, now=_now_for_offset(2, 0))
    assert result["deleted"] == [102]
    assert fake.jobs_gets == [101, 102, 102]
    assert fake.deletes == [102]


def test_forbidden_delete_aborts_after_one_attempt() -> None:
    fake = FakeDrain()
    fake.runs = [fake.run(id=101), fake.run(id=102)]
    fake.delete_responses[101] = (403, {"message": "forbidden"}, {})
    result = drain_held_runs(fake.api(), REPO, now=_now_for_offset(2, 0))
    assert result["aborted"] == "forbidden"
    assert result["deleted"] == []
    assert fake.deletes == [101]


def test_rate_limited_delete_aborts_the_pass() -> None:
    fake = FakeDrain()
    fake.runs = [fake.run()]
    fake.delete_responses[101] = (429, {"message": "rate limited"}, {})
    result = drain_held_runs(fake.api(), REPO)
    assert result["aborted"] == "rate-limited"
    assert result["deleted"] == []
    assert fake.deletes == [101]


def test_later_delete_failure_preserves_deleted_ids() -> None:
    fake = FakeDrain()
    fake.runs = [fake.run(id=101), fake.run(id=102)]
    fake.delete_responses[102] = (500, {"message": "failed"}, {})
    result = drain_held_runs(fake.api(), REPO, now=_now_for_offset(2, 0))
    assert result["deleted"] == [101]
    assert result["aborted"] == "error"
    assert "HTTP 500" in result["error"]
    assert fake.deletes == [101, 102]


def test_delete_failure_before_any_delete_still_raises() -> None:
    fake = FakeDrain()
    fake.runs = [fake.run()]
    fake.delete_responses[101] = (500, {"message": "failed"}, {})
    with pytest.raises(GuardError, match="held run delete HTTP 500"):
        drain_held_runs(fake.api(), REPO)
    assert fake.deletes == [101]


def test_missing_reread_is_tolerated_and_the_next_candidate_is_deleted() -> None:
    fake = FakeDrain()
    fake.runs = [fake.run(id=101), fake.run(id=102)]
    fake.missing_on_reread.add(101)
    result = drain_held_runs(fake.api(), REPO, now=_now_for_offset(2, 0))
    assert result["deleted"] == [102]
    assert fake.deletes == [102]


def test_missing_delete_is_tolerated_and_the_next_candidate_is_deleted() -> None:
    fake = FakeDrain()
    fake.runs = [fake.run(id=101), fake.run(id=102)]
    fake.delete_responses[101] = (404, {"message": "missing"}, {})
    result = drain_held_runs(fake.api(), REPO, now=_now_for_offset(2, 0))
    assert result["deleted"] == [102]
    assert fake.deletes == [101, 102]


def test_missing_open_pull_head_fails_closed_without_deleting() -> None:
    fake = FakeDrain()
    fake.runs = [fake.run()]
    fake.open_pull_items = [{"head": {}, "merge_commit_sha": None}]
    with pytest.raises(GuardError, match="open pull request head sha missing"):
        drain_held_runs(fake.api(), REPO)
    assert fake.deletes == []


def test_recheck_skips_a_run_that_is_no_longer_held() -> None:
    fake = FakeDrain()
    run = fake.run()
    fake.runs = [run]
    fake.run_overrides[101] = {
        **run,
        "status": "completed",
        "conclusion": "success",
    }
    result = drain_held_runs(fake.api(), REPO)
    assert result["candidates"] == 1
    assert result["deleted"] == []
    assert fake.deletes == []


def test_all_open_sweep_runs_drain_last_and_drain_error_does_not_change_exit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class PassingAssessment:
        closed = False
        hard_fail = False
        ok = True
        mode = "enforce"
        draft = False

        def __init__(self, number: int) -> None:
            self.number = number

        def to_json(self) -> dict:
            return {"ok": True, "pr": self.number}

    fake = FakeDrain()
    events: list[str] = []
    monkeypatch.setattr(
        a38_guard, "list_open_pulls", lambda _api, _repo: [1, 2]
    )

    def reconcile(_api: object, _repo: str, number: int, **_kwargs: object):
        events.append(f"pull:{number}")
        return PassingAssessment(number)

    expected = {
        "enabled": True,
        "scanned": 0,
        "protected": 0,
        "candidates": 0,
        "deleted": [],
        "planned": [],
        "aborted": None,
    }

    def drain(_api: object, _repo: str, *, dry_run: bool = False) -> dict:
        assert dry_run is False
        events.append("drain")
        return expected

    monkeypatch.setattr(a38_guard, "reconcile_pull", reconcile)
    monkeypatch.setattr(held_run_drain, "drain_held_runs", drain)
    code = a38_guard.main(
        ["reconcile", "--repo", REPO, "--all-open", "--json"],
        env={"GH_TOKEN": "fake"},
        api=fake.api(),
    )
    captured = capsys.readouterr()
    assert code == 0
    assert events == ["pull:1", "pull:2", "drain"]
    assert json.loads(captured.out)["held_run_drain"] == expected

    def fail_drain(
        _api: object, _repo: str, *, dry_run: bool = False
    ) -> dict:
        raise GuardError("delete denied")

    monkeypatch.setattr(held_run_drain, "drain_held_runs", fail_drain)
    code = a38_guard.main(
        ["reconcile", "--repo", REPO, "--all-open", "--json"],
        env={"GH_TOKEN": "fake"},
        api=fake.api(),
    )
    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out)["held_run_drain"] == {
        "error": "delete denied",
    }
    assert "a38-guard: held run drain: delete denied" in captured.err
