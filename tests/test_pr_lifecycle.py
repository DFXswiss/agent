"""CI completion, ownership, conflicts and transitions through the real guard."""
import copy
import json
from urllib.parse import urlparse

import pytest

from agent_cli.a38_guard import GuardError, reconcile_pull
from agent_cli.pr_guard_config import load_pr_guard_config, PrGuardConfigError
from agent_cli.pr_lifecycle import AUTH_MARKER, STATE_MARKER
from test_a38_guard import AUTHOR_ID, HEAD, BASE, BASE2, BOT_ID, REPO
from test_workflow_approval_core import ApprovalAPI, PATH

pytestmark = pytest.mark.no_pg
GUARD = ".github/workflows/guard.yml"


class LifecycleAPI(ApprovalAPI):
    def __init__(self):
        super().__init__()
        self.config["lifecycle"] = {"enabled": True, "auto_ready": True,
                                  "required_workflows": [PATH], "ignored_workflows": [GUARD]}
        self.set_pr_guard_config(self.config)
        self.pull.update(draft=False, mergeable=True, node_id="PR_example")
        self.runs[0].update(check_suite_id=201, conclusion="success")
        self.checks = []
        self.transitions = []
        self.graphql_error = False
        self.mutate_during_transition = False
        self.fail_comment_once = False

    def request_fn(self, method, url, body=None):
        path = urlparse(url).path
        if method == "GET" and path.endswith("/check-runs"):
            return 200, {"total_count": len(self.checks), "check_runs": copy.deepcopy(self.checks)}, {}
        if method == "POST" and path == "/graphql":
            payload = json.loads(body)
            assert payload["variables"] == {"id": "PR_example"}
            assert "mergePullRequest" not in payload["query"]
            operation = "convertPullRequestToDraft" if "convertPullRequestToDraft" in payload["query"] else "markPullRequestReadyForReview"
            if self.graphql_error:
                return 200, {"errors": [{"message": "denied"}]}, {}
            self.pull["draft"] = operation == "convertPullRequestToDraft"
            self.transitions.append(self.pull["draft"])
            if self.mutate_during_transition:
                self.pull["head"]["sha"] = BASE2
            return 200, {"data": {operation: {"pullRequest": {
                "id": "PR_example", "isDraft": self.pull["draft"],
                "headRefOid": self.pull["head"]["sha"], "baseRefOid": BASE}}}}, {}
        if method == "PATCH" and "/issues/comments/" in path and self.fail_comment_once:
            payload = json.loads(body)
            if STATE_MARKER in payload.get("body", "") and '"phase": "applied"' in payload["body"]:
                self.fail_comment_once = False
                return 503, {}, {}
        return super().request_fn(method, url, body)

    def own_authorization(self, **changes):
        record = {"repo": REPO, "pr": 1, "head": HEAD, "base": BASE,
                  "runs": [{"run_id": 101, "workflow": PATH}]}
        record.update(changes)
        self.comments.append({"id": 500, "user": {"id": BOT_ID},
                              "body": AUTH_MARKER + "\n```json\n" + json.dumps(record) + "\n```"})


@pytest.mark.parametrize("status,conclusion", [
    ("queued", None), ("waiting", None), ("in_progress", None), ("pending", None),
    ("completed", "failure"), ("completed", "cancelled"), ("completed", "timed_out"),
    ("completed", "action_required"), ("completed", "skipped"), ("completed", "neutral"),
])
def test_not_fully_green_returns_ready_to_draft_once(status, conclusion):
    fake = LifecycleAPI()
    fake.runs[0].update(status=status, conclusion=conclusion)
    result = reconcile_pull(fake.api(), REPO, 1)
    assert result.lifecycle["action"] == "draft"
    assert fake.pull["draft"] and fake.transitions == [True]
    comments = [c for c in fake.comments if c["body"].startswith(STATE_MARKER)]
    assert len(comments) == 1 and '"phase": "applied"' in comments[0]["body"]
    reconcile_pull(fake.api(), REPO, 1)
    assert fake.transitions == [True]
    assert len([c for c in fake.comments if c["body"].startswith(STATE_MARKER)]) == 1


def test_missing_ci_is_not_vacuously_green():
    fake = LifecycleAPI()
    fake.runs.clear()
    result = reconcile_pull(fake.api(), REPO, 1)
    assert fake.transitions == [True]
    assert result.lifecycle["reasons"] == [f"Missing required CI: {PATH}"]


