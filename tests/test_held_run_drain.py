"""Bounded draining of superseded never-started fork workflow holds."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from agent_cli import a38_guard, held_run_drain
from agent_cli.a38_guard import GuardError
from agent_cli.held_run_drain import MAX_DELETIONS, drain_held_runs
from test_a38_guard import REPO, FakeAPI, _pr_guard_config

pytestmark = pytest.mark.no_pg

FORK = "contributor/fork"


class FakeDrain(FakeAPI):
    """FakeAPI plus held-run inventory, jobs reads, and run deletion."""

    def __init__(self, *, configured: bool = True, enabled: bool = True) -> None:
        super().__init__()
        config = _pr_guard_config()
        if configured:
            config["held_run_drain"] = {"enabled": enabled}
        self.set_pr_guard_config(config)
        self.runs: list[dict] = []
        self.open_pull_items: list[dict] = []
        self.jobs_by_run: dict[int, list[dict]] = {}
        self.run_overrides: dict[int, dict] = {}
        self.delete_responses: dict[int, tuple[int, object, dict]] = {}
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
            "status": "completed",
            "conclusion": "action_required",
            "run_attempt": 1,
            "created_at": "2000-01-01T00:00:00Z",
        }
        data.update(changes)
        return data

    def request_fn(self, method: str, url: str, body: bytes | None = None):
        method = method.upper()
        path = urlparse(url).path
        query = parse_qs(urlparse(url).query)
        root = f"/repos/{REPO}"
        if method == "GET" and path == root:
            return 200, {"default_branch": "develop"}, {}
        if method == "GET" and path == f"{root}/pulls":
            return 200, copy.deepcopy(self.open_pull_items), {}
        if method == "GET" and path == f"{root}/actions/runs":
            self.listing_calls.append(url)
            page = int((query.get("page") or ["1"])[0])
            per_page = int((query.get("per_page") or ["100"])[0])
            start = (page - 1) * per_page
            chunk = self.runs[start : start + per_page]
            return 200, {
                "total_count": len(self.runs),
                "workflow_runs": copy.deepcopy(chunk),
            }, {}
        if path.startswith(f"{root}/actions/runs/"):
            remainder = path.split("/actions/runs/", 1)[1]
            ident = int(remainder.split("/", 1)[0])
            if method == "GET" and remainder.endswith("/jobs"):
                self.jobs_gets.append(ident)
                jobs = self.jobs_by_run.get(ident, [])
                return 200, {
                    "total_count": len(jobs),
                    "jobs": copy.deepcopy(jobs),
                }, {}
            if method == "GET" and "/" not in remainder:
                run = self.run_overrides.get(ident)
                if run is None:
                    run = next(item for item in self.runs if item["id"] == ident)
                return 200, copy.deepcopy(run), {}
            if method == "DELETE" and "/" not in remainder:
                self.deletes.append(ident)
                return self.delete_responses.get(ident, (204, None, {}))
        return super().request_fn(method, url, body)


def _open_pull(head: str, merge: str | None = None) -> dict:
    return {"head": {"sha": head}, "merge_commit_sha": merge}


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


def test_superseded_old_never_started_fork_hold_is_deleted() -> None:
    fake = FakeDrain()
    fake.runs = [fake.run()]
    result = drain_held_runs(fake.api(), REPO)
    assert result["deleted"] == [101]
    assert fake.deletes == [101]


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


def test_hold_with_a_started_job_is_not_deleted() -> None:
    fake = FakeDrain()
    fake.runs = [fake.run()]
    fake.jobs_by_run[101] = [{"id": 1}]
    result = drain_held_runs(fake.api(), REPO)
    assert result["deleted"] == []
    assert fake.jobs_gets == [101]
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
    result = drain_held_runs(fake.api(), REPO)
    assert result["candidates"] == MAX_DELETIONS + 2
    assert result["deleted"] == list(range(1, MAX_DELETIONS + 1))
    assert fake.deletes == list(range(1, MAX_DELETIONS + 1))


def test_forbidden_delete_aborts_after_one_attempt() -> None:
    fake = FakeDrain()
    fake.runs = [fake.run(id=101), fake.run(id=102)]
    fake.delete_responses[101] = (403, {"message": "forbidden"}, {})
    result = drain_held_runs(fake.api(), REPO)
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


def test_missing_delete_is_tolerated_and_the_next_candidate_is_deleted() -> None:
    fake = FakeDrain()
    fake.runs = [fake.run(id=101), fake.run(id=102)]
    fake.delete_responses[101] = (404, {"message": "missing"}, {})
    result = drain_held_runs(fake.api(), REPO)
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
        "enabled": True,
        "error": "delete denied",
    }
    assert "a38-guard: held run drain: delete denied" in captured.err
