"""Account-scoped GitHub transport for the comment-mandated conflict worker."""
from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import quote

from .conflict_mandate_config import validate_repository_name
from .conflict_mandate_state import parse_time
from .coordinator_common import (
    CoordinatorError,
    Runner,
    gh_json,
    redact,
    repository_push_permission,
)
from .store import StoreError

RESULT_MARKER_PREFIX = "<!-- agent-conflicts:result:v1:"
MERGEABLE_ATTEMPTS = 4
MERGEABLE_SLEEP_SECONDS = 1.0
_COMMENT_NUMBER_PATTERN = r"/(?:issues|pull|pulls)/([+-]?\d+)(?:$|[?#])"


class ConflictTransientError(CoordinatorError):
    """An outside read or write failed; retry on a later tick."""


class ConflictHeadRepositoryGone(CoordinatorError):
    """The pull request head repository was deleted."""

    def __init__(self, state: str) -> None:
        super().__init__("head repository is gone")
        self.state = state


def _validate_comment(comment: Any, label: str) -> dict[str, Any]:
    if not isinstance(comment, dict):
        raise ConflictTransientError(f"{label} returned an unexpected item")
    comment_id = comment.get("id")
    if (
        isinstance(comment_id, bool)
        or not isinstance(comment_id, int)
        or comment_id <= 0
    ):
        raise ConflictTransientError(f"{label} returned an unreadable comment id")
    if not isinstance(comment.get("body"), str):
        raise ConflictTransientError(f"{label} returned an unreadable comment body")
    user = comment.get("user")
    if not isinstance(user, dict) or not isinstance(user.get("login"), str):
        raise ConflictTransientError(f"{label} returned an unreadable comment author")
    if not isinstance(user.get("type"), str):
        raise ConflictTransientError(f"{label} returned an unreadable comment author")
    created_at = comment.get("created_at")
    if not isinstance(created_at, str) or parse_time(created_at) is None:
        raise ConflictTransientError(f"{label} returned an unreadable comment creation time")
    updated_at = comment.get("updated_at")
    if not isinstance(updated_at, str) or parse_time(updated_at) is None:
        raise ConflictTransientError(f"{label} returned an unreadable comment update time")
    numbers: dict[str, int] = {}
    if "html_url" not in comment:
        raise ConflictTransientError(f"{label} returned an unreadable comment URL")
    for key in ("html_url", "issue_url"):
        if key == "issue_url" and key not in comment:
            continue
        value = comment[key]
        if not isinstance(value, str):
            raise ConflictTransientError(f"{label} returned an unreadable comment URL")
        match = re.search(_COMMENT_NUMBER_PATTERN, value)
        if match is None:
            raise ConflictTransientError(f"{label} returned an unreadable comment URL")
        number = int(match.group(1))
        if number <= 0:
            raise ConflictTransientError(f"{label} returned an unreadable comment URL")
        numbers[key] = number
    if len(set(numbers.values())) > 1:
        raise ConflictTransientError(f"{label} returned inconsistent comment URLs")
    return comment


def _validate_reaction(reaction: Any, label: str) -> dict[str, Any]:
    if not isinstance(reaction, dict):
        raise ConflictTransientError(f"{label} returned an unexpected item")
    user = reaction.get("user")
    if not isinstance(user, dict) or not isinstance(user.get("login"), str):
        raise ConflictTransientError(f"{label} returned an unreadable reaction author")
    if not isinstance(reaction.get("content"), str):
        raise ConflictTransientError(f"{label} returned unreadable reaction content")
    return reaction


def _pages(
    data: Any,
    label: str,
    *,
    comments: bool = False,
    reactions: bool = False,
) -> list[dict[str, Any]]:
    if not isinstance(data, list):
        raise ConflictTransientError(f"{label} did not return an array")
    if data and all(isinstance(page, list) for page in data):
        flat: list[Any] = [item for page in data for item in page]
    else:
        flat = list(data)
    if not all(isinstance(item, dict) for item in flat):
        raise ConflictTransientError(f"{label} returned an unexpected item")
    if comments:
        return [_validate_comment(item, label) for item in flat]
    if reactions:
        return [_validate_reaction(item, label) for item in flat]
    return flat


