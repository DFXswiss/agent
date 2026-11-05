"""Delete never-started held workflow runs.

This module never approves, cancels, reruns, or dispatches anything.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .pr_lifecycle import _workflow_run_never_started
from .workflow_approval import (
    _commit_shas,
    _list_runs,
    _pending,
    _timestamp,
)

MAX_DELETIONS = 20
MAX_EXAMINED = 2 * MAX_DELETIONS
# Two sweeps per hour stay under about half the shared hourly request budget.
# Each pass is capped at 250 API requests after the three configuration reads.
# The cap is checked before every request, including each pagination page;
# the client's own GET retries are outside this wrapper's count.
MAX_REQUESTS = 250
MIN_AGE_S = 3600
ROTATION_PERIOD_S = 1800


class _BudgetExhausted(Exception):
    pass


class _BudgetedApi:
    """Count the drain's API calls without weakening its fresh checks."""

    def __init__(self, api: Any) -> None:
        self._api = api
        self.used = 0

    def _check(self) -> None:
        if self.used >= MAX_REQUESTS:
            raise _BudgetExhausted

    def request(self, *args: Any, **kwargs: Any) -> Any:
        self._check()
        self.used += 1
        return self._api.request(*args, **kwargs)

    def get_json(self, *args: Any, **kwargs: Any) -> Any:
        self._check()
        self.used += 1
        return self._api.get_json(*args, **kwargs)

    def get_optional(self, *args: Any, **kwargs: Any) -> Any:
        self._check()
        self.used += 1
        return self._api.get_optional(*args, **kwargs)

    def paginate(self, path: str, **kwargs: Any) -> Any:
        from .a38_guard import GitHubApi

        return GitHubApi.paginate(self, path, **kwargs)


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
    api: Any,
    repo: str,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    from .a38_guard import (
        GuardError,
        PR_GUARD_CONFIG_PATH,
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
        "requests": 0,
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
    api = _BudgetedApi(api)
    try:
        return _drain_enabled(api, repo, result, dry_run=dry_run, now=now)
    except _BudgetExhausted:
        result["aborted"] = "budget"
        return result
    finally:
        result["requests"] = api.used


def _drain_enabled(
    api: Any,
    repo: str,
    result: dict[str, Any],
    *,
    dry_run: bool,
    now: datetime | None,
) -> dict[str, Any]:
    from .a38_guard import (
        GuardError,
        PullCommitsTruncatedError,
        _is_rate_limit_response,
        _open_pull_numbers_for_head,
        _validate_head_branch,
        _validate_repo,
        _validate_sha,
    )

    def _detail_shas(
        detail: Mapping[str, Any], detail_head: Mapping[str, Any]
    ) -> tuple[str, str | None]:
        detail_head_sha = detail_head.get("sha")
        try:
            detail_head_sha = _validate_sha(
                detail_head_sha, "open pull request head sha"
            )
        except GuardError as exc:
            raise GuardError("open pull request head sha missing") from exc
        detail_merge_sha = detail.get("merge_commit_sha")
        if detail_merge_sha is not None:
            try:
                detail_merge_sha = _validate_sha(
                    detail_merge_sha, "open pull request merge sha"
                )
            except GuardError as exc:
                raise GuardError("open pull request merge sha invalid") from exc
        return detail_head_sha, detail_merge_sha

    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=MIN_AGE_S)
    slot = int(now.timestamp()) // ROTATION_PERIOD_S
    commit_cache: dict[int, set[str] | None] = {}
    # Keep the scan-time check broader as a cheap conservative first filter.
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
        try:
            head_sha = _validate_sha(run.get("head_sha"), "workflow run head sha")
        except GuardError:
            continue
        if head_sha in protected:
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
        candidate = dict(run)
        candidate["head_sha"] = head_sha
        if _run_is_protected(api, repo, candidate, protection, commit_cache):
            continue
        candidates.append(candidate)

    candidates.sort(
        key=lambda run: (_timestamp(run.get("created_at")), run["id"])
    )
    # Report the full backlog before the never-started proof; deleted and
    # planned remain bounded because they represent the per-sweep write budget.
    result["candidates"] = len(candidates)
    if candidates:
        offset = (slot * MAX_EXAMINED) % len(candidates)
        # Each window remains oldest-first. For a stable candidate set,
        # consecutive slots advance its start by MAX_EXAMINED modulo the set
        # size, so no non-deletable prefix can monopolise the examined slots
        # and every candidate is reached within
        # ceil(len(candidates) / MAX_EXAMINED) consecutive slots.
        candidates = candidates[offset:] + candidates[:offset]
    # MAX_REQUESTS enforces the pass-wide bound. The fresh check stays because
    # protection must be current immediately before an irreversible delete;
    # the cap, rather than a weaker check, bounds its cost.
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
            try:
                current_sha = _validate_sha(
                    current_sha, "workflow run head sha"
                )
            except GuardError:
                continue
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
                or current_sha != candidate["head_sha"]
                or not isinstance(current_head_repo, str)
                or current_head_repo == ""
                or current_head_repo.lower() == repo.lower()
                or not isinstance(current_head_branch, str)
                or current_head_branch == ""
            ):
                continue
            try:
                _validate_repo(current_head_repo)
                _validate_head_branch(current_head_branch)
            except GuardError:
                continue

            # Approval can release only a run whose fork and branch match the
            # pull, so only pulls from this candidate's head can protect here.
            detail_protected = False
            for number in _open_pull_numbers_for_head(
                api, repo, current_head_repo, current_head_branch
            ):
                detail = api.get_json(f"/repos/{repo}/pulls/{number}")
                if not isinstance(detail, Mapping):
                    raise GuardError("open pull request head sha missing")
                detail_head = detail.get("head")
                if not isinstance(detail_head, Mapping):
                    raise GuardError("open pull request head sha missing")
                detail_head_repository = detail_head.get("repo")
                detail_head_repo = None
                if detail_head_repository is not None:
                    if not isinstance(detail_head_repository, Mapping):
                        raise GuardError(
                            "open pull request head repository invalid"
                        )
                    detail_head_repo = detail_head_repository.get("full_name")
                    try:
                        detail_head_repo = _validate_repo(detail_head_repo)
                    except GuardError as exc:
                        raise GuardError(
                            "open pull request head repository invalid"
                        ) from exc
                detail_head_branch = detail_head.get("ref")
                try:
                    detail_head_branch = _validate_head_branch(
                        detail_head_branch
                    )
                except GuardError as exc:
                    raise GuardError(
                        "open pull request head ref missing"
                    ) from exc
                if detail_head_repo is None:
                    detail_head_sha, detail_merge_sha = _detail_shas(
                        detail, detail_head
                    )
                    if (
                        detail_head_sha == current_sha
                        or detail_merge_sha == current_sha
                    ):
                        detail_protected = True
                        break
                    continue
                if (
                    detail_head_repo.lower() != current_head_repo.lower()
                    or detail_head_branch != current_head_branch
                ):
                    continue
                detail_head_sha, detail_merge_sha = _detail_shas(
                    detail, detail_head
                )
                # GitHub may report null while recomputing the merge commit.
                if detail_merge_sha is None:
                    detail_protected = True
                    break
                try:
                    detail_commits = {
                        sha.lower() for sha in _commit_shas(api, repo, number)
                    }
                except PullCommitsTruncatedError:
                    detail_protected = True
                    break
                if (
                    detail_head_sha == current_sha
                    or detail_merge_sha == current_sha
                    or current_sha in detail_commits
                ):
                    detail_protected = True
                    break
            if detail_protected:
                continue

            final_status, final = api.get_optional(reread_path)
            if final_status == 404:
                continue
            if final_status != 200:
                raise GuardError(
                    f"GitHub API HTTP {final_status} for {reread_path}"
                )
            final_sha = (
                final.get("head_sha") if isinstance(final, Mapping) else None
            )
            try:
                final_sha = _validate_sha(final_sha, "workflow run head sha")
            except GuardError:
                continue
            final_head_repository = (
                final.get("head_repository")
                if isinstance(final, Mapping)
                else None
            )
            final_head_repo = (
                final_head_repository.get("full_name")
                if isinstance(final_head_repository, Mapping)
                else None
            )
            final_head_branch = (
                final.get("head_branch")
                if isinstance(final, Mapping)
                else None
            )
            if (
                not isinstance(final, Mapping)
                or final.get("id") != run_id
                or not _pending(final)
                or final_sha != current_sha
                or not isinstance(final_head_repo, str)
                or final_head_repo == ""
                or final_head_repo.lower() == repo.lower()
                or final_head_branch != current_head_branch
            ):
                continue
            final_never_started = _workflow_run_never_started(
                api, repo, candidate, missing_ok=True
            )
            if not final_never_started:
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
