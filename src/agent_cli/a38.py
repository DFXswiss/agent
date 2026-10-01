"""A38 policy load, local runner, and pure report verification.

``dfx-local-ci/v1`` stays valid. A newly written report is
``dfx-local-ci/v2`` inside the same comment markers. Report consistency is
checked against a trusted policy manifest; this is not cryptographic proof
of execution. Guard and backend integration live elsewhere.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import html
import json
import math
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .a38_job_adapters import ADAPTERS
from .readme_only import (
    MAX_FILES,
    git_changed_paths,
    markdown_and_guard_docs_only,
    paths_are_readme_only,
)
from .a38_job_adapters.commands import parse_commands_config
from .a38_job_adapters.common import BUILTIN_UNSET, DOCKER_HEAVY_LOCK, LOCK_NAME_RE, JobError
from .a38_job_adapters.compose import companion_env_missing, parse_compose_config
from .a38_job_adapters.http_smoke import parse_http_smoke_config
from .a38_job_adapters.immutable import parse_immutable_config
from .a38_jobs import add_job_parser
from .local_ci import (
    BEGIN_MARK,
    END_MARK,
    HEAD_RE,
    ID_RE,
    LocalCiError,
    LocalCiReport,
    REPO_RE,
    SCHEMA_V2,
    parse_comment,
    render_block,
)

SCHEMA_ID = "a38/v1"
STANDARD_ID = "A38"
DOCUMENTATION_PATH = "docs/a38.md"
MODES = frozenset({"enforce", "observe"})
POLICY_KEYS = frozenset(
    {"schema", "standard", "documentation", "mode", "jobs", "exclusions"}
)
JOB_COMMON_KEYS = frozenset({"id", "name", "timeout_s", "workflow", "job"})
JOB_INPUT_KEYS = JOB_COMMON_KEYS | frozenset({"command", "executor", "lock"})
_DOCKER_DEFAULT_LOCK_ADAPTERS = frozenset({"compose", "http-smoke"})
EXECUTOR_KEYS = frozenset({"adapter", "config"})
EXCLUSION_KEYS = frozenset({"workflow", "job", "reason"})

MAX_NAME_LEN = 200
MAX_COMMAND_LEN = 8192
MAX_REASON_LEN = 500
MAX_JOBS = 256
MAX_EXCLUSIONS = 256
MAX_TIMEOUT_S = 86400
TERMINATION_GRACE_S = 30
FUTURE_SKEW = timedelta(minutes=5)

WORKFLOW_RE = re.compile(r"^\.github/workflows/[A-Za-z0-9][A-Za-z0-9._-]{0,190}\.ya?ml$")
GH_JOB_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,99}$")
ORIGIN_HTTPS_RE = re.compile(
    r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)
ORIGIN_SSH_RE = re.compile(
    r"^(?:ssh://)?git@github\.com[:/]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)

TOKEN_ENV_DROP = frozenset({"GITHUB_TOKEN", "GH_TOKEN"})

RunFn = Callable[[list[str], Path | None, Mapping[str, str] | None], subprocess.CompletedProcess[str]]
ConfigParser = Callable[[str], tuple[Any, dict[str, Any]]]

_ADAPTER_CONFIG_PARSERS: dict[str, ConfigParser] = {
    "commands": parse_commands_config,
    "compose": parse_compose_config,
    "http-smoke": parse_http_smoke_config,
    "immutable": parse_immutable_config,
}


class A38Error(ValueError):
    """Invalid A38 policy or runner preflight failure."""


def _reject_nonfinite_constant(name: str) -> None:
    raise A38Error(f"JSON contains non-finite number: {name}")


def _parse_finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise A38Error(f"JSON contains non-finite number: {value}")
    return number


def _object_pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise A38Error(f"JSON contains duplicate key: {key}")
        out[key] = value
    return out


def _loads_policy_json(text: str) -> Any:
    try:
        return json.loads(
            text,
            parse_constant=_reject_nonfinite_constant,
            parse_float=_parse_finite_float,
            object_pairs_hook=_object_pairs_no_duplicates,
        )
    except A38Error:
        raise
    except json.JSONDecodeError as exc:
        raise A38Error(f"JSON is invalid: {exc.msg}") from exc
    except (RecursionError, ValueError) as exc:
        raise A38Error(f"JSON is invalid: {exc}") from exc


def _require_keys(obj: Mapping[str, Any], allowed: frozenset[str], label: str) -> None:
    keys = set(obj)
    extra = keys - allowed
    missing = allowed - keys
    if extra:
        raise A38Error(f"{label} has unknown keys: {', '.join(sorted(extra))}")
    if missing:
        raise A38Error(f"{label} missing keys: {', '.join(sorted(missing))}")


def _as_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or value == "":
        raise A38Error(f"{label} must be a non-empty string")
    return value


def _as_finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise A38Error(f"{label} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise A38Error(f"{label} must be finite")
    return number


def _no_control_chars(value: str, label: str) -> None:
    if "\n" in value or "\r" in value or "\x00" in value:
        raise A38Error(f"{label} must not contain newlines or NUL")


def _validate_workflow(value: str, label: str) -> str:
    path = _as_str(value, label)
    if WORKFLOW_RE.match(path) is None:
        raise A38Error(f"{label} must be .github/workflows/<file>.yml|yaml")
    return path


def _validate_gh_job(value: str, label: str) -> str:
    ident = _as_str(value, label)
    if GH_JOB_RE.match(ident) is None:
        raise A38Error(f"{label} is not a simple GitHub job identifier")
    return ident


def _validate_job_id(value: str, label: str) -> str:
    ident = _as_str(value, label)
    if ID_RE.match(ident) is None:
        raise A38Error(f"{label} {ident!r} is not kebab-case")
    return ident


def _validate_name(value: str, label: str) -> str:
    name = _as_str(value, label)
    if len(name) > MAX_NAME_LEN:
        raise A38Error(f"{label} exceeds {MAX_NAME_LEN} characters")
    _no_control_chars(name, label)
    return name


def _validate_command(value: str, label: str) -> str:
    command = _as_str(value, label)
    if len(command) > MAX_COMMAND_LEN:
        raise A38Error(f"{label} exceeds {MAX_COMMAND_LEN} characters")
    _no_control_chars(command, label)
    return command


def _validate_reason(value: str, label: str) -> str:
    reason = _as_str(value, label)
    if len(reason) > MAX_REASON_LEN:
        raise A38Error(f"{label} exceeds {MAX_REASON_LEN} characters")
    _no_control_chars(reason, label)
    return reason


def _validate_timeout(value: Any, label: str) -> float:
    timeout = _as_finite_number(value, label)
    if timeout <= 0 or timeout > MAX_TIMEOUT_S:
        raise A38Error(f"{label} must be > 0 and <= {MAX_TIMEOUT_S}")
    return timeout


def _require_job_keys(obj: Mapping[str, Any], label: str) -> str:
    keys = set(obj)
    extra = keys - JOB_INPUT_KEYS
    missing = JOB_COMMON_KEYS - keys
    if extra:
        raise A38Error(f"{label} has unknown keys: {', '.join(sorted(extra))}")
    if missing:
        raise A38Error(f"{label} missing keys: {', '.join(sorted(missing))}")
    inputs = keys & {"command", "executor"}
    if len(inputs) != 1:
        raise A38Error(f"{label} must contain exactly one of command or executor")
    input_key = inputs.pop()
    if input_key == "executor" and "lock" in keys:
        raise A38Error(f"{label} executor jobs must set lock in executor.config, not as a sibling")
    return input_key


def _optional_job_lock(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or LOCK_NAME_RE.fullmatch(value) is None:
        raise A38Error(f"{label} must be a lock name or null")
    return value


def _executor_command_and_lock(value: Any, label: str) -> tuple[str, str | None]:
    if not isinstance(value, dict):
        raise A38Error(f"{label} must be an object")
    _require_keys(value, EXECUTOR_KEYS, label)
    adapter = _as_str(value["adapter"], f"{label}.adapter")
    if adapter not in ADAPTERS:
        raise A38Error(f"{label}.adapter is unknown: {adapter}")
    config = value["config"]
    if not isinstance(config, dict):
        raise A38Error(f"{label}.config must be an object")
    try:
        config_text = json.dumps(
            config,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise A38Error(f"{label}.config cannot be serialized as JSON: {exc}") from exc
    parser = _ADAPTER_CONFIG_PARSERS[adapter]
    try:
        common, _parsed = parser(config_text)
    except (JobError, OverflowError, RecursionError) as exc:
        raise A38Error(f"{label}.config is invalid: {exc}") from exc
    command = f"agent a38 job {adapter} --config {shlex.quote(config_text)}"
    command = _validate_command(command, f"{label} command")
    lock = common.lock
    if lock is None and adapter in _DOCKER_DEFAULT_LOCK_ADAPTERS:
        lock = DOCKER_HEAVY_LOCK
    return command, lock


def load_policy(text: str) -> dict:
    """Validate an A38 manifest and return the normalized JSON dict."""
    payload = _loads_policy_json(text)
    if not isinstance(payload, dict):
        raise A38Error("policy must be a JSON object")
    payload = dict(payload)
    # Normalized output uses readme_only_omit; reloading that dict must work.
    if "readme_only_omit" in payload:
        if "readme_only" in payload:
            raise A38Error("readme_only and readme_only_omit cannot both be set")
        omit_norm = payload.pop("readme_only_omit")
        if not isinstance(omit_norm, list):
            raise A38Error("readme_only.omit_jobs must be a non-empty bounded array")
        if omit_norm:
            payload["readme_only"] = {"omit_jobs": list(omit_norm)}
    extra = set(payload) - POLICY_KEYS - {"readme_only"}
    missing = POLICY_KEYS - set(payload)
    if extra:
        raise A38Error(f"policy has unknown keys: {', '.join(sorted(extra))}")
    if missing:
        raise A38Error(f"policy missing keys: {', '.join(sorted(missing))}")
    schema = _as_str(payload["schema"], "schema")
    if schema != SCHEMA_ID:
        raise A38Error(f"schema must be {SCHEMA_ID}")
    standard = _as_str(payload["standard"], "standard")
    if standard != STANDARD_ID:
        raise A38Error(f"standard must be {STANDARD_ID}")
    documentation = _as_str(payload["documentation"], "documentation")
    if documentation != DOCUMENTATION_PATH:
        raise A38Error(f"documentation must be {DOCUMENTATION_PATH}")
    mode = _as_str(payload["mode"], "mode")
    if mode not in MODES:
        raise A38Error("mode must be enforce|observe")

    jobs_raw = payload["jobs"]
    if not isinstance(jobs_raw, list):
        raise A38Error("jobs must be an array")
    if not jobs_raw:
        raise A38Error("jobs must be a non-empty array")
    if len(jobs_raw) > MAX_JOBS:
        raise A38Error(f"jobs exceeds {MAX_JOBS} entries")

    exclusions_raw = payload["exclusions"]
    if not isinstance(exclusions_raw, list):
        raise A38Error("exclusions must be an array")
    if len(exclusions_raw) > MAX_EXCLUSIONS:
        raise A38Error(f"exclusions exceeds {MAX_EXCLUSIONS} entries")

    jobs: list[dict[str, Any]] = []
    job_ids: set[str] = set()
    tuples: set[tuple[str, str]] = set()

    for index, item in enumerate(jobs_raw):
        if not isinstance(item, dict):
            raise A38Error(f"jobs[{index}] must be an object")
        input_key = _require_job_keys(item, f"jobs[{index}]")
        ident = _validate_job_id(item["id"], f"jobs[{index}].id")
        if ident in job_ids:
            raise A38Error(f"duplicate job id: {ident}")
        job_ids.add(ident)
        workflow = _validate_workflow(item["workflow"], f"jobs[{index}].workflow")
        gh_job = _validate_gh_job(item["job"], f"jobs[{index}].job")
        pair = (workflow, gh_job)
        if pair in tuples:
            raise A38Error(f"duplicate workflow/job tuple: {workflow}#{gh_job}")
        tuples.add(pair)
        timeout_s = _validate_timeout(item["timeout_s"], f"jobs[{index}].timeout_s")
        if isinstance(item["timeout_s"], int) and not isinstance(item["timeout_s"], bool):
            timeout_out: int | float = int(item["timeout_s"])
        else:
            timeout_out = float(timeout_s)
        name = _validate_name(item["name"], f"jobs[{index}].name")
        if input_key == "command":
            command = _validate_command(item["command"], f"jobs[{index}].command")
            if "lock" in item:
                lock = _optional_job_lock(item["lock"], f"jobs[{index}].lock")
            else:
                lock = None
        else:
            command, lock = _executor_command_and_lock(
                item["executor"], f"jobs[{index}].executor"
            )
        jobs.append(
            {
                "id": ident,
                "name": name,
                "command": command,
                "timeout_s": timeout_out,
                "workflow": workflow,
                "job": gh_job,
                "lock": lock,
            }
        )

    exclusions: list[dict[str, Any]] = []
    for index, item in enumerate(exclusions_raw):
        if not isinstance(item, dict):
            raise A38Error(f"exclusions[{index}] must be an object")
        _require_keys(item, EXCLUSION_KEYS, f"exclusions[{index}]")
        workflow = _validate_workflow(item["workflow"], f"exclusions[{index}].workflow")
        gh_job = _validate_gh_job(item["job"], f"exclusions[{index}].job")
        pair = (workflow, gh_job)
        if pair in tuples:
            raise A38Error(f"duplicate workflow/job tuple: {workflow}#{gh_job}")
        tuples.add(pair)
        exclusions.append(
            {
                "workflow": workflow,
                "job": gh_job,
                "reason": _validate_reason(item["reason"], f"exclusions[{index}].reason"),
            }
        )

    omit: list[str] = []
    if "readme_only" in payload:
        block = payload["readme_only"]
        if not isinstance(block, dict):
            raise A38Error("readme_only must be an object")
        extra_ro = set(block) - {"omit_jobs"}
        if extra_ro:
            raise A38Error(f"readme_only has unknown keys: {', '.join(sorted(extra_ro))}")
        omit_raw = block.get("omit_jobs")
        if not isinstance(omit_raw, list) or not omit_raw or len(omit_raw) > MAX_JOBS:
            raise A38Error("readme_only.omit_jobs must be a non-empty bounded array")
        seen_omit: set[str] = set()
        for item in omit_raw:
            ident = _validate_job_id(item, "readme_only.omit_jobs")
            if ident not in job_ids:
                raise A38Error(f"readme_only.omit_jobs unknown id: {ident}")
            if ident in seen_omit:
                raise A38Error(f"readme_only.omit_jobs duplicate id: {ident}")
            seen_omit.add(ident)
            omit.append(ident)

    return {
        "schema": SCHEMA_ID,
        "standard": STANDARD_ID,
        "documentation": DOCUMENTATION_PATH,
        "mode": mode,
        "jobs": jobs,
        "exclusions": exclusions,
        "readme_only_omit": omit,
    }


def _policy_jobs_by_id(policy: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    jobs = policy.get("jobs")
    if not isinstance(jobs, list):
        raise A38Error("policy.jobs must be an array")
    out: dict[str, Mapping[str, Any]] = {}
    for job in jobs:
        if not isinstance(job, Mapping):
            raise A38Error("policy.jobs entries must be objects")
        ident = job.get("id")
        if not isinstance(ident, str):
            raise A38Error("policy.jobs entry missing id")
        out[ident] = job
    return out


def _policy_required_ids(policy: Mapping[str, Any]) -> list[str]:
    jobs = policy.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise A38Error("policy.jobs must be a non-empty array")
    ids: list[str] = []
    for job in jobs:
        if not isinstance(job, Mapping) or not isinstance(job.get("id"), str):
            raise A38Error("policy.jobs entry missing id")
        ids.append(job["id"])
    return ids


def _parse_recorded_at(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _check_recorded_at(recorded_at: str, *, now: datetime) -> str | None:
    try:
        ts = _parse_recorded_at(recorded_at)
    except ValueError:
        return "recorded_at is not a real UTC timestamp"
    if ts > now + FUTURE_SKEW:
        return "recorded_at is more than 5 minutes in the future"
    return None


def _timeout_equal(left: float, right: float) -> bool:
    return math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=1e-9)


def verify_report(
    comment: str,
    policy: dict,
    *,
    repo: str,
    head: str,
    private: bool,
) -> dict:
    """Pure validation of an author report against a trusted policy.

    No filesystem or network access. Reasons never echo the comment body.
    """
    reasons: list[str] = []
    try:
        required = _policy_required_ids(policy)
        by_id = _policy_jobs_by_id(policy)
    except A38Error as exc:
        return {"ok": False, "status": "fail", "reasons": [str(exc)]}

    if not isinstance(repo, str) or REPO_RE.match(repo) is None:
        return {"ok": False, "status": "fail", "reasons": ["expected repo must be owner/name"]}
    head_norm = head.lower() if isinstance(head, str) else ""
    if HEAD_RE.match(head_norm) is None:
        return {
            "ok": False,
            "status": "fail",
            "reasons": ["expected head must be a 40-character lowercase hex SHA"],
        }
    if not isinstance(private, bool):
        return {"ok": False, "status": "fail", "reasons": ["expected private must be a boolean"]}

    try:
        report = parse_comment(comment)
    except LocalCiError as exc:
        return {"ok": False, "status": "fail", "reasons": [f"report parse error: {exc}"]}

    if report.repo.lower() != repo.lower():
        reasons.append("repo does not match expected")
    if report.head != head_norm:
        reasons.append("head does not match expected")
    if report.private is not private:
        reasons.append("private does not match expected")

    stamp_reason = _check_recorded_at(report.recorded_at, now=datetime.now(timezone.utc))
    if stamp_reason is not None:
        reasons.append(stamp_reason)

    if list(report.required) != required:
        reasons.append("required ids do not match policy")

    omit = set(policy.get("readme_only_omit") or [])
    report_only = bool(getattr(report, "readme_only", False))
    markdown_only = bool(getattr(report, "markdown_only", False))
    run_by_id = {run.id: run for run in report.runs}
    for ident in required:
        job = by_id[ident]
        run = run_by_id.get(ident)
        if run is None:
            reasons.append(f"{ident}: missing run")
            continue
        if run.name != job["name"]:
            reasons.append(f"{ident}: name does not match policy")
        if run.command != job["command"]:
            reasons.append(f"{ident}: command does not match policy")
        if not _timeout_equal(run.timeout_s, float(job["timeout_s"])):
            reasons.append(f"{ident}: timeout_s does not match policy")
        if run.result == "not_applicable":
            readme_authorized = ident in omit and report_only
            markdown_authorized = markdown_only
            if (not readme_authorized and not markdown_authorized) or run.exit_code != 0:
                if not readme_authorized and not markdown_authorized:
                    reasons.append(f"{ident}: not_applicable is not authorized")
                else:
                    reasons.append(f"{ident}: exit_code is {run.exit_code}")
            continue
        if run.result != "pass":
            reasons.append(f"{ident}: result is {run.result}")
        if run.exit_code != 0:
            reasons.append(f"{ident}: exit_code is {run.exit_code}")
        if not math.isfinite(run.duration_s):
            reasons.append(f"{ident}: duration_s is not finite")
        elif run.duration_s < 0:
            reasons.append(f"{ident}: duration_s is negative")
        elif run.duration_s > float(job["timeout_s"]):
            reasons.append(f"{ident}: duration_s exceeds policy timeout")

    if reasons:
        return {"ok": False, "status": "fail", "reasons": reasons}
    return {"ok": True, "status": "pass", "reasons": []}


def _default_run(
    argv: list[str],
    cwd: Path | None,
    env: Mapping[str, str] | None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        argv,
        cwd=str(cwd) if cwd is not None else None,
        env=dict(env) if env is not None else None,
        text=True,
        capture_output=True,
        check=False,
    )


def _git(
    repo_path: Path,
    *parts: str,
    run: RunFn,
) -> subprocess.CompletedProcess[str]:
    return run(["git", "-C", str(repo_path), *parts], None, None)


def _require_clean_tree(repo_path: Path, *, run: RunFn) -> None:
    completed = _git(
        repo_path,
        "status",
        "--porcelain",
        "--untracked-files=all",
        run=run,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "git status failed").strip()
        raise A38Error(detail or "git status failed")
    if completed.stdout.strip():
        raise A38Error("working tree is not clean (tracked or untracked changes)")


def _head_sha(repo_path: Path, *, run: RunFn) -> str:
    completed = _git(repo_path, "rev-parse", "HEAD", run=run)
    if completed.returncode != 0:
        raise A38Error((completed.stderr or completed.stdout or "git rev-parse failed").strip())
    head = completed.stdout.strip().lower()
    if HEAD_RE.match(head) is None:
        raise A38Error("HEAD is not a 40-character hex SHA")
    return head


def _ensure_commit_exists(repo_path: Path, sha: str, *, run: RunFn) -> str:
    if HEAD_RE.match(sha.lower()) is None:
        raise A38Error("base-sha must be a 40-character hex SHA")
    completed = _git(repo_path, "rev-parse", "--verify", f"{sha}^{{commit}}", run=run)
    if completed.returncode != 0:
        raise A38Error("base-sha is not an existing commit")
    resolved = completed.stdout.strip().lower()
    if HEAD_RE.match(resolved) is None:
        raise A38Error("base-sha did not resolve to a 40-character hex SHA")
    return resolved


def _repo_root(repo_path: Path, *, run: RunFn) -> Path:
    completed = _git(repo_path, "rev-parse", "--show-toplevel", run=run)
    if completed.returncode != 0:
        raise A38Error((completed.stderr or completed.stdout or "not a git repository").strip())
    root = Path(completed.stdout.strip()).resolve()
    wanted = repo_path.resolve()
    if root != wanted:
        try:
            if not root.samefile(wanted):
                raise A38Error("repo path must be the repository root")
        except OSError as exc:
            raise A38Error("repo path must be the repository root") from exc
    return root


def parse_github_origin(url: str) -> str:
    text = url.strip()
    match = ORIGIN_HTTPS_RE.match(text) or ORIGIN_SSH_RE.match(text)
    if match is None:
        raise A38Error("origin remote must be an https or ssh GitHub URL")
    return f"{match.group(1)}/{match.group(2)}"


def _origin_repo(repo_path: Path, *, run: RunFn) -> str:
    completed = _git(repo_path, "config", "--get", "remote.origin.url", run=run)
    if completed.returncode != 0 or not completed.stdout.strip():
        raise A38Error("origin remote is not configured")
    return parse_github_origin(completed.stdout.strip())


def _is_outside(path: Path, repo_root: Path) -> bool:
    resolved = path.resolve()
    root = repo_root.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return True
    return False


def _resolve_private(
    repo_path: Path,
    private: bool | None,
    *,
    run: RunFn,
    repository: str,
    github_session: str | None = None,
    config_home: Path | None = None,
) -> bool:
    if private is not None:
        if not isinstance(private, bool):
            raise A38Error("private must be a boolean")
        return private
    if not github_session:
        raise A38Error("visibility lookup requires an explicitly configured --github-session, or --private/--public")
    from .github_accounts import AccountError, load_accounts
    try:
        if config_home is None:
            from .main import home
            config_home = home()
        account = load_accounts(config_home).for_session(github_session)
        scoped = account.runner(lambda argv: run(argv, repo_path, None))
        completed = scoped(["gh", "repo", "view", repository, "--json", "isPrivate"])
    except AccountError as exc:
        raise A38Error(str(exc)) from None
    if completed.returncode != 0:
        raise A38Error("configured GitHub account could not resolve repository visibility")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise A38Error("gh repo view returned invalid JSON") from exc
    if not isinstance(payload, dict) or "isPrivate" not in payload:
        raise A38Error("gh repo view JSON missing isPrivate")
    value = payload["isPrivate"]
    if not isinstance(value, bool):
        raise A38Error("gh repo view isPrivate must be a boolean")
    return value


def _job_env(head: str, base: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in TOKEN_ENV_DROP}
    env["A38_HEAD_SHA"] = head
    env["A38_BASE_SHA"] = base
    return env


def _preflight_jobs(jobs: Sequence[Mapping[str, Any]], env: Mapping[str, str]) -> None:
    """Raise A38Error for job requirements that are already unsatisfiable."""
    executors: list[tuple[Mapping[str, Any], list[str]]] = []
    for job in jobs:
        command = str(job["command"])
        try:
            argv = shlex.split(command)
        except ValueError:
            continue
        if (
            argv[:3] == ["agent", "a38", "job"]
            and len(argv) >= 6
            and argv[4] == "--config"
        ):
            executors.append((job, argv))

    problems: list[str] = []
    if executors and shutil.which("agent", path=env.get("PATH")) is None:
        problems.append(
            'agent executable not found on PATH; executor jobs run "agent a38 job ..."'
        )

    for job, argv in executors:
        if argv[3] != "compose":
            continue
        try:
            common, parsed = parse_compose_config(argv[5])
        except JobError:
            continue
        name = parsed["companion"]["directory_env"]
        if name in common.env:
            continue
        missing = (
            name in BUILTIN_UNSET
            or name in common.unset
            or any(name.startswith(prefix) for prefix in common.unset_prefixes)
            or not env.get(name, "").strip()
        )
        if missing:
            problems.append(f"{job['id']}: {companion_env_missing(parsed['companion'])}")

    if problems:
        raise A38Error("preflight failed: " + "; ".join(problems))


def _chmod_owner_rw(path: Path) -> None:
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _chmod_owner_rw(tmp_path)
        os.replace(tmp_path, path)
        _chmod_owner_rw(path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _utc_now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_entry(
    *,
    ident: str,
    name: str,
    command: str,
    result: str,
    exit_code: int,
    duration_s: float,
    timeout_s: float,
) -> dict[str, Any]:
    return {
        "id": ident,
        "name": name,
        "command": command,
        "result": result,
        "exit_code": exit_code,
        "duration_s": duration_s,
        "timeout_s": timeout_s,
    }


def _build_report_dict(
    *,
    repo: str,
    head: str,
    private: bool,
    recorded_at: str,
    required: Sequence[str],
    runs: Sequence[Mapping[str, Any]],
    base: str,
    changed_paths: Sequence[str] | None,
    omit_reason: str | None,
    readme_only: bool = False,
    markdown_only: bool = False,
) -> dict[str, Any]:
    """Complete original. v1 measured keys plus the outcome the human lines state."""
    if changed_paths is None:
        changed: dict[str, Any] | None = None
    else:
        count = len(changed_paths)
        changed = {
            "count": count,
            "all_markdown": count > 0 and all(path.endswith(".md") for path in changed_paths),
        }
    payload: dict[str, Any] = {
        "schema": SCHEMA_V2,
        "repo": repo,
        "head": head,
        "private": private,
        "recorded_at": recorded_at,
        "base": base,
        "policy": {"path": ".github/a38.json", "sha": base},
        "changed_paths": changed,
        "omitted": omit_reason if omit_reason else None,
        "required": list(required),
        "runs": [dict(run) for run in runs],
    }
    if readme_only:
        payload["readme_only"] = True
    if markdown_only:
        payload["markdown_only"] = True
    return payload


def _report_from_dict(payload: Mapping[str, Any]) -> LocalCiReport:
    # Re-parse through local_ci to keep a single schema authority.
    block = (
        f"{BEGIN_MARK}\n```json\n"
        f"{json.dumps(payload, indent=2, sort_keys=True)}\n"
        f"```\n{END_MARK}\n"
    )
    return parse_comment(block)


def _report_table_cell(value: str) -> str:
    # Keep policy-supplied names inside one literal Markdown/HTML table cell.
    escaped = html.escape(" ".join(value.split()))
    return "".join(f"&#{ord(char)};" if char in "\\|`*_[]{}" else char for char in escaped)


_OMIT_REASONS = frozenset({
    "markdown-only change set",
    "README-only change set",
    "guard-docs change set",
})


def _report_table(report: LocalCiReport) -> str:
    rows = [
        "| Check / Prüfung | Duration / Laufzeit | Timeout / Zeitlimit | Result / Ergebnis | Exit code |",
        "| --- | ---: | ---: | --- | ---: |",
    ]
    for run in report.runs:
        label = _report_table_cell(f"{run.id}: {run.name}")
        rows.append(
            f"| {label} | {math.ceil(run.duration_s)} s | {math.ceil(run.timeout_s)} s "
            f"| {run.result} | {run.exit_code} |"
        )
    return "\n".join(rows) + "\n"


def _report_facts(
    report: LocalCiReport,
    *,
    base_sha: str | None,
    changed_paths: Sequence[str] | None,
    omit_reason: str | None,
) -> str:
    """Outcome lines for the details. Job steps stay in the machine block."""
    lines = [
        f"Head: `{report.head}`",
        f"Recorded: `{report.recorded_at}`",
    ]
    if isinstance(base_sha, str) and HEAD_RE.match(base_sha) is not None:
        lines.append(f"Base: `{base_sha}`")
        lines.append(f"Policy: `.github/a38.json` at `{base_sha}`")
    if changed_paths is None or any(not isinstance(path, str) for path in changed_paths):
        lines.append("Changed paths: unknown. The local run is required.")
    else:
        count = len(changed_paths)
        all_md = count > 0 and all(path.endswith(".md") for path in changed_paths)
        if all_md:
            lines.append(
                f"Changed paths: {count}. Every path ends in `.md`, so the local run is not required."
            )
        else:
            lines.append(
                f"Changed paths: {count}. Not every path ends in `.md`, so the local run is required."
            )
    if omit_reason in _OMIT_REASONS and any(run.result == "not_applicable" for run in report.runs):
        lines.append(f"Omitted: {omit_reason}.")
    return "\n".join(lines) + "\n\n"


def inventory_omit_reason(paths: Sequence[str]) -> str:
    """Omission reason for a not_applicable run, or '' when none applies.

    Markdown-only wins over guard-docs, which wins over README-only.
    A README.md path is markdown, so that inventory is markdown-only.
    """
    if len(paths) > MAX_FILES:
        return ""
    markdown_only, guard_docs_only = markdown_and_guard_docs_only(paths)
    if markdown_only:
        return "markdown-only change set"
    if guard_docs_only:
        return "guard-docs change set"
    if paths_are_readme_only(paths):
        return "README-only change set"
    return ""


def report_outcome_lines(
    report: LocalCiReport,
    *,
    base_sha: str | None,
    changed_paths: Sequence[str] | None,
    omit_reason: str | None,
) -> list[str]:
    text = _report_facts(
        report,
        base_sha=base_sha,
        changed_paths=changed_paths,
        omit_reason=omit_reason,
    )
    return [line for line in text.splitlines() if line.strip()]


def report_table_lines(report: LocalCiReport) -> list[str]:
    return [line for line in _report_table(report).splitlines() if line.startswith("|")]


def _add_reason(reasons: list[str], text: str) -> None:
    if text not in reasons:
        reasons.append(text)


def _label_text(line: str) -> str:
    """Drop leading emphasis so a bold label is still that label."""
    return re.sub(r"^[*_\s]+", "", line).strip()


def _starts_with_label(line: str, label: str) -> bool:
    return _label_text(line).casefold().startswith(label.casefold() + ":")


def _prose_outside_fences(body: str) -> list[str]:
    """Comment lines that are not inside a fenced block.

    Pipe characters inside the machine block are not job-table rows.
    """
    prose: list[str] = []
    in_fence = False
    for raw in body.splitlines():
        stripped = raw.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence and stripped:
            prose.append(stripped)
    return prose


_NEGATION_WORD = re.compile(r"\b(?:not|no|never|nicht|kein|keine)\b")
_JOB_RESULTS = ("not_applicable", "timeout", "error", "fail", "pass")


def _expand_negation_words(folded: str) -> str:
    """Turn contractions into a separate not. The general n't form keeps its apostrophe."""
    folded = folded.replace("not_applicable", "\x00jobna\x00")
    folded = re.sub(r"\bcannot\b", "can not", folded)
    folded = re.sub(r"\bcan['\u2019]t\b", "can not", folded)
    folded = re.sub(r"\bwon['\u2019]t\b", "will not", folded)
    folded = re.sub(
        r"\b(is|are|was|were|does|did|do)n['\u2019]?t\b",
        r"\1 not",
        folded,
    )
    folded = re.sub(r"\b([a-z]+)n['\u2019]t\b", r"\1 not", folded)
    return folded.replace("\x00jobna\x00", "not_applicable")


