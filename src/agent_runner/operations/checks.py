"""Formal checks of every operation type on plaintext (03-api.md section 9; 05-runner.md section 12 step 3; ADR-012).

`check(request)` repeats the parameter schema of `contracts/ops/<type>.params.json` (closed objects, integers never
booleans; the guest's `local_check:"passed"` is only a hint) and adds the rules a schema cannot express, in this order
(the first failing step gives the code):

1. type known (`unknown_type`), parameters (`params_invalid` with every JSON pointer, at most 32): required and
   unknown keys, types, lengths in code points, `repo`, branch rules (`refs/heads/` removed first), `ref` (branch or
   tag name or 7-40 hex SHA), full SHAs, PR numbers, single-line title, model ID syntax, label names, pages, the
   question of `human.question` / `human.approval` (1-4000 characters); `github_identity`, `author`, `committer` and
   `co_authors` are identity and user IDs (canonical UUIDs, never free text); `github.commit`: a message without a
   trailer-like line `co-authored-by:`, `signed-off-by:` or `committed-by:` (any case, leading spaces allowed) and
   every co-author once (`params_invalid`, KOR-47); `github.update_issue` names at least one of `state`, `title`,
   `body`, `labels`, and `state_reason` only together with a fitting `state` (`reopened` with `open`,
   `completed`/`not_planned` with `closed`; `params_invalid`; the contract leaves this to the runner, KOR-25);
2. commit paths (`path_invalid`, with pointers): relative, no empty, `.`, `..` or `.git` segment;
3. repository allow-list of the workgroup settings when it is set (`repo_not_allowed`; every type with `repo`, so
   not `github.list_repositories` and `github.request_connection`; without a list every repository passes, KOR-30);
4. protected branches (`branch_protected`): the branch of `create_branch`, `commit`, `push` and the head of a pull
   request;
5. a GitHub identity may be available for the acting user (`github_not_connected`, every `github.*` type but
   `github.list_identities`; `credentials.acting`; which identity, and whether the acting user may use it, the
   pre-check decides, KOR-47);
6. `github.push`: the commit was created by an earlier `github.commit` of this task (`not_found`);
7. `subagent.request`: `model_id` is allowed for the parent session's access kind by the model catalog
   (`model_not_allowed`).

The result is the **effective** parameter set: branch names without `refs/heads/`, and `draft` of a pull request
taken from the settings' `pr_draft_default` when the session left it out. Checks that need GitHub or the workspace
(`repo_not_accessible`, commit snapshot, sizes, secret scan) are pre-checks of the executor packages
(`extensions.PRECHECKS`).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from agent_runner.model.selection import Catalog
from agent_runner.operations import rules
from agent_runner.operations.extensions import CheckFailed
from agent_runner.operations.settings import WorkgroupSettings
from agent_runner.sessions.records import LocalSession

MAX_POINTERS = 32
NOT_CONNECTED = "The person who gave this instruction has not connected GitHub."
MAX_NUMBER = 2_147_483_647
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
"""`contracts/common.json#/$defs/uuid` (UUIDv7, lower case)."""
TRAILER_RE = re.compile(r"^[ \t]*(co-authored-by|signed-off-by|committed-by):", re.IGNORECASE | re.MULTILINE)
"""A trailer-like line that would name an author (KOR-47, ADR-078 section 4)."""
MAX_CO_AUTHORS = 10
LABEL_RE = re.compile(r"^[^,\x00-\x1f\x7f]{1,100}$")
"""A label name of the issue types: no comma (GitHub joins label filters with commas), no control character."""
MAX_LABELS = 20
MAX_PER_PAGE = 100
MAX_PAGE = 1000
ISSUE_TITLE_MAX = 256
UPDATE_FIELDS = ("state", "title", "body", "labels")

Rule = Callable[[object], bool]


def _text(low: int, high: int) -> Rule:
    return lambda value: isinstance(value, str) and low <= len(value) <= high


def _number(value: object) -> bool:
    return type(value) is int and 1 <= value <= MAX_NUMBER


def _paths(value: object) -> bool:
    # the item rules beyond "a string" are step 2 (`path_invalid`)
    return (
        isinstance(value, list)
        and 1 <= len(value) <= rules.COMMIT_LIMITS["files"]
        and all(isinstance(item, str) for item in value)
    )


def _range(low: int, high: int) -> Rule:
    return lambda value: type(value) is int and low <= value <= high


def _one_of(*values: str) -> Rule:
    return lambda value: isinstance(value, str) and value in values


def _co_authors(value: object) -> bool:
    # duplicates are a rule of their own (`_commit_check`)
    return (
        isinstance(value, list)
        and 1 <= len(value) <= MAX_CO_AUTHORS
        and all(isinstance(item, str) and _UUID_RE.fullmatch(item) is not None for item in value)
    )


def _labels(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= MAX_LABELS
        and all(isinstance(item, str) and LABEL_RE.fullmatch(item) is not None for item in value)
    )


REPO: Rule = rules.repo_valid
BRANCH: Rule = rules.branch_valid
REF: Rule = rules.ref_valid
SHA: Rule = lambda value: isinstance(value, str) and rules.FULL_SHA_RE.fullmatch(value) is not None  # noqa: E731
BOOL: Rule = lambda value: isinstance(value, bool)  # noqa: E731
QUESTION: Rule = _text(1, rules.QUESTION_LIMITS["question_max_length"])
MODEL: Rule = lambda value: isinstance(value, str) and _MODEL_ID_RE.fullmatch(value) is not None  # noqa: E731
DIRECTION: Rule = _one_of("desc", "asc")
UUID: Rule = lambda value: isinstance(value, str) and _UUID_RE.fullmatch(value) is not None  # noqa: E731
_CWD_RE = re.compile(r"/[^\x00]{0,4095}", re.DOTALL)


@dataclass(frozen=True)
class TypeRules:
    fields: Mapping[str, Rule]
    required: tuple[str, ...]


TYPES: Mapping[str, TypeRules] = {
    "github.list_repositories": TypeRules({"github_identity": UUID}, ()),
    "github.checkout": TypeRules({"repo": REPO, "ref": REF, "github_identity": UUID}, ("repo",)),
    "github.get_pull_request": TypeRules(
        {"repo": REPO, "number": _number, "github_identity": UUID}, ("repo", "number")
    ),
    "github.create_branch": TypeRules(
        {"repo": REPO, "branch": BRANCH, "from": REF, "github_identity": UUID}, ("repo", "branch", "from")
    ),
    "github.commit": TypeRules(
        {
            "repo": REPO,
            "branch": BRANCH,
            "message": _text(1, rules.COMMIT_LIMITS["message_max_length"]),
            "paths": _paths,
            "github_identity": UUID,
            "author": UUID,
            "committer": UUID,
            "co_authors": _co_authors,
            "signing": _one_of("none", "key"),
        },
        ("repo", "branch", "message"),
    ),
    "github.push": TypeRules(
        {"repo": REPO, "branch": BRANCH, "commit_sha": SHA, "github_identity": UUID}, ("repo", "branch", "commit_sha")
    ),
    "github.create_pull_request": TypeRules(
        {
            "repo": REPO,
            "head": BRANCH,
            "base": BRANCH,
            "title": _text(1, rules.PULL_REQUEST_LIMITS["title_max_length"]),
            "body": _text(0, rules.PULL_REQUEST_LIMITS["body_max_length"]),
            "draft": BOOL,
            "github_identity": UUID,
        },
        ("repo", "head", "base", "title"),
    ),
    "github.comment": TypeRules(
        {
            "repo": REPO,
            "number": _number,
            "body": _text(1, rules.COMMENT_LIMITS["body_max_length"]),
            "github_identity": UUID,
        },
        ("repo", "number", "body"),
    ),
    "github.list_issues": TypeRules(
        {
            "repo": REPO,
            "state": _one_of("open", "closed", "all"),
            "labels": _labels,
            "sort": _one_of("created", "updated", "comments"),
            "direction": DIRECTION,
            "per_page": _range(1, MAX_PER_PAGE),
            "page": _range(1, MAX_PAGE),
            "github_identity": UUID,
        },
        ("repo",),
    ),
    "github.get_issue": TypeRules(
        {
            "repo": REPO,
            "number": _number,
            "comments_page": _range(1, MAX_PAGE),
            "comments_per_page": _range(1, MAX_PER_PAGE),
            "github_identity": UUID,
        },
        ("repo", "number"),
    ),
    "github.create_issue": TypeRules(
        {
            "repo": REPO,
            "title": _text(1, ISSUE_TITLE_MAX),
            "body": _text(0, rules.COMMENT_LIMITS["body_max_length"]),
            "labels": _labels,
            "github_identity": UUID,
        },
        ("repo", "title"),
    ),
    "github.update_issue": TypeRules(
        {
            "repo": REPO,
            "number": _number,
            "state": _one_of("open", "closed"),
            "state_reason": _one_of("completed", "not_planned", "reopened"),
            "title": _text(1, ISSUE_TITLE_MAX),
            "body": _text(0, rules.COMMENT_LIMITS["body_max_length"]),
            "labels": _labels,
            "github_identity": UUID,
        },
        ("repo", "number"),
    ),
    "github.list_pull_requests": TypeRules(
        {
            "repo": REPO,
            "state": _one_of("open", "closed", "all"),
            "base": BRANCH,
            "sort": _one_of("created", "updated"),
            "direction": DIRECTION,
            "per_page": _range(1, MAX_PER_PAGE),
            "page": _range(1, MAX_PAGE),
            "github_identity": UUID,
        },
        ("repo",),
    ),
    "github.request_connection": TypeRules({}, ()),
    "github.list_identities": TypeRules({}, ()),
    "subagent.request": TypeRules(
        {
            "purpose": _text(1, rules.SUBAGENT_LIMITS["purpose_max_length"]),
            "prompt": _text(1, rules.SUBAGENT_LIMITS["prompt_max_length"]),
            "model_id": MODEL,
            "github_identity": UUID,
        },
        ("purpose", "prompt"),
    ),
    "task.set_title": TypeRules({"title": rules.title_valid}, ("title",)),
    # docs/platform/spec/13-work-vm.md section 2 (ADR-059)
    "workvm.request": TypeRules(
        {"size": _one_of("standard", "large", "xlarge"), "scope": _one_of("task", "session")}, ()
    ),
    "workvm.release": TypeRules({"scope": _one_of("task", "session")}, ()),
    # docs/platform/spec/13-work-vm.md section 12 (ADR-080)
    "test.run": TypeRules(
        {
            "kind": _one_of("lint", "format", "unit", "integration", "security", "custom"),
            "command": _text(1, 65536),
            "cwd": lambda value: isinstance(value, str) and _CWD_RE.fullmatch(value) is not None,
            "timeout_seconds": _range(1, 86400),
            "size": _one_of("standard", "large", "xlarge"),
        },
        ("kind", "command"),
    ),
    "human.question": TypeRules({"question": QUESTION}, ("question",)),
    "human.approval": TypeRules({"question": QUESTION}, ("question",)),
}
"""Parameter rules per type (`contracts/ops/<type>.params.json`; a test compares the key sets with the contracts)."""

CONNECTION_FREE = frozenset({"github.list_identities"})
"""`github.*` types that need no GitHub identity (step 5)."""

BRANCH_FIELDS: Mapping[str, tuple[str, ...]] = {
    "github.create_branch": ("branch",),
    "github.commit": ("branch",),
    "github.push": ("branch",),
    "github.create_pull_request": ("head", "base"),
    "github.list_pull_requests": ("base",),
}
PROTECTED_FIELD: Mapping[str, str] = {
    "github.create_branch": "branch",
    "github.commit": "branch",
    "github.push": "branch",
    "github.create_pull_request": "head",
}


@dataclass(frozen=True)
class CheckRequest:
    op_type: str
    params: Mapping[str, Any]
    session: LocalSession
    settings: WorkgroupSettings
    catalog: Catalog
    connected: bool
    """A GitHub identity may be available for the acting user on this device (`github.identity.available`); the
    pre-check's resolution decides which one (KOR-47)."""
    task_commits: frozenset[str]
    """Commit SHAs of the task's succeeded `github.commit` operations."""


