"""Account-scoped GitHub transport for the comment-mandated conflict worker."""
from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

from .coordinator_common import CoordinatorError, Runner, gh_json, redact

RESULT_MARKER_PREFIX = "<!-- agent-conflicts:result:v1:"
MERGEABLE_ATTEMPTS = 4
MERGEABLE_SLEEP_SECONDS = 1.0


class ConflictTransientError(CoordinatorError):
    """An outside read or write failed; retry on a later tick."""


def _pages(data: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(data, list):
        raise ConflictTransientError(f"{label} did not return an array")
    if data and all(isinstance(page, list) for page in data):
        flat: list[Any] = [item for page in data for item in page]
    else:
        flat = list(data)
    if not all(isinstance(item, dict) for item in flat):
        raise ConflictTransientError(f"{label} returned an unexpected item")
    return flat


def _api(runner: Runner, argv: list[str], label: str) -> Any:
    try:
        return gh_json(runner, argv)
    except CoordinatorError as exc:
        raise ConflictTransientError(redact(str(exc))) from exc


def result_marker(comment_id: int) -> str:
    return f"{RESULT_MARKER_PREFIX}{comment_id} -->"


def pull_number_from_comment(comment: dict[str, Any]) -> int | None:
    """Return the pull/issue number encoded in a GitHub comment URL."""
    import re

    for key in ("issue_url", "html_url"):
        value = comment.get(key)
        if not isinstance(value, str):
            continue
        match = re.search(r"/(?:issues|pull|pulls)/(\d+)(?:$|[?#])", value)
        if match is not None:
            return int(match.group(1))
    return None


def list_repository_comments(runner: Runner, repo: str, *, since: str) -> list[dict[str, Any]]:
    """Read issue comments for one repository, bounded by the caller's `since`."""
    data = _api(
        runner,
        ["gh", "api", "--paginate", "--slurp", f"repos/{repo}/issues/comments?since={since}"],
        "repository comments",
    )
    return _pages(data, "repository comments")


def list_pull_comments(runner: Runner, repo: str, number: int) -> list[dict[str, Any]]:
    data = _api(
        runner,
        ["gh", "api", "--paginate", "--slurp", f"repos/{repo}/issues/{number}/comments"],
        "pull request comments",
    )
    return _pages(data, "pull request comments")


def read_pull(runner: Runner, repo: str, number: int, *, sleep=time.sleep) -> dict[str, Any]:
    """Read one pull request; bounded retries while mergeable is unknown."""
    last: dict[str, Any] | None = None
    for attempt in range(MERGEABLE_ATTEMPTS):
        data = _api(runner, ["gh", "api", f"repos/{repo}/pulls/{number}"], "pull request")
        if not isinstance(data, dict):
            raise ConflictTransientError("pull request lookup did not return an object")
        last = data
        if data.get("mergeable") is not None:
            return data
        if attempt + 1 < MERGEABLE_ATTEMPTS:
            sleep(MERGEABLE_SLEEP_SECONDS)
    assert last is not None
    return last


def read_optional_pull(runner: Runner, repo: str, number: int) -> dict[str, Any] | None:
    try:
        data = gh_json(runner, ["gh", "api", f"repos/{repo}/pulls/{number}"])
    except CoordinatorError as exc:
        text = str(exc).casefold()
        if "404" in text or "not found" in text:
            return None
        raise ConflictTransientError(redact(str(exc))) from exc
    if not isinstance(data, dict):
        raise ConflictTransientError("pull request lookup did not return an object")
    return data


def has_push_permission(runner: Runner, repo: str) -> bool:
    data = _api(runner, ["gh", "api", f"repos/{repo}"], "repository")
    if not isinstance(data, dict):
        raise ConflictTransientError("repository lookup did not return an object")
    permissions = data.get("permissions")
    return permissions.get("push") is True if isinstance(permissions, dict) else False


def read_branch_protection(runner: Runner, head_repo: str, head_ref: str) -> bool:
    """Return whether GitHub reports the head branch as protected."""
    encoded = quote(head_ref, safe="")
    data = _api(
        runner,
        ["gh", "api", f"repos/{head_repo}/branches/{encoded}"],
        "branch protection",
    )
    if not isinstance(data, dict):
        raise ConflictTransientError("branch protection lookup did not return an object")
    protected = data.get("protected")
    if not isinstance(protected, bool):
        raise ConflictTransientError("branch protection lookup is unreadable")
    return protected


def _comment_author(comment: dict[str, Any]) -> str | None:
    user = comment.get("user")
    return user.get("login") if isinstance(user, dict) and isinstance(user.get("login"), str) else None


def find_result_comment(
    runner: Runner, repo: str, number: int, *, login: str, marker: str
) -> dict[str, Any] | None:
    for comment in list_pull_comments(runner, repo, number):
        body = comment.get("body")
        if not isinstance(body, str) or not body.lstrip().startswith(marker):
            continue
        author = _comment_author(comment)
        if isinstance(author, str) and author.casefold() == login.casefold():
            return comment
    return None


def _post(runner: Runner, argv: list[str], label: str) -> None:
    try:
        completed = runner(argv)
    except OSError as exc:
        raise ConflictTransientError(redact(str(exc))) from exc
    if completed.returncode != 0:
        raise ConflictTransientError(redact(completed.stderr or completed.stdout or label))


def add_eyes(runner: Runner, repo: str, comment_id: int, *, login: str) -> None:
    try:
        data = gh_json(
            runner,
            ["gh", "api", "--paginate", "--slurp",
             f"repos/{repo}/issues/comments/{comment_id}/reactions"],
        )
    except CoordinatorError as exc:
        raise ConflictTransientError(redact(str(exc))) from exc
    reactions = _pages(data, "comment reactions")
    for reaction in reactions:
        if (
            _comment_author(reaction) is not None
            and _comment_author(reaction).casefold() == login.casefold()
            and str(reaction.get("content") or "").casefold() == "eyes"
        ):
            return
    _post(
        runner,
        ["gh", "api", "-X", "POST", f"repos/{repo}/issues/comments/{comment_id}/reactions",
         "-f", "content=eyes"],
        "comment reaction failed",
    )


def post_comment_once(
    runner: Runner,
    repo: str,
    number: int,
    *,
    login: str,
    comment_id: int,
    body: str,
) -> bool:
    """Post a marked comment unless the worker already posted that marker."""
    marker = result_marker(comment_id)
    if find_result_comment(runner, repo, number, login=login, marker=marker) is not None:
        return False
    _post(
        runner,
        ["gh", "api", "-X", "POST", f"repos/{repo}/issues/{number}/comments",
         "-f", f"body={marker}\n{redact(body, limit=8000)}"],
        "comment failed",
    )
    return True


__all__ = [
    "ConflictTransientError",
    "RESULT_MARKER_PREFIX",
    "add_eyes",
    "find_result_comment",
    "has_push_permission",
    "list_pull_comments",
    "list_repository_comments",
    "post_comment_once",
    "pull_number_from_comment",
    "read_branch_protection",
    "read_optional_pull",
    "read_pull",
    "result_marker",
]
