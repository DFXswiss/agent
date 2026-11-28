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
   every co-author once (`params_invalid`, KOR-47); `merge` (only `true`) without `paths`, `decisions` only with
   `merge`, each decided path once (`params_invalid`) and valid (`path_invalid`, KOR-48); `github.update_issue`
   names at least one of `state`, `title`, `body`, `labels`, and `state_reason` only together with a fitting
   `state` (`reopened` with `open`, `completed`/`not_planned` with `closed`; `params_invalid`; the contract leaves
   this to the runner, KOR-25); `github.update_pull_request` names at least one of `title`, `body`, `draft`,
   `labels` (`params_invalid`, HOT-30; it has no field that merges, closes or approves);
2. commit paths (`path_invalid`, with pointers): relative, no empty, `.`, `..` or `.git` segment;
3. repository allow-list of the workgroup settings when it is set (`repo_not_allowed`; every type with `repo`, so
   not `github.list_repositories` and `github.request_connection`; without a list every repository passes, KOR-30);
4. protected branches (`branch_protected`): the branch of `create_branch`, `commit`, `push` and the head of a pull
   request;
5. a GitHub identity may be available for the acting user (`github_not_connected`, every `github.*` type but
   `github.list_identities`; `credentials.acting`; which identity, and whether the acting user may use it, the
   pre-check decides, KOR-47);
6. `github.push`: the commit was created by an earlier `github.commit` of this task (`not_found`);
7. `subagent.request`: `model_id` is allowed by the model catalog for at least one access kind
   (`model_not_allowed`); which access serves it - possibly another one than the parent's - and whether the owner's
   preference, settings and the task's consent allow that access, the subagent area decides at the start
   (`subagents.controller`, HOT-31). Whether the `session_id` of `subagent.instruct` / `subagent.stop` names a
   running child of the session is the subagent area's pre-check (`subagents/target_checks.py`).

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

from agent_runner.model.catalog import ACCESS_KINDS
from agent_runner.model.selection import Catalog
from agent_runner.operations import rules
from agent_runner.operations.extensions import CheckFailed
from agent_runner.operations.settings import WorkgroupSettings
from agent_runner.profiles.model import NAME_RE as PROFILE_NAME_RE
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
MAX_DECISIONS = 1000
EVIDENCE_ITEMS = 50
"""Items of `review.verdict` `evidence` (one message, ADR-094 section 4)."""
EVIDENCE_REF_LENGTH = 1000
CHOICES = ("head", "base", "deleted", "edited")
ISSUE_TITLE_MAX = 256
UPDATE_FIELDS = ("state", "title", "body", "labels")
PULL_UPDATE_FIELDS = ("title", "body", "draft", "labels")

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