def _pointer(name: str) -> str:
    return "/params/" + name.replace("~", "~0").replace("/", "~1")


def _schema(op_type: str, params: Mapping[str, Any]) -> None:
    found = TYPES.get(op_type)
    if found is None:
        raise CheckFailed("unknown_type", message="The operation type is not known.")
    bad = {name for name in params if name not in found.fields}
    bad |= {name for name in found.required if name not in params}
    bad |= {name for name, rule in found.fields.items() if name in params and not rule(params[name])}
    if bad:
        pointers = sorted(_pointer(name) for name in bad)[:MAX_POINTERS]
        raise CheckFailed(
            "params_invalid", pointers=pointers, message="The parameters do not match the operation type."
        )


def _paths_check(params: Mapping[str, Any]) -> None:
    paths = params.get("paths", [])
    bad = [f"/params/paths/{index}" for index, path in enumerate(paths) if not rules.path_valid(path)]
    if bad:
        raise CheckFailed("path_invalid", pointers=bad[:MAX_POINTERS], message="A path is not a valid relative path.")


def _commit_check(params: Mapping[str, Any]) -> None:
    """`github.commit`: no trailer-like line in the message, every co-author once (KOR-47, ADR-078 section 4)."""
    if TRAILER_RE.search(params["message"]):
        raise CheckFailed(
            "params_invalid",
            pointers=[_pointer("message")],
            message="The message must not contain Co-authored-by, Signed-off-by or Committed-by lines.",
        )
    co_authors = params.get("co_authors", [])
    if len(set(co_authors)) != len(co_authors):
        raise CheckFailed("params_invalid", pointers=[_pointer("co_authors")], message="A co-author is named twice.")


