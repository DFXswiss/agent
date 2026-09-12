"""CI completion, ownership, conflicts and transitions through the real guard."""
import copy
import json
from urllib.parse import urlparse

import pytest

from agent_cli import pr_lifecycle
from agent_cli.a38_guard import GuardError, reconcile_pull
from agent_cli.pr_guard_config import load_pr_guard_config, PrGuardConfigError
from agent_cli.pr_lifecycle import AUTH_MARKER, STATE_MARKER, visible_transition_sentences
from test_a38_guard import AUTHOR_ID, HEAD, BASE, BASE2, BOT_ID, REPO, _report_comment
from test_workflow_approval_core import ApprovalAPI, PATH

pytestmark = pytest.mark.no_pg
GUARD = ".github/workflows/guard.yml"


def test_visible_transition_sentences_action_required_only():
    en, de = visible_transition_sentences({
        "state": "draft",
        "reasons": [f"CI not green: {PATH} (action_required)"],
    })
    assert en == (
        "This pull request is back in Draft because CI is waiting for approval."
    )
    assert de == (
        "Dieser Pull Request steht wieder auf Draft, weil die CI auf Freigabe wartet."
    )
    assert " or " not in en
    assert " oder " not in de
    assert "merge conflicts" not in en.lower()
    assert "Merge-Konflikte" not in de


def test_visible_transition_sentences_merge_conflicts_only():
    en, de = visible_transition_sentences({
        "state": "draft",
        "reasons": ["Merge conflicts"],
    })
    assert en == (
        "This pull request is back in Draft because merge conflicts exist."
    )
    assert de == (
        "Dieser Pull Request steht wieder auf Draft, weil Merge-Konflikte bestehen."
    )
    assert " or " not in en
    assert " oder " not in de
    assert "CI" not in en
    assert "CI" not in de


def test_visible_transition_sentences_conflicts_and_ci_failed():
    en, de = visible_transition_sentences({
        "state": "draft",
        "reasons": [
            "Merge conflicts",
            f"CI not green: {PATH} (failure)",
        ],
    })
    assert en == (
        "This pull request is back in Draft because merge conflicts exist and CI failed."
    )
    assert de == (
        "Dieser Pull Request steht wieder auf Draft, weil Merge-Konflikte bestehen "
        "und die CI fehlgeschlagen ist."
    )
    assert " or " not in en
    assert " oder " not in de


def test_visible_transition_sentences_backend_5498_shape():
    en, de = visible_transition_sentences({
        "state": "draft",
        "reasons": [
            "CI not green: .github/workflows/api-pr.yaml (action_required)",
            "CI not green: .github/workflows/codeql.yml (action_required)",
            "Required CI check not green: .github/workflows/api-pr.yaml / Test",
            "CI status not green: A38 / report (develop) (failure)",
        ],
    })
    assert en == (
        "This pull request is back in Draft because CI is waiting for approval "
        "and A38 is not green."
    )
    assert de == (
        "Dieser Pull Request steht wieder auf Draft, weil die CI auf Freigabe wartet "
        "und A38 nicht grün ist."
    )
    assert " or " not in en
    assert " oder " not in de
    assert "merge conflicts" not in en.lower()


def test_visible_transition_sentences_mixed_non_a38_failure_and_a38():
    en, de = visible_transition_sentences({
        "state": "draft",
        "reasons": [
            "CI not green: .github/workflows/api-pr.yaml (failure)",
            "CI status not green: A38 / report (develop) (failure)",
        ],
    })
    assert en == (
        "This pull request is back in Draft because CI failed and A38 is not green."
    )
    assert de == (
        "Dieser Pull Request steht wieder auf Draft, weil die CI fehlgeschlagen ist "
        "und A38 nicht grün ist."
    )
    assert " or " not in en
    assert " oder " not in de
    assert "merge conflicts" not in en.lower()
    assert "Merge-Konflikte" not in de


def test_visible_transition_sentences_cancelled_only():
    en, de = visible_transition_sentences({
        "state": "draft",
        "reasons": [f"CI not green: {PATH} (cancelled)"],
    })
    assert en == (
        "This pull request is back in Draft because CI was cancelled."
    )
    assert de == (
        "Dieser Pull Request steht wieder auf Draft, weil die CI abgebrochen wurde."
    )
    assert " or " not in en
    assert " oder " not in de
    assert "failed" not in en
    assert "merge conflicts" not in en.lower()
    assert "Merge-Konflikte" not in de


