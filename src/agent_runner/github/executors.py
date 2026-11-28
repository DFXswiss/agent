"""Executors of the GitHub area, parts I and II (`<area>/executors.py:EXECUTORS`, ADR-027 section 4; 05-runner.md
section 12.1): REST only (one GraphQL mutation for a pull request's draft state, HOT-30), with the GitHub identity each
operation's pre-check resolved for its acting user (`ops.executor`, `github.identity`, KOR-47; the acting user is
`credentials.acting`'s, HOT-14); checkout, commit and push (RUN-10) in their own modules; the issue types, the
pull-request list and the connection request (KOR-25) in `issues`, `pull_list` and `connection`; the identity list
(KOR-47) in `listing`; the merge of a pull request's base into the workspace (KOR-48) in `merge_base`; reviews, checks
and the merge of a pull request (KOR-51) in `merge`; the change of a pull request's title, text, draft state and labels
(HOT-30) in `pull_update`."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_runner.github import (
    branches,
    checkout,
    comments,
    commit,
    connection,
    issues,
    listing,
    merge,
    merge_base,
    ops,
    pull_list,
    pull_update,
    pulls,
    push,
    repos,
)

EXECUTORS: dict[str, Callable[..., Any]] = {
    "github.list_repositories": repos.execute,
    "github.get_pull_request": pulls.execute_get,
    "github.create_branch": branches.execute,
    "github.create_pull_request": pulls.execute_create,
    "github.comment": comments.execute,
    "github.checkout": checkout.execute,
    "github.commit": commit.execute,
    "github.push": push.execute,
    "github.list_issues": issues.execute_list,
    "github.get_issue": issues.execute_get,
    "github.create_issue": issues.execute_create,
    "github.update_issue": issues.execute_update,
    "github.update_pull_request": pull_update.execute,
    "github.list_pull_requests": pull_list.execute,
    "github.request_connection": connection.execute,
    "github.list_identities": listing.execute,
    "github.merge_base": merge_base.execute,
    "github.list_reviews": ops.executor(merge.list_reviews),
    "github.get_checks": ops.executor(merge.get_checks),
    "github.merge": ops.executor(merge.merge),
}
