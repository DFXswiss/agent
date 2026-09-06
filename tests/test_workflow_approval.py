"""Workflow-approval opt-in: config, latest run, fork association, writes."""

from __future__ import annotations

import copy
import json
from urllib.parse import parse_qs, urlparse

import pytest

from agent_cli import workflow_approval as wa
from agent_cli.a38_guard import (
    POLICY_APPROVAL_PREFIX,
    Assessment,
    GuardError,
    assess_pull,
    reconcile_pull,
)
from agent_cli.pr_guard_config import PrGuardConfigError, load_pr_guard_config
from test_a38_guard import (
    BASE,
    BASE2,
    HEAD,
    REPO,
    FakeAPI,
    _pr_guard_config,
    _report_comment,
)

pytestmark = pytest.mark.no_pg

PATH = ".github/workflows/test.yml"
OTHER = ".github/workflows/other.yaml"
FORK = "contributor/fork"
BRANCH = "feature"
MAINTAINER = 3030
A38_BODY = {
    "schema": "pr-guard/v1",
    "a38": {"enforce": [], "exclude": [], "default": "enforce"},
}


def _cfg(approval: dict) -> dict:
    payload = copy.deepcopy(A38_BODY)
    payload["workflow_approval"] = approval
    return payload


def _link(*, number: int = 1, head: str = HEAD, base: str = BASE) -> dict:
    return {"number": number, "head": {"sha": head}, "base": {"sha": base}}


class FakeApproval(FakeAPI):
    """FakeAPI plus Actions inventory, approve POST, fork association extras."""

    def __init__(self) -> None:
        super().__init__()
        self.pull["head"]["repo"]["full_name"] = FORK
        self.pull["head"]["ref"] = BRANCH
        self.pull["created_at"] = "2026-09-01T00:00:00Z"
        self.config = _cfg({"enabled": True, "workflows": [PATH]})
        self.set_pr_guard_config(self.config)
        self.add_author_report(_report_comment(), updated_at="2026-09-05T12:00:00Z", cid=21)
        self.runs = [self.run()]
        self.posts: list[int] = []
        self.actions_pages: list[int] = []
        self.compare_urls: list[str] = []
        self.event_reads = 0
        self.post_status = 201
        self.inventory = None
        self.on_inventory = None
        self.before_run_read = None
        self.extra_pulls: list[dict] = []
        self.list_head_sha: str | None = None
        self.events: list[dict] = []
        self.comparison = "ahead"
        self.run_override = None
        self.listed_pulls: list[dict] | None = None

    @staticmethod
    def run(**changes: object) -> dict:
        data: dict = {
            "id": 101,
            "path": PATH,
            "event": "pull_request",
            "head_sha": HEAD,
            "head_branch": BRANCH,
            "repository": {"full_name": REPO},
            "head_repository": {"full_name": FORK},
            "pull_requests": [],
            "status": "completed",
            "conclusion": "action_required",
            "run_attempt": 1,
            "created_at": "2026-09-05T11:00:00Z",
        }
        data.update(changes)
        return data

    def request_fn(self, method: str, url: str, body: bytes | None = None):
        path = urlparse(url).path
        root = f"/repos/{REPO}"
        if method == "GET" and path == f"{root}/actions/runs":
            page = int((parse_qs(urlparse(url).query).get("page") or ["1"])[0])
            per_page = int((parse_qs(urlparse(url).query).get("per_page") or ["100"])[0])
            self.actions_pages.append(page)
            if self.on_inventory is not None:
                self.on_inventory(self, page)
            if callable(self.inventory):
                return 200, copy.deepcopy(self.inventory(page)), {}
            if self.inventory is not None:
                return 200, copy.deepcopy(self.inventory), {}
            start = (page - 1) * per_page
            chunk = self.runs[start : start + per_page]
            return 200, {"total_count": len(self.runs), "workflow_runs": copy.deepcopy(chunk)}, {}
        if path.startswith(f"{root}/actions/runs/"):
            ident = int(path.split("/actions/runs/")[1].split("/")[0])
            run = next(r for r in self.runs if r["id"] == ident)
            if method == "POST":
                assert path.endswith("/approve")
                self.posts.append(ident)
                if self.post_status == 201:
                    run.update(status="queued", conclusion=None)
                return self.post_status, {}, {}
            if self.before_run_read is not None:
                callback, self.before_run_read = self.before_run_read, None
                callback(self)
            payload = self.run_override if self.run_override is not None else run
            return 200, copy.deepcopy(payload), {}
        if method == "GET" and path == f"{root}/pulls":
            if self.listed_pulls is not None:
                return 200, copy.deepcopy(self.listed_pulls), {}
            listed = copy.deepcopy(self.pull)
            if self.list_head_sha is not None:
                listed["head"]["sha"] = self.list_head_sha
            return 200, [listed, *copy.deepcopy(self.extra_pulls)], {}
        if method == "GET" and path.startswith(f"{root}/compare/"):
            self.compare_urls.append(path)
            return 200, {"status": self.comparison}, {}
        if method == "GET" and path == f"{root}/issues/1/events":
            self.event_reads += 1
            return 200, copy.deepcopy(self.events), {}
        status, data, headers = super().request_fn(method, url, body)
        return status, copy.deepcopy(data), headers