def test_visible_transition_sentences_false_positive_a38_name():
    en, de = visible_transition_sentences({
        "state": "draft",
        "reasons": ["CI not green: .github/workflows/A38-compat.yml (failure)"],
    })
    assert en == (
        "This pull request is back in Draft because CI failed."
    )
    assert de == (
        "Dieser Pull Request steht wieder auf Draft, weil die CI fehlgeschlagen ist."
    )
    assert "A38 is not green" not in en
    assert "A38 nicht grün" not in de
    assert " or " not in en
    assert " oder " not in de
    assert "merge conflicts" not in en.lower()
    assert "Merge-Konflikte" not in de


def test_visible_transition_sentences_restore_author_write_action_required():
    en, de = visible_transition_sentences(
        {
            "state": "ready",
            "reasons": [f"CI not green: {PATH} (action_required)"],
        },
        write_ready_reason="author has write",
    )
    assert en == (
        "The author has write; this pull request is ready for review "
        "even though CI is waiting for approval."
    )
    assert de == (
        "Der Autor hat Write; dieser Pull Request ist bereit zum Review, "
        "auch wenn die CI auf Freigabe wartet."
    )
    assert " or " not in en
    assert " oder " not in de
    assert "or merge conflicts" not in en
    assert "Merge-Konflikte" not in de


def test_visible_transition_sentences_green_ready_unchanged():
    en, de = visible_transition_sentences({"state": "ready", "reasons": []})
    assert en == (
        "The authorized CI runs are green and no merge conflicts exist; "
        "this pull request is ready for review."
    )
    assert de == (
        "Die freigegebenen CI-Läufe sind grün und es gibt keine Merge-Konflikte; "
        "dieser Pull Request ist bereit zum Review."
    )


def test_visible_transition_sentences_queued_ci():
    en, de = visible_transition_sentences({
        "state": "draft",
        "reasons": [f"CI not green: {PATH} (queued)"],
    })
    assert en == (
        "This pull request is back in Draft because CI is still running."
    )
    assert de == (
        "Dieser Pull Request steht wieder auf Draft, weil die CI noch läuft."
    )
    assert " or " not in en
    assert " oder " not in de


def test_visible_transition_sentences_missing_required_ci():
    en, de = visible_transition_sentences({
        "state": "draft",
        "reasons": [f"Missing required CI: {PATH}"],
    })
    assert en == (
        "This pull request is back in Draft because required CI is missing."
    )
    assert de == (
        "Dieser Pull Request steht wieder auf Draft, weil erforderliche CI fehlt."
    )
    assert " or " not in en
    assert " oder " not in de


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
        self.graphql_noop_draft = False
        self.graphql_ready_without_rest = False
        self.graphql_noop_ready = False
        self.graphql_noop_ready_null = False
        self.graphql_mergeable = "MERGEABLE"
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
            query = payload["query"]
            if (
                "mergeable" in query
                and "convertPullRequestToDraft" not in query
                and "markPullRequestReadyForReview" not in query
            ):
                return 200, {"data": {"node": {"mergeable": self.graphql_mergeable}}}, {}
            operation = "convertPullRequestToDraft" if "convertPullRequestToDraft" in query else "markPullRequestReadyForReview"
            if self.graphql_error:
                return 200, {"errors": [{"message": "denied"}]}, {}
            if self.graphql_noop_draft and operation == "convertPullRequestToDraft":
                return 200, {"data": {operation: {"pullRequest": {
                    "id": "PR_example", "isDraft": False,
                    "headRefOid": self.pull["head"]["sha"], "baseRefOid": BASE}}}}, {}
            if self.graphql_ready_without_rest and operation == "markPullRequestReadyForReview":
                return 200, {"data": {operation: {"pullRequest": {
                    "id": "PR_example", "isDraft": False,
                    "headRefOid": self.pull["head"]["sha"], "baseRefOid": BASE}}}}, {}
            if self.graphql_noop_ready and operation == "markPullRequestReadyForReview":
                return 200, {"data": {operation: {"pullRequest": {
                    "id": "PR_example", "isDraft": True,
                    "headRefOid": self.pull["head"]["sha"], "baseRefOid": BASE}}}}, {}
            if self.graphql_noop_ready_null and operation == "markPullRequestReadyForReview":
                return 200, {"data": {operation: {"pullRequest": {
                    "id": "PR_example", "isDraft": None,
                    "headRefOid": self.pull["head"]["sha"], "baseRefOid": BASE}}}}, {}
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


