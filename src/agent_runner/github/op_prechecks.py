"""Pre-checks of this area's operation types (`<area>/op_prechecks.py:PRECHECKS`, RUN-07 / ADR-041)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_runner.github import checkout, commit, precheck, push

PRECHECKS: dict[str, Callable[..., Any]] = {
    "github.list_repositories": precheck.list_repositories,
    "github.get_pull_request": precheck.repo_access,
    "github.create_branch": precheck.repo_access,
    "github.create_pull_request": precheck.repo_access,
    "github.comment": precheck.repo_access,
    "github.checkout": checkout.check,
    "github.commit": commit.check,
    "github.push": push.check,
    "github.list_issues": precheck.repo_access,
    "github.get_issue": precheck.repo_access,
    "github.create_issue": precheck.repo_access,
    "github.update_issue": precheck.repo_access,
    "github.list_pull_requests": precheck.repo_access,
    "github.request_connection": precheck.verified_connection,
}
