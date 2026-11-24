"""Pre-checks of this area's operation types (`<area>/op_prechecks.py:PRECHECKS`, RUN-07 / ADR-041).

Every `github.*` type resolves the GitHub identity it acts with first (`identity.resolved`, KOR-47, ADR-076 section
3), except `github.request_connection` (the acting human's own connection) and `github.list_identities` (no
connection needed; its pre-check only notes the acting human and the identity the resolution picks now)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_runner.github import checkout, commit, identity, listing, precheck, push

PRECHECKS: dict[str, Callable[..., Any]] = {
    "github.list_repositories": identity.resolved(precheck.list_repositories),
    "github.get_pull_request": identity.resolved(precheck.repo_access),
    "github.create_branch": identity.resolved(precheck.repo_access),
    "github.create_pull_request": identity.resolved(precheck.repo_access),
    "github.comment": identity.resolved(precheck.repo_access),
    "github.checkout": identity.resolved(checkout.check),
    "github.commit": identity.resolved(commit.check),
    "github.push": identity.resolved(push.check),
    "github.list_issues": identity.resolved(precheck.repo_access),
    "github.get_issue": identity.resolved(precheck.repo_access),
    "github.create_issue": identity.resolved(precheck.repo_access),
    "github.update_issue": identity.resolved(precheck.repo_access),
    "github.list_pull_requests": identity.resolved(precheck.repo_access),
    "github.request_connection": precheck.verified_connection,
    "github.list_identities": listing.check,
}