def _span_negated(folded: str, start: int, end: int) -> bool:
    """True when this claim's own clause contains a negation.

    The clause runs from the previous comma or sentence break to the next one.
    A later clause can negate a different claim without undoing this one.
    """
    prefix = re.split(r"[,.!;]", folded[:start])[-1]
    suffix = re.split(r"[,.!;]", folded[end:], maxsplit=1)[0]
    return any(
        _NEGATION_WORD.search(part) is not None
        for part in (prefix, folded[start:end], suffix)
    )


def _sha_line_ok(line: str, sha: str) -> bool:
    folded = _expand_negation_words(line.casefold())
    expected = sha.casefold()
    found = re.findall(r"[0-9a-f]{40}", folded)
    if found != [expected]:
        return False
    at = folded.find(expected)
    return not _span_negated(folded, at, at + len(expected))


def _recorded_line_ok(line: str, recorded_at: str) -> bool:
    if recorded_at not in line:
        return False
    stamps = re.findall(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", line)
    if stamps != [recorded_at]:
        return False
    folded = _expand_negation_words(line.casefold())
    expected = recorded_at.casefold()
    at = folded.find(expected)
    if at < 0:
        return False
    return not _span_negated(folded, at, at + len(expected))


def _md_claim(text: str) -> bool | None:
    """True when every path is markdown, False when not, None when unstated."""
    folded = _expand_negation_words(text.casefold().replace("`", ""))
    negative = bool(
        re.search(r"\bnot every\b.{0,80}\.md\b", folded)
        or re.search(r"\bnot all\b.{0,80}markdown\b", folded)
    )
    positive = False
    for pattern in (
        r"(?<!not )\bevery\b.{0,80}\.md\b",
        r"(?<!not )\ball\b.{0,40}markdown\b",
    ):
        for match in re.finditer(pattern, folded):
            if _span_negated(folded, match.start(), match.end()):
                continue
            positive = True
            break
    if negative and positive:
        return None
    if negative:
        return False
    if positive:
        return True
    return None


def _changed_path_chunks(prose: Sequence[str]) -> list[str]:
    """Each Changed-paths line, plus one following wrap line when it is not a new fact."""
    labels = ("head:", "recorded:", "base:", "policy:", "changed paths:", "omitted:")
    chunks: list[str] = []
    index = 0
    while index < len(prose):
        line = prose[index]
        if _starts_with_label(line, "Changed paths"):
            chunk = _label_text(line)
            if index + 1 < len(prose):
                nxt = prose[index + 1]
                folded = _label_text(nxt).casefold()
                if not nxt.startswith("|") and not any(folded.startswith(label) for label in labels):
                    chunk = f"{chunk} {nxt}"
                    index += 1
            chunks.append(chunk)
        index += 1
    return chunks


def _changed_paths_ok(chunk: str, paths: Sequence[str]) -> bool:
    numbers = re.findall(r"\d+", chunk)
    if not numbers or int(numbers[0]) != len(paths):
        return False
    all_md = len(paths) > 0 and all(path.endswith(".md") for path in paths)
    return _md_claim(chunk) is all_md


def _accepted_omit_reasons(paths: Sequence[str]) -> frozenset[str]:
    """Reasons that are true of this inventory, not only the runner's preferred one.

    The generated line still prefers markdown-only, then guard-docs, then README-only.
    A comment may name any of those three that this inventory actually is.
    """
    if len(paths) > MAX_FILES:
        return frozenset()
    accepted: set[str] = set()
    markdown_only, guard_docs_only = markdown_and_guard_docs_only(paths)
    if markdown_only:
        accepted.add("markdown-only change set")
    if guard_docs_only:
        accepted.add("guard-docs change set")
    if paths_are_readme_only(paths):
        accepted.add("README-only change set")
    return frozenset(accepted)


def _omit_claims(line: str) -> set[str]:
    folded = _expand_negation_words(line.casefold())
    claimed: set[str] = set()
    for reason in _OMIT_REASONS:
        token = reason.casefold()
        start = 0
        while True:
            index = folded.find(token, start)
            if index < 0:
                break
            if not _span_negated(folded, index, index + len(token)):
                claimed.add(reason)
            start = index + len(token)
    return claimed


def _split_row(line: str) -> list[str]:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    return [cell.strip() for cell in text.split("|")]


def _markdown_tables(prose: Sequence[str]) -> list[list[list[str]]]:
    tables: list[list[list[str]]] = []
    current: list[list[str]] = []
    for line in prose:
        if line.startswith("|"):
            current.append(_split_row(line))
            continue
        if current:
            tables.append(current)
            current = []
    if current:
        tables.append(current)
    return tables


_HEADER_WORDS = (
    ("label", ("check", "prüfung", "prufung")),
    ("duration", ("duration", "laufzeit")),
    ("timeout", ("timeout", "zeitlimit")),
    ("result", ("result", "ergebnis")),
    ("exit", ("exit",)),
)
_DEFAULT_COLUMNS = {"label": 0, "duration": 1, "timeout": 2, "result": 3, "exit": 4}


def _is_separator(cells: Sequence[str]) -> bool:
    if not cells:
        return False
    return all(re.fullmatch(r":?-{3,}:?", cell) or cell == "" for cell in cells)


def _column_map(cells: Sequence[str]) -> dict[str, int] | None:
    found: dict[str, int] = {}
    for index, cell in enumerate(cells):
        folded = cell.casefold()
        for key, words in _HEADER_WORDS:
            if key in found:
                continue
            if any(word in folded for word in words):
                found[key] = index
                break
    if not {"duration", "timeout", "result", "exit"} <= found.keys():
        return None
    if "label" not in found:
        used = set(found.values())
        found["label"] = next((index for index in range(len(cells)) if index not in used), 0)
    return found


def _mentions_id(cell: str, run_id: str) -> bool:
    return re.search(
        rf"(?<![A-Za-z0-9_-]){re.escape(run_id)}(?![A-Za-z0-9_-])",
        cell,
    ) is not None


def _first_whole(cell: str) -> int | None:
    match = re.search(r"-?\d+(?:\.\d+)?", cell)
    if match is None:
        return None
    number = float(match.group())
    if not number.is_integer():
        return None
    return int(number)


def _unnegated_job_results(cell: str) -> set[str]:
    folded = _expand_negation_words(cell.casefold())
    found: set[str] = set()
    occupied: list[tuple[int, int]] = []
    for name in _JOB_RESULTS:
        for match in re.finditer(
            rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])",
            folded,
        ):
            span = match.span()
            if any(span[0] < hi and span[1] > lo for lo, hi in occupied):
                continue
            occupied.append(span)
            if _span_negated(folded, span[0], span[1]):
                continue
            found.add(name)
    return found