def test_draft_comment_names_action_required_without_or_merge_conflicts():
    fake = LifecycleAPI()
    fake.pull["head"]["repo"]["full_name"] = REPO
    fake.runs[0].update(status="completed", conclusion="action_required")
    reconcile_pull(fake.api(), REPO, 1)
    comments = [c for c in fake.comments if c["body"].startswith(STATE_MARKER)]
    assert len(comments) == 1
    body = comments[0]["body"]
    assert '"phase": "applied"' in body
    assert "CI is waiting for approval" in body
    assert "die CI auf Freigabe wartet" in body
    assert "or merge conflicts" not in body
    assert "oder Merge-Konflikte" not in body


def test_draft_comment_names_merge_conflicts_only():
    fake = LifecycleAPI()
    fake.pull["mergeable"] = False
    reconcile_pull(fake.api(), REPO, 1)
    assert fake.pull["draft"] and fake.transitions == [True]
    comments = [c for c in fake.comments if c["body"].startswith(STATE_MARKER)]
    assert len(comments) == 1
    body = comments[0]["body"]
    assert "This pull request is back in Draft because merge conflicts exist." in body
    assert "Dieser Pull Request steht wieder auf Draft, weil Merge-Konflikte bestehen." in body
    en = body.split("EN:\n", 1)[1].split("\n\nDE:\n", 1)[0]
    de = body.split("DE:\n", 1)[1].split("\n\n<details>", 1)[0]
    assert "CI" not in en and " or " not in en
    assert "CI" not in de and " oder " not in de


def test_draft_comment_joins_conflicts_and_failed_ci_with_and():
    fake = LifecycleAPI()
    fake.pull["mergeable"] = False
    fake.runs[0].update(status="completed", conclusion="failure")
    reconcile_pull(fake.api(), REPO, 1)
    assert fake.pull["draft"] and fake.transitions == [True]
    comments = [c for c in fake.comments if c["body"].startswith(STATE_MARKER)]
    assert len(comments) == 1
    body = comments[0]["body"]
    assert (
        "This pull request is back in Draft because merge conflicts exist and CI failed."
        in body
    )
    assert (
        "Dieser Pull Request steht wieder auf Draft, weil Merge-Konflikte bestehen "
        "und die CI fehlgeschlagen ist."
    ) in body
    en = body.split("EN:\n", 1)[1].split("\n\nDE:\n", 1)[0]
    de = body.split("DE:\n", 1)[1].split("\n\n<details>", 1)[0]
    assert " or " not in en
    assert " oder " not in de


def test_draft_comment_names_a38_when_author_report_missing():
    fake = LifecycleAPI()
    fake.comments.clear()
    reconcile_pull(fake.api(), REPO, 1)
    assert fake.pull["draft"] and fake.transitions == [True]
    comments = [c for c in fake.comments if c["body"].startswith(STATE_MARKER)]
    assert len(comments) == 1
    body = comments[0]["body"]
    assert "This pull request is back in Draft because A38 is not green." in body
    assert "Dieser Pull Request steht wieder auf Draft, weil A38 nicht grün ist." in body
    assert "or merge conflicts" not in body
    assert "oder Merge-Konflikte" not in body


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


def test_ignored_workflow_left_in_auth_does_not_block_auto_ready():
    fake = LifecycleAPI()
    fake.pull["draft"] = True
    fake.own_authorization(runs=[
        {"run_id": 101, "workflow": PATH},
        {"run_id": 999, "workflow": GUARD},
    ])
    reconcile_pull(fake.api(), REPO, 1)
    assert fake.transitions == [False]


def test_auth_only_ignored_workflow_does_not_auto_ready():
    fake = LifecycleAPI()
    fake.pull["draft"] = True
    fake.own_authorization(runs=[{"run_id": 999, "workflow": GUARD}])
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


