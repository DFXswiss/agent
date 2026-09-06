"""Repository pr-guard configuration: schema, validation and scope evaluation.

Optional `.github/pr-guard.json` controls which PR target branches A38 enforces.
The default branch is only the trusted location used to *find* this file; it is
not itself a built-in enforcement rule. Missing configuration retains legacy
enforce-all. Malformed configuration fails closed.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Mapping

SCHEMA_ID = "pr-guard/v1"
CONFIG_PATH = ".github/pr-guard.json"
TOP_KEYS = frozenset({"schema", "a38"})
WORKFLOW_APPROVAL_KEYS = frozenset({"enabled", "workflows"})
WORKFLOW_PATH_RE = re.compile(r"^\.github/workflows/[A-Za-z0-9_-][A-Za-z0-9_.-]*\.(?:yml|yaml)$")
A38_KEYS = frozenset({"enforce", "exclude", "default"})
SCOPE_MODES = frozenset({"enforce", "exclude"})
MAX_BRANCH_LIST = 256
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,74}$")


class PrGuardConfigError(ValueError):
    """Invalid pr-guard configuration (schema, keys, overlap, or JSON)."""


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


def load_pr_guard_config(text: str) -> dict[str, Any]:
    """Validate pr-guard/v1 JSON and return a normalized object."""
    payload = _loads_json(text)
    if not isinstance(payload, dict):
        raise PrGuardConfigError("pr-guard config must be a JSON object")
    _require_keys({k: v for k, v in payload.items() if k not in {"workflow_approval", "lifecycle"}}, TOP_KEYS, "pr-guard config")
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
    return normalized


def evaluate_a38_scope(
    config: Mapping[str, Any] | None, base_ref: str
) -> tuple[str, str]:
    """Return (decision, reason) for a PR target branch.

    ``config is None`` means the file is absent on the trusted revision and
    retains legacy enforce-all. Listed branches use exact case-sensitive match;
    unlisted branches follow ``a38.default``. There are no built-in branch rules.
    """
    branch = validate_branch_name(base_ref, "base_ref")
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
