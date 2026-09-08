"""Real guard reconciliation with an in-memory GitHub transport."""
import copy
import json
from urllib.parse import urlparse, parse_qs

import pytest

from agent_cli.a38_guard import GuardError, reconcile_pull
from agent_cli.pr_guard_config import load_pr_guard_config, PrGuardConfigError
from test_a38_guard import FakeAPI, HEAD, BASE, BASE2, DEFAULT_TIP, REPO, _report_comment, _pr_guard_config

pytestmark = pytest.mark.no_pg
PATH = ".github/workflows/test.yml"
FORK = "author/public-app"


class ApprovalAPI(FakeAPI):
    def __init__(self):
        super().__init__()
        self.pull["head"]["repo"]["full_name"] = FORK
        self.pull["head"]["ref"] = "feature"
        self.pull["created_at"] = "2026-09-01T00:00:00Z"
        self.config = _pr_guard_config()
        self.config["workflow_approval"] = {"enabled": True, "workflows": [PATH]}
        self.set_pr_guard_config(self.config)
        self.add_author_report(_report_comment(), updated_at="2026-09-05T12:00:00Z", cid=21)
        self.runs = [self.run()]
        self.posts = []
        self.cancels = []
        self.actions_gets = []
        self.post_status = 201
        self.before_run_read = None
        self.extra_pulls = []
        self.events = []
        self.comparison = "ahead"
        self.inventory = None

    @staticmethod
    def run(**changes):
        data = dict(id=101, path=PATH, workflow_id=10, event="pull_request", head_sha=HEAD,
                    head_branch="feature", repository={"full_name": REPO},
                    head_repository={"full_name": FORK}, pull_requests=[],
                    status="completed", conclusion="action_required", run_attempt=1,
                    created_at="2026-09-05T11:00:00Z")
        data.update(changes)
        return data

    def request_fn(self, method, url, body=None):
        path = urlparse(url).path
        root = f"/repos/{REPO}"
        if method == "GET" and path == root + "/actions/runs":
            self.actions_gets.append(path)
            data = self.inventory or {"total_count": len(self.runs), "workflow_runs": self.runs}
            return 200, copy.deepcopy(data), {}
        if path.startswith(root + "/actions/runs/"):
            ident = int(path.split("/actions/runs/")[1].split("/")[0])
            run = next(r for r in self.runs if r["id"] == ident)
            if method == "POST":
                if path.endswith("/cancel"):
                    self.cancels.append(ident)
                    run.update(status="completed", conclusion="cancelled")
                    return 202, {}, {}
                assert path.endswith("/approve"), "no rerun/dispatch endpoint allowed"
                self.posts.append(ident)
                if self.post_status == 201:
                    run.update(status="queued", conclusion=None)
                return self.post_status, {}, {}
            if self.before_run_read:
                callback, self.before_run_read = self.before_run_read, None
                callback(self)
            return 200, copy.deepcopy(run), {}
        if method == "GET" and path == root + "/pulls":
            return 200, copy.deepcopy([self.pull, *self.extra_pulls]), {}
        if method == "GET" and path.startswith(root + "/compare/"):
            return 200, {"status": self.comparison}, {}
        if method == "GET" and path == root + "/issues/1/events":
            return 200, copy.deepcopy(self.events), {}
        status, data, headers = super().request_fn(method, url, body)
        return status, copy.deepcopy(data), headers


def test_real_reconcile_approves_initial_private_fork_run_and_is_idempotent():
    fake = ApprovalAPI()
    result = reconcile_pull(fake.api(), REPO, 1)
    assert result.ok
    assert result.workflow_approvals == [{"run_id": 101, "workflow": PATH, "head": HEAD, "status": "approved"}]
    assert "workflow:approve:101" in result.writes
    assert fake.posts == [101]
    assert reconcile_pull(fake.api(), REPO, 1).workflow_approvals == []
    assert fake.posts == [101]


def test_dry_run_previews_without_any_writes():
    fake = ApprovalAPI()
    result = reconcile_pull(fake.api(), REPO, 1, dry_run=True)
    assert result.workflow_approvals[0]["status"] == "planned"
    assert not fake.posts and not fake.writes


@pytest.mark.parametrize("case", ["missing", "disabled", "report", "same_repo", "closed", "observe", "exclude"])
def test_no_approval_or_actions_inventory_without_authorization(case):
    fake = ApprovalAPI()
    if case == "missing":
        fake.set_pr_guard_config(None)
    elif case == "disabled":
        fake.config["workflow_approval"]["enabled"] = False
        fake.set_pr_guard_config(fake.config)
    elif case == "report":
        fake.comments.clear()
    elif case == "same_repo":
        fake.pull["head"]["repo"]["full_name"] = REPO
    elif case == "closed":
        fake.pull["state"] = "closed"
    elif case == "observe":
        policy = json.loads(fake.files[(BASE, ".github/a38.json")])
        policy["mode"] = "observe"
        fake.files[(BASE, ".github/a38.json")] = json.dumps(policy).encode()
    else:
        fake.config["a38"]["default"] = "exclude"
        fake.set_pr_guard_config(fake.config)
    result = reconcile_pull(fake.api(), REPO, 1)
    assert result.workflow_approvals == []
    assert not fake.posts and not fake.actions_gets