def _result_token(cell: str, result: str) -> bool:
    return _unnegated_job_results(cell) == {result}


def _cell(cells: Sequence[str], mapping: Mapping[str, int], key: str) -> str:
    index = mapping[key]
    if index >= len(cells):
        return ""
    return cells[index]


def _job_row_ok(cells: Sequence[str], mapping: Mapping[str, int], run: Any) -> bool:
    if _first_whole(_cell(cells, mapping, "duration")) != math.ceil(run.duration_s):
        return False
    if _first_whole(_cell(cells, mapping, "timeout")) != math.ceil(run.timeout_s):
        return False
    if _first_whole(_cell(cells, mapping, "exit")) != run.exit_code:
        return False
    return _result_token(_cell(cells, mapping, "result"), run.result)


def _table_covers_runs(prose: Sequence[str], report: LocalCiReport) -> bool:
    confirmed: set[str] = set()
    for rows in _markdown_tables(prose):
        mapping = dict(_DEFAULT_COLUMNS)
        header: list[str] | None = None
        for cells in rows:
            if _is_separator(cells):
                continue
            mapped = _column_map(cells)
            if mapped is None:
                continue
            label = _cell(cells, mapped, "label")
            if any(_mentions_id(label, run.id) for run in report.runs):
                continue
            mapping = mapped
            header = cells
            break
        for cells in rows:
            if _is_separator(cells) or cells is header:
                continue
            label = _cell(cells, mapping, "label")
            mentioned = [run for run in report.runs if _mentions_id(label, run.id)]
            if len(mentioned) != 1:
                continue
            run = mentioned[0]
            if not _job_row_ok(cells, mapping, run):
                return False
            confirmed.add(run.id)
    return all(run.id in confirmed for run in report.runs)