def _update_check(params: Mapping[str, Any]) -> None:
    """`github.update_issue` changes something (one of `UPDATE_FIELDS`), and `state_reason` comes with `state`."""
    if not any(name in params for name in UPDATE_FIELDS):
        raise CheckFailed(
            "params_invalid",
            pointers=sorted(_pointer(name) for name in UPDATE_FIELDS),
            message="The update names none of state, title, body and labels.",
        )
    if "state_reason" in params and "state" not in params:
        raise CheckFailed("params_invalid", pointers=[_pointer("state_reason")], message="state_reason needs state.")
    if "state_reason" in params and (params["state_reason"] == "reopened") != (params["state"] == "open"):
        raise CheckFailed(
            "params_invalid",
            pointers=[_pointer("state_reason")],
            message="state_reason reopened goes with state open, completed and not_planned with state closed.",
        )


def effective(op_type: str, params: Mapping[str, Any], settings: WorkgroupSettings) -> dict[str, Any]:
    """Branch names without `refs/heads/`; a pull request's missing `draft` from the settings."""
    out = dict(params)
    for name in BRANCH_FIELDS.get(op_type, ()):
        if name in out:
            out[name] = rules.branch_name(out[name])
    if op_type == "github.create_pull_request" and "draft" not in out:
        out["draft"] = settings.pr_draft_default
    return out