def test_excluded_scope_skips_lifecycle_even_with_conflicts():
    fake = LifecycleAPI()
    fake.pull["mergeable"] = False
    fake.config["a38"]["default"] = "exclude"
    fake.set_pr_guard_config(fake.config)
    result = reconcile_pull(fake.api(), REPO, 1)
    assert result.status == "not_applicable"
    assert result.scope_decision == "exclude"
    assert fake.transitions == []
    assert result.lifecycle == {}
    assert not [c for c in fake.comments if c["body"].startswith(STATE_MARKER)]


def test_excluded_target_with_in_progress_required_ci_skips_lifecycle():
    fake = LifecycleAPI()
    fake.runs[0].update(status="in_progress", conclusion=None)
    fake.config["a38"]["default"] = "exclude"
    fake.set_pr_guard_config(fake.config)
    result = reconcile_pull(fake.api(), REPO, 1)
    assert result.status == "not_applicable"
    assert result.scope_decision == "exclude"
    assert fake.transitions == []
    assert result.lifecycle == {}
    assert not [c for c in fake.comments if c["body"].startswith(STATE_MARKER)]


def test_main_target_without_config_skips_lifecycle_even_with_red_ci():
    fake = LifecycleAPI()
    fake.pull["base"]["ref"] = "main"
    fake.set_pr_guard_config(None)
    fake.runs[0].update(status="in_progress", conclusion=None)
    fake.pull["mergeable"] = False
    result = reconcile_pull(fake.api(), REPO, 1)
    assert result.status == "not_applicable"
    assert result.scope_decision == "exclude"
    assert fake.transitions == []
    assert result.lifecycle == {}
    assert not [c for c in fake.comments if c["body"].startswith(STATE_MARKER)]
    assert fake.pull["draft"] is False


def test_main_target_with_enforce_list_and_lifecycle_skips_red_ci():
    fake = LifecycleAPI()
    fake.pull["base"]["ref"] = "main"
    fake.config["a38"]["enforce"] = ["main"]
    fake.set_pr_guard_config(fake.config)
    fake.runs[0].update(status="in_progress", conclusion=None)
    fake.pull["mergeable"] = False
    result = reconcile_pull(fake.api(), REPO, 1)
    assert result.status == "not_applicable"
    assert result.scope_decision == "exclude"
    assert fake.transitions == []
    assert result.lifecycle == {}
    assert not [c for c in fake.comments if c["body"].startswith(STATE_MARKER)]
    assert fake.pull["draft"] is False


def test_unknown_mergeability_never_promotes_or_invents_conflicts():
    fake = LifecycleAPI()
    fake.pull["mergeable"] = None
    fake.own_authorization()
    reconcile_pull(fake.api(), REPO, 1)
    fake.pull["draft"] = True
    reconcile_pull(fake.api(), REPO, 1)
    assert fake.transitions == []


def test_approval_then_pending_then_green_promotes_once_without_rerunning():
    fake = LifecycleAPI()
    fake.runs[0]["conclusion"] = "action_required"
    first = reconcile_pull(fake.api(), REPO, 1)
    assert first.workflow_approvals[0]["status"] == "approved"
    assert fake.posts == [101] and fake.transitions == [True]
    assert len([c for c in fake.comments if c["body"].startswith(AUTH_MARKER)]) == 1
    reconcile_pull(fake.api(), REPO, 1)
    assert fake.transitions == [True]
    fake.runs[0].update(status="completed", conclusion="success")
    assert reconcile_pull(fake.api(), REPO, 1).lifecycle["action"] == "ready"
    reconcile_pull(fake.api(), REPO, 1)
    assert fake.posts == [101] and fake.transitions == [True, False]


@pytest.mark.parametrize("case", ["missing", "forged", "head", "base", "run", "report", "disabled"])
def test_green_alone_does_not_authorize_auto_ready(case):
    fake = LifecycleAPI()
    fake.pull["draft"] = True
    if case != "missing":
        fake.own_authorization()
    if case == "forged":
        fake.comments[-1]["user"]["id"] = 77
    elif case in {"head", "base"}:
        fake.comments[-1]["body"] = fake.comments[-1]["body"].replace(HEAD if case == "head" else BASE, BASE2)
    elif case == "run":
        fake.runs[0]["id"] = 102
    elif case == "report":
        fake.comments = fake.comments[1:]
    elif case == "disabled":
        fake.config["lifecycle"]["auto_ready"] = False
        fake.set_pr_guard_config(fake.config)
    reconcile_pull(fake.api(), REPO, 1)
    assert fake.transitions == []


