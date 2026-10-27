"""Delete never-started held workflow runs.

This module never approves, cancels, reruns, or dispatches anything.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .pr_lifecycle import _workflow_run_never_started
from .workflow_approval import _list_runs, _pending, _timestamp

MAX_DELETIONS = 50
MIN_AGE_S = 3600


def drain_held_runs(
    api: Any, repo: str, *, dry_run: bool = False
) -> dict[str, Any]:
    from .a38_guard import (
        GuardError,
        PR_GUARD_CONFIG_PATH,
        _is_rate_limit_response,
        _parse_default_branch,
        fetch_file_at_ref,
        resolve_default_branch_commit,
    )
    from .pr_guard_config import PrGuardConfigError, load_pr_guard_config

    result: dict[str, Any] = {
        "enabled": False,
        "scanned": 0,
        "protected": 0,
        "candidates": 0,
        "deleted": [],
        "planned": [],
        "aborted": None,
    }
    repository = api.get_json(f"/repos/{repo}")
    default_branch = _parse_default_branch(
        repository.get("default_branch") if isinstance(repository, Mapping) else None,
        required=True,
    )
    revision = resolve_default_branch_commit(api, repo, default_branch)
    raw = fetch_file_at_ref(api, repo, PR_GUARD_CONFIG_PATH, revision)
    if raw is None:
        return result
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GuardError(
            f"maintainer config error: {PR_GUARD_CONFIG_PATH} is not valid UTF-8: {exc}"
        ) from exc
    try:
        config = load_pr_guard_config(text)
    except PrGuardConfigError as exc:
        raise GuardError(
            f"maintainer config error: invalid {PR_GUARD_CONFIG_PATH} on "
            f"trusted revision {revision}: {exc}"
        ) from exc
    drain = config.get("held_run_drain")
    if not isinstance(drain, Mapping) or drain.get("enabled") is not True:
        return result

    result["enabled"] = True
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=MIN_AGE_S)
    protected: set[str] = set()
    for pull in api.paginate(f"/repos/{repo}/pulls?state=open"):
        head = pull.get("head") if isinstance(pull, Mapping) else None
        head_sha = head.get("sha") if isinstance(head, Mapping) else None
        if not isinstance(head_sha, str) or head_sha == "":
            raise GuardError("open pull request head sha missing")
        protected.add(head_sha)
        merge_sha = pull.get("merge_commit_sha")
        if isinstance(merge_sha, str) and merge_sha != "":
            protected.add(merge_sha)
    result["protected"] = len(protected)

    runs = _list_runs(
        api,
        repo,
        {"event": "pull_request", "status": "action_required"},
    )
    result["scanned"] = len(runs)
    candidates: list[Mapping[str, Any]] = []
    for run in runs:
        if not _pending(run):
            continue
        head_sha = run.get("head_sha")
        if not isinstance(head_sha, str) or head_sha == "" or head_sha in protected:
            continue
        head_repository = run.get("head_repository")
        if not isinstance(head_repository, Mapping):
            continue
        head_repo = head_repository.get("full_name")
        if (
            not isinstance(head_repo, str)
            or head_repo == ""
            or head_repo.lower() == repo.lower()
        ):
            continue
        if _timestamp(run.get("created_at")) >= cutoff:
            continue
        if not _workflow_run_never_started(api, repo, run):
            continue
        candidates.append(run)

    candidates.sort(
        key=lambda run: (_timestamp(run.get("created_at")), run["id"])
    )
    # Report the full backlog for observability; deleted and planned remain
    # bounded because they represent the per-sweep write budget.
    result["candidates"] = len(candidates)
    # Each deletion costs a re-read plus the delete, and each candidate costs a
    # jobs read. Fifty deletions bound a pass at roughly 160 requests, allowing
    # two sweeps an hour to drain up to 100 holds while preserving the shared
    # installation budget for workflow approval.
    for candidate in candidates:
        completed = result["planned"] if dry_run else result["deleted"]
        if len(completed) >= MAX_DELETIONS:
            break
        run_id = candidate["id"]
        reread_path = f"/repos/{repo}/actions/runs/{run_id}"
        reread_status, current = api.get_optional(reread_path)
        if reread_status == 404:
            continue
        if reread_status != 200:
            raise GuardError(f"GitHub API HTTP {reread_status} for {reread_path}")
        current_sha = current.get("head_sha") if isinstance(current, Mapping) else None
        current_head_repository = (
            current.get("head_repository") if isinstance(current, Mapping) else None
        )
        current_head_repo = (
            current_head_repository.get("full_name")
            if isinstance(current_head_repository, Mapping)
            else None
        )
        if (
            not isinstance(current, Mapping)
            or current.get("id") != run_id
            or not _pending(current)
            or not isinstance(current_sha, str)
            or current_sha == ""
            or current_sha in protected
            or not isinstance(current_head_repo, str)
            or current_head_repo == ""
            or current_head_repo.lower() == repo.lower()
        ):
            continue
        if dry_run:
            result["planned"].append(run_id)
            continue
        status, data, headers = api.request(
            "DELETE", f"/repos/{repo}/actions/runs/{run_id}", retry=False
        )
        if status in {200, 204}:
            result["deleted"].append(run_id)
            continue
        if status == 404:
            continue
        if _is_rate_limit_response(status, data, headers):
            result["aborted"] = "rate-limited"
            return result
        if status in {401, 403}:
            result["aborted"] = "forbidden"
            return result
        raise GuardError(f"held run delete HTTP {status}")
    return result
