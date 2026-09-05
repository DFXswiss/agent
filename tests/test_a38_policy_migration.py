"""Policy migrations, fork boundaries and stale-evidence publication guards."""

from __future__ import annotations

import json
import os
from unittest import mock
from urllib.parse import urlparse

import pytest

from agent_cli import a38_guard as guard
from test_a38_guard import (
    AUTHOR_ID, BASE, BASE2, HEAD, REPO, FakeAPI, _policy, _report_comment,
)

pytestmark = pytest.mark.no_pg
MAINTAINER = 3030


def migration() -> FakeAPI:
    fake = FakeAPI()
    fake.files[(HEAD, ".github/a38.json")] = json.dumps(_policy()).encode()
    fake.files[(HEAD, ".github/workflows/test.yml")] += b"\n# reviewed workflow change\n"
    fake.add_author_report(_report_comment(), updated_at="2026-09-05T12:00:00Z", cid=80)
    fake.permissions["maintainer"] = {"permission": "write", "user": {"id": MAINTAINER}}
    fake.reviews = [{
        "id": 100, "user": {"id": MAINTAINER, "login": "maintainer"},
        "state": "APPROVED", "commit_id": HEAD,
        "submitted_at": "2026-09-05T13:00:00Z",
        "body": f"{guard.POLICY_APPROVAL_PREFIX} head={HEAD} base={BASE}",
    }]
    return fake


def test_explicit_maintainer_approval_allows_changed_workflows() -> None:
    result = guard.reconcile_pull(migration().api(), REPO, 1)
    assert result.ok
    assert result.policy_sha == HEAD
    assert result.approval_fingerprint
    assert result.context == "A38 / report (develop)"


@pytest.mark.parametrize("change", ["ordinary", "stale-head", "stale-base", "author", "read", "dismissed"])
def test_invalid_approval_cannot_adopt_head_policy(change: str) -> None:
    fake = migration()
    review = fake.reviews[0]
    if change == "ordinary":
        review["body"] = "Looks good"
    elif change == "stale-head":
        review["commit_id"] = BASE2
    elif change == "stale-base":
        review["body"] = str(review["body"]).replace(BASE, BASE2)
    elif change == "author":
        review["user"]["id"] = AUTHOR_ID
    elif change == "read":
        fake.permissions["maintainer"]["permission"] = "read"
    else:
        review["state"] = "DISMISSED"
    assert not guard.assess_pull(fake.api(), REPO, 1).ok


def test_latest_substantive_review_and_other_changes_request_block() -> None:
    fake = migration()
    later = dict(fake.reviews[0], id=101, state="CHANGES_REQUESTED", submitted_at="2026-09-05T14:00:00Z")
    fake.reviews.append(later)
    assert not guard.assess_pull(fake.api(), REPO, 1).ok
    later["state"] = "COMMENTED"
    assert guard.assess_pull(fake.api(), REPO, 1).ok
    later["state"] = "CHANGES_REQUESTED"
    later["user"] = {"id": 4040, "login": "other"}
    fake.permissions["other"] = {"permission": "admin", "user": {"id": 4040}}
    assert not guard.assess_pull(fake.api(), REPO, 1).ok


def test_permission_identity_and_api_refusal_fail_closed() -> None:
    fake = migration()
    fake.permissions["maintainer"]["user"]["id"] = 9999
    with pytest.raises(guard.GuardError, match="identity mismatch"):
        guard.assess_pull(fake.api(), REPO, 1)
    fake.denied_prefixes.append(f"/repos/{REPO}/collaborators/")
    with pytest.raises(guard.GuardError, match="denied"):
        guard.reconcile_pull(fake.api(), REPO, 1)
    assert fake.statuses[0]["state"] == "error"


def test_bootstrap_requires_explicit_approval_and_cannot_weaken_mode() -> None:
    fake = migration()
    fake.files[(HEAD, ".github/a38.json")] = json.dumps(_policy(mode="observe")).encode()
    result = guard.assess_pull(fake.api(), REPO, 1)
    assert result.ok and result.mode == "enforce"
    del fake.files[(BASE, ".github/a38.json")]
    result = guard.assess_pull(fake.api(), REPO, 1)
    assert result.ok and result.mode == "enforce"
    fake.reviews.clear()
    assert not guard.assess_pull(fake.api(), REPO, 1).ok