def _labeled(prose: Sequence[str], label: str) -> list[str]:
    return [_label_text(line) for line in prose if _starts_with_label(line, label)]


def report_fact_reasons(
    body: str,
    report: LocalCiReport,
    *,
    base_sha: str | None,
    changed_paths: Sequence[str] | None,
) -> list[str]:
    """Require the outcome facts to match this pull request.

    Wording, punctuation, column order, and an extra sentence do not matter.
    The path count and the markdown claim come from the guard's inventory.
    ``Changed paths: unknown`` does not satisfy a known inventory. A later
    use of the word unknown does not undo a count and markdown claim that
    already match. An omission reason is accepted when it is true of that
    inventory, not only when it is the runner's preferred sentence.
    """
    reasons: list[str] = []
    if (
        changed_paths is None
        or any(not isinstance(path, str) for path in changed_paths)
    ):
        reasons.append("report changed paths unavailable")
    if not isinstance(base_sha, str) or HEAD_RE.fullmatch(base_sha) is None:
        reasons.append("report base is unavailable")
    if reasons or not isinstance(body, str):
        if not isinstance(body, str):
            _add_reason(reasons, "report outcome does not match the pull request")
        return reasons

    prose = _prose_outside_fences(body)
    heads = _labeled(prose, "Head")
    recorded = _labeled(prose, "Recorded")
    bases = _labeled(prose, "Base")
    policies = _labeled(prose, "Policy")
    changed = _changed_path_chunks(prose)
    if (
        not heads
        or any(not _sha_line_ok(line, report.head) for line in heads)
        or not recorded
        or any(not _recorded_line_ok(line, report.recorded_at) for line in recorded)
        or not bases
        or any(not _sha_line_ok(line, base_sha) for line in bases)
        or not policies
        or any(
            ".github/a38.json" not in line or not _sha_line_ok(line, base_sha)
            for line in policies
        )
        or not changed
        or any(not _changed_paths_ok(line, changed_paths) for line in changed)
        or not _table_covers_runs(prose, report)
    ):
        _add_reason(reasons, "report outcome does not match the pull request")

    not_applicable = any(run.result == "not_applicable" for run in report.runs)
    omitted = _labeled(prose, "Omitted")
    if not not_applicable:
        if omitted:
            _add_reason(reasons, "report states an omission without not_applicable")
    else:
        accepted = _accepted_omit_reasons(changed_paths)
        if not omitted or not accepted or any(
            not (claimed := _omit_claims(line)) or not claimed <= accepted
            for line in omitted
        ):
            _add_reason(reasons, "report omission does not match the change set")
    if report.schema == SCHEMA_V2:
        _add_v2_outcome_reasons(
            reasons,
            report,
            base_sha=base_sha,
            changed_paths=changed_paths,
            not_applicable=not_applicable,
        )
    return reasons


