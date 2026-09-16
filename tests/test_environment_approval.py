"""Environment-deployment approval behavior and side-effect boundaries."""

from __future__ import annotations

import copy
import json
from urllib.parse import parse_qs, urlparse

import pytest

from agent_cli import environment_approval as ea
from agent_cli.a38_guard import Assessment, GuardError, reconcile_pull
from agent_cli.pr_lifecycle import AUTH_MARKER
from test_a38_guard import (
    BASE,
    HEAD,
    REPO,
    FakeAPI,
    _pr_guard_config,
    _report_comment,
)

pytestmark = pytest.mark.no_pg

PATH = ".github/workflows/pr.yml"
BRANCH = "feature"
ENVIRONMENT = "pr-ci"
ENVIRONMENT_ID = 4242


def _environment_config(*, enabled: bool = True) -> dict:
    config = copy.deepcopy(_pr_guard_config())
    config["environment_approval"] = {
        "enabled": enabled,
        "environment": ENVIRONMENT,
        "workflows": [PATH],
    }
    return config


def _link() -> dict:
    return {"number": 1, "head": {"sha": HEAD}, "base": {"sha": BASE}}


class FakeEnvironmentApproval(FakeAPI):
    """FakeAPI with Actions runs and controllable pending deployments."""

    def __init__(self) -> None:
        super().__init__()
        self.pull["head"]["repo"]["full_name"] = REPO
        self.pull["head"]["ref"] = BRANCH
        self.pull["created_at"] = "2026-09-01T00:00:00Z"
        self.config = _environment_config()
        self.set_pr_guard_config(self.config)
        self.add_author_report(
            _report_comment(), updated_at="2026-09-05T12:00:00Z", cid=21
        )
        self.runs = [self.run()]
        self.pending_by_run = {
            101: [
                {
                    "environment": {
                        "id": ENVIRONMENT_ID,
                        "name": ENVIRONMENT,
                    }
                }
            ]
        }
        self.pending_posts: list[dict] = []
        self.environment_post_status = 200
        self.requests: list[tuple[str, str]] = []

    @staticmethod
    def run(**changes: object) -> dict:
        data: dict = {
            "id": 101,
            "path": PATH,
            "event": "pull_request",
            "head_sha": HEAD,
            "head_branch": BRANCH,
            "repository": {"full_name": REPO},
            "head_repository": {"full_name": REPO},
            "pull_requests": [_link()],
            "status": "waiting",
            "conclusion": None,
            "run_attempt": 1,
            "created_at": "2026-09-05T11:00:00Z",
        }
        data.update(changes)
        return data

    def request_fn(self, method: str, url: str, body: bytes | None = None):
        parsed = urlparse(url)
        path = parsed.path
        self.requests.append((method, path))
        root = f"/repos/{REPO}"
        pending_suffix = "/pending_deployments"

        if path.startswith(f"{root}/actions/runs/") and path.endswith(
            pending_suffix
        ):
            ident = int(
                path.removeprefix(f"{root}/actions/runs/").removesuffix(
                    pending_suffix
                )
            )
            if method == "GET":
                return 200, copy.deepcopy(self.pending_by_run.get(ident, [])), {}
            if method == "POST":
                assert isinstance(body, bytes)
                self.pending_posts.append(json.loads(body))
                if self.environment_post_status == 200:
                    self.pending_by_run[ident] = []
                return self.environment_post_status, {}, {}

        if method == "GET" and path == f"{root}/actions/runs":
            page = int((parse_qs(parsed.query).get("page") or ["1"])[0])
            per_page = int(
                (parse_qs(parsed.query).get("per_page") or ["100"])[0]
            )
            start = (page - 1) * per_page
            chunk = self.runs[start : start + per_page]
            return 200, {
                "total_count": len(self.runs),
                "workflow_runs": copy.deepcopy(chunk),
            }, {}

        run_prefix = f"{root}/actions/runs/"
        if method == "GET" and path.startswith(run_prefix):
            suffix = path.removeprefix(run_prefix)
            if suffix.isdigit():
                ident = int(suffix)
                run = next(run for run in self.runs if run["id"] == ident)
                return 200, copy.deepcopy(run), {}

        status, data, headers = super().request_fn(method, url, body)
        return status, copy.deepcopy(data), headers