def test_guard_itself_and_superseded_failures_do_not_block():
    fake = LifecycleAPI()
    fake.pull["draft"] = True
    fake.own_authorization()
    fake.runs.append(fake.run(id=99, check_suite_id=199, conclusion="failure", created_at="2026-09-04T00:00:00Z"))
    fake.runs.append(fake.run(id=102, path=GUARD, check_suite_id=202, event="pull_request_target", status="in_progress", conclusion=None))
    fake.checks = [dict(id=10, name="old test", check_suite={"id": 199}, status="completed", conclusion="failure"),
                   dict(id=11, name="guard", check_suite={"id": 202}, status="in_progress", conclusion=None)]
    reconcile_pull(fake.api(), REPO, 1)
    assert fake.transitions == [False]


@pytest.mark.parametrize("status,conclusion", [("in_progress", None), ("completed", "failure")])
def test_independent_check_blocks_ready(status, conclusion):
    fake = LifecycleAPI()
    fake.checks = [dict(id=11, name="security", check_suite={"id": 300}, status=status, conclusion=conclusion)]
    reconcile_pull(fake.api(), REPO, 1)
    assert fake.transitions == [True]


def test_commit_status_pending_blocks():
    fake = LifecycleAPI()
    fake.statuses.append({"sha": HEAD, "context": "external", "state": "pending"})
    reconcile_pull(fake.api(), REPO, 1)
    assert fake.transitions == [True]


@pytest.mark.parametrize("labels,base,expected", [([], "develop", False), (["full"], "develop", True), ([], "release", True)])
def test_conditional_required_workflow_uses_repo_branch_or_label(labels, base, expected):
    from agent_cli.pr_lifecycle import ci_state
    from agent_cli.a38_guard import assess_pull
    fake = LifecycleAPI()
    fake.config["lifecycle"]["conditional_workflows"] = [{"workflow": ".github/workflows/security.yml", "base_branches": ["release"], "labels_any": ["full"]}]
    assessment = assess_pull(fake.api(), REPO, 1)
    assessment.base_ref = base
    reasons, _ = ci_state(fake.api(), assessment, fake.config["lifecycle"], {"labels": [{"name": n} for n in labels]})
    assert any("Missing required CI" in r for r in reasons) is expected


@pytest.mark.parametrize("conclusion", [None, "skipped", "failure", "success"])
def test_successful_workflow_cannot_hide_a_missing_or_skipped_required_test(conclusion):
    fake = LifecycleAPI()
    fake.config["lifecycle"]["required_checks"] = {PATH: ["Test"]}
    fake.set_pr_guard_config(fake.config)
    if conclusion:
        fake.checks = [{"id": 22, "name": "Test", "check_suite": {"id": 201}, "status": "completed", "conclusion": conclusion}]
    reconcile_pull(fake.api(), REPO, 1)
    assert fake.transitions == ([] if conclusion == "success" else [True])


@pytest.mark.parametrize(
    "check_name,required,expect",
    [
        ("Full-stack E2E", "Full-stack E2E", True),
        ("Full-stack E2E / Full-stack E2E", "Full-stack E2E", True),
        ("Full-stack E2E / smoke", "Full-stack E2E", True),
        ("Testing", "Test", False),
        ("Test extra", "Test", False),
        (None, "Test", False),
        ("Test", "", False),
    ],
)
def test_required_check_matches_reusable_workflow_names(check_name, required, expect):
    from agent_cli.pr_lifecycle import required_check_matches
    assert required_check_matches(check_name, required) is expect


def test_reusable_workflow_required_check_name_allows_auto_ready():
    fake = LifecycleAPI()
    fake.pull["draft"] = True
    fake.own_authorization()
    fake.config["lifecycle"]["required_checks"] = {PATH: ["Full-stack E2E"]}
    fake.set_pr_guard_config(fake.config)
    fake.checks = [
        {
            "id": 22,
            "name": "Full-stack E2E / Full-stack E2E",
            "check_suite": {"id": 201},
            "status": "completed",
            "conclusion": "success",
        }
    ]
    reconcile_pull(fake.api(), REPO, 1)
    assert fake.transitions == [False]