def _private_fork() -> FakeApproval:
    fake = FakeApproval()
    fake.pull["base"]["repo"]["private"] = True
    fake.comments.clear()
    fake.add_author_report(
        _report_comment(private=True), updated_at="2026-09-05T12:00:00Z", cid=21
    )
    return fake


# --- config -----------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        json.dumps(_cfg({"enabled": True, "workflows": [PATH], "retry": True})),
        json.dumps({**A38_BODY, "extra": 1, "workflow_approval": {"enabled": True, "workflows": [PATH]}}),
        json.dumps(_cfg({"enabled": "true", "workflows": [PATH]})),
        json.dumps(_cfg({"enabled": 1, "workflows": [PATH]})),
        json.dumps(_cfg({"enabled": True, "workflows": PATH})),
        json.dumps(_cfg({"enabled": True, "workflows": [1]})),
        json.dumps({**A38_BODY, "workflow_approval": [PATH]}),
        json.dumps(_cfg({"enabled": True, "workflows": []})),
        json.dumps(_cfg({"enabled": True, "workflows": [PATH, PATH]})),
        json.dumps(_cfg({"enabled": False, "workflows": [PATH, PATH]})),
        json.dumps(_cfg({"enabled": True, "workflows": [".github/workflows/*.yml"]})),
        json.dumps(_cfg({"enabled": True, "workflows": [".github/workflows/test-?.yml"]})),
        json.dumps(_cfg({"enabled": True, "workflows": [".github/workflows/ci[ab].yml"]})),
        json.dumps(_cfg({"enabled": True, "workflows": [".github/workflows/../x.yml"]})),
        json.dumps(_cfg({"enabled": True, "workflows": [".github/workflows/foo/bar.yml"]})),
        json.dumps(_cfg({"enabled": True, "workflows": [".github/workflows/foo/../../etc.yml"]})),
        json.dumps(_cfg({"enabled": True, "workflows": ["/etc/passwd.yml"]})),
        json.dumps(_cfg({"enabled": True, "workflows": [".github/workflows/x.yml\\y.yml"]})),
        json.dumps(_cfg({"enabled": True})),
        json.dumps(_cfg({"workflows": [PATH]})),
        json.dumps(_cfg({"enabled": True, "workflows": [PATH + "x" * 240]})),
        json.dumps(_cfg({"enabled": True, "workflows": [f".github/workflows/w{i}.yml" for i in range(65)]})),
        '{"schema":"pr-guard/v1","a38":{"enforce":[],"exclude":[],"default":"enforce"},'
        '"workflow_approval":{"enabled":true,"enabled":false,"workflows":[".github/workflows/test.yml"]}}',
    ],
    ids=[
        "unknown-approval-field",
        "unknown-top-field",
        "enabled-string",
        "enabled-int",
        "workflows-string",
        "workflows-int-item",
        "approval-array",
        "enabled-empty",
        "duplicate-paths",
        "duplicate-paths-disabled",
        "glob-star",
        "glob-question",
        "glob-brackets",
        "traversal-dotdot",
        "nested-dir",
        "traversal-nested",
        "absolute-path",
        "backslash",
        "missing-workflows",
        "missing-enabled",
        "path-too-long",
        "sixty-five-paths",
        "duplicate-json-key",
    ],
)
def test_invalid_workflow_approval_config_fails_closed(raw: str) -> None:
    with pytest.raises(PrGuardConfigError):
        load_pr_guard_config(raw)