def _add_v2_outcome_reasons(
    reasons: list[str],
    report: LocalCiReport,
    *,
    base_sha: str | None,
    changed_paths: Sequence[str] | None,
    not_applicable: bool,
) -> None:
    """The original must carry the same outcome the inventory has."""
    if (
        isinstance(base_sha, str)
        and HEAD_RE.fullmatch(base_sha) is not None
        and (
            report.base != base_sha
            or report.policy_sha != base_sha
            or report.policy_path != ".github/a38.json"
        )
    ):
        _add_reason(reasons, "report outcome does not match the pull request")
    if changed_paths is not None and all(isinstance(path, str) for path in changed_paths):
        count = len(changed_paths)
        all_md = count > 0 and all(path.endswith(".md") for path in changed_paths)
        if report.changed_count != count or report.all_markdown is not all_md:
            _add_reason(reasons, "report outcome does not match the pull request")
    elif report.changed_count is not None or report.all_markdown is not None:
        _add_reason(reasons, "report outcome does not match the pull request")
    if not not_applicable:
        if report.omitted is not None:
            _add_reason(reasons, "report states an omission without not_applicable")
        return
    accepted = (
        _accepted_omit_reasons(changed_paths)
        if changed_paths is not None and all(isinstance(path, str) for path in changed_paths)
        else frozenset()
    )
    if not isinstance(report.omitted, str) or report.omitted not in accepted:
        _add_reason(reasons, "report omission does not match the change set")