def test_reusable_prefix_cannot_hide_a_skipped_sibling_required_job():
    fake = LifecycleAPI()
    fake.config["lifecycle"]["required_checks"] = {PATH: ["Full-stack E2E"]}
    fake.set_pr_guard_config(fake.config)
    fake.checks = [
        {
            "id": 30,
            "name": "Full-stack E2E / setup",
            "check_suite": {"id": 201},
            "status": "completed",
            "conclusion": "success",
        },
        {
            "id": 22,
            "name": "Full-stack E2E / Full-stack E2E",
            "check_suite": {"id": 201},
            "status": "completed",
            "conclusion": "skipped",
        },
    ]
    reconcile_pull(fake.api(), REPO, 1)
    assert fake.transitions == [True]


def _e2e_named_policy_and_report(fake: LifecycleAPI) -> None:
    from test_a38_guard import BASE as POLICY_SHA
    from test_a38_guard import _policy
    policy = _policy()
    policy["jobs"][0]["name"] = "Full-stack E2E"
    fake.files[(POLICY_SHA, ".github/a38.json")] = json.dumps(policy).encode()
    fake.pull["base"]["repo"]["private"] = True
    fake.comments = [c for c in fake.comments if c.get("user", {}).get("id") != AUTHOR_ID]
    fake.add_author_report(
        _report_comment(
            private=True,
            extra_runs=[
                {
                    "id": "pytest",
                    "name": "Full-stack E2E",
                    "command": "pytest",
                    "result": "pass",
                    "exit_code": 0,
                    "duration_s": 1.0,
                    "timeout_s": 600,
                }
            ],
        ),
        updated_at="2026-09-05T12:00:00Z",
        cid=21,
    )


def test_skipped_github_e2e_is_ready_when_a38_e2e_passed():
    fake = LifecycleAPI()
    fake.pull["draft"] = True
    fake.own_authorization()
    fake.config["lifecycle"]["required_checks"] = {PATH: ["Full-stack E2E"]}
    fake.set_pr_guard_config(fake.config)
    _e2e_named_policy_and_report(fake)
    fake.checks = [
        {
            "id": 22,
            "name": "Full-stack E2E / Full-stack E2E",
            "check_suite": {"id": 201},
            "status": "completed",
            "conclusion": "skipped",
        }
    ]
    reconcile_pull(fake.api(), REPO, 1)
    assert fake.transitions == [False]


def test_failed_github_e2e_still_blocks_when_a38_e2e_passed():
    fake = LifecycleAPI()
    fake.config["lifecycle"]["required_checks"] = {PATH: ["Full-stack E2E"]}
    fake.set_pr_guard_config(fake.config)
    _e2e_named_policy_and_report(fake)
    fake.checks = [
        {
            "id": 22,
            "name": "Full-stack E2E / Full-stack E2E",
            "check_suite": {"id": 201},
            "status": "completed",
            "conclusion": "failure",
        }
    ]
    reconcile_pull(fake.api(), REPO, 1)
    assert fake.transitions == [True]


def test_pytest_only_a38_does_not_waive_skipped_e2e():
    fake = LifecycleAPI()
    fake.pull["draft"] = True
    fake.pull["base"]["repo"]["private"] = True
    fake.own_authorization()
    fake.config["lifecycle"]["required_checks"] = {PATH: ["Full-stack E2E"]}
    fake.set_pr_guard_config(fake.config)
    fake.comments = [c for c in fake.comments if c.get("user", {}).get("id") != AUTHOR_ID]
    fake.add_author_report(
        _report_comment(private=True),
        updated_at="2026-09-05T12:00:00Z",
        cid=21,
    )
    fake.checks = [
        {
            "id": 22,
            "name": "Full-stack E2E / Full-stack E2E",
            "check_suite": {"id": 201},
            "status": "completed",
            "conclusion": "skipped",
        }
    ]
    reconcile_pull(fake.api(), REPO, 1)
    assert fake.transitions == []


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
    with pytest.raises(GuardError, match="denied"):
        reconcile_pull(fake.api(), REPO, 1)
    assert not fake.transitions
    assert all('"phase": "applied"' not in c["body"] for c in fake.comments)