def test_valid_strict_workflow_approval_config() -> None:
    long_name = "a" * (255 - len(".github/workflows/") - len(".yml"))
    long_path = f".github/workflows/{long_name}.yml"
    assert len(long_path) == 255
    cfg = load_pr_guard_config(
        json.dumps(
            _cfg(
                {
                    "enabled": True,
                    "workflows": [PATH, OTHER, long_path],
                }
            )
        )
    )
    assert cfg["workflow_approval"] == {
        "enabled": True,
        "workflows": [PATH, OTHER, long_path],
    }
    sixty_four = load_pr_guard_config(
        json.dumps(_cfg({"enabled": True, "workflows": [f".github/workflows/w{i}.yml" for i in range(64)]}))
    )
    assert len(sixty_four["workflow_approval"]["workflows"]) == 64
    disabled = load_pr_guard_config(json.dumps(_cfg({"enabled": False, "workflows": []})))
    assert disabled["workflow_approval"] == {"enabled": False, "workflows": []}
    missing = load_pr_guard_config(json.dumps(A38_BODY))
    assert "workflow_approval" not in missing


def test_invalid_trusted_config_never_lists_actions() -> None:
    fake = FakeApproval()
    fake.set_pr_guard_config(_cfg({"enabled": True, "workflows": [".github/workflows/*.yml"]}))
    with pytest.raises(GuardError, match="pr-guard"):
        reconcile_pull(fake.api(), REPO, 1)
    assert fake.actions_pages == []
    assert fake.posts == []


# --- opt-in / short-circuit -------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"ok": False, "status": "fail", "mode": "enforce", "workflow_approval_enabled": True},
        {"ok": True, "status": "pass", "mode": "enforce", "workflow_approval_enabled": True, "closed": True},
        {"ok": True, "status": "pass", "mode": "observe", "workflow_approval_enabled": True},
        {"ok": True, "status": "pass", "mode": "enforce", "workflow_approval_enabled": False},
        {"ok": True, "status": "not_applicable", "mode": "enforce", "workflow_approval_enabled": True},
    ],
)
def test_approve_short_circuits_without_touching_api(kwargs: dict) -> None:
    assert wa.approve_workflow_runs(object(), Assessment(**kwargs)) == []


@pytest.mark.parametrize("case", ["missing", "disabled", "same_repo", "closed", "observe", "fail"])
def test_opt_in_disabled_or_missing_never_calls_actions(case: str) -> None:
    fake = FakeApproval()
    if case == "missing":
        fake.set_pr_guard_config(_pr_guard_config())
    elif case == "disabled":
        fake.set_pr_guard_config(_cfg({"enabled": False, "workflows": [PATH]}))
    elif case == "same_repo":
        fake.pull["head"]["repo"]["full_name"] = REPO
    elif case == "closed":
        fake.pull["state"] = "closed"
    elif case == "observe":
        policy = json.loads(fake.files[(BASE, ".github/a38.json")])
        policy["mode"] = "observe"
        fake.files[(BASE, ".github/a38.json")] = json.dumps(policy).encode()
    else:
        fake.comments.clear()
    result = reconcile_pull(fake.api(), REPO, 1)
    assert result.workflow_approvals == []
    assert "workflow_approvals" in result.to_json()
    assert result.to_json()["workflow_approvals"] == []
    assert fake.actions_pages == []
    assert fake.posts == []


# --- reconcile integration --------------------------------------------------