def _write_report(
    output: Path,
    payload: Mapping[str, Any],
    *,
    base_sha: str | None = None,
    changed_paths: Sequence[str] | None = None,
    omit_reason: str | None = None,
) -> None:
    report = _report_from_dict(payload)
    text = (
        "EN:\nThe A38 report below records the checks, results and durations.\n\n"
        "DE:\nDer A38-Bericht unten dokumentiert die Prüfungen, Ergebnisse und Laufzeiten.\n\n"
        "<details>\n<summary>Details</summary>\n\n"
        + _report_facts(
            report,
            base_sha=base_sha,
            changed_paths=changed_paths,
            omit_reason=omit_reason,
        )
        + f"{_report_table(report)}\n"
        "Durations and timeouts rounded up to whole seconds / "
        "Laufzeiten und Zeitlimits auf ganze Sekunden aufgerundet.\n\n"
        "<details>\n<summary>Original report / Originalbericht</summary>\n\n"
        f"{render_block(report)}\n"
        "</details>\n\n"
        "</details>\n"
    )
    _write_bytes_atomic(output, text.encode("utf-8"))


_active_job_procs: list[subprocess.Popen[Any]] = []
_active_job_procs_lock = threading.Lock()


def _terminate_active_job_procs() -> None:
    # SIGTERM is delivered to the main thread; workers will not see KeyboardInterrupt.
    with _active_job_procs_lock:
        procs = list(_active_job_procs)
    for proc in procs:
        _terminate_process_group(proc)


def _terminate_process_group(proc: subprocess.Popen[Any]) -> None:
    """Allow the leader up to 30 seconds for owned-resource cleanup on TERM.

    An exited leader returns immediately; surviving descendants are then killed.
    A timed-out job remains a timeout, with cleanup included in measured duration.
    The grace can add up to 30 seconds beyond the job timeout, plus final reaping.
    """
    try:
        if hasattr(os, "killpg") and proc.pid:
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=TERMINATION_GRACE_S)
    except subprocess.TimeoutExpired:
        pass
    try:
        if hasattr(os, "killpg") and proc.pid:
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass


def _run_one_job(
    *,
    repo_path: Path,
    command: str,
    timeout_s: float,
    log_path: Path,
    env: Mapping[str, str],
) -> tuple[str, int, float]:
    start = time.monotonic()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "wb") as log_handle:
        _chmod_owner_rw(log_path)
        popen_kwargs: dict[str, Any] = {
            "args": ["bash", "-o", "pipefail", "-c", command],
            "cwd": str(repo_path),
            "env": dict(env),
            "stdout": log_handle,
            "stderr": subprocess.STDOUT,
        }
        if sys.platform != "win32":
            popen_kwargs["start_new_session"] = True
        proc = subprocess.Popen(**popen_kwargs)  # noqa: S603
        with _active_job_procs_lock:
            _active_job_procs.append(proc)
        timed_out = False
        try:
            try:
                proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate_process_group(proc)
            except KeyboardInterrupt:
                _terminate_process_group(proc)
                raise
        finally:
            # The session leader may have exited while descendants still run.
            _terminate_process_group(proc)
            with _active_job_procs_lock:
                try:
                    _active_job_procs.remove(proc)
                except ValueError:
                    pass
    duration = time.monotonic() - start
    if timed_out or duration > timeout_s:
        return "timeout", proc.returncode if proc.returncode is not None else -1, duration
    code = proc.returncode if proc.returncode is not None else -1
    if code == 0:
        return "pass", 0, duration
    return "fail", code, duration