def test_approval_does_not_waive_workflow_coverage_or_report() -> None:
    fake = migration()
    fake.files[(HEAD, ".github/workflows/test.yml")] += b"  extra:\n    runs-on: ubuntu-latest\n    steps: []\n"
    assert not guard.assess_pull(fake.api(), REPO, 1).ok
    fake = migration()
    fake.comments.clear()
    assert not guard.assess_pull(fake.api(), REPO, 1).ok


def test_report_edit_before_publication_is_reassessed() -> None:
    fake = migration()
    original = fake.request_fn
    comment_reads = 0

    def request(method, url, body=None):
        nonlocal comment_reads
        if method == "GET" and urlparse(url).path.endswith("/issues/1/comments"):
            comment_reads += 1
            if comment_reads == 2:
                fake.comments[0]["body"] = _report_comment(result="fail", exit_code=1)
        return original(method, url, body)

    api = guard.GitHubApi("test", request_fn=request)
    result = guard.reconcile_pull(api, REPO, 1)
    assert not result.ok
    assert not any(s["state"] == "success" for s in fake.statuses)


def test_report_deleted_after_comment_write_never_posts_success() -> None:
    fake = migration()
    original = fake.request_fn

    def request(method, url, body=None):
        response = original(method, url, body)
        if method == "POST" and urlparse(url).path.endswith("/issues/1/comments"):
            fake.comments[:] = [c for c in fake.comments if c["user"]["id"] != AUTHOR_ID]
        return response

    result = guard.reconcile_pull(guard.GitHubApi("test", request_fn=request), REPO, 1)
    assert not result.ok
    assert not any(s["state"] == "success" for s in fake.statuses)


def test_review_dismissal_before_publication_is_reassessed() -> None:
    fake = migration()
    original = fake.request_fn
    review_reads = 0

    def request(method, url, body=None):
        nonlocal review_reads
        if method == "GET" and urlparse(url).path.endswith("/reviews"):
            review_reads += 1
            if review_reads == 2:
                fake.reviews[0]["state"] = "DISMISSED"
        return original(method, url, body)

    result = guard.reconcile_pull(guard.GitHubApi("test", request_fn=request), REPO, 1)
    assert not result.ok
    assert not any(s["state"] == "success" for s in fake.statuses)


def test_fork_files_are_fetched_from_head_repository_only() -> None:
    fake = migration()
    fork = "contributor/fork"
    fake.pull["head"]["repo"]["full_name"] = fork
    original = fake.request_fn
    reads = []

    def request(method, url, body=None):
        if method == "GET" and ("/contents/" in url or "/git/trees/" in url):
            reads.append(url)
        return original(method, url, body)

    result = guard.assess_pull(guard.GitHubApi("test", request_fn=request), REPO, 1)
    assert result.ok
    assert all(f"/repos/{fork}/" in url for url in reads if HEAD in url)
    assert all(f"/repos/{REPO}/" in url for url in reads if BASE in url)


@pytest.mark.parametrize("visibility", [None, 0, "false"])
def test_visibility_is_not_coerced(visibility) -> None:
    fake = FakeAPI()
    fake.pull["base"]["repo"]["private"] = visibility
    with pytest.raises(guard.GuardError, match="visibility"):
        guard.assess_pull(fake.api(), REPO, 1)


def test_required_context_stays_stable_after_base_merge() -> None:
    fake = FakeAPI()
    first = guard.assess_pull(fake.api(), REPO, 1)
    fake.pull["base"]["sha"] = BASE2
    fake.tree_paths[BASE2] = fake.tree_paths[BASE]
    for (sha, path), content in list(fake.files.items()):
        if sha == BASE:
            fake.files[(BASE2, path)] = content
    second = guard.assess_pull(fake.api(), REPO, 1)
    assert first.context == second.context == "A38 / report (develop)"


@pytest.mark.parametrize("content", [
    b"jobs: {test: {}, test: {}}",
    b"jobs: {test: {}}\njobs: {other: {}}",
    b"jobs: &j {test: {}}\nother: *j",
    b"jobs: {}" + b" " * guard.MAX_FILE_BYTES,
])
def test_ambiguous_or_oversized_yaml_rejected(content: bytes) -> None:
    with pytest.raises(guard.GuardError):
        guard.enumerate_workflow_jobs(".github/workflows/test.yml", content)