def test_reconcile_approves_and_exposes_writes_then_is_idempotent() -> None:
    fake = FakeApproval()
    result = reconcile_pull(fake.api(), REPO, 1)
    assert result.ok
    assert result.status == "pass"
    expected = {"run_id": 101, "workflow": PATH, "head": HEAD, "status": "approved"}
    assert result.workflow_approvals == [expected]
    assert result.to_json()["workflow_approvals"] == [expected]
    assert "workflow:approve:101" in result.writes
    assert fake.posts == [101]
    second = reconcile_pull(fake.api(), REPO, 1)
    assert second.ok
    assert second.workflow_approvals == []
    assert fake.posts == [101]


def test_dry_run_plans_without_post() -> None:
    fake = FakeApproval()
    result = reconcile_pull(fake.api(), REPO, 1, dry_run=True)
    assert result.workflow_approvals == [
        {"run_id": 101, "workflow": PATH, "head": HEAD, "status": "planned"}
    ]
    assert result.to_json()["workflow_approvals"][0]["status"] == "planned"
    assert fake.posts == []
    assert fake.writes == []
    assert fake.actions_pages  # inventory is still read


def test_linked_association_approves_without_open_pr_fallback() -> None:
    fake = FakeApproval()
    fake.runs[0]["pull_requests"] = [_link()]
    other = copy.deepcopy(fake.pull)
    other["number"] = 2
    fake.extra_pulls = [other]
    fake.comparison = "diverged"
    fake.events = [{"event": "reopened", "created_at": "2026-09-05T11:30:00Z"}]
    result = reconcile_pull(fake.api(), REPO, 1)
    assert result.workflow_approvals[0]["status"] == "approved"
    assert fake.posts == [101]
    assert fake.compare_urls == []
    assert fake.event_reads == 0


@pytest.mark.parametrize("status", ["ahead", "identical"])
def test_private_fork_empty_associations_unique_open_pr_and_base_ancestor(status: str) -> None:
    fake = _private_fork()
    fake.comparison = status
    fake.events = [{"event": "reopened", "created_at": "2026-09-04T00:00:00Z"}]
    result = reconcile_pull(fake.api(), REPO, 1)
    assert result.workflow_approvals == [
        {"run_id": 101, "workflow": PATH, "head": HEAD, "status": "approved"}
    ]
    assert fake.posts == [101]
    assert any(f"{BASE}...{HEAD}" in url for url in fake.compare_urls)
    assert fake.event_reads >= 1


# --- latest-per-workflow / exact match / attempt ----------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "completed", "conclusion": "success"},
        {"status": "completed", "conclusion": "failure"},
        {"status": "queued", "conclusion": None},
        {"status": "in_progress", "conclusion": None},
        {"status": "completed", "conclusion": "cancelled"},
        {"status": "waiting", "conclusion": None},
        {"run_attempt": 2},
    ],
)
def test_newer_run_of_any_status_suppresses_old_action_required(changes: dict) -> None:
    fake = FakeApproval()
    fake.runs.append(fake.run(id=102, created_at="2026-09-05T11:01:00Z", **changes))
    result = reconcile_pull(fake.api(), REPO, 1)
    assert result.workflow_approvals == []
    assert fake.posts == []


def test_newer_action_required_is_approved_over_older_success() -> None:
    fake = FakeApproval()
    fake.runs = [
        fake.run(id=100, created_at="2026-09-05T10:00:00Z", conclusion="success"),
        fake.run(id=101, created_at="2026-09-05T11:00:00Z"),
    ]
    result = reconcile_pull(fake.api(), REPO, 1)
    assert result.workflow_approvals[0]["run_id"] == 101
    assert fake.posts == [101]


def test_same_timestamp_higher_id_is_latest() -> None:
    fake = FakeApproval()
    stamp = "2026-09-05T11:00:00Z"
    fake.runs = [
        fake.run(id=101, created_at=stamp),
        fake.run(id=102, created_at=stamp, conclusion="success"),
    ]
    reconcile_pull(fake.api(), REPO, 1)
    assert fake.posts == []
    fake.posts.clear()
    fake.runs = [
        fake.run(id=101, created_at=stamp, conclusion="success"),
        fake.run(id=102, created_at=stamp),
    ]
    result = reconcile_pull(fake.api(), REPO, 1)
    assert result.workflow_approvals[0]["run_id"] == 102
    assert fake.posts == [102]


