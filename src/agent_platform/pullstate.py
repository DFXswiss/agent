"""The fresh state of pull requests as a device reads it (KOR-48, ADR-084 section 2; device request
`github.pull_status`, `contracts/requests/github.pull_status.response.json`, 05-runner.md section 11; twin of
`web/src/core/pulls/`).

`valid_response` is the tool's check of a device's `done` answer (`devicereq.response_body`): exactly the keys of the
contract, 1 to 50 pull requests, each either found (`title`, `state` `open`/`closed`, `draft`, `merged`, `mergeable`
true, false or null, `mergeable_state`, `head_ref`, `head_sha`, `head_repo` or null, `base_ref`, the `url`,
`updated_at`; the limits of `web/src/core/requests/bodies.ts`) or not found with one of `NOT_FOUND_CODES`. Anything
else is not shown as content.

`merge_state` is what a person reads off the state: `merged`, `closed`, `checking` while GitHub has not computed
`mergeable` yet (null: never "no conflict"), `conflict` for `mergeable: false` alone (`mergeable_state` is display
only, a draft says `draft` even with a conflict) and `clean`; None for a pull request the device did not find.
`fixable` is a conflict on an open pull request whose head lives in the same repository: a fork's branch has other
rights and "Fix conflicts" is not offered for it (ADR-084 section 4).
"""

from __future__ import annotations

import re
from typing import Any

PULLS_MAX = 50
REPO_RE = re.compile(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}")
"""`owner/name` as GitHub spells it (03-api.md section 9)."""
SHA_RE = re.compile(r"[0-9a-f]{40}")
REF_RE = re.compile(r"[!-~]{1,255}")
MERGEABLE_STATE_RE = re.compile(r"[a-z_]{1,32}")
URL_RE = re.compile(r"https://[!-~]{1,2040}|http://(127\.0\.0\.1|localhost)(:[0-9]{1,5})?(/[!-~]{0,2000})?")
"""The address of a pull request (`https`, or a loopback `http` of a GitHub double), as `devicereq.URL_RE`."""
TIME_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z")
TITLE_MAX = 1024
NOT_FOUND_CODES = ("not_found", "repo_not_accessible", "repo_not_allowed", "upstream_error")
FOUND_KEYS = frozenset(
    {
        "repo",
        "number",
        "found",
        "title",
        "state",
        "draft",
        "merged",
        "mergeable",
        "mergeable_state",
        "head_ref",
        "head_sha",
        "head_repo",
        "base_ref",
        "url",
        "updated_at",
    }
)
MISSING_KEYS = frozenset({"repo", "number", "found", "code"})
MERGE_STATES = ("merged", "closed", "checking", "conflict", "clean")


def _number(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _match(value: object, pattern: re.Pattern[str], most: int = 2048) -> bool:
    return isinstance(value, str) and len(value) <= most and pattern.fullmatch(value) is not None


def _found(item: dict[str, Any]) -> bool:
    head_repo = item.get("head_repo")
    return (
        set(item) == FOUND_KEYS
        and isinstance(item["title"], str)
        and len(item["title"]) <= TITLE_MAX
        and item["state"] in ("open", "closed")
        and isinstance(item["draft"], bool)
        and isinstance(item["merged"], bool)
        and (item["mergeable"] is None or isinstance(item["mergeable"], bool))
        and _match(item["mergeable_state"], MERGEABLE_STATE_RE)
        and _match(item["head_ref"], REF_RE)
        and _match(item["head_sha"], SHA_RE)
        and (head_repo is None or _match(head_repo, REPO_RE))
        and _match(item["base_ref"], REF_RE)
        and _match(item["url"], URL_RE)
        and _match(item["updated_at"], TIME_RE)
    )


def valid_pull(item: object) -> bool:
    """One `pull_state` of the contract."""
    if not isinstance(item, dict):
        return False
    if not _match(item.get("repo"), REPO_RE) or not _number(item.get("number")):
        return False
    if item.get("found") is True:
        return _found(item)
    return item.get("found") is False and set(item) == MISSING_KEYS and item.get("code") in NOT_FOUND_CODES


def valid_response(body: dict[str, Any]) -> bool:
    """A device's final `{step: "done", pulls: [pull_state]}` (1 to `PULLS_MAX`)."""
    pulls = body.get("pulls")
    return (
        body.get("step") == "done"
        and set(body) == {"step", "pulls"}
        and isinstance(pulls, list)
        and 1 <= len(pulls) <= PULLS_MAX
        and all(valid_pull(item) for item in pulls)
    )


def merge_state(pull: dict[str, Any]) -> str | None:
    """One of `MERGE_STATES` for a found pull request, None otherwise (module docstring)."""
    if pull.get("found") is not True:
        return None
    if pull["merged"]:
        return "merged"
    if pull["state"] == "closed":
        return "closed"
    mergeable = pull["mergeable"]
    return "checking" if mergeable is None else "clean" if mergeable else "conflict"


def is_fork(pull: dict[str, Any]) -> bool:
    """The head lives in another repository (or in one that is gone)."""
    head = pull.get("head_repo")
    return not isinstance(head, str) or head.lower() != str(pull["repo"]).lower()


def fixable(pull: dict[str, Any]) -> bool:
    """A conflict that "Fix conflicts" may address: open, `mergeable: false`, head in the same repository."""
    return merge_state(pull) == "conflict" and not is_fork(pull)
