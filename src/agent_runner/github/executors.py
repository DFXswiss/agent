"""Executors of the GitHub area, parts I and II (`<area>/executors.py:EXECUTORS`, ADR-027 section 4; 05-runner.md
section 12.1): REST only, with the acting user's credential (`ops.actor_client`, HOT-14); checkout, commit and push
(RUN-10) in their own modules; the issue types, the pull-request list and the connection request (KOR-25) in `issues`,
`pull_list` and `connection`."""

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
    pull_list,
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
    "github.list_pull_requests": pull_list.execute,
    "github.request_connection": connection.execute,
}