@pytest.mark.parametrize(
    "changes",
    [
        {"event": "push"},
        {"event": "workflow_dispatch"},
        {"head_sha": BASE},
        {"head_branch": "other"},
        {"repository": {"full_name": FORK}},
        {"head_repository": {"full_name": REPO}},
        {"path": OTHER},
        {"run_attempt": 2},
        {"run_attempt": 3},
        {"status": "completed", "conclusion": "success"},
    ],
)
def test_inexact_or_retry_runs_are_never_approved(changes: dict) -> None:
    fake = FakeApproval()
    fake.runs = [fake.run(**changes)]
    result = reconcile_pull(fake.api(), REPO, 1)
    assert result.workflow_approvals == []
    assert fake.posts == []


def test_allowlist_approves_only_listed_workflow() -> None:
    fake = FakeApproval()
    fake.runs = [
        fake.run(id=101),
        fake.run(id=202, path=OTHER),
    ]
    result = reconcile_pull(fake.api(), REPO, 1)
    assert result.workflow_approvals == [
        {"run_id": 101, "workflow": PATH, "head": HEAD, "status": "approved"}
    ]
    assert fake.posts == [101]


# --- pagination -------------------------------------------------------------


def test_pagination_boundary_reads_page_two_for_the_pending_run() -> None:
    fake = FakeApproval()
    fake.runs = [fake.run(id=i, path=OTHER) for i in range(1, 101)]
    fake.runs.append(fake.run(id=101))
    result = reconcile_pull(fake.api(), REPO, 1)
    assert fake.posts == [101]
    assert 2 in fake.actions_pages
    assert result.workflow_approvals[0]["run_id"] == 101


def test_pagination_exact_page_does_not_fetch_another_page() -> None:
    fake = FakeApproval()
    fake.runs = [fake.run(id=i, path=OTHER) for i in range(1, 100)]
    fake.runs.append(fake.run(id=100))

    def inventory(page: int) -> dict:
        if page != 1:
            raise AssertionError(f"unexpected page {page} when total_count is 100")
        return {"total_count": 100, "workflow_runs": copy.deepcopy(fake.runs)}

    fake.inventory = inventory
    result = reconcile_pull(fake.api(), REPO, 1)
    assert fake.posts == [100]
    assert fake.actions_pages == [1, 1]
    assert result.workflow_approvals[0]["run_id"] == 100


@pytest.mark.parametrize(
    "inventory",
    [
        {"total_count": 1000, "workflow_runs": []},
        {"total_count": -1, "workflow_runs": []},
        {"workflow_runs": []},
        {"total_count": True, "workflow_runs": []},
        {"total_count": 1, "workflow_runs": "runs"},
        {"total_count": 1, "workflow_runs": [{}]},
        {"total_count": 1, "workflow_runs": [{"id": "101"}]},
        {"total_count": 2, "workflow_runs": [FakeApproval.run(), FakeApproval.run()]},
        {"total_count": 1, "workflow_runs": [FakeApproval.run(), "x"]},
    ],
    ids=[
        "at-max-bound",
        "negative-count",
        "missing-total",
        "bool-total",
        "runs-not-list",
        "missing-id",
        "string-id",
        "duplicate-ids",
        "non-mapping-item",
    ],
)
def test_malformed_inventory_fails_closed_without_post(inventory: dict) -> None:
    fake = FakeApproval()
    fake.inventory = inventory
    with pytest.raises(GuardError):
        reconcile_pull(fake.api(), REPO, 1)
    assert fake.posts == []


def test_pagination_count_changed_between_pages_fails_closed() -> None:
    fake = FakeApproval()

    def inventory(page: int) -> dict:
        if page == 1:
            return {
                "total_count": 101,
                "workflow_runs": [FakeApproval.run(id=i, path=OTHER) for i in range(1, 101)],
            }
        return {"total_count": 102, "workflow_runs": [FakeApproval.run(id=101)]}

    fake.inventory = inventory
    with pytest.raises(GuardError, match="changed or exceeds"):
        reconcile_pull(fake.api(), REPO, 1)
    assert fake.posts == []


