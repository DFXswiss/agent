"""Formal checks of every operation type on plaintext (03-api.md section 9; 05-runner.md section 12 step 3; ADR-012).

`check(request)` repeats the parameter schema of `contracts/ops/<type>.params.json` (closed objects, integers never
booleans; the guest's `local_check:"passed"` is only a hint) and adds the rules a schema cannot express, in this order
(the first failing step gives the code):

1. type known (`unknown_type`), parameters (`params_invalid` with every JSON pointer, at most 32): required and
   unknown keys, types, lengths in code points, `repo`, branch rules (`refs/heads/` removed first), `ref` (branch or
   tag name or 7-40 hex SHA), full SHAs, PR numbers, single-line title, model ID syntax;
2. commit paths (`path_invalid`, with pointers): relative, no empty, `.`, `..` or `.git` segment;
3. repository allow-list of the workgroup settings (`repo_not_allowed`; not for `github.list_repositories`);
4. protected branches (`branch_protected`): the branch of `create_branch`, `commit`, `push` and the head of a pull
   request;
5. the owner's GitHub connection (`github_not_connected`, every `github.*` type);
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
MAX_NUMBER = 2_147_483_647
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")

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


REPO: Rule = rules.repo_valid
BRANCH: Rule = rules.branch_valid
REF: Rule = rules.ref_valid
SHA: Rule = lambda value: isinstance(value, str) and rules.FULL_SHA_RE.fullmatch(value) is not None  # noqa: E731
BOOL: Rule = lambda value: isinstance(value, bool)  # noqa: E731
MODEL: Rule = lambda value: isinstance(value, str) and _MODEL_ID_RE.fullmatch(value) is not None  # noqa: E731


@dataclass(frozen=True)
class TypeRules:
    fields: Mapping[str, Rule]
    required: tuple[str, ...]


TYPES: Mapping[str, TypeRules] = {
    "github.list_repositories": TypeRules({}, ()),
    "github.checkout": TypeRules({"repo": REPO, "ref": REF}, ("repo",)),
    "github.get_pull_request": TypeRules({"repo": REPO, "number": _number}, ("repo", "number")),
    "github.create_branch": TypeRules({"repo": REPO, "branch": BRANCH, "from": REF}, ("repo", "branch", "from")),
    "github.commit": TypeRules(
        {
            "repo": REPO,
            "branch": BRANCH,
            "message": _text(1, rules.COMMIT_LIMITS["message_max_length"]),
            "paths": _paths,
        },
        ("repo", "branch", "message"),
    ),
    "github.push": TypeRules({"repo": REPO, "branch": BRANCH, "commit_sha": SHA}, ("repo", "branch", "commit_sha")),
    "github.create_pull_request": TypeRules(
        {
            "repo": REPO,
            "head": BRANCH,
            "base": BRANCH,
            "title": _text(1, rules.PULL_REQUEST_LIMITS["title_max_length"]),
            "body": _text(0, rules.PULL_REQUEST_LIMITS["body_max_length"]),
            "draft": BOOL,
        },
        ("repo", "head", "base", "title"),
    ),
    "github.comment": TypeRules(
        {"repo": REPO, "number": _number, "body": _text(1, rules.COMMENT_LIMITS["body_max_length"])},
        ("repo", "number", "body"),
    ),
    "subagent.request": TypeRules(
        {
            "purpose": _text(1, rules.SUBAGENT_LIMITS["purpose_max_length"]),
            "prompt": _text(1, rules.SUBAGENT_LIMITS["prompt_max_length"]),
            "model_id": MODEL,
        },
        ("purpose", "prompt"),
    ),
    "task.set_title": TypeRules({"title": rules.title_valid}, ("title",)),
}
"""Parameter rules per type (`contracts/ops/<type>.params.json`; a test compares the key sets with the contracts)."""

BRANCH_FIELDS: Mapping[str, tuple[str, ...]] = {
    "github.create_branch": ("branch",),
    "github.commit": ("branch",),
    "github.push": ("branch",),
    "github.create_pull_request": ("head", "base"),
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
    """The task owner's GitHub credential is present on this device."""
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


def effective(op_type: str, params: Mapping[str, Any], settings: WorkgroupSettings) -> dict[str, Any]:
    """Branch names without `refs/heads/`; a pull request's missing `draft` from the settings."""
    out = dict(params)
    for name in BRANCH_FIELDS.get(op_type, ()):
        out[name] = rules.branch_name(out[name])
    if op_type == "github.create_pull_request" and "draft" not in out:
        out["draft"] = settings.pr_draft_default
    return out


def check(request: CheckRequest) -> dict[str, Any]:
    """The effective parameters of a request that passes every formal check; `CheckFailed` otherwise."""
    op_type, params, settings = request.op_type, request.params, request.settings
    _schema(op_type, params)
    if op_type == "github.commit":
        _paths_check(params)
    out = effective(op_type, params, settings)
    github = op_type.startswith("github.")
    if github and op_type != "github.list_repositories" and not settings.allows(out["repo"]):
        raise CheckFailed("repo_not_allowed", message="The repository is not allowed for this workgroup.")
    field = PROTECTED_FIELD.get(op_type)
    if field is not None and rules.protected(out[field], settings.protected_branches):
        raise CheckFailed("branch_protected", message="The branch is protected.")
    if github and not request.connected:
        raise CheckFailed("github_not_connected", message="The task owner has not connected GitHub.")
    if op_type == "github.push" and out["commit_sha"] not in request.task_commits:
        raise CheckFailed("not_found", message="The commit was not created by a commit operation of this task.")
    if op_type == "subagent.request" and "model_id" in out:
        kind = request.session.model_access
        allowed = request.catalog.policy(kind).allowed if kind is not None else ()
        if out["model_id"] not in allowed:
            raise CheckFailed("model_not_allowed", message="The model is not allowed for this session.")
    return out
