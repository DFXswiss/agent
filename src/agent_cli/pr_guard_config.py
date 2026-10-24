"""Repository pr-guard configuration: schema, validation and scope evaluation.

Optional `.github/pr-guard.json` controls which PR target branches A38 enforces.
Exact target ``main`` is a built-in out-of-scope rule when it is **not** the
repository default branch (a release PR onto production). When the default
branch **is** ``main``, it is the integration branch and ``a38.enforce`` /
``a38.default`` apply, including listing ``main`` in ``enforce``. The default
branch name is never an implicit enforce by itself; it only locates this file
and, for exact ``main``, decides whether the built-in skip applies. Missing
configuration retains legacy enforce-all for every in-scope target. Malformed
configuration fails closed.
"""

from __future__ import annotations

import json
import math
import re
import urllib.parse
from typing import Any, Mapping

SCHEMA_ID = "pr-guard/v1"
CONFIG_PATH = ".github/pr-guard.json"
TOP_KEYS = frozenset({"schema", "a38"})
WORKFLOW_APPROVAL_KEYS = frozenset({"enabled", "workflows"})
ENVIRONMENT_APPROVAL_KEYS = frozenset({"enabled", "environment", "workflows"})
ONE_REVIEW_AUTHORS_KEY = "one_review_authors"
MAX_ONE_REVIEW_AUTHORS = 64
# GitHub login: 1–39 characters, letters, digits, and hyphens that are not
# leading, trailing, or doubled.
_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
WORKFLOW_PATH_RE = re.compile(r"^\.github/workflows/[A-Za-z0-9_-][A-Za-z0-9_.-]*\.(?:yml|yaml)$")
A38_KEYS = frozenset({"enforce", "exclude", "default"})
SCOPE_MODES = frozenset({"enforce", "exclude"})
MAX_BRANCH_LIST = 256
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,74}$")


class PrGuardConfigError(ValueError):
    """Invalid pr-guard configuration (schema, keys, overlap, or JSON)."""


class PrGuardLookupError(Exception):
    """The trusted default-branch file could not be read."""


def _object_pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise PrGuardConfigError(f"JSON contains duplicate key: {key}")
        out[key] = value
    return out


def _reject_nonfinite_constant(value: str) -> Any:
    raise PrGuardConfigError(f"JSON contains non-finite constant: {value}")


def _parse_finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise PrGuardConfigError(f"JSON contains non-finite number: {value}")
    return number


def _loads_json(text: str) -> Any:
    try:
        return json.loads(
            text,
            parse_constant=_reject_nonfinite_constant,
            parse_float=_parse_finite_float,
            object_pairs_hook=_object_pairs_no_duplicates,
        )
    except PrGuardConfigError:
        raise
    except json.JSONDecodeError as exc:
        raise PrGuardConfigError(f"JSON is invalid: {exc.msg}") from exc
    except (RecursionError, ValueError) as exc:
        raise PrGuardConfigError(f"JSON is invalid: {exc}") from exc


def _require_keys(obj: Mapping[str, Any], allowed: frozenset[str], label: str) -> None:
    keys = set(obj)
    extra = keys - allowed
    missing = allowed - keys
    if extra:
        raise PrGuardConfigError(f"{label} has unknown keys: {', '.join(sorted(extra))}")
    if missing:
        raise PrGuardConfigError(f"{label} missing keys: {', '.join(sorted(missing))}")


def validate_branch_name(value: Any, label: str) -> str:
    """Exact branch name: bounded ASCII, no globs, same limits as status contexts."""
    if not isinstance(value, str) or not value:
        raise PrGuardConfigError(f"{label} must be a non-empty string")
    if BRANCH_RE.fullmatch(value) is None:
        raise PrGuardConfigError(
            f"{label} must be a bounded branch name (1–75 characters)"
        )
    if ".." in value or "//" in value or value.endswith(("/", ".", ".lock")):
        raise PrGuardConfigError(f"{label} is an invalid branch name")
    if any(ch in value for ch in "*?[]{}\\"):
        raise PrGuardConfigError(f"{label} must be an exact branch name (no glob DSL)")
    return value


def _parse_branch_list(raw: Any, label: str) -> list[str]:
    if not isinstance(raw, list):
        raise PrGuardConfigError(f"{label} must be an array")
    if len(raw) > MAX_BRANCH_LIST:
        raise PrGuardConfigError(f"{label} exceeds {MAX_BRANCH_LIST} entries")
    out: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        name = validate_branch_name(item, f"{label}[{index}]")
        if name in seen:
            raise PrGuardConfigError(f"{label} contains duplicate branch {name!r}")
        seen.add(name)
        out.append(name)
    return out