def _auth_comments(fake: FakeEnvironmentApproval) -> list[dict]:
    return [
        comment
        for comment in fake.comments
        if str(comment.get("body", "")).startswith(AUTH_MARKER)
    ]


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "ok": False,
            "status": "fail",
            "mode": "enforce",
            "environment_approval_enabled": True,
        },
        {
            "ok": True,
            "status": "pass",
            "mode": "enforce",
            "environment_approval_enabled": True,
            "closed": True,
        },
        {
            "ok": True,
            "status": "pass",
            "mode": "observe",
            "environment_approval_enabled": True,
        },
        {
            "ok": True,
            "status": "pass",
            "mode": "enforce",
            "environment_approval_enabled": False,
        },
        {
            "ok": True,
            "status": "not_applicable",
            "mode": "enforce",
            "environment_approval_enabled": True,
        },
    ],
)
def test_approve_short_circuits_without_touching_api(kwargs: dict) -> None:
    assert ea.approve_environment_deployments(object(), Assessment(**kwargs)) == []


@pytest.mark.parametrize("case", ["missing", "disabled"])
def test_missing_or_disabled_config_never_posts_pending_deployments(
    case: str,
) -> None:
    fake = FakeEnvironmentApproval()
    config = (
        _pr_guard_config()
        if case == "missing"
        else _environment_config(enabled=False)
    )
    fake.set_pr_guard_config(config)

    result = reconcile_pull(fake.api(), REPO, 1)

    assert result.environment_approvals == []
    assert fake.pending_posts == []


def test_same_repository_pr_approves_matching_pending_environment() -> None:
    fake = FakeEnvironmentApproval()

    result = reconcile_pull(fake.api(), REPO, 1)

    expected = {
        "run_id": 101,
        "workflow": PATH,
        "environment": ENVIRONMENT,
        "head": HEAD,
        "status": "approved",
    }
    assert result.environment_approvals == [expected]
    assert result.to_json()["environment_approvals"] == [expected]
    assert fake.pending_posts == [
        {
            "environment_ids": [ENVIRONMENT_ID],
            "state": "approved",
            "comment": "A38 enforce pass",
        }
    ]
    assert "environment:approve:101" in result.writes
    assert len(_auth_comments(fake)) == 1
    assert result.workflow_approvals == []


def test_empty_pending_deployments_do_not_post() -> None:
    fake = FakeEnvironmentApproval()
    fake.pending_by_run[101] = []

    result = reconcile_pull(fake.api(), REPO, 1)

    assert result.environment_approvals == []
    assert fake.pending_posts == []


def test_different_pending_environment_does_not_post() -> None:
    fake = FakeEnvironmentApproval()
    fake.pending_by_run[101] = [
        {"environment": {"id": 9001, "name": "production"}}
    ]

    result = reconcile_pull(fake.api(), REPO, 1)

    assert result.environment_approvals == []
    assert fake.pending_posts == []


def test_dry_run_plans_without_post_write_or_auth_comment() -> None:
    fake = FakeEnvironmentApproval()

    result = reconcile_pull(fake.api(), REPO, 1, dry_run=True)

    assert result.environment_approvals == [
        {
            "run_id": 101,
            "workflow": PATH,
            "environment": ENVIRONMENT,
            "head": HEAD,
            "status": "planned",
        }
    ]
    assert fake.pending_posts == []
    assert fake.writes == []
    assert _auth_comments(fake) == []


def test_successful_approval_is_idempotent_when_pending_inventory_clears() -> None:
    fake = FakeEnvironmentApproval()

    first = reconcile_pull(fake.api(), REPO, 1)
    second = reconcile_pull(fake.api(), REPO, 1)

    assert first.environment_approvals[0]["status"] == "approved"
    assert second.environment_approvals == []
    assert len(fake.pending_posts) == 1


def test_unexpected_environment_approval_status_fails_loudly() -> None:
    fake = FakeEnvironmentApproval()
    fake.environment_post_status = 202

    with pytest.raises(GuardError, match="environment approval HTTP 202"):
        reconcile_pull(fake.api(), REPO, 1)

    assert len(fake.pending_posts) == 1
    assert "environment:approve:101" not in fake.writes
    assert _auth_comments(fake) == []


def test_environment_approval_never_uses_other_mutation_endpoints() -> None:
    fake = FakeEnvironmentApproval()

    reconcile_pull(fake.api(), REPO, 1)

    mutations = [
        path
        for method, path in fake.requests
        if method in {"POST", "PUT", "PATCH", "DELETE"}
    ]
    forbidden_suffixes = (
        "/approve",
        "/cancel",
        "/rerun",
        "/rerun-failed-jobs",
        "/dispatches",
        "/merge",
        "/reviews",
    )
    extra = [
        path
        for path in mutations
        if not path.endswith("/pending_deployments")
        and path.endswith(forbidden_suffixes)
    ]
    assert extra == []
