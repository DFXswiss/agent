"""Delete never-started held workflow runs.

This module never approves, cancels, reruns, or dispatches anything.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .pr_lifecycle import _workflow_run_never_started
from .workflow_approval import _commit_shas, _list_runs, _pending, _timestamp

MAX_DELETIONS = 20
MAX_EXAMINED = 2 * MAX_DELETIONS
MIN_AGE_S = 3600


def _open_pull_protection(
    api: Any,
    repo: str,
) -> tuple[set[str], dict[tuple[str, str], list[int]]]:
    from .a38_guard import GuardError, _validate_sha

    protected: set[str] = set()
    pulls_by_branch: dict[tuple[str, str], list[int]] = {}
    for pull in api.paginate(f"/repos/{repo}/pulls?state=open"):
        head = pull.get("head") if isinstance(pull, Mapping) else None
        head_sha = head.get("sha") if isinstance(head, Mapping) else None
        try:
            head_sha = _validate_sha(head_sha, "open pull request head sha")
        except GuardError as exc:
            raise GuardError("open pull request head sha missing") from exc
        protected.add(head_sha)

        number = pull.get("number") if isinstance(pull, Mapping) else None
        if type(number) is not int or number <= 0:
            raise GuardError("open pull request number missing")

        head_repo = head.get("repo")
        if head_repo is not None:
            if not isinstance(head_repo, Mapping):
                raise GuardError("open pull request head repository invalid")
            full_name = head_repo.get("full_name")
            if not isinstance(full_name, str) or full_name == "":
                raise GuardError("open pull request head repository invalid")
            head_ref = head.get("ref")
            if not isinstance(head_ref, str) or head_ref == "":
                raise GuardError("open pull request head ref missing")
            pulls_by_branch.setdefault(
                (full_name.lower(), head_ref), []
            ).append(number)

        merge_sha = pull.get("merge_commit_sha")
        if merge_sha is None:
            detail = api.get_json(f"/repos/{repo}/pulls/{number}")
            if not isinstance(detail, Mapping):
                raise GuardError("open pull request merge sha invalid")
            detail_head = detail.get("head")
            detail_head_sha = (
                detail_head.get("sha")
                if isinstance(detail_head, Mapping)
                else None
            )
            try:
                detail_head_sha = _validate_sha(
                    detail_head_sha, "open pull request head sha"
                )
            except GuardError as exc:
                raise GuardError("open pull request head sha missing") from exc
            protected.add(detail_head_sha)
            merge_sha = detail.get("merge_commit_sha")
        if merge_sha is not None:
            try:
                merge_sha = _validate_sha(
                    merge_sha, "open pull request merge sha"
                )
            except GuardError as exc:
                raise GuardError("open pull request merge sha invalid") from exc
            protected.add(merge_sha)

    return protected, pulls_by_branch


def _run_is_protected(
    api: Any,
    repo: str,
    run: Mapping[str, Any],
    protection: tuple[set[str], dict[tuple[str, str], list[int]]],
    commit_cache: dict[int, set[str] | None],
) -> bool:
    from .a38_guard import PullCommitsTruncatedError

    protected, pulls_by_branch = protection
    head_sha = run.get("head_sha")
    if not isinstance(head_sha, str):
        return False
    head_sha = head_sha.lower()
    if head_sha in protected:
        return True
    head_repository = run.get("head_repository")
    head_repo = (
        head_repository.get("full_name")
        if isinstance(head_repository, Mapping)
        else None
    )
    head_branch = run.get("head_branch")
    if not isinstance(head_repo, str) or not isinstance(head_branch, str):
        return False
    for number in pulls_by_branch.get((head_repo.lower(), head_branch), []):
        if number not in commit_cache:
            try:
                commit_cache[number] = {
                    sha.lower() for sha in _commit_shas(api, repo, number)
                }
            except PullCommitsTruncatedError:
                commit_cache[number] = None
        commits = commit_cache[number]
        if commits is None or head_sha in commits:
            return True
    return False


def drain_held_runs(
    api: Any, repo: str, *, dry_run: bool = False
) -> dict[str, Any]:
    from .a38_guard import (
        GuardError,
        PR_GUARD_CONFIG_PATH,
        _is_rate_limit_response,
        _parse_default_branch,
        _validate_sha,
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
    commit_cache: dict[int, set[str] | None] = {}
    protection = _open_pull_protection(api, repo)
    protected = protection[0]
    result["protected"] = len(protected)

    # A capped window only omits candidates; protection does not depend on it.
    runs = _list_runs(
        api,
        repo,
        {"event": "pull_request", "status": "action_required"},
        allow_truncated=True,
    )
    result["scanned"] = len(runs)
    candidates: list[Mapping[str, Any]] = []
    for run in runs:
        if not _pending(run):
            continue
        head_sha = run.get("head_sha")
        if (
            not isinstance(head_sha, str)
            or head_sha == ""
            or head_sha.lower() in protected
        ):
            continue
        head_repository = run.get("head_repository")
        if not isinstance(head_repository, Mapping):
            continue
        head_repo = head_repository.get("full_name")
        head_branch = run.get("head_branch")
        if (
            not isinstance(head_repo, str)
            or head_repo == ""
            or head_repo.lower() == repo.lower()
            or not isinstance(head_branch, str)
            or head_branch == ""
        ):
            continue
        if _timestamp(run.get("created_at")) >= cutoff:
            continue
        if _run_is_protected(api, repo, run, protection, commit_cache):
            continue
        candidates.append(run)

    candidates.sort(
        key=lambda run: (_timestamp(run.get("created_at")), run["id"])
    )
    # Report the full backlog before the never-started proof; deleted and
    # planned remain bounded because they represent the per-sweep write budget.
    result["candidates"] = len(candidates)
    # Request bound, excluding the API client's own GET retries: 3 gate reads
    # (repository, default-branch commit, config),
    # at most 10 run-list pages twice after a shift, scan-time P0 open-pull
    # reads, and up to 3M commit pages for M distinct branch-matching pulls.
    # For E examined and proven subset K:
    # 23 + P0 + 3M + E + sum(i in K)(2 + Pi + 4mi).
    # P is pull pages plus null-merge GETs; mi is matching pulls, each costing
    # one detail GET and up to 3 commit pages. With P=1, M=m=1, E=K=40:
    # 23+1+3+40+40*(2+1+4)=347 requests in the all-proven worst case; a run
    # on a branch with no open pull (m=0) costs 4, about 190 for a full pass.
    # Two sweeps an hour stay well inside the budget approval shares, and 40
    # examined runs an hour outpace a backlog that grows a few runs an hour.
    # MAX_EXAMINED bounds jobs reads and rechecks even if every examined run skips.
    examined = 0
    try:
        for candidate in candidates:
            completed = result["planned"] if dry_run else result["deleted"]
            if len(completed) >= MAX_DELETIONS:
                break
            if examined >= MAX_EXAMINED:
                break
            examined += 1
            if not _workflow_run_never_started(
                api, repo, candidate, missing_ok=True
            ):
                continue
            run_id = candidate["id"]
            reread_path = f"/repos/{repo}/actions/runs/{run_id}"
            reread_status, current = api.get_optional(reread_path)
            if reread_status == 404:
                continue
            if reread_status != 200:
                raise GuardError(
                    f"GitHub API HTTP {reread_status} for {reread_path}"
                )
            current_sha = (
                current.get("head_sha") if isinstance(current, Mapping) else None
            )
            current_head_repository = (
                current.get("head_repository")
                if isinstance(current, Mapping)
                else None
            )
            current_head_repo = (
                current_head_repository.get("full_name")
                if isinstance(current_head_repository, Mapping)
                else None
            )
            current_head_branch = (
                current.get("head_branch")
                if isinstance(current, Mapping)
                else None
            )
            if (
                not isinstance(current, Mapping)
                or current.get("id") != run_id
                or not _pending(current)
                or not isinstance(current_sha, str)
                or current_sha == ""
                or not isinstance(current_head_repo, str)
                or current_head_repo == ""
                or current_head_repo.lower() == repo.lower()
                or not isinstance(current_head_branch, str)
                or current_head_branch == ""
            ):
                continue
            current_protection = _open_pull_protection(api, repo)
            branch_key = (current_head_repo.lower(), current_head_branch)
            detail_protected = False
            for number in current_protection[1].get(branch_key, []):
                detail = api.get_json(f"/repos/{repo}/pulls/{number}")
                if not isinstance(detail, Mapping):
                    raise GuardError("open pull request head sha missing")
                detail_head = detail.get("head")
                detail_head_sha = (
                    detail_head.get("sha")
                    if isinstance(detail_head, Mapping)
                    else None
                )
                try:
                    detail_head_sha = _validate_sha(
                        detail_head_sha, "open pull request head sha"
                    )
                except GuardError as exc:
                    raise GuardError(
                        "open pull request head sha missing"
                    ) from exc
                detail_merge_sha = detail.get("merge_commit_sha")
                if detail_merge_sha is not None:
                    try:
                        detail_merge_sha = _validate_sha(
                            detail_merge_sha, "open pull request merge sha"
                        )
                    except GuardError as exc:
                        raise GuardError(
                            "open pull request merge sha invalid"
                        ) from exc
                if detail_head_sha == current_sha.lower() or (
                    detail_merge_sha is not None
                    and detail_merge_sha == current_sha.lower()
                ):
                    detail_protected = True
                    break
            if detail_protected:
                continue
            if _run_is_protected(api, repo, current, current_protection, {}):
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
    except GuardError as exc:
        if result["deleted"]:
            result["aborted"] = "error"
            result["error"] = str(exc)
            return result
        raise
    return result