def test_readme_only_accepts_skipped_required_test_for_auto_ready():
    fake = LifecycleAPI()
    fake.pull["draft"] = True
    fake.own_authorization()
    fake.config["lifecycle"]["required_checks"] = {PATH: ["Test"]}
    fake.set_pr_guard_config(fake.config)
    fake.checks = [
        {
            "id": 22,
            "name": "Test",
            "check_suite": {"id": 201},
            "status": "completed",
            "conclusion": "skipped",
        }
    ]
    fake.pull_files = [{"filename": "README.md", "status": "modified"}]
    reconcile_pull(fake.api(), REPO, 1)
    assert fake.transitions == [False]


def test_markdown_only_accepts_skipped_required_test_for_auto_ready():
    fake = LifecycleAPI()
    fake.pull["draft"] = True
    fake.own_authorization()
    fake.config["lifecycle"]["required_checks"] = {PATH: ["Test"]}
    fake.set_pr_guard_config(fake.config)
    fake.checks = [
        {
            "id": 22,
            "name": "Test",
            "check_suite": {"id": 201},
            "status": "completed",
            "conclusion": "skipped",
        }
    ]
    fake.pull_files = [{"filename": "docs/guide.md", "status": "modified"}]
    reconcile_pull(fake.api(), REPO, 1)
    assert fake.transitions == [False]


def test_dry_run_is_read_only():
    fake = LifecycleAPI()
    fake.runs.clear()
    result = reconcile_pull(fake.api(), REPO, 1, dry_run=True)
    assert result.lifecycle["action"] == "draft"
    assert not fake.transitions and not fake.writes


def test_graphql_error_is_not_a_successful_transition():
    fake = LifecycleAPI()
    fake.runs.clear()
    fake.graphql_error = True
    with pytest.raises(GuardError, match="mutation failed"):
        reconcile_pull(fake.api(), REPO, 1)
    assert not fake.transitions
    assert all('"phase": "applied"' not in c["body"] for c in fake.comments)


def test_changed_head_during_ready_is_restored_to_draft():
    fake = LifecycleAPI()
    fake.pull["draft"] = True
    fake.own_authorization()
    fake.mutate_during_transition = True
    with pytest.raises(GuardError, match="restored Draft"):
        reconcile_pull(fake.api(), REPO, 1)
    assert fake.transitions == [False, True]


def test_comment_failure_is_repaired_without_repeating_transition():
    fake = LifecycleAPI()
    fake.runs.clear()
    fake.fail_comment_once = True
    with pytest.raises(GuardError, match="comment HTTP 503"):
        reconcile_pull(fake.api(), REPO, 1)
    assert fake.transitions == [True]
    reconcile_pull(fake.api(), REPO, 1)
    assert fake.transitions == [True]
    comments = [c for c in fake.comments if c["body"].startswith(STATE_MARKER)]
    assert len(comments) == 1 and '"phase": "applied"' in comments[0]["body"]


@pytest.mark.parametrize("change", [
    {"enabled": "true"}, {"auto_ready": 1}, {"unknown": True},
    {"required_workflows": []}, {"required_workflows": [PATH, PATH]},
    {"required_workflows": [GUARD]}, {"ignored_workflows": ["*.yml"]},
])
def test_lifecycle_config_is_strict(change):
    fake = LifecycleAPI()
    fake.config["lifecycle"].update(change)
    with pytest.raises(PrGuardConfigError):
        load_pr_guard_config(json.dumps(fake.config))


def test_no_write_ready_with_in_progress_ci_still_drafts():
    fake = LifecycleAPI()
    fake.runs[0].update(status="in_progress", conclusion=None)
    result = reconcile_pull(fake.api(), REPO, 1)
    assert result.lifecycle["action"] == "draft"
    assert fake.transitions == [True]
    assert fake.pull["draft"]