@pytest.mark.parametrize("changes", [
    {"status": "queued", "conclusion": None}, {"conclusion": "success"}, {"conclusion": "failure"},
    {"run_attempt": 2}, {"event": "push"}, {"head_sha": BASE}, {"head_branch": "other"},
    {"repository": {"full_name": "other/repo"}}, {"head_repository": {"full_name": "other/fork"}},
    {"path": ".github/workflows/unknown.yml"},
])
def test_ineligible_runs_are_never_approved(changes):
    fake = ApprovalAPI()
    fake.runs = [fake.run(**changes)]
    reconcile_pull(fake.api(), REPO, 1)
    assert not fake.posts
    if changes.get("path") == ".github/workflows/unknown.yml":
        assert fake.cancels == [101]
    else:
        assert not fake.cancels


@pytest.mark.parametrize("changes", [{"conclusion": "success"}, {"conclusion": "failure"},
                                     {"status": "queued", "conclusion": None}, {"run_attempt": 2}])
def test_newer_run_suppresses_old_blocked_run_even_when_not_successful(changes):
    fake = ApprovalAPI()
    fake.runs.append(fake.run(id=102, created_at="2026-09-05T11:01:00Z", **changes))
    reconcile_pull(fake.api(), REPO, 1)
    assert not fake.posts
    assert fake.cancels == [101]


def test_permission_denial_fails_and_is_not_retried():
    fake = ApprovalAPI()
    fake.post_status = 403
    with pytest.raises(GuardError, match="Actions write"):
        reconcile_pull(fake.api(), REPO, 1)
    assert fake.posts == [101]
    assert fake.statuses[0]["state"] == "error"


@pytest.mark.parametrize("what", ["head", "base", "config", "report"])
def test_change_before_write_rejects_stale_authorization(what):
    fake = ApprovalAPI()
    def mutate(f):
        if what == "head":
            f.pull["head"]["sha"] = BASE2
        elif what == "base":
            f.pull["base"]["sha"] = BASE2
        elif what == "config":
            f.ref_commits["develop"] = BASE2
        else:
            f.comments[0]["body"] += "\nChanged author evidence"
    fake.before_run_read = mutate
    with pytest.raises(GuardError):
        reconcile_pull(fake.api(), REPO, 1)
    assert not fake.posts


@pytest.mark.parametrize("what", ["ambiguous", "not_rebased", "retargeted", "reopened", "older", "foreign_link"])
def test_private_fork_association_must_be_proven(what):
    fake = ApprovalAPI()
    if what == "ambiguous":
        other = copy.deepcopy(fake.pull)
        other["number"] = 2
        fake.extra_pulls = [other]
    elif what == "not_rebased":
        fake.comparison = "diverged"
    elif what in {"retargeted", "reopened"}:
        fake.events = [{"event": "base_ref_changed" if what == "retargeted" else "reopened",
                        "created_at": "2026-09-05T11:30:00Z"}]
    elif what == "older":
        fake.runs[0]["created_at"] = "2026-08-01T00:00:00Z"
    else:
        fake.runs[0]["pull_requests"] = [{"number": 2, "head": {"sha": HEAD}, "base": {"sha": BASE}}]
    with pytest.raises(GuardError):
        reconcile_pull(fake.api(), REPO, 1)
    assert not fake.posts


@pytest.mark.parametrize("inventory", [{"total_count": 1000, "workflow_runs": []},
    {"total_count": 2, "workflow_runs": []}, {"total_count": True, "workflow_runs": []},
    {"total_count": 1, "workflow_runs": [{}]},
    {"total_count": 2, "workflow_runs": [ApprovalAPI.run(), ApprovalAPI.run()]}])
def test_partial_or_malformed_inventory_cannot_authorize(inventory):
    fake = ApprovalAPI()
    fake.inventory = inventory
    with pytest.raises(GuardError):
        reconcile_pull(fake.api(), REPO, 1)
    assert not fake.posts


@pytest.mark.parametrize("approval", [None, {}, {"enabled": "true", "workflows": [PATH]},
    {"enabled": True, "workflows": []}, {"enabled": True, "workflows": [PATH, PATH]},
    {"enabled": True, "workflows": [".github/workflows/*.yml"]},
    {"enabled": True, "workflows": [".github/workflows/../x.yml"]},
    {"enabled": True, "workflows": [PATH], "override": True}])
def test_invalid_approval_config_fails_closed(approval):
    config = _pr_guard_config()
    config["workflow_approval"] = approval
    with pytest.raises(PrGuardConfigError):
        load_pr_guard_config(json.dumps(config))