def check(request: CheckRequest) -> dict[str, Any]:
    """The effective parameters of a request that passes every formal check; `CheckFailed` otherwise."""
    op_type, params, settings = request.op_type, request.params, request.settings
    _schema(op_type, params)
    if op_type == "github.commit":
        _commit_check(params)
        _paths_check(params)
    if op_type == "github.update_issue":
        _update_check(params)
    out = effective(op_type, params, settings)
    github = op_type.startswith("github.")
    if "repo" in out and not settings.allows(out["repo"]):
        raise CheckFailed("repo_not_allowed", message="The repository is not allowed for this workgroup.")
    field = PROTECTED_FIELD.get(op_type)
    if field is not None and rules.protected(out[field], settings.protected_branches):
        raise CheckFailed("branch_protected", message="The branch is protected.")
    if github and op_type not in CONNECTION_FREE and not request.connected:
        raise CheckFailed("github_not_connected", message=NOT_CONNECTED)
    if op_type == "github.push" and out["commit_sha"] not in request.task_commits:
        raise CheckFailed("not_found", message="The commit was not created by a commit operation of this task.")
    if op_type == "subagent.request" and "model_id" in out:
        kind = request.session.model_access
        allowed = request.catalog.policy(kind).allowed if kind is not None else ()
        if out["model_id"] not in allowed:
            raise CheckFailed("model_not_allowed", message="The model is not allowed for this session.")
    return out
