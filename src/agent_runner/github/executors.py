"""Executors of the GitHub area, parts I and II (`<area>/executors.py:EXECUTORS`, ADR-027 section 4; 05-runner.md
section 12.1): REST only, with the task owner's credential; checkout, commit and push (RUN-10) in their own modules."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_runner.github import branches, checkout, comments, commit, pulls, push, repos

EXECUTORS: dict[str, Callable[..., Any]] = {
    "github.list_repositories": repos.execute,
    "github.get_pull_request": pulls.execute_get,
    "github.create_branch": branches.execute,
    "github.create_pull_request": pulls.execute_create,
    "github.comment": comments.execute,
    "github.checkout": checkout.execute,
    "github.commit": commit.execute,
    "github.push": push.execute,
}