def test_ready_mutation_http_200_with_unchanged_draft_fails_closed():
    fake = LifecycleAPI()
    fake.pull["draft"] = True
    fake.own_authorization()
    fake.graphql_noop_ready = True
    with pytest.raises(GuardError, match="contents: write"):
        reconcile_pull(fake.api(), REPO, 1)
    assert not fake.transitions
    assert fake.pull["draft"] is True


def test_ready_mutation_non_boolean_is_draft_fails_closed():
    fake = LifecycleAPI()
    fake.pull["draft"] = True
    fake.own_authorization()
    fake.graphql_noop_ready_null = True
    with pytest.raises(GuardError, match=r"isDraft=None"):
        reconcile_pull(fake.api(), REPO, 1)
    assert not fake.transitions
    assert fake.pull["draft"] is True
    assert all('"phase": "applied"' not in c["body"] for c in fake.comments)


def test_graphql_error_still_approves_then_fails_closed():
    fake = LifecycleAPI()
    fake.runs[0].update(status="completed", conclusion="action_required")
    fake.graphql_error = True
    with pytest.raises(GuardError, match="mutation failed"):
        reconcile_pull(fake.api(), REPO, 1)
    assert fake.posts == [101]
    assert not fake.transitions
    states = [c for c in fake.comments if c["body"].startswith(STATE_MARKER)]
    assert states and '"phase": "planned"' in states[-1]["body"]
    assert '"phase": "applied"' not in states[-1]["body"]


def test_unchanged_draft_state_still_approves_waiting_fork_run():
    fake = LifecycleAPI()
    fake.runs[0].update(status="completed", conclusion="action_required")
    fake.graphql_noop_draft = True
    result = reconcile_pull(fake.api(), REPO, 1)
    assert result.workflow_approvals == [
        {"run_id": 101, "workflow": PATH, "head": HEAD, "status": "approved"}
    ]
    assert fake.posts == [101]
    assert result.lifecycle["action"] == "unchanged"
    assert not fake.pull["draft"] and not fake.transitions
    states = [c for c in fake.comments if c["body"].startswith(STATE_MARKER)]
    assert states and '"phase": "applied"' in states[-1]["body"]
    assert '"state": "ready"' in states[-1]["body"]
    assert '"phase": "planned"' not in states[-1]["body"]


def test_changed_head_during_ready_restore_noop_fails_closed():
    fake = LifecycleAPI()
    fake.pull["draft"] = True
    fake.own_authorization()
    fake.mutate_during_transition = True
    fake.graphql_noop_draft = True
    with pytest.raises(GuardError, match="Draft restore did not take effect"):
        reconcile_pull(fake.api(), REPO, 1)
    assert fake.transitions == [False]


def test_changed_head_during_ready_is_restored_to_draft():
    fake = LifecycleAPI()
    fake.pull["draft"] = True
    fake.own_authorization()
    fake.mutate_during_transition = True
    with pytest.raises(GuardError, match="restored Draft"):
        reconcile_pull(fake.api(), REPO, 1)
    assert fake.transitions == [False, True]


def test_graphql_ready_without_rest_draft_fails_closed():
    fake = LifecycleAPI()
    fake.pull["draft"] = True
    fake.own_authorization()
    fake.graphql_ready_without_rest = True
    with pytest.raises(GuardError, match=r"REST draft.*intended Ready/Draft state"):
        reconcile_pull(fake.api(), REPO, 1)
    assert fake.pull["draft"] is True
    assert all('"phase": "applied"' not in c["body"] for c in fake.comments)


def test_graphql_ready_with_non_bool_rest_draft_fails_closed():
    fake = LifecycleAPI()
    fake.pull["draft"] = True
    fake.own_authorization()
    fake.graphql_ready_without_rest = True
    original = fake.request_fn

    def request(method, url, body=None):
        status, data, headers = original(method, url, body)
        if method == "POST" and urlparse(url).path == "/graphql":
            fake.pull["draft"] = "ready"
        return status, data, headers

    fake.request_fn = request
    with pytest.raises(GuardError, match=r"REST draft.*intended Ready/Draft state"):
        reconcile_pull(fake.api(), REPO, 1)
    assert fake.pull["draft"] == "ready"
    assert all('"phase": "applied"' not in c["body"] for c in fake.comments)


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