def test_pagination_duplicate_id_across_pages_fails_closed() -> None:
    fake = FakeApproval()

    def inventory(page: int) -> dict:
        if page == 1:
            return {
                "total_count": 101,
                "workflow_runs": [FakeApproval.run(id=i, path=OTHER) for i in range(1, 101)],
            }
        return {"total_count": 101, "workflow_runs": [FakeApproval.run(id=1, path=OTHER)]}

    fake.inventory = inventory
    with pytest.raises(GuardError, match="duplicate"):
        reconcile_pull(fake.api(), REPO, 1)
    assert fake.posts == []


def test_pagination_empty_page_and_bound_exceeded_fail_closed() -> None:
    fake = FakeApproval()

    def incomplete(page: int) -> dict:
        if page == 1:
            return {"total_count": 2, "workflow_runs": [fake.run()]}
        return {"total_count": 2, "workflow_runs": []}

    fake.inventory = incomplete
    with pytest.raises(GuardError, match="incomplete"):
        reconcile_pull(fake.api(), REPO, 1)
    assert fake.posts == []

    def ten_short_pages(page: int) -> dict:
        return {
            "total_count": 150,
            "workflow_runs": [FakeApproval.run(id=page * 100 + i, path=OTHER) for i in range(10)],
        }

    fake = FakeApproval()
    fake.inventory = ten_short_pages
    with pytest.raises(GuardError, match="pagination bound"):
        reconcile_pull(fake.api(), REPO, 1)
    assert fake.posts == []
    assert fake.actions_pages == list(range(1, 11))


def test_runs_helper_rejects_count_change_and_duplicates() -> None:
    class Scripted:
        def get_json(self, path: str) -> dict:
            page = int((parse_qs(urlparse(path).query).get("page") or ["1"])[0])
            if page == 1:
                return {
                    "total_count": 3,
                    "workflow_runs": [FakeApproval.run(id=1), FakeApproval.run(id=2)],
                }
            return {"total_count": 4, "workflow_runs": [FakeApproval.run(id=3)]}

    with pytest.raises(GuardError, match="changed or exceeds"):
        wa._runs(Scripted(), REPO, HEAD)

    class Dupes:
        def get_json(self, path: str) -> dict:
            return {"total_count": 2, "workflow_runs": [FakeApproval.run(), FakeApproval.run()]}

    with pytest.raises(GuardError, match="duplicate"):
        wa._runs(Dupes(), REPO, HEAD)


# --- 403 / mutation / association failures ----------------------------------


def test_approve_403_fails_loud_without_post_retry() -> None:
    fake = FakeApproval()
    fake.post_status = 403
    api = fake.api()
    seen: list[dict] = []
    inner = api.request

    def wrapped(method: str, path: str, **kwargs: object):
        if method.upper() == "POST" and "/approve" in str(path):
            seen.append({"retry": kwargs.get("retry"), "path": path})
        return inner(method, path, **kwargs)

    api.request = wrapped  # type: ignore[method-assign]
    with pytest.raises(GuardError, match="HTTP 403"):
        reconcile_pull(api, REPO, 1)
    assert fake.posts == [101]
    assert len(seen) == 1
    assert seen[0]["retry"] is False
    assert fake.statuses[0]["state"] == "error"


def _add_maintainer_approval(fake: FakeApproval) -> None:
    fake.files[(HEAD, ".github/a38.json")] = fake.files[(BASE, ".github/a38.json")]
    fake.permissions["maintainer"] = {"permission": "write", "user": {"id": MAINTAINER}}
    fake.reviews = [
        {
            "id": 100,
            "user": {"id": MAINTAINER, "login": "maintainer"},
            "state": "APPROVED",
            "commit_id": HEAD,
            "submitted_at": "2026-09-05T13:00:00Z",
            "body": f"{POLICY_APPROVAL_PREFIX} head={HEAD} base={BASE}",
        }
    ]