def test_denied_pat_identity_does_not_impersonate_actions_bot() -> None:
    fake = FakeAPI()
    fake.user_endpoint_denied = True
    with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}):
        with pytest.raises(guard.GuardError, match="acting user"):
            fake.api().resolve_own_user()


@pytest.mark.parametrize("action", ["submitted", "edited", "dismissed"])
def test_review_events_reconcile(action: str) -> None:
    fake = migration()
    payload = {"action": action, "pull_request": {"number": 1}, "repository": {"full_name": REPO}}
    result = guard.reconcile_event(fake.api(), event_name="pull_request_review", payload=payload, dry_run=True)
    assert isinstance(result, guard.Assessment) and result.ok
    assert fake.writes == []


def test_all_open_dispatch_bypasses_schedule_event_parsing() -> None:
    fake = FakeAPI()
    fake.add_author_report(_report_comment(), updated_at="2026-09-05T12:00:00Z", cid=80)
    code = guard.main(
        ["--repo", REPO, "--all-open", "--dry-run"],
        env={"GITHUB_EVENT_NAME": "schedule", "GITHUB_EVENT_PATH": "/unused/event.json"},
        api=fake.api(),
    )
    assert code == 0
    assert fake.writes == []


def test_only_immutable_reads_are_cached() -> None:
    fake = FakeAPI()
    calls = []

    def request(method, url, body=None):
        calls.append(url)
        return fake.request_fn(method, url, body)

    api = guard.GitHubApi("test", request_fn=request)
    for _ in range(2):
        guard.fetch_policy_text(api, REPO, BASE)
        guard.collect_comments(api, REPO, 1)
        guard.fetch_pull(api, REPO, 1)
    assert len([url for url in calls if "/contents/" in url]) == 1
    assert len([url for url in calls if "/comments" in url]) == 2
    assert len([url for url in calls if url.endswith("/pulls/1")]) == 2


def test_all_open_observe_failure_is_advisory(capsys) -> None:
    fake = FakeAPI()
    fake.files[(BASE, ".github/a38.json")] = json.dumps(_policy(mode="observe")).encode()
    code = guard.main(["--repo", REPO, "--all-open", "--dry-run"], env={}, api=fake.api())
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["results"][0]["ok"] is False
    assert payload["results"][0]["mode"] == "observe"
    assert fake.writes == []


def test_all_open_mixed_modes_retains_enforce_failure(capsys) -> None:
    fake = FakeAPI()
    fake.open_pulls = [1, 2]
    assessments = [
        guard.Assessment(ok=False, status="fail", mode="observe"),
        guard.Assessment(ok=False, status="fail", mode="enforce"),
    ]
    with mock.patch.object(guard, "reconcile_pull", side_effect=assessments):
        code = guard.main(["--repo", REPO, "--all-open", "--dry-run"], env={}, api=fake.api())
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert [item["ok"] for item in payload["results"]] == [False, False]
    assert [item["mode"] for item in payload["results"]] == ["observe", "enforce"]


@pytest.mark.parametrize("path", ["direct", "event", "batch", "publish"])
def test_closed_pr_cli_is_successful_noop(path: str, tmp_path, capsys) -> None:
    fake = FakeAPI()
    fake.pull["state"] = "closed"
    fake.files.clear()
    fake.denied_prefixes.extend([f"/repos/{REPO}/contents/", f"/repos/{REPO}/pulls/1/reviews"])
    args = ["--repo", REPO, "--pr", "1"]
    if path == "event":
        event = tmp_path / "event.json"
        event.write_text(json.dumps({
            "action": "created", "repository": {"full_name": REPO},
            "issue": {"number": 1, "pull_request": {}},
            "comment": {"user": {"id": AUTHOR_ID}, "body": "Thanks"},
        }))
        args = ["reconcile", "--event-name", "issue_comment", "--event-file", str(event)]
    elif path == "batch":
        args = ["--repo", REPO, "--all-open"]
    elif path == "publish":
        assessment = tmp_path / "assessment.json"
        assessment.write_text("{}")
        args = ["publish", "--repo", REPO, "--pr", "1", "--assessment-file", str(assessment)]
    code = guard.main(args, env={}, api=fake.api())
    payload = json.loads(capsys.readouterr().out)
    result = payload["results"][0] if path == "batch" else payload
    assert code == 0
    assert result["ok"] is True
    assert result["status"] == "closed"
    assert result["closed"] is True
    assert result["comment_body"] == ""
    assert result["state"] == ""
    assert fake.writes == []