def test_write_author_ready_conflicts_return_to_draft_and_do_not_restore():
    fake = LifecycleAPI()
    fake.comments.clear()
    fake.permissions["author"] = {
        "permission": "write",
        "user": {"id": AUTHOR_ID, "login": "author", "type": "User"},
    }
    fake.pull["mergeable"] = False
    result = reconcile_pull(fake.api(), REPO, 1)
    assert result.lifecycle["action"] == "draft"
    assert fake.transitions == [True]
    assert fake.pull["draft"]
    follow = reconcile_pull(fake.api(), REPO, 1)
    assert follow.lifecycle["action"] == "unchanged"
    assert fake.transitions == [True]
    assert fake.pull["draft"]


def test_write_author_ready_graphql_conflicting_returns_to_draft():
    fake = LifecycleAPI()
    fake.comments.clear()
    fake.permissions["author"] = {
        "permission": "write",
        "user": {"id": AUTHOR_ID, "login": "author", "type": "User"},
    }
    fake.pull["mergeable"] = None
    fake.graphql_mergeable = "CONFLICTING"
    result = reconcile_pull(fake.api(), REPO, 1)
    assert result.lifecycle["action"] == "draft"
    assert fake.transitions == [True]
    assert fake.pull["draft"]


def test_graphql_unknown_does_not_invent_conflicts_on_write_ready():
    fake = LifecycleAPI()
    fake.comments.clear()
    fake.permissions["author"] = {
        "permission": "write",
        "user": {"id": AUTHOR_ID, "login": "author", "type": "User"},
    }
    fake.pull["mergeable"] = None
    fake.graphql_mergeable = "UNKNOWN"
    result = reconcile_pull(fake.api(), REPO, 1)
    assert fake.transitions == []
    assert not fake.pull["draft"]
    assert "Merge conflicts" not in result.lifecycle["reasons"]


@pytest.mark.parametrize("status,conclusion", [
    ("in_progress", None),
    ("completed", "failure"),
    ("completed", "success"),
])
def test_write_hold_after_conflicts_disappear_on_reread_keeps_ready(status, conclusion, monkeypatch):
    fake = LifecycleAPI()
    fake.comments.clear()
    fake.permissions["author"] = {
        "permission": "write",
        "user": {"id": AUTHOR_ID, "login": "author", "type": "User"},
    }
    fake.runs[0].update(status=status, conclusion=conclusion)
    fake.pull["mergeable"] = False
    conflict_reads = []
    real = pr_lifecycle._has_merge_conflicts

    def wrapped(api, pull):
        found = real(api, pull)
        conflict_reads.append(found)
        if found:
            fake.pull["mergeable"] = True
        return found

    monkeypatch.setattr(pr_lifecycle, "_has_merge_conflicts", wrapped)
    result = reconcile_pull(fake.api(), REPO, 1)
    assert result.lifecycle["action"] == "unchanged"
    assert fake.transitions == []
    assert not fake.pull["draft"]
    assert conflict_reads == [True, False]
    states = [c for c in fake.comments if c["body"].startswith(STATE_MARKER)]
    assert states and '"phase": "applied"' in states[-1]["body"]
    assert '"state": "ready"' in states[-1]["body"]


@pytest.mark.parametrize("status,conclusion", [
    ("in_progress", None),
    ("completed", "failure"),
])
def test_write_author_ready_conflicts_with_red_ci_still_draft(status, conclusion):
    fake = LifecycleAPI()
    fake.comments.clear()
    fake.permissions["author"] = {
        "permission": "write",
        "user": {"id": AUTHOR_ID, "login": "author", "type": "User"},
    }
    fake.runs[0].update(status=status, conclusion=conclusion)
    fake.pull["mergeable"] = False
    result = reconcile_pull(fake.api(), REPO, 1)
    assert result.lifecycle["action"] == "draft"
    assert fake.transitions == [True]
    assert fake.pull["draft"]
    follow = reconcile_pull(fake.api(), REPO, 1)
    assert follow.lifecycle["action"] == "unchanged"
    assert fake.transitions == [True]
    assert fake.pull["draft"]


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