def _decisions(value: object) -> bool:
    # paths beyond "a string" and duplicates are rules of their own (`_merge_check`)
    return (
        isinstance(value, list)
        and 1 <= len(value) <= MAX_DECISIONS
        and all(
            isinstance(item, dict)
            and set(item) == {"path", "choice"}
            and isinstance(item["path"], str)
            and item["choice"] in CHOICES
            for item in value
        )
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
PROFILE: Rule = lambda value: isinstance(value, str) and PROFILE_NAME_RE.fullmatch(value) is not None  # noqa: E731
DIRECTION: Rule = _one_of("desc", "asc")
EVIDENCE_KINDS = frozenset({"operation", "test_run", "security_report", "url", "note"})
FINDING_LEVELS = frozenset({"critical", "high", "medium", "low", "info"})
_TOOL_RE = re.compile(r"[A-Za-z0-9._+-]{1,64}")
_RUN_ID_RE = re.compile(r"tr-[0-9a-f]{12}")


def _evidence(value: object) -> bool:
    """`review.verdict` `evidence`: at most 50 `{kind, ref}` (one message, ADR-094 section 4)."""
    return (
        isinstance(value, list)
        and len(value) <= EVIDENCE_ITEMS
        and all(
            isinstance(item, dict)
            and set(item) == {"kind", "ref"}
            and item["kind"] in EVIDENCE_KINDS
            and isinstance(item["ref"], str)
            and 1 <= len(item["ref"]) <= EVIDENCE_REF_LENGTH
            for item in value
        )
    )


def _findings(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == FINDING_LEVELS
        and all(type(count) is int and count >= 0 for count in value.values())
    )


EVIDENCE: Rule = _evidence
FINDINGS: Rule = _findings
TOOL: Rule = lambda value: isinstance(value, str) and _TOOL_RE.fullmatch(value) is not None  # noqa: E731
RUN_ID: Rule = lambda value: isinstance(value, str) and _RUN_ID_RE.fullmatch(value) is not None  # noqa: E731
UUID: Rule = lambda value: isinstance(value, str) and _UUID_RE.fullmatch(value) is not None  # noqa: E731
_CWD_RE = re.compile(r"/[^\x00]{0,4095}", re.DOTALL)
_BROWSER_URL_RE = re.compile(r"https?://[^\x00-\x20\x7f]{1,4088}")
_TIME_RE = re.compile(
    r"[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\.[0-9]{3}Z"
)
_MAILBOX_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
_MAIL_TEXT_RE = re.compile(r"[ !#-\[\]-~]{1,200}")
_MAIL_ID_RE = re.compile(r"(inbox|spam):[1-9][0-9]{0,9}")


def _matches(pattern: re.Pattern[str]) -> Rule:
    return lambda value: isinstance(value, str) and pattern.fullmatch(value) is not None


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
            "merge": lambda value: value is True,
            "decisions": _decisions,
        },
        ("repo", "branch", "message"),
    ),
    "github.merge_base": TypeRules(
        {"repo": REPO, "number": _number, "discard": BOOL, "github_identity": UUID}, ("repo", "number")
    ),
    # ADR-094 section 4 (KOR-51)
    "github.list_reviews": TypeRules({"repo": REPO, "number": _number, "github_identity": UUID}, ("repo", "number")),
    "github.get_checks": TypeRules({"repo": REPO, "ref": REF, "github_identity": UUID}, ("repo", "ref")),
    "github.merge": TypeRules(
        {
            "repo": REPO,
            "number": _number,
            "head_sha": SHA,
            "method": _one_of("merge", "squash", "rebase"),
            "github_identity": UUID,
        },
        ("repo", "number", "head_sha"),
    ),
    "review.verdict": TypeRules(
        {
            "repo": REPO,
            "number": _number,
            "head_sha": SHA,
            "outcome": _one_of("passed", "rejected"),
            "summary": _text(1, 4000),
            "evidence": EVIDENCE,
        },
        ("repo", "number", "head_sha", "outcome", "summary"),
    ),
    "security.report": TypeRules(
        {
            "repo": REPO,
            "ref": REF,
            "tool": TOOL,
            "passed": BOOL,
            "findings": FINDINGS,
            "run_id": RUN_ID,
            "summary": _text(1, 4000),
        },
        ("repo", "ref", "tool", "passed", "findings"),
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
    # HOT-30: a pull request's title, text, draft state and labels; no state, merge or review field
    "github.update_pull_request": TypeRules(
        {
            "repo": REPO,
            "number": _number,
            "title": _text(1, rules.PULL_REQUEST_LIMITS["title_max_length"]),
            "body": _text(0, rules.PULL_REQUEST_LIMITS["body_max_length"]),
            "draft": BOOL,
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
            "profile": PROFILE,
        },
        ("purpose", "prompt"),
    ),
    # ADR-088 section 1: whether `session_id` names a running child of the session is the area's pre-check
    "subagent.instruct": TypeRules(
        {"session_id": UUID, "prompt": _text(1, rules.SUBAGENT_LIMITS["prompt_max_length"])}, ("session_id", "prompt")
    ),
    "subagent.stop": TypeRules({"session_id": UUID}, ("session_id",)),
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
    # docs/platform/spec/13-work-vm.md section 13.1 (ADR-085)
    "browser.open": TypeRules(
        {
            "url": lambda value: isinstance(value, str) and _BROWSER_URL_RE.fullmatch(value) is not None,
            "idle_seconds": _range(60, 3600),
        },
        (),
    ),
    "browser.close": TypeRules({}, ()),
    "human.question": TypeRules({"question": QUESTION}, ("question",)),
    "human.approval": TypeRules({"question": QUESTION}, ("question",)),
    # ADR-099 section 4
    "mail.find": TypeRules(
        {
            "mailbox": _matches(_MAILBOX_RE),
            "from": _matches(_MAIL_TEXT_RE),
            "subject": _matches(_MAIL_TEXT_RE),
            "since": _matches(_TIME_RE),
            "until": _matches(_TIME_RE),
            "folder": _one_of("inbox", "spam", "all"),
            "limit": _range(1, 20),
            "body": BOOL,
            "id": _matches(_MAIL_ID_RE),
            "wait_seconds": _range(0, 300),
        },
        ("mailbox",),
    ),
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


def _merge_check(params: Mapping[str, Any]) -> None:
    """`github.commit` with `merge`: no `paths` (a partial snapshot would revert the base outside them), `decisions`
    only with `merge`, every decision path valid and once (ADR-084 section 4)."""
    decisions = params.get("decisions", [])
    if "merge" in params and "paths" in params:
        raise CheckFailed("params_invalid", pointers=[_pointer("paths")], message="paths cannot be used with merge.")
    if decisions and "merge" not in params:
        raise CheckFailed("params_invalid", pointers=[_pointer("decisions")], message="decisions need merge: true.")
    paths = [item["path"] for item in decisions]
    bad = [f"/params/decisions/{index}/path" for index, path in enumerate(paths) if not rules.path_valid(path)]
    if bad:
        raise CheckFailed("path_invalid", pointers=bad[:MAX_POINTERS], message="A path is not a valid relative path.")
    if len(set(paths)) != len(paths):
        raise CheckFailed("params_invalid", pointers=[_pointer("decisions")], message="A path is decided twice.")


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


def _pull_update_check(params: Mapping[str, Any]) -> None:
    """`github.update_pull_request` changes something (one of `PULL_UPDATE_FIELDS`, HOT-30)."""
    if not any(name in params for name in PULL_UPDATE_FIELDS):
        raise CheckFailed(
            "params_invalid",
            pointers=sorted(_pointer(name) for name in PULL_UPDATE_FIELDS),
            message="The update names none of title, body, draft and labels.",
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
        _merge_check(params)
    if op_type == "github.update_issue":
        _update_check(params)
    if op_type == "github.update_pull_request":
        _pull_update_check(params)
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
        catalog = request.catalog
        if request.session.model_access is None or not any(
            out["model_id"] in catalog.policy(kind).allowed for kind in ACCESS_KINDS
        ):
            raise CheckFailed("model_not_allowed", message="The model is not in the model catalog of any model access.")
    return out
