"""Git push and GitHub mergeability checks for `agent run`."""

from __future__ import annotations

import json
import re
from collections.abc import Callable

from .runtime import Completed

Runner = Callable[[list[str]], Completed]

PROTECTED = frozenset({"develop", "main", "master"})

_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


class GitActError(Exception):
    """Fail-loud; cmd_run maps this to die()."""


def _git(cwd: str, *parts: str) -> list[str]:
    return ["git", "-C", cwd, *parts]


def _fail_detail(completed: Completed, fallback: str) -> str:
    detail = (completed.stderr or completed.stdout or fallback).strip()
    return detail or fallback


def push_branch(*, cwd: str, runner: Runner) -> str:
    """Push the current branch if needed. Return HEAD sha (lowercase hex)."""
    completed = runner(_git(cwd, "rev-parse", "--abbrev-ref", "HEAD"))
    if completed.returncode != 0:
        raise GitActError(_fail_detail(completed, "git failed"))
    branch = completed.stdout.strip()
    if not branch:
        raise GitActError("empty branch name")
    if branch in PROTECTED:
        raise GitActError(f"refusing to push protected branch {branch}")

    completed = runner(_git(cwd, "status", "--porcelain"))
    if completed.returncode != 0:
        raise GitActError(_fail_detail(completed, "git failed"))
    if completed.stdout.strip():
        raise GitActError("uncommitted changes")

    completed = runner(_git(cwd, "rev-parse", "--abbrev-ref", "@{upstream}"))
    if completed.returncode != 0:
        raise GitActError("no upstream")
    upstream = completed.stdout.strip()
    if not upstream:
        raise GitActError("no upstream")
    short = upstream.rsplit("/", 1)[-1]
    if short in PROTECTED:
        raise GitActError(f"upstream tracks protected branch {short}")

    completed = runner(_git(cwd, "config", "--get", f"branch.{branch}.remote"))
    if completed.returncode != 0 or not completed.stdout.strip():
        raise GitActError("no upstream remote")
    remote = completed.stdout.strip()
    completed = runner(_git(cwd, "config", "--get", f"branch.{branch}.merge"))
    if completed.returncode != 0 or not completed.stdout.strip():
        raise GitActError("no upstream merge ref")
    merge_ref = completed.stdout.strip()
    if not merge_ref.startswith("refs/heads/"):
        raise GitActError(f"unexpected merge ref {merge_ref!r}")
    merge_short = merge_ref[len("refs/heads/") :]
    if merge_short in PROTECTED:
        raise GitActError(f"upstream tracks protected branch {merge_short}")

    completed = runner(
        _git(cwd, "rev-list", "--left-right", "--count", "@{upstream}...HEAD")
    )
    if completed.returncode != 0:
        raise GitActError(_fail_detail(completed, "git failed"))
    count_raw = completed.stdout.strip()
    parts = count_raw.replace("\t", " ").split()
    if len(parts) != 2:
        raise GitActError(f"bad rev-list count: {count_raw!r}")
    try:
        behind = int(parts[0])
        ahead = int(parts[1])
    except ValueError as exc:
        raise GitActError(f"bad rev-list count: {count_raw!r}") from exc
    if behind > 0:
        raise GitActError("branch is behind upstream")
    if ahead > 0:
        completed = runner(
            _git(cwd, "push", "--", remote, f"HEAD:{merge_ref}")
        )
        if completed.returncode != 0:
            raise GitActError(_fail_detail(completed, "git push failed"))

    completed = runner(_git(cwd, "rev-parse", "HEAD"))
    if completed.returncode != 0:
        raise GitActError(_fail_detail(completed, "git failed"))
    sha = completed.stdout.strip()
    if not _SHA_RE.fullmatch(sha):
        raise GitActError(f"bad HEAD sha: {sha!r}")
    return sha.lower()


def measure_mergeable(*, cwd: str, runner: Runner) -> str:
    """Return a short evidence string when the current branch PR is MERGEABLE
    and every GitHub check is SUCCESS (or there are no checks). Else raise GitActError."""
    _ = cwd  # gh argv has no -C; cwd is inherited from _exec_argv
    completed = runner(["gh", "pr", "view", "--json", "mergeable,state,url,number"])
    if completed.returncode != 0:
        raise GitActError(_fail_detail(completed, "gh failed"))
    raw = completed.stdout.strip()
    if raw == "":
        raise GitActError("gh returned empty output")
    try:
        view = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GitActError("gh returned invalid JSON") from exc
    if not isinstance(view, dict):
        raise GitActError("gh pr view is not a JSON object")
    mergeable = view.get("mergeable")
    state = view.get("state")
    state_ok = isinstance(state, str) and state.upper() == "OPEN"
    if mergeable != "MERGEABLE" or not state_ok:
        raise GitActError(f"mergeable={mergeable!r} state={state!r}")
    number = view.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise GitActError("pr view missing number")

    completed = runner(["gh", "pr", "checks", "--json", "name,state"])
    if completed.returncode != 0:
        raise GitActError(_fail_detail(completed, "gh failed"))
    raw = completed.stdout.strip()
    if raw == "":
        raise GitActError("gh returned empty output")
    try:
        checks = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GitActError("gh returned invalid JSON") from exc
    if not isinstance(checks, list):
        raise GitActError("gh pr checks is not a JSON array")
    for item in checks:
        if not isinstance(item, dict):
            raise GitActError("check entry is not an object")
        name = item.get("name")
        if not isinstance(name, str) or name == "":
            raise GitActError("check missing name")
        check_state = item.get("state")
        if str(check_state or "").upper() != "SUCCESS":
            raise GitActError(f"check {name} is {check_state}")

    return f"mergeable number={number} checks=ok"