@pytest.mark.parametrize("status,conclusion", [
    ("in_progress", None),
    ("completed", "failure"),
])
def test_markdown_only_ready_does_not_hold_against_red_or_pending_ci(status, conclusion):
    fake = LifecycleAPI()
    fake.pull["draft"] = False
    fake.pull_files = [{"filename": "docs/guide.md", "status": "modified"}]
    fake.runs[0].update(status=status, conclusion=conclusion)
    result = reconcile_pull(fake.api(), REPO, 1)
    assert result.write_ready
    assert result.write_ready_reason == "markdown-only change set"
    assert result.lifecycle["action"] == "draft"
    assert fake.transitions == [True]
    assert fake.pull["draft"]


@pytest.mark.parametrize("status,conclusion", [
    ("in_progress", None),
    ("completed", "failure"),
])
def test_write_author_ready_holds_against_red_or_pending_ci(status, conclusion):
    fake = LifecycleAPI()
    fake.comments.clear()
    fake.permissions["author"] = {
        "permission": "write",
        "user": {"id": AUTHOR_ID, "login": "author", "type": "User"},
    }
    fake.runs[0].update(status=status, conclusion=conclusion)
    result = reconcile_pull(fake.api(), REPO, 1)
    assert result.ok and result.status == "pass"
    assert result.write_ready
    assert result.lifecycle["action"] == "unchanged"
    assert result.lifecycle["reasons"]
    assert fake.transitions == []
    assert not fake.pull["draft"]


def test_auto_draft_then_timeline_restores_write_ready():
    fake = LifecycleAPI()
    fake.comments.clear()
    fake.runs[0].update(status="in_progress", conclusion=None)
    first = reconcile_pull(fake.api(), REPO, 1)
    assert first.lifecycle["action"] == "draft"
    assert fake.pull["draft"]
    fake.timeline = [
        {
            "event": "ready_for_review",
            "id": 2,
            "created_at": "2026-09-05T12:00:00Z",
            "actor": {"id": 3003, "login": "maintainer", "type": "User"},
        }
    ]
    fake.permissions["maintainer"] = {
        "permission": "admin",
        "user": {"id": 3003, "login": "maintainer", "type": "User"},
    }
    second = reconcile_pull(fake.api(), REPO, 1)
    assert second.ok and second.write_ready_reason == "ready by write collaborator"
    assert second.lifecycle["action"] == "ready"
    assert fake.transitions[-1] is False
    assert not fake.pull["draft"]
    bodies = [c["body"] for c in fake.comments]
    assert any("write collaborator marked Ready" in b for b in bodies)


def test_human_draft_with_ready_timeline_does_not_restore():
    fake = LifecycleAPI()
    fake.comments.clear()
    fake.pull["draft"] = True
    fake.timeline = [
        {
            "event": "ready_for_review",
            "id": 2,
            "created_at": "2026-09-05T12:00:00Z",
            "actor": {"id": 3003, "login": "maintainer", "type": "User"},
        }
    ]
    fake.permissions["maintainer"] = {
        "permission": "admin",
        "user": {"id": 3003, "login": "maintainer", "type": "User"},
    }
    fake.runs[0].update(status="in_progress", conclusion=None)
    result = reconcile_pull(fake.api(), REPO, 1)
    assert result.write_ready
    assert result.lifecycle["action"] == "unchanged"
    assert fake.transitions == []
    assert fake.pull["draft"]


def test_hold_clears_draft_record_so_later_human_draft_does_not_restore():
    fake = LifecycleAPI()
    fake.comments.clear()
    fake.runs[0].update(status="in_progress", conclusion=None)
    first = reconcile_pull(fake.api(), REPO, 1)
    assert first.lifecycle["action"] == "draft"
    assert fake.pull["draft"]
    fake.pull["draft"] = False
    fake.timeline = [
        {
            "event": "ready_for_review",
            "id": 2,
            "created_at": "2026-09-05T12:00:00Z",
            "actor": {"id": 3003, "login": "maintainer", "type": "User"},
        }
    ]
    fake.permissions["maintainer"] = {
        "permission": "admin",
        "user": {"id": 3003, "login": "maintainer", "type": "User"},
    }
    second = reconcile_pull(fake.api(), REPO, 1)
    assert second.lifecycle["action"] == "unchanged"
    assert not fake.pull["draft"]
    fake.pull["draft"] = True
    third = reconcile_pull(fake.api(), REPO, 1)
    assert third.write_ready
    assert third.lifecycle["action"] == "unchanged"
    assert fake.pull["draft"]