def _parse_login_list(raw: Any, label: str) -> list[str]:
    if not isinstance(raw, list):
        raise PrGuardConfigError(f"{label} must be an array")
    if len(raw) > MAX_ONE_REVIEW_AUTHORS:
        raise PrGuardConfigError(f"{label} exceeds {MAX_ONE_REVIEW_AUTHORS} entries")
    out: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, str) or _LOGIN_RE.fullmatch(item) is None:
            raise PrGuardConfigError(
                f"{label}[{index}] must be a GitHub login "
                "(1–39 characters, letters, digits, single hyphens)"
            )
        folded = item.casefold()
        if folded in seen:
            raise PrGuardConfigError(f"{label} contains duplicate login {item!r}")
        seen.add(folded)
        out.append(folded)
    return out


def one_review_for(config: Mapping[str, Any] | None, login: str) -> bool:
    """Two reviews are the standard.

    True only when this pull opener is listed in ``one_review_authors``.
    A missing file, a missing key, or an empty list keeps two reviews required.
    The caller passes the trusted default-branch config. An entry that exists
    only on the pull request head does not count. Matching ignores case.
    """
    if not isinstance(config, Mapping) or not isinstance(login, str) or not login:
        return False
    authors = config.get(ONE_REVIEW_AUTHORS_KEY)
    if not isinstance(authors, list):
        return False
    folded = login.casefold()
    return any(isinstance(name, str) and name.casefold() == folded for name in authors)