def _interrupt_run(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt


def _force_fail_runs(runs: list[dict[str, Any]]) -> None:
    for run in runs:
        run["result"] = "fail"
        if run.get("exit_code") == 0:
            run["exit_code"] = 1


def _job_conflict_keys(job: Mapping[str, Any]) -> frozenset[str]:
    lock = job["lock"]
    if lock == DOCKER_HEAVY_LOCK:
        return frozenset({DOCKER_HEAVY_LOCK})
    keys: set[str] = {"#cpu"}
    if isinstance(lock, str) and lock:
        keys.add(lock)
    return frozenset(keys)


def _max_in_flight(env: Mapping[str, str]) -> int:
    raw = env.get("A38_MAX_IN_FLIGHT", "2")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise A38Error("A38_MAX_IN_FLIGHT must be an integer >= 1") from None
    if value < 1:
        raise A38Error("A38_MAX_IN_FLIGHT must be an integer >= 1")
    return value


def _await_in_flight_as_interrupted(
    in_flight: dict[
        concurrent.futures.Future[Any], tuple[Mapping[str, Any], float]
    ],
    run_by_id: dict[str, dict[str, Any]],
    reasons: list[str],
) -> None:
    if in_flight:
        concurrent.futures.wait(tuple(in_flight))
    for fut, (job, started) in list(in_flight.items()):
        ident = str(job["id"])
        try:
            fut.result()
        except Exception:
            pass
        if ident not in run_by_id:
            run_by_id[ident] = _run_entry(
                ident=ident,
                name=str(job["name"]),
                command=str(job["command"]),
                result="error",
                exit_code=-1,
                duration_s=time.monotonic() - started,
                timeout_s=float(job["timeout_s"]),
            )
            reasons.append(f"{ident}: interrupted")
    in_flight.clear()


def _prepare_run_output(
    repo_path: Path,
    output: Path,
    logs_dir: Path,
    *,
    policy_path: Path | None = None,
    run: RunFn | None = None,
) -> Path:
    """Validate destinations, then remove prior evidence before run preflight."""
    runner = run or _default_run
    repo_path = Path(repo_path)
    output = Path(output)
    logs_dir = Path(logs_dir)
    if not repo_path.is_dir():
        raise A38Error("repo path is not a directory")

    root = _repo_root(repo_path, run=runner)
    if not _is_outside(output, root):
        raise A38Error("output path must be outside the repository")
    if not _is_outside(logs_dir, root):
        raise A38Error("logs-dir must be outside the repository")

    # Validate writable locations before invalidating any prior report. Never
    # follow symlinks or overwrite the manifest when input/output paths collide.
    for path in (output, logs_dir):
        if path.is_symlink():
            raise A38Error("output and logs paths must not be symlinks")
    if output.exists() and not output.is_file():
        raise A38Error("output path must be a regular file")
    if logs_dir.exists() and not logs_dir.is_dir():
        raise A38Error("logs-dir must be a directory")
    if output.resolve() == logs_dir.resolve():
        raise A38Error("output and logs-dir must be distinct")
    if policy_path is not None:
        source = Path(policy_path).resolve()
        same_output = output.exists() and source.exists() and output.samefile(source)
        if source == output.resolve() or same_output or source.is_relative_to(logs_dir.resolve()):
            raise A38Error("policy path must not collide with output or logs")
    output.unlink(missing_ok=True)

    return root


def run_policy(
    repo_path: Path,
    policy: dict,
    *,
    output: Path,
    logs_dir: Path,
    base_sha: str | None = None,
    private: bool | None = None,
    run: RunFn | None = None,
    repository: str | None = None,
    policy_path: Path | None = None,
    github_session: str | None = None,
    config_home: Path | None = None,
) -> dict:
    """Execute policy jobs and write a complete ``dfx-local-ci/v2`` report."""
    runner = run or _default_run
    output = Path(output)
    logs_dir = Path(logs_dir)
    root = _prepare_run_output(
        repo_path, output, logs_dir, policy_path=policy_path, run=runner,
    )

    # Revalidate the complete manifest for programmatic callers too.
    try:
        policy = load_policy(json.dumps(dict(policy)))
    except (TypeError, ValueError) as exc:
        raise A38Error(f"invalid policy: {exc}") from exc
    required = _policy_required_ids(policy)
    jobs = list(policy["jobs"])
    for job in jobs:
        log_path = logs_dir / f"{job['id']}.log"
        if log_path.resolve() == output.resolve():
            raise A38Error("output path must not collide with a job log")
        if log_path.is_symlink() or (
            log_path.exists() and (not log_path.is_file() or log_path.stat().st_nlink > 1)
        ):
            raise A38Error("job log must be a regular file, not a symlink")
    if base_sha is None or base_sha == "":
        raise A38Error("base-sha is required")
    if repository is not None and (
        not isinstance(repository, str) or REPO_RE.fullmatch(repository) is None
    ):
        raise A38Error("repository must be owner/name")
    _require_clean_tree(root, run=runner)
    head = _head_sha(root, run=runner)
    base = _ensure_commit_exists(root, base_sha, run=runner)
    repo = repository if repository is not None else _origin_repo(root, run=runner)
    is_private = _resolve_private(
        root, private, run=runner, repository=repo,
        github_session=github_session, config_home=config_home,
    )
    logs_dir.mkdir(parents=True, exist_ok=True)
    omit = set(policy.get("readme_only_omit") or [])
    # One git inventory: a second listing must not flip omit classification.
    paths = git_changed_paths(root, base, head)
    markdown_only, guard_docs_only = markdown_and_guard_docs_only(paths)
    if paths is None or len(paths) > MAX_FILES:
        readme_only = False
    else:
        readme_only = bool(omit) and paths_are_readme_only(paths)
    if omit and not readme_only:
        omit = set()
    if markdown_only:
        omit = set(required)
        guard_docs_only = False
    elif guard_docs_only:
        omit = set(required)

    env = _job_env(head, base)
    max_in_flight = _max_in_flight(env)
    _preflight_jobs([job for job in jobs if str(job["id"]) not in omit], env)
    reasons: list[str] = []
    interrupted = False
    drift = False
    if markdown_only:
        omit_log = "omitted: markdown-only change set\n"
        omit_reason = "markdown-only change set"
    elif guard_docs_only:
        omit_log = "omitted: guard-docs change set\n"
        omit_reason = "guard-docs change set"
    else:
        omit_log = "omitted: README-only change set\n"
        omit_reason = "README-only change set" if readme_only else None
    run_by_id: dict[str, dict[str, Any]] = {}
    stop_starting = False

    def execute_job(job: Mapping[str, Any]) -> dict[str, Any]:
        ident = str(job["id"])
        name = str(job["name"])
        command = str(job["command"])
        timeout_s = float(job["timeout_s"])
        log_path = logs_dir / f"{ident}.log"
        started = time.monotonic()
        result, exit_code, duration_s = _run_one_job(
            repo_path=root,
            command=command,
            timeout_s=timeout_s,
            log_path=log_path,
            env=env,
        )
        if result == "pass" and duration_s > timeout_s:
            result = "timeout"
        return _run_entry(
            ident=ident,
            name=name,
            command=command,
            result=result,
            exit_code=exit_code,
            duration_s=duration_s,
            timeout_s=timeout_s,
        )

    previous_sigterm = None
    if threading.current_thread() is threading.main_thread():
        previous_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, _interrupt_run)
    in_flight: dict[
        concurrent.futures.Future[dict[str, Any]], tuple[Mapping[str, Any], float]
    ] = {}
    pending: list[Mapping[str, Any]] = []
    try:
        for job in jobs:
            ident = str(job["id"])
            if ident in omit:
                (logs_dir / f"{ident}.log").write_text(omit_log, encoding="utf-8")
                run_by_id[ident] = _run_entry(
                    ident=ident,
                    name=str(job["name"]),
                    command=str(job["command"]),
                    result="not_applicable",
                    exit_code=0,
                    duration_s=0.0,
                    timeout_s=float(job["timeout_s"]),
                )
            else:
                pending.append(job)

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_in_flight) as pool:
          try:
            while pending or in_flight:
                if not stop_starting:
                    inflight_keys: set[str] = set()
                    for running_job, _started in in_flight.values():
                        inflight_keys.update(_job_conflict_keys(running_job))
                    still_pending: list[Mapping[str, Any]] = []
                    for job in pending:
                        keys = _job_conflict_keys(job)
                        if (
                            len(in_flight) >= max_in_flight
                            or keys & inflight_keys
                        ):
                            still_pending.append(job)
                            continue
                        started = time.monotonic()
                        future = pool.submit(execute_job, job)
                        in_flight[future] = (job, started)
                        inflight_keys.update(keys)
                    pending = still_pending

                if not in_flight:
                    break

                try:
                    done, _pending_futs = concurrent.futures.wait(
                        tuple(in_flight),
                        return_when=concurrent.futures.FIRST_COMPLETED,
                    )
                except KeyboardInterrupt:
                    interrupted = True
                    stop_starting = True
                    reasons.append("run interrupted")
                    _terminate_active_job_procs()
                    _await_in_flight_as_interrupted(in_flight, run_by_id, reasons)
                    break

                for future in done:
                    job, started = in_flight.pop(future)
                    ident = str(job["id"])
                    try:
                        entry = future.result()
                    except KeyboardInterrupt:
                        interrupted = True
                        stop_starting = True
                        entry = _run_entry(
                            ident=ident,
                            name=str(job["name"]),
                            command=str(job["command"]),
                            result="error",
                            exit_code=-1,
                            duration_s=time.monotonic() - started,
                            timeout_s=float(job["timeout_s"]),
                        )
                        reasons.append(f"{ident}: interrupted")
                        _terminate_active_job_procs()
                        _await_in_flight_as_interrupted(in_flight, run_by_id, reasons)
                        run_by_id[ident] = entry
                        pending.clear()
                        break
                    except Exception as exc:
                        entry = _run_entry(
                            ident=ident,
                            name=str(job["name"]),
                            command=str(job["command"]),
                            result="error",
                            exit_code=-1,
                            duration_s=time.monotonic() - started,
                            timeout_s=float(job["timeout_s"]),
                        )
                        reasons.append(f"{ident}: execution failed ({type(exc).__name__})")
                    run_by_id[ident] = entry
                    if entry["result"] != "pass" or entry["exit_code"] != 0:
                        if f"{ident}: result is {entry['result']}" not in reasons:
                            reasons.append(f"{ident}: result is {entry['result']}")
                    if interrupted:
                        break
                    try:
                        _require_clean_tree(root, run=runner)
                        after = _head_sha(root, run=runner)
                    except A38Error as exc:
                        drift = True
                        stop_starting = True
                        reasons.append(f"working tree or HEAD drifted: {exc}")
                        pending.clear()
                        _terminate_active_job_procs()
                        _await_in_flight_as_interrupted(in_flight, run_by_id, reasons)
                        break
                    if after != head:
                        drift = True
                        stop_starting = True
                        reasons.append("HEAD drifted during run")
                        pending.clear()
                        _terminate_active_job_procs()
                        _await_in_flight_as_interrupted(in_flight, run_by_id, reasons)
                        break
          except KeyboardInterrupt:
            interrupted = True
            stop_starting = True
            reasons.append("run interrupted")
            _terminate_active_job_procs()
            _await_in_flight_as_interrupted(in_flight, run_by_id, reasons)
    except KeyboardInterrupt:
        interrupted = True
        reasons.append("run interrupted")
        _terminate_active_job_procs()
        _await_in_flight_as_interrupted(in_flight, run_by_id, reasons)
    finally:
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)

    runs: list[dict[str, Any]] = []
    for job in jobs:
        ident = str(job["id"])
        if ident in run_by_id:
            runs.append(run_by_id[ident])
            continue
        runs.append(
            _run_entry(
                ident=ident,
                name=str(job["name"]),
                command=str(job["command"]),
                result="error",
                exit_code=-1,
                duration_s=0.0,
                timeout_s=float(job["timeout_s"]),
            )
        )
        reasons.append(f"{ident}: not run")

    if drift:
        _force_fail_runs(runs)
        if "HEAD drifted during run" not in reasons and not any(
            r.startswith("working tree or HEAD drifted") for r in reasons
        ):
            reasons.append("repository drifted")

    # Final clean/HEAD check for a successful report.
    if not drift and not interrupted:
        try:
            _require_clean_tree(root, run=runner)
            final_head = _head_sha(root, run=runner)
            if final_head != head:
                drift = True
                reasons.append("HEAD drifted after run")
                _force_fail_runs(runs)
        except A38Error as exc:
            drift = True
            reasons.append(f"post-run tree check failed: {exc}")
            _force_fail_runs(runs)
        except KeyboardInterrupt:
            interrupted = True
            reasons.append("final tree check interrupted")

    # Interruption can occur after the last job passed but before its checkout
    # integrity check finished. Never serialize those rows as usable evidence.
    if interrupted:
        for run_item in runs:
            if run_item["result"] in {"pass", "not_applicable"}:
                run_item["result"] = "error"
                run_item["exit_code"] = -1

    recorded_at = _utc_now_stamp()
    payload = _build_report_dict(
        repo=repo,
        head=head,
        private=is_private,
        recorded_at=recorded_at,
        required=required,
        runs=runs,
        base=base,
        changed_paths=paths if isinstance(paths, list) else None,
        omit_reason=omit_reason,
        readme_only=readme_only,
        markdown_only=markdown_only,
    )
    try:
        _write_report(
            output,
            payload,
            base_sha=base,
            changed_paths=paths,
            omit_reason=omit_reason,
        )
    except LocalCiError as exc:
        raise A38Error(f"failed to write report: {exc}") from exc

    ok = (
        not interrupted
        and not drift
        and all(
            run_item["exit_code"] == 0
            and run_item["result"] in {"pass", "not_applicable"}
            for run_item in runs
        )
    )
    status = "pass" if ok else "fail"
    if ok:
        reasons = []
    return {
        "ok": ok,
        "status": status,
        "reasons": reasons,
        "repo": repo,
        "head": head,
        "private": is_private,
        "required": list(required),
        "output": str(output),
    }


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise A38Error(f"cannot read {path}: {exc}") from exc


