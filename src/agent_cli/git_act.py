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


def _resolve_remote(cwd: str, runner: Runner) -> str:
    """Pick the remote to use: the sole remote, or 'origin' when there are several.

    Shared by the fresh-branch push (no @{upstream} yet) and, as a
    defense-in-depth check, by the existing-upstream push path — both must
    agree on which remote an unattended push is allowed to target.
    """
    remotes_done = runner(_git(cwd, "remote"))
    if remotes_done.returncode != 0:
        raise GitActError(_fail_detail(remotes_done, "git remote failed"))
    remotes = [r for r in remotes_done.stdout.splitlines() if r.strip()]
    if not remotes:
        raise GitActError("no remotes")
    if len(remotes) == 1:
        return remotes[0]
    if "origin" in remotes:
        return "origin"
    raise GitActError("ambiguous remotes (no origin)")


def _normalize_repo_identity(raw: str) -> str | None:
    """Normalize a git remote URL or org/repo string down to 'org/repo'."""
    s = raw.strip()
    if not s:
        return None
    if s.endswith(".git"):
        s = s[: -len(".git")]
    if "://" in s:
        after_scheme = s.split("://", 1)[1]
        parts = after_scheme.split("/")
        if len(parts) < 3 or not parts[1] or not parts[2]:
            return None
        return f"{parts[1]}/{parts[2]}"
    if "@" in s and ":" in s.rsplit("@", 1)[-1]:
        path = s.rsplit(":", 1)[-1]
        parts = path.split("/")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            return None
        return f"{parts[0]}/{parts[1]}"
    parts = s.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return f"{parts[0]}/{parts[1]}"


def _ensure_remote_matches_repo(
    cwd: str, runner: Runner, remote: str, expected_repo: str
) -> None:
    """Fail-closed when remote's push URL does not resolve to expected_repo."""
    completed = runner(_git(cwd, "remote", "get-url", "--push", remote))
    if completed.returncode != 0:
        raise GitActError(_fail_detail(completed, "git remote get-url failed"))
    url = completed.stdout.strip()
    got = _normalize_repo_identity(url)
    want = _normalize_repo_identity(expected_repo)
    if got is None or want is None or got != want:
        raise GitActError(
            f"remote {remote!r} push URL does not match expected repo {expected_repo!r}"
        )


def push_branch(
    *,
    cwd: str,
    runner: Runner,
    expected_branch: str | None = None,
    expected_repo: str | None = None,
) -> str:
    """Push the current branch if needed. Return HEAD sha (lowercase hex)."""
    completed = runner(_git(cwd, "rev-parse", "--abbrev-ref", "HEAD"))
    if completed.returncode != 0:
        raise GitActError(_fail_detail(completed, "git failed"))
    branch = completed.stdout.strip()
    if not branch:
        raise GitActError("empty branch name")
    if expected_branch is not None and branch != expected_branch:
        raise GitActError(
            f"on branch {branch!r} but task expects {expected_branch!r} — refusing to push"
        )
    if branch in PROTECTED:
        raise GitActError(f"refusing to push protected branch {branch}")

    completed = runner(_git(cwd, "status", "--porcelain", "--untracked-files=all"))
    if completed.returncode != 0:
        raise GitActError(_fail_detail(completed, "git failed"))
    if completed.stdout.strip():
        raise GitActError("uncommitted changes")

    completed = runner(_git(cwd, "rev-parse", "--abbrev-ref", "@{upstream}"))
    if completed.returncode != 0 or not completed.stdout.strip():
        if expected_branch is None:
            # No identity to check against: keep the original fail-closed
            # behavior for ordinary (non-error-fix) tasks — a human must
            # push manually rather than this silently auto-setting upstream.
            raise GitActError("no upstream")
        # Fresh branch (e.g. error-fix checkout -B): set upstream on first push.
        remote = _resolve_remote(cwd, runner)
        if expected_repo is not None:
            _ensure_remote_matches_repo(cwd, runner, remote, expected_repo)
        merge_ref = f"refs/heads/{branch}"
        merge_short = branch
        if merge_short in PROTECTED:
            raise GitActError(f"upstream tracks protected branch {merge_short}")
        completed = runner(
            _git(cwd, "push", "--set-upstream", "--", remote, f"HEAD:{merge_ref}")
        )
        if completed.returncode != 0:
            raise GitActError(_fail_detail(completed, "git push failed"))
    else:
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
        if expected_branch is not None and merge_short != expected_branch:
            raise GitActError(
                f"branch {branch!r} tracks {merge_short!r} but task expects "
                f"{expected_branch!r} — refusing to push"
            )
        expected_remote = _resolve_remote(cwd, runner)
        if remote != expected_remote:
            raise GitActError(
                f"branch {branch!r} tracks remote {remote!r} but expected "
                f"{expected_remote!r} — refusing to push"
            )
        if expected_repo is not None:
            _ensure_remote_matches_repo(cwd, runner, remote, expected_repo)

        completed = runner(_git(cwd, "fetch", "--", remote))
        if completed.returncode != 0:
            raise GitActError(_fail_detail(completed, "git fetch failed"))

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
        if behind < 0 or ahead < 0:
            raise GitActError(f"bad rev-list count: {count_raw!r}")
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


def measure_mergeable(
    *, cwd: str, runner: Runner, expected_head: str | None = None
) -> str:
    """Return a short evidence string when the current branch PR is MERGEABLE
    and every GitHub check is SUCCESS (or there are no checks). Else raise GitActError."""
    _ = cwd  # gh argv has no -C; cwd is inherited from _exec_argv
    completed = runner(
        ["gh", "pr", "view", "--json", "mergeable,state,url,number,headRefOid"]
    )
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
    oid = view.get("headRefOid")
    if not isinstance(oid, str) or oid == "":
        raise GitActError("pr view missing headRefOid")
    oid = oid.lower()
    if expected_head:
        want = expected_head.lower()
        if want != oid and not (7 <= len(want) < len(oid) and oid.startswith(want)):
            raise GitActError(f"pr head {oid} does not match {want}")

    completed = runner(
        ["gh", "pr", "checks", str(number), "--json", "name,state"]
    )
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