def load_pr_guard_config(text: str) -> dict[str, Any]:
    """Validate pr-guard/v1 JSON and return a normalized object."""
    payload = _loads_json(text)
    if not isinstance(payload, dict):
        raise PrGuardConfigError("pr-guard config must be a JSON object")
    _require_keys(
        {
            k: v
            for k, v in payload.items()
            if k
            not in {
                "workflow_approval",
                "environment_approval",
                "lifecycle",
                ONE_REVIEW_AUTHORS_KEY,
            }
        },
        TOP_KEYS,
        "pr-guard config",
    )
    schema = payload["schema"]
    if not isinstance(schema, str) or schema != SCHEMA_ID:
        raise PrGuardConfigError(f"schema must be {SCHEMA_ID}")
    a38 = payload["a38"]
    if not isinstance(a38, dict):
        raise PrGuardConfigError("a38 must be an object")
    _require_keys(a38, A38_KEYS, "a38")
    enforce = _parse_branch_list(a38["enforce"], "a38.enforce")
    exclude = _parse_branch_list(a38["exclude"], "a38.exclude")
    default = a38["default"]
    if not isinstance(default, str) or default not in SCOPE_MODES:
        raise PrGuardConfigError("a38.default must be enforce|exclude")
    overlap = sorted(set(enforce) & set(exclude))
    if overlap:
        raise PrGuardConfigError(
            "a38.enforce and a38.exclude overlap: "
            + ", ".join(repr(name) for name in overlap)
        )
    normalized = {
        "schema": SCHEMA_ID,
        "a38": {
            "enforce": enforce,
            "exclude": exclude,
            "default": default,
        },
    }
    if "workflow_approval" in payload:
        approval = payload["workflow_approval"]
        if not isinstance(approval, dict):
            raise PrGuardConfigError("workflow_approval must be an object")
        _require_keys(approval, WORKFLOW_APPROVAL_KEYS, "workflow_approval")
        enabled = approval["enabled"]
        paths = approval["workflows"]
        if type(enabled) is not bool:
            raise PrGuardConfigError("workflow_approval.enabled must be boolean")
        if not isinstance(paths, list) or len(paths) > 64:
            raise PrGuardConfigError("workflow_approval.workflows must be an array of at most 64 paths")
        if any(not isinstance(p, str) or len(p) > 255 or WORKFLOW_PATH_RE.fullmatch(p) is None for p in paths):
            raise PrGuardConfigError("workflow_approval.workflows must contain exact workflow YAML paths")
        if len(set(paths)) != len(paths) or (enabled and not paths):
            raise PrGuardConfigError("workflow approval needs a nonempty, duplicate-free allowlist when enabled")
        normalized["workflow_approval"] = {"enabled": enabled, "workflows": list(paths)}
    if "environment_approval" in payload:
        approval = payload["environment_approval"]
        if not isinstance(approval, dict):
            raise PrGuardConfigError("environment_approval must be an object")
        _require_keys(approval, ENVIRONMENT_APPROVAL_KEYS, "environment_approval")
        enabled = approval["enabled"]
        environment = approval["environment"]
        paths = approval["workflows"]
        if type(enabled) is not bool:
            raise PrGuardConfigError("environment_approval.enabled must be boolean")
        if not isinstance(environment, str) or not environment or len(environment) > 255:
            raise PrGuardConfigError(
                "environment_approval.environment must be a non-empty string of at most 255 characters"
            )
        if not isinstance(paths, list) or len(paths) > 64:
            raise PrGuardConfigError(
                "environment_approval.workflows must be an array of at most 64 paths"
            )
        if any(
            not isinstance(path, str)
            or len(path) > 255
            or WORKFLOW_PATH_RE.fullmatch(path) is None
            for path in paths
        ):
            raise PrGuardConfigError(
                "environment_approval.workflows must contain exact workflow YAML paths"
            )
        if len(set(paths)) != len(paths) or (enabled and not paths):
            raise PrGuardConfigError(
                "environment approval needs a nonempty, duplicate-free allowlist when enabled"
            )
        normalized["environment_approval"] = {
            "enabled": enabled,
            "environment": environment,
            "workflows": list(paths),
        }
    if "lifecycle" in payload:
        lifecycle = payload["lifecycle"]
        if not isinstance(lifecycle, dict):
            raise PrGuardConfigError("lifecycle must be an object")
        _require_keys({k: v for k, v in lifecycle.items() if k not in {"conditional_workflows", "required_checks"}},
                      frozenset({"enabled", "auto_ready", "required_workflows", "ignored_workflows"}), "lifecycle")
        if type(lifecycle["enabled"]) is not bool or type(lifecycle["auto_ready"]) is not bool:
            raise PrGuardConfigError("lifecycle enabled/auto_ready must be boolean")
        for key in ("required_workflows", "ignored_workflows"):
            paths = lifecycle[key]
            if (not isinstance(paths, list) or len(paths) > 64
                    or any(not isinstance(p, str) or len(p) > 255 or WORKFLOW_PATH_RE.fullmatch(p) is None for p in paths)
                    or len(set(paths)) != len(paths)):
                raise PrGuardConfigError(f"lifecycle.{key} must be a bounded list of unique workflow YAML paths")
        if set(lifecycle["required_workflows"]) & set(lifecycle["ignored_workflows"]):
            raise PrGuardConfigError("required and ignored lifecycle workflows overlap")
        conditions = lifecycle.get("conditional_workflows", [])
        if not isinstance(conditions, list) or len(conditions) > 64:
            raise PrGuardConfigError("conditional_workflows must be a bounded list")
        condition_paths = set(lifecycle["required_workflows"] + lifecycle["ignored_workflows"])
        for condition in conditions:
            if not isinstance(condition, dict):
                raise PrGuardConfigError("conditional workflow must be an object")
            _require_keys(condition, frozenset({"workflow", "base_branches", "labels_any"}), "conditional workflow")
            path = condition["workflow"]
            if not isinstance(path, str) or len(path) > 255 or WORKFLOW_PATH_RE.fullmatch(path) is None or path in condition_paths:
                raise PrGuardConfigError("conditional workflow path invalid or duplicated")
            condition_paths.add(path)
            branches = _parse_branch_list(condition["base_branches"], "conditional workflow base_branches")
            labels = condition["labels_any"]
            if (not isinstance(labels, list) or len(labels) > 64
                    or any(not isinstance(label, str) or not label or len(label) > 100
                           or any(ord(c) < 32 for c in label) for label in labels)
                    or len(set(labels)) != len(labels) or (not branches and not labels)):
                raise PrGuardConfigError("conditional workflow needs exact branches or labels")
        if lifecycle["auto_ready"] and (not lifecycle["enabled"] or not lifecycle["required_workflows"]
                or not normalized.get("workflow_approval", {}).get("enabled")):
            raise PrGuardConfigError("auto_ready requires enabled lifecycle, workflow approval and required workflows")
        checks = lifecycle.get("required_checks", {})
        eligible_paths = set(lifecycle["required_workflows"]) | {c["workflow"] for c in conditions}
        if not isinstance(checks, dict) or set(checks) - eligible_paths:
            raise PrGuardConfigError("required_checks must map required workflow paths to check names")
        for names in checks.values():
            if (not isinstance(names, list) or not 1 <= len(names) <= 64
                    or any(not isinstance(n, str) or not n or len(n) > 255 for n in names)
                    or len(set(names)) != len(names)):
                raise PrGuardConfigError("required_checks needs bounded unique exact check names")
        normalized["lifecycle"] = dict(lifecycle)
    if ONE_REVIEW_AUTHORS_KEY in payload:
        normalized[ONE_REVIEW_AUTHORS_KEY] = _parse_login_list(
            payload[ONE_REVIEW_AUTHORS_KEY], ONE_REVIEW_AUTHORS_KEY
        )
    return normalized