def _validate_pull(data: Any, label: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ConflictTransientError(f"{label} lookup did not return an object")
    state = data.get("state")
    if not isinstance(state, str) or state not in {"open", "closed"}:
        raise ConflictTransientError(f"{label} state is unreadable")
    head = data.get("head")
    base = data.get("base")
    if not isinstance(head, dict) or not isinstance(base, dict):
        raise ConflictTransientError(f"{label} branch fields are unreadable")
    for field, value in (
        ("head.sha", head.get("sha")),
        ("head.ref", head.get("ref")),
        ("base.ref", base.get("ref")),
        ("base.sha", base.get("sha")),
    ):
        if not isinstance(value, str):
            raise ConflictTransientError(f"{label} {field} is unreadable")
        if (
            field.endswith(".sha")
            and re.fullmatch(r"[0-9a-fA-F]{40}", value) is None
        ):
            raise ConflictTransientError(f"{label} {field} is unreadable")
        if field.endswith(".ref") and not value:
            raise ConflictTransientError(f"{label} {field} is unreadable")
    if "repo" not in head:
        raise ConflictTransientError(f"{label} head repository is unreadable")
    head_repo = head.get("repo")
    if head_repo is None:
        raise ConflictHeadRepositoryGone(state)
    full_name = head_repo.get("full_name") if isinstance(head_repo, dict) else None
    try:
        validate_repository_name(full_name)
    except StoreError:
        raise ConflictTransientError(f"{label} head repository is unreadable")
    if "mergeable" not in data:
        raise ConflictTransientError(f"{label} mergeable state is unreadable")
    mergeable = data.get("mergeable")
    if mergeable is not None and not isinstance(mergeable, bool):
        raise ConflictTransientError(f"{label} mergeable state is unreadable")
    return data


def _api(runner: Runner, argv: list[str], label: str) -> Any:
    try:
        return gh_json(runner, argv)
    except UnicodeError as exc:
        raise ConflictTransientError(
            f"{label} returned output that is not valid UTF-8"
        ) from exc
    except CoordinatorError as exc:
        raise ConflictTransientError(redact(str(exc))) from exc


def result_marker(comment_id: int) -> str:
    return f"{RESULT_MARKER_PREFIX}{comment_id} -->"


def pull_number_from_comment(comment: dict[str, Any]) -> int | None:
    """Return the pull/issue number encoded in the comment's HTML URL."""
    numbers: list[int] = []
    for key in ("html_url", "issue_url"):
        if key == "issue_url" and key not in comment:
            continue
        if key not in comment:
            return None
        value = comment[key]
        if not isinstance(value, str):
            return None
        match = re.search(_COMMENT_NUMBER_PATTERN, value)
        if match is None:
            return None
        number = int(match.group(1))
        if number <= 0:
            return None
        numbers.append(number)
    if not numbers or len(set(numbers)) > 1:
        return None
    return numbers[0]


def list_repository_comments(runner: Runner, repo: str, *, since: str) -> list[dict[str, Any]]:
    """Read issue comments for one repository, bounded by the caller's `since`."""
    data = _api(
        runner,
        ["gh", "api", "--paginate", "--slurp", f"repos/{repo}/issues/comments?since={since}"],
        "repository comments",
    )
    return _pages(data, "repository comments", comments=True)


def list_pull_comments(runner: Runner, repo: str, number: int) -> list[dict[str, Any]]:
    data = _api(
        runner,
        ["gh", "api", "--paginate", "--slurp", f"repos/{repo}/issues/{number}/comments"],
        "pull request comments",
    )
    return _pages(data, "pull request comments", comments=True)


def read_pull(runner: Runner, repo: str, number: int, *, sleep=time.sleep) -> dict[str, Any]:
    """Read one pull request; bounded retries while mergeable is unknown."""
    last: dict[str, Any] | None = None
    for attempt in range(MERGEABLE_ATTEMPTS):
        data = _api(runner, ["gh", "api", f"repos/{repo}/pulls/{number}"], "pull request")
        _validate_pull(data, "pull request")
        last = data
        if data.get("mergeable") is not None:
            return data
        if attempt + 1 < MERGEABLE_ATTEMPTS:
            sleep(MERGEABLE_SLEEP_SECONDS)
    assert last is not None
    return last


def read_optional_pull(runner: Runner, repo: str, number: int) -> dict[str, Any]:
    data = _api(
        runner,
        ["gh", "api", f"repos/{repo}/pulls/{number}"],
        "pull request",
    )
    return _validate_pull(data, "pull request")


def has_push_permission(runner: Runner, repo: str) -> bool:
    try:
        push = repository_push_permission(runner, repo)
    except UnicodeError as exc:
        raise ConflictTransientError(
            "repository permissions returned output that is not valid UTF-8"
        ) from exc
    except (CoordinatorError, OSError) as exc:
        raise ConflictTransientError(redact(str(exc))) from exc
    if push is None:
        raise ConflictTransientError("repository permissions are unreadable")
    return push


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


def read_remote_head(runner: Runner, head_repo: str, head_ref: str) -> str:
    """Return the publication branch head sha through the GitHub API."""
    encoded = quote(head_ref, safe="")
    data = _api(
        runner,
        ["gh", "api", f"repos/{head_repo}/git/ref/heads/{encoded}"],
        "publication branch",
    )
    if not isinstance(data, dict):
        raise ConflictTransientError("publication branch lookup did not return an object")
    obj = data.get("object")
    sha = obj.get("sha") if isinstance(obj, dict) else None
    if not isinstance(sha, str) or re.fullmatch(r"[0-9a-fA-F]{40}", sha) is None:
        raise ConflictTransientError("publication branch head is unreadable")
    return sha.lower()


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
    except UnicodeError as exc:
        raise ConflictTransientError(
            f"{label} returned output that is not valid UTF-8"
        ) from exc
    except OSError as exc:
        raise ConflictTransientError(redact(str(exc))) from exc
    if completed.returncode != 0:
        raise ConflictTransientError(redact(completed.stderr or completed.stdout or label))


def add_eyes(runner: Runner, repo: str, comment_id: int, *, login: str) -> None:
    data = _api(
        runner,
        ["gh", "api", "--paginate", "--slurp",
         f"repos/{repo}/issues/comments/{comment_id}/reactions"],
        "comment reactions",
    )
    reactions = _pages(data, "comment reactions", reactions=True)
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
    "ConflictHeadRepositoryGone",
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
    "read_remote_head",
    "result_marker",
]
