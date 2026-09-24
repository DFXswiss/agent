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
from agent_cli.pr_lifecycle import AUTH_MARKER, CANCEL_MARKER, RESULT_MARKER
from test_a38_guard import (
    BASE,
    BASE2,
    BOT_ID,
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
        self.cancels: list[int] = []
        self.cancel_status = 202
        self.actions_pages: list[int] = []
        self.compare_urls: list[str] = []
        self.event_reads = 0
        self.post_status = 201
        self.run_get_status = 200
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
            remainder = path.split("/actions/runs/", 1)[1]
            if (
                not any(r["id"] == ident for r in self.runs)
                and method == "GET"
                and "/" not in remainder
            ):
                return 404, {}, {}
            run = next(r for r in self.runs if r["id"] == ident)
            if method == "POST":
                if path.endswith("/cancel"):
                    self.cancels.append(ident)
                    if self.cancel_status in {202, 409}:
                        if self.cancel_status == 202:
                            run.update(status="completed", conclusion="cancelled")
                    return self.cancel_status, {}, {}
                assert path.endswith("/approve")
                self.posts.append(ident)
                if self.post_status == 201:
                    run.update(status="queued", conclusion=None)
                return self.post_status, {}, {}
            if self.before_run_read is not None:
                callback, self.before_run_read = self.before_run_read, None
                callback(self)
            payload = self.run_override if self.run_override is not None else run
            status = (
                self.run_get_status
                if method == "GET" and "/" not in remainder
                else 200
            )
            return status, copy.deepcopy(payload), {}
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
    assert fake.cancels == []


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
def test_opt_in_disabled_or_missing_never_approves(case: str) -> None:
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
    assert "manual_workflows" in result.to_json()
    assert fake.posts == []
    assert fake.cancels == []
    if case == "closed":
        assert fake.actions_pages == []
    else:
        assert fake.actions_pages


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
    assert fake.posts == []
    assert fake.cancels == [101]
    assert "workflow:cancel:101" in result.writes
    assert any(item["run_id"] == 101 and item["status"] == "cancelled" for item in result.workflow_approvals)


def test_older_action_required_is_cancelled_when_newer_is_approved() -> None:
    fake = FakeApproval()
    fake.runs = [
        fake.run(id=100, created_at="2026-09-05T10:00:00Z"),
        fake.run(id=101, created_at="2026-09-05T11:00:00Z"),
    ]
    result = reconcile_pull(fake.api(), REPO, 1)
    assert fake.posts == [101]
    assert fake.cancels == [100]
    assert "workflow:cancel:100" in result.writes
    assert "workflow:approve:101" in result.writes
    statuses = {item["run_id"]: item["status"] for item in result.workflow_approvals}
    assert statuses[101] == "approved"
    assert statuses[100] == "cancelled"


def test_non_allowlisted_held_run_on_this_head_is_cancelled() -> None:
    fake = FakeApproval()
    fake.runs.append(
        fake.run(id=202, path=OTHER, created_at="2026-09-05T11:00:00Z")
    )
    result = reconcile_pull(fake.api(), REPO, 1)
    assert fake.posts == [101]
    assert fake.cancels == [202]
    assert "workflow:cancel:202" in result.writes
    assert any(item["run_id"] == 202 and item["status"] == "cancelled" for item in result.workflow_approvals)


def test_dry_run_plans_cancel_without_post() -> None:
    fake = FakeApproval()
    fake.runs.append(fake.run(id=202, path=OTHER))
    result = reconcile_pull(fake.api(), REPO, 1, dry_run=True)
    statuses = {item["run_id"]: item["status"] for item in result.workflow_approvals}
    assert statuses[101] == "planned"
    assert statuses[202] == "planned-cancel"
    assert fake.posts == []
    assert fake.cancels == []
    assert not any(w.startswith("workflow:") for w in result.writes)


def test_cancel_http_409_is_idempotent_success_not_approve() -> None:
    fake = FakeApproval()
    fake.cancel_status = 409
    fake.runs.append(fake.run(id=202, path=OTHER))
    result = reconcile_pull(fake.api(), REPO, 1)
    assert fake.cancels == [202]
    assert fake.posts == [101]
    assert "workflow:cancel:202" in result.writes
    assert "workflow:approve:202" not in result.writes
    statuses = {item["run_id"]: item["status"] for item in result.workflow_approvals}
    assert statuses[202] == "cancelled"


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
    assert fake.cancels == [101]
    fake.posts.clear()
    fake.cancels.clear()
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
    assert fake.posts == []
    on_pull = changes.get("path") == OTHER
    if on_pull:
        assert fake.cancels == [101]
        assert any(item["status"] == "cancelled" for item in result.workflow_approvals)
    else:
        assert result.workflow_approvals == []
        assert fake.cancels == []


def test_allowlist_approves_only_listed_workflow() -> None:
    fake = FakeApproval()
    fake.runs = [
        fake.run(id=101),
        fake.run(id=202, path=OTHER),
    ]
    result = reconcile_pull(fake.api(), REPO, 1)
    assert fake.posts == [101]
    assert fake.cancels == [202]
    statuses = {item["run_id"]: item["status"] for item in result.workflow_approvals}
    assert statuses[101] == "approved"
    assert statuses[202] == "cancelled"


# --- pagination -------------------------------------------------------------


def test_pagination_boundary_reads_page_two_for_the_pending_run() -> None:
    fake = FakeApproval()
    fake.runs = [fake.run(id=i, path=OTHER) for i in range(1, 101)]
    fake.runs.append(fake.run(id=101))
    result = reconcile_pull(fake.api(), REPO, 1)
    assert fake.posts == [101]
    assert 2 in fake.actions_pages
    assert any(item["run_id"] == 101 and item["status"] == "approved" for item in result.workflow_approvals)
    assert len(fake.cancels) == 100


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
    # Approval refreshes the inventory, then the always-on manual-activation
    # scan reads it again. Neither call may walk past page 1 when total_count is 100.
    assert fake.actions_pages == [1, 1, 1]
    assert 2 not in fake.actions_pages
    assert any(item["run_id"] == 100 and item["status"] == "approved" for item in result.workflow_approvals)
    assert len(fake.cancels) == 99


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


# --- visible AUTH / CANCEL comments -----------------------------------------


AUTH_EN = "I have authorized the recorded CI runs; their results are still pending."
AUTH_DE = "Ich habe die dokumentierten CI-Läufe freigegeben; ihre Ergebnisse stehen noch aus."
CANCEL_EN = (
    "I cancelled waiting workflow runs that were superseded or not on the allowlist, "
    "so they no longer await approval."
)
CANCEL_DE = (
    "Ich habe wartende Workflow-Läufe abgebrochen, die überholt oder nicht auf der "
    "Allowlist sind, damit sie nicht weiter auf Freigabe warten."
)
RESULT_OK_EN = "The recorded CI runs finished successfully."
RESULT_OK_DE = "Die dokumentierten CI-Läufe sind erfolgreich abgeschlossen."
RESULT_BAD_EN = "The recorded CI runs finished; not every run succeeded."
RESULT_BAD_DE = "Die dokumentierten CI-Läufe sind abgeschlossen; nicht jeder Lauf war erfolgreich."


def _marker_comments(fake: FakeApproval, marker: str) -> list[dict]:
    return [c for c in fake.comments if str(c.get("body", "")).startswith(marker)]


def _comment_record(comment: dict) -> dict:
    return json.loads(comment["body"].split("```json\n", 1)[1].split("\n```", 1)[0])


def test_approve_201_posts_auth_comment_without_lifecycle() -> None:
    fake = FakeApproval()
    assert "lifecycle" not in fake.config
    result = reconcile_pull(fake.api(), REPO, 1)
    assert "workflow:approve:101" in result.writes
    auths = _marker_comments(fake, AUTH_MARKER)
    assert len(auths) == 1
    body = auths[0]["body"]
    assert body.startswith(AUTH_MARKER)
    assert AUTH_EN in body
    assert AUTH_DE in body
    assert any(row.get("run_id") == 101 for row in _comment_record(auths[0])["runs"])


def test_two_allowlisted_approves_in_one_reconcile_share_one_auth_comment() -> None:
    fake = FakeApproval()
    fake.config = _cfg({"enabled": True, "workflows": [PATH, OTHER]})
    fake.set_pr_guard_config(fake.config)
    fake.runs = [fake.run(id=101, path=PATH), fake.run(id=202, path=OTHER)]
    result = reconcile_pull(fake.api(), REPO, 1)
    assert "workflow:approve:101" in result.writes
    assert "workflow:approve:202" in result.writes
    auths = _marker_comments(fake, AUTH_MARKER)
    assert len(auths) == 1
    ids = {row["run_id"] for row in _comment_record(auths[0])["runs"]}
    assert ids == {101, 202}


def test_later_reconcile_posts_new_auth_comment_without_patching_history() -> None:
    fake = FakeApproval()
    fake.config = _cfg({"enabled": True, "workflows": [PATH, OTHER]})
    fake.set_pr_guard_config(fake.config)
    fake.runs = [fake.run(id=101, path=PATH)]
    first = reconcile_pull(fake.api(), REPO, 1)
    assert "workflow:approve:101" in first.writes
    fake.runs.append(fake.run(id=202, path=OTHER, created_at="2026-09-05T11:02:00Z"))
    second = reconcile_pull(fake.api(), REPO, 1)
    assert "workflow:approve:202" in second.writes
    auths = _marker_comments(fake, AUTH_MARKER)
    assert len(auths) == 2
    older, latest = sorted(auths, key=lambda c: c["id"])
    older_ids = {row["run_id"] for row in _comment_record(older)["runs"]}
    latest_ids = {row["run_id"] for row in _comment_record(latest)["runs"]}
    assert 202 not in older_ids
    assert latest_ids == {101, 202}


def test_cancel_202_posts_cancel_comment_and_approve_posts_auth() -> None:
    fake = FakeApproval()
    fake.runs.append(fake.run(id=202, path=OTHER))
    result = reconcile_pull(fake.api(), REPO, 1)
    assert "workflow:cancel:202" in result.writes
    assert "workflow:approve:101" in result.writes
    cancels = _marker_comments(fake, CANCEL_MARKER)
    auths = _marker_comments(fake, AUTH_MARKER)
    assert len(cancels) == 1
    assert len(auths) == 1
    body = cancels[0]["body"]
    assert body.startswith(CANCEL_MARKER)
    assert CANCEL_EN in body
    assert CANCEL_DE in body
    assert any(row.get("run_id") == 202 for row in _comment_record(cancels[0])["runs"])


def test_cancel_409_records_write_without_cancel_comment() -> None:
    fake = FakeApproval()
    fake.cancel_status = 409
    fake.runs.append(fake.run(id=202, path=OTHER))
    result = reconcile_pull(fake.api(), REPO, 1)
    assert "workflow:cancel:202" in result.writes
    assert not _marker_comments(fake, CANCEL_MARKER)


def test_dry_run_cancel_candidate_writes_no_audit_comments() -> None:
    fake = FakeApproval()
    fake.runs.append(fake.run(id=202, path=OTHER))
    reconcile_pull(fake.api(), REPO, 1, dry_run=True)
    assert fake.writes == []
    assert not _marker_comments(fake, AUTH_MARKER)
    assert not _marker_comments(fake, CANCEL_MARKER)


def test_two_same_path_cancels_in_one_reconcile_list_both_run_ids() -> None:
    fake = FakeApproval()
    fake.runs = [
        fake.run(id=101, path=PATH),
        fake.run(id=202, path=OTHER),
        fake.run(id=203, path=OTHER, created_at="2026-09-05T11:01:00Z"),
    ]
    result = reconcile_pull(fake.api(), REPO, 1)
    assert "workflow:approve:101" in result.writes
    assert "workflow:cancel:202" in result.writes
    assert "workflow:cancel:203" in result.writes
    cancels = _marker_comments(fake, CANCEL_MARKER)
    assert len(cancels) == 1
    ids = {row["run_id"] for row in _comment_record(cancels[0])["runs"]}
    assert ids == {202, 203}
    auths = _marker_comments(fake, AUTH_MARKER)
    assert len(auths) == 1
    assert any(row.get("run_id") == 101 for row in _comment_record(auths[0])["runs"])


def test_create_false_patches_this_invocation_comment_not_latest_by_id() -> None:
    fake = FakeApproval()
    fake.config = _cfg({"enabled": True, "workflows": [PATH, OTHER]})
    fake.set_pr_guard_config(fake.config)
    fake.runs = [fake.run(id=101, path=PATH), fake.run(id=202, path=OTHER)]
    planted_body = (
        AUTH_MARKER + "\n```json\n"
        + json.dumps({
            "repo": REPO, "pr": 1, "head": HEAD, "base": BASE,
            "runs": [{"run_id": 999, "workflow": PATH}],
        })
        + "\n```"
    )
    fake.comments.append({
        "id": 500,
        "user": {"id": BOT_ID},
        "body": planted_body,
    })
    result = reconcile_pull(fake.api(), REPO, 1)
    assert "workflow:approve:101" in result.writes
    assert "workflow:approve:202" in result.writes
    auths = _marker_comments(fake, AUTH_MARKER)
    planted = next(c for c in auths if c["id"] == 500)
    assert planted["body"] == planted_body
    fresh = [c for c in auths if c["id"] != 500]
    assert len(fresh) == 1
    assert fresh[0]["id"] < 500
    ids = {row["run_id"] for row in _comment_record(fresh[0])["runs"]}
    assert ids == {101, 202}


# --- authorized run result comments -----------------------------------------


def test_finished_success_posts_result_comment_without_patching_auth() -> None:
    fake = FakeApproval()
    first = reconcile_pull(fake.api(), REPO, 1)
    auths = _marker_comments(fake, AUTH_MARKER)
    assert len(auths) == 1
    auth_body = auths[0]["body"]
    assert AUTH_EN in auth_body
    assert AUTH_DE in auth_body
    assert not _marker_comments(fake, RESULT_MARKER)
    assert first.ci_results == [{"status": "waiting"}]
    fake.runs[0].update(status="completed", conclusion="success")
    second = reconcile_pull(fake.api(), REPO, 1)
    auths = _marker_comments(fake, AUTH_MARKER)
    assert len(auths) == 1
    assert auths[0]["body"] == auth_body
    assert AUTH_EN in auths[0]["body"]
    assert AUTH_DE in auths[0]["body"]
    results = _marker_comments(fake, RESULT_MARKER)
    assert len(results) == 1
    assert results[0]["id"] > auths[0]["id"]
    body = results[0]["body"]
    assert RESULT_OK_EN in body
    assert RESULT_OK_DE in body
    assert RESULT_BAD_EN not in body
    assert RESULT_BAD_DE not in body
    assert _comment_record(results[0])["runs"][0]["conclusion"] == "success"
    assert second.ci_results[0]["status"] == "posted"


def test_same_conclusion_does_not_repost_result_comment() -> None:
    fake = FakeApproval()
    reconcile_pull(fake.api(), REPO, 1)
    fake.runs[0].update(status="completed", conclusion="success")
    reconcile_pull(fake.api(), REPO, 1)
    results = _marker_comments(fake, RESULT_MARKER)
    assert len(results) == 1
    body = results[0]["body"]
    third = reconcile_pull(fake.api(), REPO, 1)
    assert third.ci_results[0]["status"] == "exists"
    results = _marker_comments(fake, RESULT_MARKER)
    assert len(results) == 1
    assert results[0]["body"] == body


def test_finished_failure_uses_not_every_run_succeeded_sentences() -> None:
    fake = FakeApproval()
    reconcile_pull(fake.api(), REPO, 1)
    fake.runs[0].update(status="completed", conclusion="failure")
    result = reconcile_pull(fake.api(), REPO, 1)
    results = _marker_comments(fake, RESULT_MARKER)
    assert len(results) == 1
    body = results[0]["body"]
    assert RESULT_BAD_EN in body
    assert RESULT_BAD_DE in body
    assert RESULT_OK_EN not in body
    assert RESULT_OK_DE not in body
    assert _comment_record(results[0])["runs"][0]["conclusion"] == "failure"
    assert result.ci_results[0]["status"] == "posted"


def test_finished_skipped_uses_not_every_run_succeeded_sentences() -> None:
    fake = FakeApproval()
    reconcile_pull(fake.api(), REPO, 1)
    fake.runs[0].update(status="completed", conclusion="skipped")
    result = reconcile_pull(fake.api(), REPO, 1)
    results = _marker_comments(fake, RESULT_MARKER)
    assert len(results) == 1
    body = results[0]["body"]
    assert RESULT_BAD_EN in body
    assert RESULT_BAD_DE in body
    assert RESULT_OK_EN not in body
    assert RESULT_OK_DE not in body
    assert _comment_record(results[0])["runs"][0]["conclusion"] == "skipped"
    assert result.ci_results[0]["status"] == "posted"


def test_finished_neutral_uses_not_every_run_succeeded_sentences() -> None:
    fake = FakeApproval()
    reconcile_pull(fake.api(), REPO, 1)
    fake.runs[0].update(status="completed", conclusion="neutral")
    result = reconcile_pull(fake.api(), REPO, 1)
    results = _marker_comments(fake, RESULT_MARKER)
    assert len(results) == 1
    body = results[0]["body"]
    assert RESULT_BAD_EN in body
    assert RESULT_BAD_DE in body
    assert RESULT_OK_EN not in body
    assert RESULT_OK_DE not in body
    assert _comment_record(results[0])["runs"][0]["conclusion"] == "neutral"
    assert result.ci_results[0]["status"] == "posted"


def test_queued_run_posts_no_result_comment() -> None:
    fake = FakeApproval()
    reconcile_pull(fake.api(), REPO, 1)
    second = reconcile_pull(fake.api(), REPO, 1)
    assert not _marker_comments(fake, RESULT_MARKER)
    assert second.ci_results[0]["status"] == "waiting"


def test_completed_action_required_posts_no_result_comment() -> None:
    fake = FakeApproval()
    reconcile_pull(fake.api(), REPO, 1)
    fake.runs[0].update(status="completed", conclusion="action_required")
    fake.set_pr_guard_config(_cfg({"enabled": False, "workflows": [PATH]}))
    second = reconcile_pull(fake.api(), REPO, 1)
    assert not _marker_comments(fake, RESULT_MARKER)
    assert second.ci_results[0]["status"] == "waiting"
    assert fake.runs[0]["conclusion"] == "action_required"


def test_one_of_two_authorized_runs_still_queued_posts_no_result() -> None:
    fake = FakeApproval()
    fake.config = _cfg({"enabled": True, "workflows": [PATH, OTHER]})
    fake.set_pr_guard_config(fake.config)
    fake.runs = [fake.run(id=101, path=PATH), fake.run(id=202, path=OTHER)]
    reconcile_pull(fake.api(), REPO, 1)
    next(run for run in fake.runs if run["id"] == 101).update(
        status="completed", conclusion="success"
    )
    second = reconcile_pull(fake.api(), REPO, 1)
    assert not _marker_comments(fake, RESULT_MARKER)
    assert second.ci_results[0]["status"] == "waiting"


def test_dry_run_plans_result_comment_without_posting() -> None:
    fake = FakeApproval()
    reconcile_pull(fake.api(), REPO, 1)
    fake.runs[0].update(status="completed", conclusion="success")
    auths = _marker_comments(fake, AUTH_MARKER)
    auth_body = auths[0]["body"]
    comment_count = len(fake.comments)
    writes = list(fake.writes)
    planned = reconcile_pull(fake.api(), REPO, 1, dry_run=True)
    assert not _marker_comments(fake, RESULT_MARKER)
    assert planned.ci_results[0]["status"] == "planned"
    assert auths[0]["body"] == auth_body
    assert len(fake.comments) == comment_count
    assert fake.writes == writes


def test_mismatched_auth_head_posts_no_result_comment() -> None:
    fake = FakeApproval()
    fake.runs[0].update(status="completed", conclusion="success")
    planted_body = (
        AUTH_MARKER + "\n```json\n"
        + json.dumps({
            "repo": REPO, "pr": 1, "head": BASE2, "base": BASE,
            "runs": [{"run_id": 101, "workflow": PATH}],
        })
        + "\n```"
    )
    fake.comments.append({
        "id": 500,
        "user": {"id": BOT_ID},
        "body": planted_body,
    })
    result = reconcile_pull(fake.api(), REPO, 1)
    assert not _marker_comments(fake, RESULT_MARKER)
    assert result.ci_results == []
    planted = next(c for c in fake.comments if c["id"] == 500)
    assert planted["body"] == planted_body


def test_older_matching_auth_is_used_when_newer_auth_is_another_head() -> None:
    fake = FakeApproval()
    fake.runs[0].update(status="completed", conclusion="success")
    matching_body = (
        AUTH_MARKER + "\n```json\n"
        + json.dumps({
            "repo": REPO, "pr": 1, "head": HEAD, "base": BASE,
            "runs": [{"run_id": 101, "workflow": PATH}],
        })
        + "\n```"
    )
    other_head_body = (
        AUTH_MARKER + "\n```json\n"
        + json.dumps({
            "repo": REPO, "pr": 1, "head": BASE2, "base": BASE,
            "runs": [{"run_id": 202, "workflow": PATH}],
        })
        + "\n```"
    )
    fake.comments.append({
        "id": 500,
        "user": {"id": BOT_ID},
        "body": matching_body,
    })
    fake.comments.append({
        "id": 501,
        "user": {"id": BOT_ID},
        "body": other_head_body,
    })
    result = reconcile_pull(fake.api(), REPO, 1)
    results = _marker_comments(fake, RESULT_MARKER)
    assert len(results) == 1
    posted = _comment_record(results[0])["runs"]
    assert posted[0]["run_id"] == 101
    assert posted[0]["conclusion"] == "success"
    planted_matching = next(c for c in fake.comments if c["id"] == 500)
    planted_other = next(c for c in fake.comments if c["id"] == 501)
    assert planted_matching["body"] == matching_body
    assert planted_other["body"] == other_head_body
    assert result.ci_results[0]["status"] == "posted"
    assert 101 not in fake.posts


def test_unreadable_run_get_posts_no_result_comment() -> None:
    fake = FakeApproval()
    reconcile_pull(fake.api(), REPO, 1)
    fake.runs[0].update(status="completed", conclusion="success")
    fake.run_get_status = 500
    result = reconcile_pull(fake.api(), REPO, 1)
    assert not _marker_comments(fake, RESULT_MARKER)
    assert result.ci_results[0]["status"] == "unread"


def test_missing_run_get_is_unread_without_raising() -> None:
    fake = FakeApproval()
    reconcile_pull(fake.api(), REPO, 1)
    auths = _marker_comments(fake, AUTH_MARKER)
    auth_body = auths[0]["body"]
    fake.runs.clear()
    result = reconcile_pull(fake.api(), REPO, 1)
    assert not _marker_comments(fake, RESULT_MARKER)
    assert result.ci_results == [{"status": "unread"}]
    auths = _marker_comments(fake, AUTH_MARKER)
    assert len(auths) == 1
    assert auths[0]["body"] == auth_body


def test_new_auth_set_posts_second_result_after_new_run_finishes() -> None:
    fake = FakeApproval()
    reconcile_pull(fake.api(), REPO, 1)
    fake.runs[0].update(status="completed", conclusion="success")
    first_posted = reconcile_pull(fake.api(), REPO, 1)
    assert first_posted.ci_results[0]["status"] == "posted"
    results = _marker_comments(fake, RESULT_MARKER)
    assert len(results) == 1
    first_body = results[0]["body"]
    fake.config = _cfg({"enabled": True, "workflows": [PATH, OTHER]})
    fake.set_pr_guard_config(fake.config)
    fake.runs.append(fake.run(id=202, path=OTHER, created_at="2026-09-05T11:02:00Z"))
    waiting = reconcile_pull(fake.api(), REPO, 1)
    auths = _marker_comments(fake, AUTH_MARKER)
    assert len(auths) == 2
    results = _marker_comments(fake, RESULT_MARKER)
    assert len(results) == 1
    assert results[0]["body"] == first_body
    assert waiting.ci_results[0]["status"] == "waiting"
    next(run for run in fake.runs if run["id"] == 202).update(
        status="completed", conclusion="success"
    )
    posted = reconcile_pull(fake.api(), REPO, 1)
    results = _marker_comments(fake, RESULT_MARKER)
    assert len(results) == 2
    older, latest = sorted(results, key=lambda c: c["id"])
    assert older["body"] == first_body
    ids = {row["run_id"] for row in _comment_record(latest)["runs"]}
    assert ids == {101, 202}
    assert posted.ci_results[0]["status"] == "posted"
    auths = _marker_comments(fake, AUTH_MARKER)
    assert len(auths) == 2