def evaluate_a38_scope(
    config: Mapping[str, Any] | None,
    base_ref: str,
    default_branch: str = "",
) -> tuple[str, str]:
    """Return (decision, reason) for a PR target branch.

    Exact ``main`` is out of scope when it is not the repository default
    branch (release PRs), before legacy enforce-all and before ``a38.enforce``
    / ``a38.exclude`` / ``a38.default``. When ``default_branch`` is ``main``,
    that skip does not apply and the file (or legacy enforce-all) decides.
    ``config is None`` means the file is absent on the trusted revision and
    retains legacy enforce-all for every in-scope name. Listed branches use
    exact case-sensitive match; unlisted branches follow ``a38.default``.
    """
    branch = validate_branch_name(base_ref, "base_ref")
    if branch == "main" and default_branch != "main":
        return ("exclude", "target branch 'main' has nothing for A38 to check")
    if config is None:
        return (
            "enforce",
            (
                f"{CONFIG_PATH} missing on trusted default-branch revision; "
                "legacy enforce-all applies"
            ),
        )
    a38 = config.get("a38")
    if not isinstance(a38, Mapping):
        raise PrGuardConfigError("a38 must be an object")
    enforce = a38.get("enforce")
    exclude = a38.get("exclude")
    default = a38.get("default")
    if not isinstance(enforce, list) or not isinstance(exclude, list):
        raise PrGuardConfigError("a38.enforce and a38.exclude must be arrays")
    if default not in SCOPE_MODES:
        raise PrGuardConfigError("a38.default must be enforce|exclude")
    if branch in enforce:
        return (
            "enforce",
            f"target branch {branch!r} is listed in {CONFIG_PATH} a38.enforce",
        )
    if branch in exclude:
        return (
            "exclude",
            f"target branch {branch!r} is listed in {CONFIG_PATH} a38.exclude",
        )
    return (
        str(default),
        (
            f"target branch {branch!r} is unlisted; "
            f"{CONFIG_PATH} a38.default={default!r} applies"
        ),
    )


_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _lookup_completed(runner: Any, argv: list[str]) -> Any:
    try:
        return runner(argv)
    except OSError as exc:
        raise PrGuardLookupError("trusted pr-guard file could not be read") from exc


def _lookup_json(completed: Any, label: str) -> Any:
    if getattr(completed, "returncode", 1) != 0:
        raise PrGuardLookupError(f"{label} could not be read")
    raw = getattr(completed, "stdout", "") or ""
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PrGuardLookupError(f"{label} could not be read") from exc


def _contents_file_missing(completed: Any) -> bool:
    """True only for an HTTP 404 of the contents response.

    A commit SHA in the request URL can contain the hex digits ``404``.
    That substring is not a missing file.
    """
    detail = f"{getattr(completed, 'stderr', '') or ''}\n{getattr(completed, 'stdout', '') or ''}"
    if "HTTP 404" in detail:
        return True
    compact = detail.replace(" ", "")
    return '"message":"Not Found"' in compact


def fetch_trusted_pr_guard(runner: Any, repo: str) -> dict[str, Any] | None:
    """Load ``.github/pr-guard.json`` from the default-branch commit.

    Two reviews are required. One review is enough only when the opener is
    listed in ``one_review_authors`` on this trusted file. A missing file, a
    missing key, or an empty list keeps two reviews required. An entry that
    exists only on the pull request head does not count: this read never uses
    the head. A missing file returns None. Malformed JSON returns None. A
    duplicate key returns None. A transport failure raises
    ``PrGuardLookupError``.
    """
    if not isinstance(repo, str) or _REPO_RE.fullmatch(repo) is None:
        return None
    repo_doc = _lookup_json(
        _lookup_completed(runner, ["gh", "api", f"repos/{repo}"]),
        "default branch",
    )
    branch = repo_doc.get("default_branch") if isinstance(repo_doc, dict) else None
    if not isinstance(branch, str) or not branch or branch.strip() != branch:
        return None
    quoted = urllib.parse.quote(branch, safe="")
    commit_doc = _lookup_json(
        _lookup_completed(runner, ["gh", "api", f"repos/{repo}/commits/{quoted}"]),
        "default-branch commit",
    )
    sha = commit_doc.get("sha") if isinstance(commit_doc, dict) else None
    if not isinstance(sha, str) or _SHA_RE.fullmatch(sha) is None:
        return None
    completed = _lookup_completed(
        runner,
        [
            "gh",
            "api",
            "-H",
            "Accept: application/vnd.github.raw",
            f"repos/{repo}/contents/{CONFIG_PATH}?ref={sha}",
        ],
    )
    if getattr(completed, "returncode", 1) != 0:
        if _contents_file_missing(completed):
            return None
        raise PrGuardLookupError("trusted pr-guard file could not be read")
    raw = getattr(completed, "stdout", "") or ""
    try:
        return load_pr_guard_config(raw)
    except PrGuardConfigError:
        return None