def test_closed_deleted_fork_is_a_noop(capsys) -> None:
    fake = FakeAPI()
    fake.pull["state"] = "closed"
    fake.pull["head"]["repo"] = None
    code = guard.main(["--repo", REPO, "--pr", "1"], env={}, api=fake.api())
    assert code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "closed"
    assert fake.writes == []


@pytest.mark.parametrize("event_name,payload", [
    ("issue_comment", {"action": "created", "issue": {"number": 9}}),
    ("issue_comment", {"action": "created", "issue": {"number": 1, "pull_request": {}},
                       "comment": {"user": {"id": guard.GITHUB_ACTIONS_BOT_ID}}}),
    ("pull_request_target", {"action": "closed"}),
])
def test_ignored_event_cli_exits_successfully(event_name, payload, tmp_path, capsys) -> None:
    fake = FakeAPI()
    event = tmp_path / "ignored.json"
    event.write_text(json.dumps(payload))
    code = guard.main(["reconcile", "--event-name", event_name, "--event-file", str(event)],
                      env={}, api=fake.api())
    result = json.loads(capsys.readouterr().out)
    assert code == 0
    assert result["status"] == "ignored"
    assert fake.writes == []


def test_empty_all_open_cli_exits_successfully(capsys) -> None:
    fake = FakeAPI()
    fake.open_pulls = []
    code = guard.main(["--repo", REPO, "--all-open"], env={}, api=fake.api())
    assert code == 0
    assert json.loads(capsys.readouterr().out) == {"results": []}
    assert fake.writes == []


@pytest.mark.parametrize("approval_read", [2, 3])
def test_new_approval_activating_stricter_policy_prevents_stale_success(approval_read: int) -> None:
    fake = migration()
    # This policy-only PR initially passes under the base rules. Its head rules
    # become authoritative only when a new explicit approval arrives.
    fake.files[(HEAD, ".github/workflows/test.yml")] = fake.files[(BASE, ".github/workflows/test.yml")]
    stricter = _policy()
    stricter["jobs"][0]["command"] = "pytest --strict-markers"
    fake.files[(HEAD, ".github/a38.json")] = json.dumps(stricter).encode()
    approval = fake.reviews.pop()
    original = fake.request_fn
    review_reads = 0

    def request(method, url, body=None):
        nonlocal review_reads
        if method == "GET" and urlparse(url).path.endswith("/reviews"):
            review_reads += 1
            # Read 2 is before the bot comment; read 3 is immediately before
            # success status publication. Both empty-to-approved races matter.
            if review_reads == approval_read:
                fake.reviews.append(approval)
        return original(method, url, body)

    result = guard.reconcile_pull(guard.GitHubApi("test", request_fn=request), REPO, 1)
    assert not result.ok
    assert result.policy_sha == HEAD
    assert result.approval_fingerprint
    assert any("command" in reason.lower() for reason in result.reasons)
    assert not any(status["state"] == "success" for status in fake.statuses)


@pytest.mark.parametrize("reviewer", ["app[bot]", "community-user"])
@pytest.mark.parametrize("review_state", ["CHANGES_REQUESTED", "APPROVED"])
def test_ineligible_reviewer_does_not_invalidate_report(reviewer: str, review_state: str) -> None:
    fake = FakeAPI()
    fake.add_author_report(_report_comment(), updated_at="2026-09-05T12:00:00Z", cid=80)
    fake.reviews = [{
        "id": 100, "user": {"id": 9090, "login": reviewer},
        "state": review_state, "commit_id": HEAD,
        "submitted_at": "2026-09-05T13:00:00Z",
        "body": f"{guard.POLICY_APPROVAL_PREFIX} head={HEAD} base={BASE}",
    }]
    original = fake.request_fn

    def request(method, url, body=None):
        if method == "GET" and "/collaborators/" in url:
            assert reviewer == "community-user", "bot must never reach permission lookup"
            return 404, {"message": "Not Found"}, {}
        return original(method, url, body)

    result = guard.reconcile_pull(guard.GitHubApi("test", request_fn=request), REPO, 1)
    assert result.ok
    assert result.policy_sha == BASE
    assert result.approval_fingerprint == ""
    assert fake.statuses
    assert all(status["state"] == "success" for status in fake.statuses)
