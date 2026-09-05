"""A38 policy load, local runner, and pure report verification.

Uses the frozen ``dfx-local-ci/v1`` comment schema. Report consistency is
checked against a trusted policy manifest; this is not cryptographic proof
of execution. Guard and backend integration live elsewhere.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
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

from .local_ci import (
    BEGIN_MARK,
    END_MARK,
    HEAD_RE,
    ID_RE,
    LocalCiError,
    LocalCiReport,
    REPO_RE,
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
JOB_KEYS = frozenset({"id", "name", "command", "timeout_s", "workflow", "job"})
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


class A38Error(ValueError):
    """Invalid A38 policy or runner preflight failure."""


def _reject_nonfinite_constant(name: str) -> None:
    raise A38Error(f"JSON contains non-finite number: {name}")


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
            object_pairs_hook=_object_pairs_no_duplicates,
        )
    except A38Error:
        raise
    except json.JSONDecodeError as exc:
        raise A38Error(f"JSON is invalid: {exc.msg}") from exc


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


def load_policy(text: str) -> dict:
    """Validate an A38 manifest and return the normalized JSON dict."""
    payload = _loads_policy_json(text)
    if not isinstance(payload, dict):
        raise A38Error("policy must be a JSON object")
    _require_keys(payload, POLICY_KEYS, "policy")
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
        _require_keys(item, JOB_KEYS, f"jobs[{index}]")
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
        jobs.append(
            {
                "id": ident,
                "name": _validate_name(item["name"], f"jobs[{index}].name"),
                "command": _validate_command(item["command"], f"jobs[{index}].command"),
                "timeout_s": timeout_out,
                "workflow": workflow,
                "job": gh_job,
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

    return {
        "schema": SCHEMA_ID,
        "standard": STANDARD_ID,
        "documentation": DOCUMENTATION_PATH,
        "mode": mode,
        "jobs": jobs,
        "exclusions": exclusions,
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
) -> bool:
    if private is not None:
        if not isinstance(private, bool):
            raise A38Error("private must be a boolean")
        return private
    completed = run(
        ["gh", "repo", "view", repository, "--json", "isPrivate"],
        repo_path,
        None,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "gh repo view failed").strip()
        raise A38Error(detail or "gh repo view failed")
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
) -> dict[str, Any]:
    return {
        "schema": "dfx-local-ci/v1",
        "repo": repo,
        "head": head,
        "private": private,
        "recorded_at": recorded_at,
        "required": list(required),
        "runs": [dict(run) for run in runs],
    }


def _report_from_dict(payload: Mapping[str, Any]) -> LocalCiReport:
    # Re-parse through local_ci to keep a single schema authority.
    block = (
        f"{BEGIN_MARK}\n```json\n"
        f"{json.dumps(payload, indent=2, sort_keys=True)}\n"
        f"```\n{END_MARK}\n"
    )
    return parse_comment(block)


def _write_report(output: Path, payload: Mapping[str, Any]) -> None:
    report = _report_from_dict(payload)
    text = render_block(report)
    _write_bytes_atomic(output, text.encode("utf-8"))


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
) -> dict:
    """Execute policy jobs and write a complete ``dfx-local-ci/v1`` report."""
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

    # Revalidate the complete manifest for programmatic callers too.
    try:
        policy = load_policy(json.dumps(policy))
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
    is_private = _resolve_private(root, private, run=runner, repository=repo)
    logs_dir.mkdir(parents=True, exist_ok=True)

    env = _job_env(head, base)
    runs: list[dict[str, Any]] = []
    reasons: list[str] = []
    interrupted = False
    drift = False

    previous_sigterm = None
    if threading.current_thread() is threading.main_thread():
        previous_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, _interrupt_run)
    try:
        for job in jobs:
            ident = str(job["id"])
            name = str(job["name"])
            command = str(job["command"])
            timeout_s = float(job["timeout_s"])
            log_path = logs_dir / f"{ident}.log"
            started = time.monotonic()
            try:
                result, exit_code, duration_s = _run_one_job(
                    repo_path=root,
                    command=command,
                    timeout_s=timeout_s,
                    log_path=log_path,
                    env=env,
                )
            except KeyboardInterrupt:
                interrupted = True
                runs.append(
                    _run_entry(
                        ident=ident,
                        name=name,
                        command=command,
                        result="error",
                        exit_code=-1,
                        duration_s=time.monotonic() - started,
                        timeout_s=timeout_s,
                    )
                )
                reasons.append(f"{ident}: interrupted")
                break
            except OSError as exc:
                result, exit_code, duration_s = "error", -1, time.monotonic() - started
                reasons.append(f"{ident}: execution failed ({type(exc).__name__})")
            if result == "pass" and duration_s > timeout_s:
                result = "timeout"
            runs.append(
                _run_entry(
                    ident=ident,
                    name=name,
                    command=command,
                    result=result,
                    exit_code=exit_code,
                    duration_s=duration_s,
                    timeout_s=timeout_s,
                )
            )
            if result != "pass" or exit_code != 0:
                reasons.append(f"{ident}: result is {result}")

            try:
                _require_clean_tree(root, run=runner)
                after = _head_sha(root, run=runner)
            except A38Error as exc:
                drift = True
                reasons.append(f"working tree or HEAD drifted: {exc}")
                break
            if after != head:
                drift = True
                reasons.append("HEAD drifted during run")
                break
    except KeyboardInterrupt:
        interrupted = True
        reasons.append("run interrupted")
    finally:
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)

    finished_ids = {run_item["id"] for run_item in runs}
    for job in jobs:
        ident = str(job["id"])
        if ident in finished_ids:
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
            if run_item["result"] == "pass":
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
    )
    try:
        _write_report(output, payload)
    except LocalCiError as exc:
        raise A38Error(f"failed to write report: {exc}") from exc

    ok = (
        not interrupted
        and not drift
        and all(run_item["result"] == "pass" and run_item["exit_code"] == 0 for run_item in runs)
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
    if args.private and args.public:
        print("a38: --private and --public are mutually exclusive", file=sys.stderr)
        return 1
    private: bool | None
    if args.private:
        private = True
    elif args.public:
        private = False
    else:
        private = None
    try:
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