@pytest.mark.parametrize("what", ["head", "base", "config", "report", "approval"])
def test_mutation_before_write_is_caught_by_live_reassessment(what: str) -> None:
    fake = FakeApproval()
    if what == "approval":
        fake.files[(HEAD, ".github/a38.json")] = fake.files[(BASE, ".github/a38.json")]
        fake.permissions["maintainer"] = {"permission": "write", "user": {"id": MAINTAINER}}
    fired = {"done": False}

    def mutate(f: FakeApproval, page: int) -> None:
        if fired["done"] or page != 1:
            return
        fired["done"] = True
        if what == "head":
            f.pull["head"]["sha"] = BASE2
        elif what == "base":
            f.pull["base"]["sha"] = BASE2
        elif what == "config":
            # Same-SHA content edits are invisible: GitHubApi caches contents by commit.
            f.ref_commits["develop"] = BASE2
            widened = _cfg({"enabled": True, "workflows": [PATH, OTHER]})
            f.files[(BASE2, ".github/pr-guard.json")] = json.dumps(widened).encode()
        elif what == "report":
            f.comments[0]["body"] += "\n"
        else:
            _add_maintainer_approval(f)

    fake.on_inventory = mutate
    api = fake.api()
    assessment = assess_pull(api, REPO, 1)
    assert assessment.ok
    assert assessment.status == "pass"
    assert assessment.workflow_approval_enabled
    with pytest.raises(GuardError, match="changed before workflow approval"):
        wa.approve_workflow_runs(api, assessment)
    assert fake.posts == []


def test_get_run_identity_change_fails_closed() -> None:
    fake = FakeApproval()
    fake.run_override = fake.run(repository={"full_name": "other/public-app"})
    with pytest.raises(GuardError, match="identity changed"):
        reconcile_pull(fake.api(), REPO, 1)
    assert fake.posts == []


@pytest.mark.parametrize(
    "what",
    [
        "missing",
        "not-list",
        "ambiguous",
        "cross-pr",
        "cross-head",
        "cross-base",
        "predates",
    ],
)
def test_missing_ambiguous_and_cross_pr_links_fail_closed(what: str) -> None:
    fake = FakeApproval()
    run = fake.runs[0]
    if what == "missing":
        del run["pull_requests"]
    elif what == "not-list":
        run["pull_requests"] = None
    elif what == "ambiguous":
        run["pull_requests"] = [_link(), _link(number=2)]
    elif what == "cross-pr":
        run["pull_requests"] = [_link(number=2)]
    elif what == "cross-head":
        run["pull_requests"] = [_link(head=BASE2)]
    elif what == "cross-base":
        run["pull_requests"] = [_link(base=BASE2)]
    else:
        run["pull_requests"] = [_link()]
        run["created_at"] = "2026-08-01T00:00:00Z"
    with pytest.raises(GuardError):
        reconcile_pull(fake.api(), REPO, 1)
    assert fake.posts == []


@pytest.mark.parametrize(
    "what",
    [
        "ambiguous-open",
        "number-mismatch",
        "list-sha-changed",
        "diverged",
        "behind",
        "retargeted",
        "force-pushed",
        "reopened",
        "compare-malformed",
        "event-malformed",
    ],
)
def test_private_fork_fallback_fails_closed_without_unique_stable_link(what: str) -> None:
    fake = _private_fork()
    if what == "ambiguous-open":
        other = copy.deepcopy(fake.pull)
        other["number"] = 2
        fake.extra_pulls = [other]
    elif what == "number-mismatch":
        listed = copy.deepcopy(fake.pull)
        listed["number"] = 9
        fake.listed_pulls = [listed]
    elif what == "list-sha-changed":
        fake.list_head_sha = BASE2
    elif what == "diverged":
        fake.comparison = "diverged"
    elif what == "behind":
        fake.comparison = "behind"
    elif what == "retargeted":
        fake.events = [{"event": "base_ref_changed", "created_at": "2026-09-05T11:30:00Z"}]
    elif what == "force-pushed":
        fake.events = [{"event": "base_ref_force_pushed", "created_at": "2026-09-05T11:30:00Z"}]
    elif what == "reopened":
        fake.events = [{"event": "reopened", "created_at": "2026-09-05T11:30:00Z"}]
    elif what == "compare-malformed":
        fake.comparison = "nope"
    else:
        fake.events = ["not-an-object"]
    with pytest.raises(GuardError):
        reconcile_pull(fake.api(), REPO, 1)
    assert fake.posts == []