def _cmd_policy(args: argparse.Namespace) -> int:
    text = _read_text(Path(args.file))
    policy = load_policy(text)
    json.dump(policy, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    if args.private and args.public:
        print("a38: --private and --public are mutually exclusive", file=sys.stderr)
        return 1
    if not args.private and not args.public:
        print("a38: verify requires --private or --public", file=sys.stderr)
        return 1
    try:
        policy = load_policy(_read_text(Path(args.policy)))
        comment = _read_text(Path(args.file))
    except A38Error as exc:
        payload = {"ok": False, "status": "fail", "reasons": [str(exc)]}
        json.dump(payload, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 1
    verdict = verify_report(
        comment,
        policy,
        repo=args.repo,
        head=args.head,
        private=bool(args.private),
    )
    json.dump(verdict, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if verdict.get("ok") is True and verdict.get("status") == "pass" else 1


def _cmd_run(args: argparse.Namespace) -> int:
    private: bool | None
    if args.private:
        private = True
    elif args.public:
        private = False
    else:
        private = None
    try:
        # Establish a safe output destination before any policy/value preflight.
        # Unsafe/ambiguous paths and parser usage errors never delete files.
        _prepare_run_output(
            Path(args.repo), Path(args.output), Path(args.logs_dir),
            policy_path=Path(args.policy),
        )
        if args.private and args.public:
            raise A38Error("--private and --public are mutually exclusive")
        policy = load_policy(_read_text(Path(args.policy)))
        verdict = run_policy(
            Path(args.repo),
            policy,
            output=Path(args.output),
            logs_dir=Path(args.logs_dir),
            base_sha=args.base_sha,
            private=private,
            repository=args.repository,
            policy_path=Path(args.policy),
            github_session=args.github_session,
        )
    except (A38Error, OSError) as exc:
        print(f"a38: {exc}", file=sys.stderr)
        return 1
    json.dump(
        {key: verdict[key] for key in ("ok", "status", "reasons") if key in verdict},
        sys.stdout,
        indent=2,
        sort_keys=True,
    )
    sys.stdout.write("\n")
    return 0 if verdict.get("ok") is True else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m agent_cli.a38", description="A38 local CI")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Execute policy jobs and write a local-CI report")
    run_p.add_argument("--repo", required=True, help="Path to the repository root")
    run_p.add_argument("--repository", help="Target PR base repository owner/name (defaults to origin)")
    run_p.add_argument("--policy", required=True, help="Path to .github/a38.json (or equivalent)")
    run_p.add_argument("--output", required=True, help="Output path for the marked report")
    run_p.add_argument("--logs-dir", required=True, dest="logs_dir", help="Directory for job logs")
    run_p.add_argument("--base-sha", required=True, dest="base_sha", help="Base commit SHA")
    run_p.add_argument("--private", action="store_true", help="Mark report private=true")
    run_p.add_argument("--public", action="store_true", help="Mark report private=false")
    run_p.add_argument("--github-session", help="Explicit GitHub account session for visibility lookup")
    run_p.set_defaults(func=_cmd_run)

    verify_p = sub.add_parser("verify", help="Verify an author report against a trusted policy")
    verify_p.add_argument("--policy", required=True, help="Path to trusted policy JSON")
    verify_p.add_argument("--file", required=True, help="Path to comment or report file")
    verify_p.add_argument("--repo", required=True, help="Expected owner/name")
    verify_p.add_argument("--head", required=True, help="Expected 40-character head SHA")
    verify_p.add_argument("--private", action="store_true", help="Expect private=true")
    verify_p.add_argument("--public", action="store_true", help="Expect private=false")
    verify_p.set_defaults(func=_cmd_verify)

    policy_p = sub.add_parser("policy", help="Validate and print a policy manifest")
    policy_p.add_argument("--file", required=True, help="Path to policy JSON")
    policy_p.set_defaults(func=_cmd_policy)

    add_job_parser(sub)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except A38Error as exc:
        print(f"a38: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
