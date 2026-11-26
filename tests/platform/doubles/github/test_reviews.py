"""U-DBL-github (KOR-51, ADR-094 section 4): the GitHub double serves the commits and reviews of a pull request, check
runs and commit statuses of a commit, and merges a pull request like GitHub: only an open, non-draft one without a
conflict (`405` otherwise), only at the head the caller names (`409` otherwise), with a merge commit of two parents on
the base branch; the pull request is closed and merged afterwards."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tests.platform.doubles.github.test_mergeable import REPO, advance, head, seed


def test_commits_and_reviews_of_a_pull_request(start_github: Callable[..., Any]) -> None:
    gh = start_github(seed())
    feature = head(gh, "feature")
    assert [item["sha"] for item in gh.get(f"{REPO}/pulls/1/commits")] == [feature]
    assert gh.get(f"{REPO}/pulls/1/reviews") == []
    created = gh.api("POST", f"{REPO}/pulls/1/reviews", {"event": "APPROVE", "body": "Fine."})
    assert created.status == 200
    review = created.json()
    assert (review["state"], review["commit_id"], review["user"]["login"]) == ("APPROVED", feature, "octo-user")
    other = gh.api("POST", f"{REPO}/pulls/1/reviews", {"event": "REQUEST_CHANGES", "commit_id": feature})
    assert other.json()["state"] == "CHANGES_REQUESTED"
    assert [item["state"] for item in gh.get(f"{REPO}/pulls/1/reviews")] == ["APPROVED", "CHANGES_REQUESTED"]
    assert gh.api("POST", f"{REPO}/pulls/1/reviews", {"event": "MAYBE"}).status == 422
    assert gh.api("POST", f"{REPO}/pulls/1/reviews", {"event": "COMMENT", "commit_id": "0" * 40}).status == 422
    assert gh.api("GET", f"{REPO}/pulls/9/reviews").status == 404


def test_check_runs_and_statuses(start_github: Callable[..., Any]) -> None:
    gh = start_github(seed())
    feature = head(gh, "feature")
    assert gh.get(f"{REPO}/commits/feature/check-runs") == {"total_count": 0, "check_runs": []}
    assert gh.get(f"{REPO}/commits/feature/status")["state"] == "pending"
    run = gh.api("POST", f"{REPO}/check-runs", {"name": "ci", "head_sha": feature, "conclusion": "success"})
    assert run.status == 201
    assert run.json()["status"] == "completed"
    queued = gh.api("POST", f"{REPO}/check-runs", {"name": "lint", "head_sha": feature})
    assert queued.json()["status"] == "queued"
    found = gh.get(f"{REPO}/commits/{feature}/check-runs")
    assert [(item["name"], item["conclusion"]) for item in found["check_runs"]] == [("ci", "success"), ("lint", None)]
    assert gh.api("POST", f"{REPO}/check-runs", {"name": "x", "head_sha": "0" * 40}).status == 422
    assert gh.api("POST", f"{REPO}/check-runs", {"name": "x", "head_sha": feature, "status": "odd"}).status == 422
    assert gh.api("POST", f"{REPO}/statuses/{feature}", {"state": "success", "context": "build"}).status == 201
    assert gh.get(f"{REPO}/commits/feature/status")["state"] == "success"
    gh.api("POST", f"{REPO}/statuses/{head(gh, 'main')}", {"state": "failure", "context": "deploy"})
    gh.api("POST", f"{REPO}/statuses/{feature}", {"state": "pending", "context": "deploy"})
    assert gh.get(f"{REPO}/commits/feature/status")["state"] == "pending"
    gh.api("POST", f"{REPO}/statuses/{feature}", {"state": "error", "context": "deploy"})
    combined = gh.get(f"{REPO}/commits/feature/status")
    assert (combined["state"], combined["total_count"], combined["sha"]) == ("failure", 2, feature)
    assert gh.api("POST", f"{REPO}/statuses/{feature}", {"state": "green"}).status == 422
    assert gh.api("GET", f"{REPO}/commits/nothing/status").status == 404


def test_the_merge_of_a_pull_request(start_github: Callable[..., Any]) -> None:
    gh = start_github(seed())
    feature, main = head(gh, "feature"), head(gh, "main")
    assert gh.api("PUT", f"{REPO}/pulls/1/merge", {"merge_method": "octopus"}).status == 422
    assert gh.api("PUT", f"{REPO}/pulls/1/merge", {"sha": main}).status == 409
    advance(gh, "main", {"README.md": None, "docs/new.md": "n\n"})  # the base moved without a conflict
    merged = gh.api("PUT", f"{REPO}/pulls/1/merge", {"sha": feature, "merge_method": "squash"})
    assert merged.status == 200, merged.body
    sha = merged.json()["sha"]
    assert merged.json()["merged"] is True
    assert head(gh, "main") == sha
    commit = gh.get(f"{REPO}/git/commits/{sha}")
    assert len(commit["parents"]) == 2
    tree = gh.get(f"{REPO}/git/trees/{commit['tree']['sha']}?recursive=1")
    assert {item["path"] for item in tree["tree"] if item["type"] == "blob"} == {"docs/new.md", "src/app.py"}
    pull = gh.get(f"{REPO}/pulls/1")
    assert (pull["state"], pull["merged"], pull["merge_commit_sha"]) == ("closed", True, sha)
    assert pull["merged_at"] is not None
    assert gh.api("PUT", f"{REPO}/pulls/1/merge", {}).status == 405  # closed now


def test_a_conflict_or_a_draft_is_not_mergeable(start_github: Callable[..., Any]) -> None:
    gh = start_github(seed(draft=True))
    assert gh.api("PUT", f"{REPO}/pulls/1/merge", {}).status == 405
    gh = start_github(seed())
    advance(gh, "main", {"src/app.py": "x = 3\n"})
    assert gh.api("PUT", f"{REPO}/pulls/1/merge", {}).status == 405
