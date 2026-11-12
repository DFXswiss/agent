"""A38 phases for the script-owned issue coordinator.

The worker account publishes author evidence. The external second reviewer
communicates only through existing hub pings and session mail. This module
never submits a GitHub review approval and never changes draft state.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sys
import uuid
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from uuid import NAMESPACE_URL, uuid5, uuid4

from .a38 import report_fact_reasons, verify_report
from .a38_guard import POLICY_PATH, status_context_enforce
from .a38_review import (
    REVIEW_BEGIN,
    REVIEW_END,
    ReviewError,
    parse_review_block,
    render_review_record,
    validate_declaration,
    validate_review_record,
    validate_visible,
)
from .allow import (
    EXTERNAL_SECOND_REVIEW_PREFIX,
    evaluate_allow,
    external_second_review_ok,
    newest_reviews_block,
)
from .coordinator_common import (
    check_label,
    CoordinatorError,
    NonRecoverableCoordinatorError,
    Runner,
    as_int,
    coord,
    evidence_base_sha,
    gh_list,
    gh_json,
    is_sha,
    redact,
    reopen_base_bound_checklist,
    save_task,
    scoped,
    target_repo,
)
from .coordinator_config import WorkerConfig
from .coordinator_exec import run_bounded
from .coordinator_git import (
    control_dir,
    ensure_draft,
    execute_github,
    git,
    queue_activity,
    repo_cfg,
    require_git_ok,
    verify_signed_clean_head,
)
from .coordinator_lanes import (
    gate_prompt_evidence,
    invalidate_head_evidence,
    latest_gates,
    review_rules_bound,
    set_checklist,
)
from .github_act import (
    ACTIVITY_MARKER,
    A38_REPORT_BINDING_KEY,
    a38_report_activity_lock,
    a38_report_task_lock,
    coordinator_a38_report_activity_id,
    is_coordinator_a38_report_activity_for_task,
)
from .local_ci import LocalCiError, parse_comment
from .pr_guard_config import CONFIG_PATH as PR_GUARD_CONFIG_PATH
from .readme_only import (
    GITHUB_PR_FILES_CAP,
    GUARD_WORKFLOW_PATH,
    github_file_paths,
    markdown_and_guard_docs_only,
)
from .store import Store, utcnow

PROVIDER_DISPLAY = {
    "grok": "xAI",
    "codex": "OpenAI",
    "claude": "Anthropic",
    "openrouter": "OpenRouter",
}
SECOND_REVIEW_KIND = "a38.second-review/v1"
SECOND_REVIEW_NOTE_LIMIT = 1000
GITHUB_COMMENT_LIMIT = 65_536
A38_CONTEXT_READ_ATTEMPTS = 3
# The executor appends "\n" plus the activity marker with a 36-character activity id.
ACTIVITY_MARKER_LENGTH = len("\n" + ACTIVITY_MARKER.format(id="0" * 36))
# Both prompts appear twice in the comment (record and machine block) and the findings once;
# the rest of the comment (titles, summaries, model names) fits in the remaining margin.
SECOND_REVIEW_TEXT_LIMIT = 56_000
_SECOND_KEYS = frozenset(
    {"kind", "task_id", "head", "defects", "blocks_merge", "findings", "lanes"}
)
_LANE_KEYS = frozenset(
    {"id", "provider", "model", "model_number", "prompt", "status", "result"}
)


class _PullRequestBaseUnavailable(CoordinatorError):
    """Transient missing or malformed base revision from a pull-request read."""


class _PullRequestNotOpen(CoordinatorError):
    """The pull request closed before an A38 measurement context was accepted."""

    def __init__(self, state: str) -> None:
        self.state = state
        super().__init__(f"A38 pull request is already {state.lower()}")


class A38ReportCheckpointConflict(CoordinatorError):
    """Transient conflict with a fresher A38 report task checkpoint."""


def _bounded_text(value: Any, label: str, *, maximum: int = 20000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise CoordinatorError(f"{label} requires nonempty text of at most {maximum} characters")
    if "\x00" in value:
        raise CoordinatorError(f"{label} contains a NUL byte")
    return value


def _public_text(value: Any, label: str, *, maximum: int = 20000) -> str:
    """Bounded text that is published verbatim, so it must survive redaction unchanged."""
    text = _bounded_text(value, label, maximum=maximum)
    if redact(text, limit=maximum + 1) != text:
        raise CoordinatorError(f"{label} contains secret-like text or a private path")
    return text


def validate_second_review_payload(raw: Any) -> dict[str, Any]:
    """Strict, shared validator for reviewer CLI input and coordinator mail."""
    if not isinstance(raw, Mapping) or set(raw) != _SECOND_KEYS:
        raise CoordinatorError("second review must contain exactly the documented keys")
    if raw.get("kind") != SECOND_REVIEW_KIND:
        raise CoordinatorError(f"second review kind must be {SECOND_REVIEW_KIND}")
    task_id = _bounded_text(raw.get("task_id"), "second review task_id", maximum=36)
    try:
        parsed_task = uuid.UUID(task_id)
    except ValueError as exc:
        raise CoordinatorError("second review task_id must be a UUID") from exc
    if str(parsed_task) != task_id:
        raise CoordinatorError("second review task_id must be a canonical UUID")
    head = raw.get("head")
    if not isinstance(head, str) or not is_sha(head):
        raise CoordinatorError("second review head must be a lowercase 40-hex SHA")
    defects = raw.get("defects")
    if isinstance(defects, bool) or not isinstance(defects, int) or defects < 0:
        raise CoordinatorError("second review defects must be an integer >= 0")
    blocks_merge = raw.get("blocks_merge")
    if not isinstance(blocks_merge, bool):
        raise CoordinatorError("second review blocks_merge must be a boolean")
    if blocks_merge and defects < 1:
        raise CoordinatorError("second review blocking defects must be counted")
    findings = raw.get("findings")
    if not isinstance(findings, str) or len(findings) > 20000 or "\x00" in findings:
        raise CoordinatorError("second review findings must be text of at most 20000 characters")
    if defects >= 1 and not findings.strip():
        raise CoordinatorError("second review findings are required when defects >= 1")
    if defects == 0 and findings.strip():
        raise CoordinatorError("second review findings require defects >= 1")
    lanes = raw.get("lanes")
    if not isinstance(lanes, list) or len(lanes) != 2:
        raise CoordinatorError("second review requires conformity-b and logic-b lanes")
    validated_lanes: list[dict[str, str]] = []
    for index, expected in enumerate(("conformity-b", "logic-b")):
        lane = lanes[index]
        if not isinstance(lane, Mapping) or set(lane) != _LANE_KEYS:
            raise CoordinatorError(f"second review lane {expected} has unknown or missing keys")
        if lane.get("id") != expected:
            raise CoordinatorError("second review lanes must be conformity-b then logic-b")
        if lane.get("status") != "complete" or lane.get("result") != "pass":
            raise CoordinatorError(f"second review lane {expected} must be complete/pass")
        validated_lanes.append(
            {
                "id": expected,
                "provider": _public_text(lane.get("provider"), f"{expected} provider", maximum=200),
                "model": _public_text(lane.get("model"), f"{expected} model", maximum=200),
                "model_number": _public_text(
                    lane.get("model_number"), f"{expected} model_number", maximum=200
                ),
                "prompt": _public_text(lane.get("prompt"), f"{expected} prompt"),
                "status": "complete",
                "result": "pass",
            }
        )
    if validated_lanes[0]["model"] != validated_lanes[1]["model"]:
        raise CoordinatorError("both second review lanes must name the same model")
    published = len(findings) + 2 * sum(
        len(json.dumps(lane["prompt"], ensure_ascii=False)) for lane in validated_lanes
    )
    if published > SECOND_REVIEW_TEXT_LIMIT:
        raise CoordinatorError(
            "second review prompts and findings are too long for one review comment"
        )
    return {
        "kind": SECOND_REVIEW_KIND,
        "task_id": task_id,
        "head": head,
        "defects": defects,
        "blocks_merge": blocks_merge,
        "findings": findings,
        "lanes": validated_lanes,
    }


def _a38_state(c: dict[str, Any]) -> dict[str, Any]:
    value = c.setdefault("a38", {})
    if not isinstance(value, dict):
        value = {}
        c["a38"] = value
    return value


def _observation(c: dict[str, Any], text: str) -> None:
    observations = c.setdefault("observations", [])
    if not isinstance(observations, list):
        observations = []
        c["observations"] = observations
    observations.append({"at": utcnow(), "text": redact(text, limit=1000)})
    del observations[:-50]


def _record_local_check(
    store: Store,
    task: Mapping[str, Any],
    *,
    name: str,
    command: str,
    passed: bool | None,
    output: str,
    head: str,
) -> None:
    ident = str(uuid.uuid4())
    store.write(
        "local_check",
        "insert",
        ident,
        {
            "id": ident,
            "task_id": task["id"],
            "name": name,
            "command": command,
            "result": "skip" if passed is None else ("pass" if passed else "fail"),
            "output": redact(output),
            "ran_at": utcnow(),
            "head_sha": head,
        },
    )


def _a38_failure_summary(report_path: Path, logs_dir: Path, fallback: str) -> str:
    lines: list[str] = []
    try:
        report = parse_comment(report_path.read_text(encoding="utf-8"))
        for run in report.runs:
            if run.result != "pass" or run.exit_code != 0:
                lines.append(f"{run.id} ({run.name}): {run.result}, exit {run.exit_code}")
    except (OSError, UnicodeError, LocalCiError):
        pass
    if logs_dir.is_dir():
        for path in sorted(logs_dir.iterdir())[:10]:
            if not path.is_file():
                continue
            try:
                tail = path.read_text(encoding="utf-8", errors="replace")[-1200:].strip()
            except OSError:
                continue
            if tail:
                lines.append(f"{path.name} tail:\n{tail}")
    return redact("\n".join(lines) or fallback, limit=6000)


def _run_cli(argv: list[str], *, worker: WorkerConfig, cwd: Path) -> Any:
    return run_bounded(
        [sys.executable, "-m", "agent_cli.main", "a38", *argv],
        timeout=worker.check_timeout,
        cwd=str(cwd),
        clear_ambient_github=True,
    )


def _own_report_activity(
    store: Store, worker: WorkerConfig, task: dict[str, Any], activity: dict[str, Any], head: str
) -> bool:
    """A recovered report activity counts only if this device wrote it for this task's PR.

    The activity id is derived from the task and head, so origin, session and target
    decide whether a replicated row on that id may be reused."""
    payload = activity.get("payload")
    c = coord(task)
    number = as_int(c.get("pr_number") or task.get("ref"))
    return (
        activity.get("_origin_device_id") == store.device_id()
        and activity.get("session_id") == worker.session_id
        and activity.get("type") == "comment.post"
        and isinstance(payload, dict)
        and payload.get("exact_body") is True
        and payload.get("repo") == target_repo(task)
        and as_int(payload.get("number")) == number
        and isinstance(payload.get("body"), str)
        and bool(payload["body"])
    )


def _pull_a38_context(
    api: Runner,
    *,
    repo: str,
    number: int,
    expected_head: str,
    expected_base: str,
) -> tuple[str, list[str], bool, bool]:
    """Return metadata and waiver classification from one stable PR snapshot."""
    metadata_argv = [
        "gh",
        "pr",
        "view",
        str(number),
        "--repo",
        repo,
        "--json",
        "headRefOid,baseRefOid,baseRefName,state",
    ]

    def metadata() -> tuple[str, str, str, str]:
        view = gh_json(api, metadata_argv)
        if not isinstance(view, dict):
            raise CoordinatorError("A38 pull request metadata is invalid")
        head = str(view.get("headRefOid") or "").lower()
        base_sha = str(view.get("baseRefOid") or "").lower()
        base_name = str(view.get("baseRefName") or "")
        state = str(view.get("state") or "").upper()
        if state in ("MERGED", "CLOSED"):
            raise _PullRequestNotOpen(state)
        if state != "OPEN":
            raise CoordinatorError("A38 pull request state is invalid")
        return head, base_sha, base_name, state

    last_before: tuple[str, str, str, str] | None = None
    for _attempt in range(A38_CONTEXT_READ_ATTEMPTS):
        before = metadata()
        last_before = before
        if (
            before[0] != expected_head
            or not is_sha(before[1])
            or before[2] != expected_base
        ):
            continue
        entries = gh_list(
            api,
            [
                "gh",
                "api",
                "--paginate",
                "--slurp",
                f"repos/{repo}/pulls/{number}/files?per_page=100",
            ],
        )
        # GitHub caps this endpoint without an explicit truncation marker.
        if len(entries) >= GITHUB_PR_FILES_CAP:
            raise CoordinatorError("A38 pull request file inventory is incomplete")
        paths = github_file_paths(entries)
        if paths is None:
            raise CoordinatorError("A38 pull request file inventory is invalid")
        # Match the guard exactly: GitHub rename/copy entries expand to both
        # paths before the shared 500-path waiver ceiling is applied.
        markdown_only, guard_docs_only = markdown_and_guard_docs_only(paths)
        after = metadata()
        if after == before:
            return before[1], paths, markdown_only, guard_docs_only
    if last_before is not None and last_before[0] != expected_head:
        raise CoordinatorError(
            "A38 pull request head differs from the clean signed head"
        )
    if last_before is not None and not is_sha(last_before[1]):
        raise _PullRequestBaseUnavailable(
            "A38 pull request base revision is not a 40-hex SHA"
        )
    if last_before is not None and last_before[2] != expected_base:
        raise CoordinatorError(
            "A38 pull request base branch differs from coordinator configuration"
        )
    raise CoordinatorError(
        "A38 pull request changed while reading its file inventory; retrying"
    )


def _report_activity_id(
    task_id: str, *, head: str, base_sha: str, policy_revision: str
) -> str:
    """Bind a report publication intent to every revision the report covers."""
    return coordinator_a38_report_activity_id(
        task_id,
        head=head,
        base_sha=base_sha,
        policy_revision=policy_revision,
    )


def _report_body_digest(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _load_report_policy(
    store: Store,
    worker: WorkerConfig,
    runner: Runner,
    worktree: str,
    base_sha: str,
) -> tuple[Any, dict[str, Any]]:
    result = git(
        store, worker, runner, worktree, "show", f"{base_sha}:.github/a38.json"
    )
    require_git_ok(result, "load A38 policy from base")
    try:
        policy = json.loads(result.stdout or "")
        if not isinstance(policy, dict):
            raise ValueError("policy is not an object")
    except (json.JSONDecodeError, ValueError) as exc:
        raise CoordinatorError("A38 base policy is invalid") from exc
    return result, policy


def _repository_visibility(api: Runner, repo: str) -> bool:
    visibility = gh_json(api, ["gh", "api", f"repos/{repo}", "--jq", ".private"])
    if not isinstance(visibility, bool):
        raise CoordinatorError("target repository visibility lookup did not return a boolean")
    return visibility


def _report_body_private(body: str) -> bool:
    """Read the exact visibility asserted by a stored local-CI report body."""
    try:
        report = parse_comment(body)
    except LocalCiError as exc:
        raise NonRecoverableCoordinatorError(
            "stored A38 report body is invalid"
        ) from exc
    return report.private


def _validate_report_body(
    body: str,
    *,
    policy: dict[str, Any],
    repo: str,
    head: str,
    base_sha: str,
    private: bool,
    changed_paths: list[str],
) -> None:
    """Revalidate every fact needed before a report intent may be recovered."""
    verdict = verify_report(body, policy, repo=repo, head=head, private=private)
    try:
        report = parse_comment(body)
    except LocalCiError as exc:
        raise CoordinatorError("A38 report checkpoint body is invalid") from exc
    reasons = list(verdict.get("reasons") or [])
    if report.schema != "dfx-local-ci/v2":
        reasons.append("report schema is not dfx-local-ci/v2")
    reasons.extend(
        report_fact_reasons(
            body,
            report,
            base_sha=base_sha,
            changed_paths=changed_paths,
        )
    )
    if verdict.get("ok") is not True or verdict.get("status") != "pass" or reasons:
        raise CoordinatorError("A38 report checkpoint does not satisfy the current policy")


def _report_intent_matches(
    value: Any,
    *,
    head: str,
    base_sha: str,
    policy_revision: str,
) -> bool:
    return (
        isinstance(value, dict)
        and value.get("head") == head
        and value.get("base_sha") == base_sha
        and value.get("policy_revision") == policy_revision
        and isinstance(value.get("activity_id"), str)
        and bool(value["activity_id"])
        and isinstance(value.get("body_sha256"), str)
        and len(value["body_sha256"]) == 64
        and isinstance(value.get("private"), bool)
    )


def _report_activity_payload(
    task: dict[str, Any],
    *,
    repo: str,
    number: int,
    body: str,
    head: str,
    base_sha: str,
    policy_revision: str,
    body_sha256: str,
    private: bool,
) -> dict[str, Any]:
    """Bind a pending report row to the task intent the executor must verify."""
    return {
        "repo": repo,
        "number": number,
        "body": body,
        "target": "pr",
        "exact_body": True,
        A38_REPORT_BINDING_KEY: {
            "task_id": str(task["id"]),
            "head": head,
            "base_sha": base_sha,
            "policy_revision": policy_revision,
            "body_sha256": body_sha256,
            "private": private,
        },
    }


def _retire_pending_report_activity(
    store: Store, intent: Any, *, reason: str
) -> None:
    """Make a discarded report intent ineligible for the generic executor."""
    if not isinstance(intent, dict):
        return
    activity_id = intent.get("activity_id")
    if not isinstance(activity_id, str) or not activity_id:
        return
    row = store.row("activity", activity_id)
    if (
        not isinstance(row, dict)
        or row.get("_origin_device_id") != store.device_id()
        or row.get("execution_status") != "pending"
    ):
        return
    updated = {key: value for key, value in row.items() if not key.startswith("_")}
    updated["execution_status"] = "error"
    updated["execution_error"] = reason
    store.write("activity", "update", activity_id, updated)


def _task_pending_report_activity_ids(
    store: Store, task: dict[str, Any]
) -> list[str]:
    """Owned pending exact-body A38 report rows for this task's pull request."""
    c = coord(task)
    repo = target_repo(task)
    number = as_int(c.get("pr_number") or task.get("ref"))
    matches: list[str] = []
    for row in store.rows("activity"):
        payload = row.get("payload")
        if (
            row.get("_origin_device_id") == store.device_id()
            and row.get("session_id") == task.get("session_id")
            and row.get("type") == "comment.post"
            and row.get("execution_status") == "pending"
            and isinstance(payload, dict)
            and payload.get("repo") == repo
            and as_int(payload.get("number")) == number
            and payload.get("target") == "pr"
            and payload.get("exact_body") is True
            and is_coordinator_a38_report_activity_for_task(row, task)
        ):
            matches.append(str(row["id"]))
    return sorted(matches)


def _report_task_lock(task_id: object) -> str:
    return a38_report_task_lock(task_id)


def _report_checkpoint_state(task: Mapping[str, Any]) -> tuple[Any, ...]:
    candidate = dict(task)
    c = coord(candidate)
    a38 = _a38_state(c)
    return (
        c.get("head_sha"),
        a38.get("pending_base_sha"),
        a38.get("base_sha"),
        a38.get("policy_revision"),
        a38.get("post_suite_checkpoint"),
        a38.get("report_intent"),
    )


def post_suite_checkpoint_pending(task: Mapping[str, Any]) -> bool:
    """Return whether a verified suite is waiting for live-head finalization."""
    candidate = dict(task)
    return isinstance(
        _a38_state(coord(candidate)).get("post_suite_checkpoint"), dict
    )


def _validated_post_suite_checkpoint(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CoordinatorError("A38 post-suite checkpoint is missing")
    head = value.get("head")
    base_sha = value.get("base_sha")
    policy_revision = value.get("policy_revision")
    body = value.get("body")
    body_sha256 = value.get("body_sha256")
    private = value.get("private")
    repo = value.get("repo")
    number = value.get("number")
    if (
        not is_sha(head)
        or not is_sha(base_sha)
        or policy_revision != base_sha
        or not isinstance(body, str)
        or not body
        or not isinstance(body_sha256, str)
        or len(body_sha256) != 64
        or _report_body_digest(body) != body_sha256
        or not isinstance(private, bool)
        or (repo is not None and (not isinstance(repo, str) or not repo))
        or (number is not None and as_int(number) is None)
    ):
        raise CoordinatorError("A38 post-suite checkpoint is invalid")
    return value


def _stable_live_pr_context(
    task: Mapping[str, Any],
    api: Runner,
) -> dict[str, Any] | None:
    """Read one stable live PR snapshot through an already scoped GitHub runner."""
    candidate = dict(task)
    c = coord(candidate)
    number = as_int(c.get("pr_number") or candidate.get("ref"))
    repo = target_repo(candidate)
    if number is None or not repo:
        return None
    previous: dict[str, Any] | None = None
    for _attempt in range(A38_CONTEXT_READ_ATTEMPTS + 1):
        try:
            view = gh_json(
                api,
                [
                    "gh",
                    "pr",
                    "view",
                    str(number),
                    "--repo",
                    repo,
                    "--json",
                    "number,headRefOid,baseRefOid,baseRefName,state",
                ],
            )
        except CoordinatorError:
            return None
        if not isinstance(view, dict):
            return None
        current = {
            "repo": repo,
            "number": as_int(view.get("number")),
            "head": str(view.get("headRefOid") or "").lower(),
            "base_sha": str(view.get("baseRefOid") or "").lower(),
            "base_name": str(view.get("baseRefName") or ""),
            "state": str(view.get("state") or "").upper(),
        }
        if (
            current["number"] != number
            or not is_sha(current["head"])
            or not is_sha(current["base_sha"])
            or not current["base_name"]
            or current["state"] not in ("OPEN", "CLOSED", "MERGED")
        ):
            return None
        if current == previous:
            return current
        previous = current
    return None


def _checkpoint_post_suite_report(
    store: Store,
    task: dict[str, Any],
    *,
    expected_state: tuple[Any, ...],
    checkpoint: dict[str, Any],
) -> None:
    """Durably bind a verified suite before its live PR head is re-read."""
    _validated_post_suite_checkpoint(checkpoint)
    with store.exclusive(_report_task_lock(task["id"])):
        fresh = store.row("task", str(task["id"]))
        if fresh is None:
            raise CoordinatorError("A38 report task no longer exists")
        if _report_checkpoint_state(fresh) != expected_state:
            raise A38ReportCheckpointConflict(
                "A38 report task changed before post-suite checkpoint"
            )
        committed_task = copy.deepcopy(fresh)
        _a38_state(coord(committed_task))["post_suite_checkpoint"] = copy.deepcopy(
            checkpoint
        )
        with store.conn.transaction():
            save_task(store, committed_task)
    task.clear()
    task.update(committed_task)


def _active_report_base(a38: Mapping[str, Any]) -> str:
    """Return the pending measurement base, or the promoted base when stable."""
    return str(a38.get("pending_base_sha") or a38.get("base_sha") or "")


def _checkpoint_report_context(
    store: Store,
    task: dict[str, Any],
    *,
    expected_state: tuple[Any, ...],
    head: str,
    base_sha: str,
) -> None:
    """Persist the live binding when the locked row still matches tick start."""
    with store.exclusive(_report_task_lock(task["id"])):
        fresh = store.row("task", str(task["id"]))
        if fresh is None:
            raise CoordinatorError("A38 report task no longer exists")
        if _report_checkpoint_state(fresh) != expected_state:
            raise A38ReportCheckpointConflict(
                "A38 report task context changed during measurement"
            )
        committed_task = copy.deepcopy(fresh)
        committed_c = coord(committed_task)
        committed_a38 = _a38_state(committed_c)
        intent = committed_a38.get("report_intent")
        activity_id = intent.get("activity_id") if isinstance(intent, dict) else None
        active_base = _active_report_base(committed_a38)
        binding_changed = (
            str(committed_c.get("head_sha") or "") != head
            or active_base != base_sha
        )
        committed_c["head_sha"] = head
        if evidence_base_sha(committed_c, a38=True) != base_sha:
            committed_a38["pending_base_sha"] = base_sha
        lock_key = a38_report_activity_lock(str(activity_id or "missing"))
        with store.exclusive(lock_key), store.conn.transaction():
            if binding_changed:
                _retire_pending_report_activity(
                    store,
                    intent,
                    reason="A38 report intent invalidated by a task head or base checkpoint change",
                )
                committed_a38.pop("report_intent", None)
            save_task(store, committed_task)
    task.clear()
    task.update(committed_task)


def _checkpoint_report_intent(
    store: Store,
    task: dict[str, Any],
    *,
    expected_intent: Any,
    activity_id: str,
    session_id: str,
    payload: dict[str, Any],
) -> None:
    """Atomically save one intent and retire every superseded pending report."""
    candidate_intent = _a38_state(coord(task)).get("report_intent")
    if not isinstance(candidate_intent, dict):
        raise CoordinatorError("A38 report checkpoint intent is missing")
    expected_head = str(candidate_intent.get("head") or "")
    expected_base = str(candidate_intent.get("base_sha") or "")
    expected_policy = str(candidate_intent.get("policy_revision") or "")
    with store.exclusive(_report_task_lock(task["id"])):
        fresh = store.row("task", str(task["id"]))
        if fresh is None:
            raise CoordinatorError("A38 report task no longer exists")
        fresh_c = coord(fresh)
        fresh_a38 = _a38_state(fresh_c)
        active_base = _active_report_base(fresh_a38)
        current_policy = str(fresh_a38.get("policy_revision") or "")
        if (
            str(fresh_c.get("head_sha") or "") != expected_head
            or active_base != expected_base
            or (current_policy and current_policy != expected_policy)
            or fresh_a38.get("report_intent") != expected_intent
        ):
            raise A38ReportCheckpointConflict(
                "A38 report task head, base, policy, or intent changed before checkpoint"
            )
        committed_task = copy.deepcopy(fresh)
        _a38_state(coord(committed_task))["report_intent"] = copy.deepcopy(
            candidate_intent
        )
        pending_ids = _task_pending_report_activity_ids(store, committed_task)
        lock_ids = sorted(set((*pending_ids, activity_id)))
        with ExitStack() as locks:
            for pending_id in lock_ids:
                locks.enter_context(
                    store.exclusive(a38_report_activity_lock(pending_id))
                )
            with store.conn.transaction():
                for pending_id in _task_pending_report_activity_ids(
                    store, committed_task
                ):
                    if pending_id == activity_id:
                        continue
                    _retire_pending_report_activity(
                        store,
                        {"activity_id": pending_id},
                        reason="A38 report activity superseded by the current report intent",
                    )
                save_task(store, committed_task)
                queue_activity(
                    store,
                    activity_id=activity_id,
                    session_id=session_id,
                    typ="comment.post",
                    payload=payload,
                )
    task.clear()
    task.update(committed_task)


def _discard_report_intent(
    store: Store, task: dict[str, Any], *, reason: str
) -> None:
    """Retire a pending report row and its task intent in one transaction."""
    candidate_intent = _a38_state(coord(task)).get("report_intent")
    with store.exclusive(_report_task_lock(task["id"])):
        fresh = store.row("task", str(task["id"]))
        if fresh is None:
            raise CoordinatorError("A38 report task no longer exists")
        fresh_a38 = _a38_state(coord(fresh))
        intent = fresh_a38.get("report_intent")
        if intent != candidate_intent:
            raise A38ReportCheckpointConflict(
                "A38 report intent changed before invalidation"
            )
        if intent is None:
            task.clear()
            task.update(fresh)
            return
        activity_id = intent.get("activity_id") if isinstance(intent, dict) else None
        lock_key = a38_report_activity_lock(str(activity_id or "missing"))
        committed_task = copy.deepcopy(fresh)
        committed_a38 = _a38_state(coord(committed_task))
        with store.exclusive(lock_key), store.conn.transaction():
            _retire_pending_report_activity(store, intent, reason=reason)
            committed_a38.pop("report_intent", None)
            save_task(store, committed_task)
    task.clear()
    task.update(committed_task)


def _retire_post_suite_checkpoint(
    store: Store,
    task: dict[str, Any],
    *,
    reason: str,
    phase: str,
    head: str | None = None,
    base_sha: str | None = None,
) -> None:
    """Atomically retire post-suite publication state before reroute or stop."""
    candidate_state = _report_checkpoint_state(task)
    with store.exclusive(_report_task_lock(task["id"])):
        fresh = store.row("task", str(task["id"]))
        if fresh is None:
            raise CoordinatorError("A38 report task no longer exists")
        if _report_checkpoint_state(fresh) != candidate_state:
            raise A38ReportCheckpointConflict(
                "A38 report task changed before post-suite invalidation"
            )
        committed_task = copy.deepcopy(fresh)
        committed_c = coord(committed_task)
        committed_a38 = _a38_state(committed_c)
        intent = committed_a38.get("report_intent")
        activity_id = intent.get("activity_id") if isinstance(intent, dict) else None
        lock_key = a38_report_activity_lock(str(activity_id or "missing"))
        with store.exclusive(lock_key), store.conn.transaction():
            _retire_pending_report_activity(store, intent, reason=reason)
            if head is not None:
                committed_c["head_sha"] = head
                invalidate_head_evidence(committed_c, head)
            if base_sha is not None:
                _drop_base_evidence(committed_c)
                _a38_state(committed_c)["pending_base_sha"] = base_sha
                reopen_base_bound_checklist(store, committed_task)
            committed_a38 = _a38_state(committed_c)
            committed_a38.pop("report_intent", None)
            committed_a38.pop("post_suite_checkpoint", None)
            committed_c["phase"] = phase
            save_task(store, committed_task)
    task.clear()
    task.update(committed_task)


def _resume_post_suite_report(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
    api: Runner,
) -> list[str]:
    """Finish only live-head validation, publication, and evidence promotion."""
    c = coord(task)
    a38 = _a38_state(c)
    checkpoint = _validated_post_suite_checkpoint(
        a38.get("post_suite_checkpoint")
    )
    head = str(checkpoint["head"])
    base_sha = str(checkpoint["base_sha"])
    policy_revision = str(checkpoint["policy_revision"])
    report_body = str(checkpoint["body"])
    body_sha256 = str(checkpoint["body_sha256"])
    visibility = bool(checkpoint["private"])
    checkpoint_repo = checkpoint.get("repo")
    checkpoint_number = as_int(checkpoint.get("number"))
    current_repo = target_repo(task)
    current_number = as_int(c.get("pr_number") or task.get("ref"))
    cfg = repo_cfg(worker, current_repo, task)

    def validate_live_context(live: dict[str, Any]) -> list[str] | None:
        identity_mismatch = (
            checkpoint_repo != current_repo
            or checkpoint_number is None
            or checkpoint_number != current_number
            or live.get("repo") != checkpoint_repo
            or live.get("number") != checkpoint_number
        )
        head_mismatch = live.get("head") != head
        base_mismatch = (
            live.get("base_sha") != base_sha
            or live.get("base_name") != cfg.base
        )
        if not (identity_mismatch or head_mismatch or base_mismatch):
            return None
        state = str(live.get("state") or "")
        reason = "A38 post-suite checkpoint invalidated by live pull request metadata"
        if state == "MERGED":
            _retire_post_suite_checkpoint(
                store,
                task,
                reason=reason,
                phase="tests",
            )
            details = (
                "identity" if identity_mismatch else "head" if head_mismatch else "base"
            )
            raise NonRecoverableCoordinatorError(
                f"task-done refused: merged PR {details} differs from verified A38 "
                "checkpoint; a merged PR cannot be remeasured"
            )
        if state == "CLOSED":
            _retire_post_suite_checkpoint(
                store,
                task,
                reason=reason,
                phase="await_merge",
            )
            return [
                "PR closed with changed post-suite metadata; verifying the merge"
            ]
        _retire_post_suite_checkpoint(
            store,
            task,
            reason=reason,
            phase="tests",
            head=str(live["head"]) if head_mismatch else None,
            base_sha=str(live["base_sha"]) if base_mismatch else None,
        )
        return [
            "PR metadata changed after the A38 suite; invalidating the checkpoint "
            "and remeasuring"
        ]

    live = _stable_live_pr_context(task, api)
    if live is None:
        return ["PR state temporarily unavailable after the A38 suite; retrying"]
    mismatch_lines = validate_live_context(live)
    if mismatch_lines is not None:
        return mismatch_lines
    live_state = str(live["state"])

    def validate_live_visibility(api: Runner) -> list[str] | None:
        try:
            live_visibility = _repository_visibility(api, cfg.repo)
        except CoordinatorError:
            return [
                "repository visibility temporarily unavailable before A38 report "
                "finalization; retrying"
            ]
        if (
            _report_body_private(report_body) is visibility
            and live_visibility is visibility
        ):
            return None
        reason = "A38 report visibility no longer matches the target repository"
        if live_state == "MERGED":
            _retire_post_suite_checkpoint(
                store,
                task,
                reason=reason,
                phase="tests",
            )
            raise NonRecoverableCoordinatorError(
                "task-done refused: merged PR visibility differs from the stored "
                "A38 report; a merged PR cannot be remeasured"
            )
        if live_state == "CLOSED":
            _retire_post_suite_checkpoint(
                store,
                task,
                reason=reason,
                phase="await_merge",
            )
            return [
                "PR closed after repository visibility changed; verifying the merge "
                "without promoting the A38 report"
            ]
        _retire_post_suite_checkpoint(
            store,
            task,
            reason=reason,
            phase="tests",
        )
        return [
            "repository visibility changed after A38 measurement; invalidating "
            "the report and remeasuring"
        ]

    # A transient visibility lookup must not trigger draft or report effects.
    visibility_lines = validate_live_visibility(api)
    if visibility_lines is not None:
        return visibility_lines
    draft_lines: list[str] = []
    if live_state == "OPEN":
        draft_lines = ensure_draft(store, worker, task, runner)
        c = coord(task)
        a38 = _a38_state(c)
        if str(c.get("head_sha") or "") != head:
            _retire_post_suite_checkpoint(
                store,
                task,
                reason="A38 post-suite checkpoint invalidated by task head change",
                phase="tests",
            )
            return [
                "task head changed after the A38 suite; invalidating the checkpoint "
                "and remeasuring"
            ]

    number = as_int(c.get("pr_number") or task.get("ref"))
    if number is None:
        raise CoordinatorError("A38 report comment requires a pull request number")

    # Publication gets a final coherent live join after draft/checklist recovery.
    # No report intent is queued from an unreadable or changed snapshot.
    live = _stable_live_pr_context(task, api)
    if live is None:
        return ["PR state temporarily unavailable before A38 publication; retrying"]
    mismatch_lines = validate_live_context(live)
    if mismatch_lines is not None:
        return mismatch_lines
    live_state = str(live["state"])

    visibility_lines = validate_live_visibility(api)
    if visibility_lines is not None:
        return visibility_lines

    default_activity_id = _report_activity_id(
        str(task["id"]),
        head=head,
        base_sha=base_sha,
        policy_revision=policy_revision,
    )
    intent = a38.get("report_intent")
    intent_matches = _report_intent_matches(
        intent,
        head=head,
        base_sha=base_sha,
        policy_revision=policy_revision,
    )
    resumed_publication = intent_matches
    if intent is not None and (
        not intent_matches
        or intent.get("body_sha256") != body_sha256
        or intent.get("private") is not visibility
    ):
        raise A38ReportCheckpointConflict(
            "A38 report intent differs from the post-suite checkpoint"
        )
    activity_id = str(intent["activity_id"]) if intent_matches else default_activity_id
    expected_payload = _report_activity_payload(
        task,
        repo=target_repo(task),
        number=number,
        body=report_body,
        head=head,
        base_sha=base_sha,
        policy_revision=policy_revision,
        body_sha256=body_sha256,
        private=visibility,
    )
    activity = store.row("activity", activity_id)
    if not intent_matches and isinstance(activity, dict):
        if (
            not _own_report_activity(store, worker, task, activity, head)
            or activity.get("payload") != expected_payload
        ):
            activity_id = str(
                uuid5(
                    NAMESPACE_URL,
                    f"coordinator-a38-report-body:{task['id']}:{head}:"
                    f"{base_sha}:{policy_revision}:{body_sha256}",
                )
            )
            activity = store.row("activity", activity_id)
            if isinstance(activity, dict) and (
                not _own_report_activity(store, worker, task, activity, head)
                or activity.get("payload") != expected_payload
            ):
                raise CoordinatorError("verified A38 report activity id is occupied")

    if not intent_matches:
        previous_intent = copy.deepcopy(a38.get("report_intent"))
        a38["report_intent"] = {
            "head": head,
            "base_sha": base_sha,
            "policy_revision": policy_revision,
            "activity_id": activity_id,
            "body_sha256": body_sha256,
            "private": visibility,
        }
        _checkpoint_report_intent(
            store,
            task,
            expected_intent=previous_intent,
            activity_id=activity_id,
            session_id=worker.session_id,
            payload=expected_payload,
        )
        c = coord(task)
        a38 = _a38_state(c)
        activity = store.row("activity", activity_id)
    elif (
        not isinstance(activity, dict)
        or not _own_report_activity(store, worker, task, activity, head)
        or activity.get("payload") != expected_payload
    ):
        raise A38ReportCheckpointConflict(
            "A38 report activity no longer matches its post-suite checkpoint"
        )

    if not isinstance(activity, dict):
        raise A38ReportCheckpointConflict("A38 report activity is missing")
    if activity.get("execution_status") != "done":
        queue_activity(
            store,
            activity_id=activity_id,
            session_id=worker.session_id,
            typ="comment.post",
            payload=expected_payload,
        )
        execute_github(store, runner, activity_ids=(activity_id,))
        activity = store.row("activity", activity_id)
    result = activity.get("result") if isinstance(activity, dict) else None
    if (
        not isinstance(activity, dict)
        or activity.get("execution_status") != "done"
        or not isinstance(result, dict)
        or not isinstance(result.get("url"), str)
        or not result["url"]
    ):
        raise CoordinatorError("A38 report comment was not verified published")

    # Close the visibility race after publication and immediately before the
    # report becomes completion evidence.
    visibility_lines = validate_live_visibility(api)
    if visibility_lines is not None:
        return visibility_lines

    _finalize_report_evidence(
        store,
        worker,
        task,
        expected_state=copy.deepcopy(_report_checkpoint_state(task)),
        head=head,
        base_sha=base_sha,
        policy_revision=policy_revision,
        live_state=live_state,
        activity_id=activity_id,
    )
    publication = (
        f"A38 report already posted on {head[:7]}"
        if resumed_publication
        else f"A38 passed and report posted on {head[:7]}"
    )
    if live_state in ("MERGED", "CLOSED"):
        return [
            f"{publication}; PR {live_state.lower()}, verifying the merge"
        ]
    return [publication, *draft_lines]


def _finalize_report_evidence(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    *,
    expected_state: tuple[Any, ...],
    head: str,
    base_sha: str,
    policy_revision: str,
    live_state: str | None,
    activity_id: str | None = None,
    waiver_reason: str | None = None,
    coordinator_check_command: str | None = None,
) -> None:
    """Commit published-report or waiver evidence against one fresh checkpoint."""
    if (activity_id is None) == (waiver_reason is None):
        raise CoordinatorError("A38 finalization requires exactly one report or waiver")
    expected_intent = expected_state[-1]
    lock_key = a38_report_activity_lock(
        str(activity_id or f"waiver:{task['id']}")
    )
    with store.exclusive(_report_task_lock(task["id"])):
        fresh = store.row("task", str(task["id"]))
        if fresh is None:
            raise CoordinatorError("A38 report task no longer exists")
        fresh_a38 = _a38_state(coord(fresh))
        if (
            _report_checkpoint_state(fresh) != expected_state
            or fresh_a38.get("report_intent") != expected_intent
        ):
            raise A38ReportCheckpointConflict(
                "A38 report task or intent changed before finalization"
            )

        committed_task = copy.deepcopy(fresh)
        committed_c = coord(committed_task)
        committed_a38 = _a38_state(committed_c)
        with store.exclusive(lock_key), store.conn.transaction():
            if activity_id is not None:
                activity = store.row("activity", activity_id)
                payload = activity.get("payload") if isinstance(activity, dict) else None
                binding = (
                    payload.get(A38_REPORT_BINDING_KEY)
                    if isinstance(payload, dict)
                    else None
                )
                result = activity.get("result") if isinstance(activity, dict) else None
                if (
                    not isinstance(expected_intent, dict)
                    or expected_intent.get("activity_id") != activity_id
                    or not isinstance(activity, dict)
                    or not _own_report_activity(
                        store, worker, committed_task, activity, head
                    )
                    or activity.get("execution_status") != "done"
                    or not isinstance(binding, dict)
                    or binding
                    != {
                        "task_id": str(committed_task["id"]),
                        "head": head,
                        "base_sha": base_sha,
                        "policy_revision": policy_revision,
                        "body_sha256": expected_intent.get("body_sha256"),
                        "private": expected_intent.get("private"),
                    }
                    or not isinstance(result, dict)
                    or not isinstance(result.get("url"), str)
                    or not result["url"]
                ):
                    raise A38ReportCheckpointConflict(
                        "published A38 report activity no longer matches its intent"
                    )
                committed_a38.pop("report_waiver", None)
                committed_a38["report"] = {
                    "head": head,
                    "base_sha": base_sha,
                    "policy_revision": policy_revision,
                    "activity_id": activity_id,
                    "body": str(payload["body"]),
                    "id": result.get("id"),
                    "url": result["url"],
                }
                evidence = committed_c.setdefault("evidence", {})
                if isinstance(evidence, dict):
                    evidence.pop("tests_waiver", None)
                    evidence["tests_pass"] = True
                    evidence["tests_head"] = head
                checklist_evidence = f"A38 pass on {head}"
            else:
                assert waiver_reason is not None
                committed_a38.pop("report", None)
                committed_a38["report_waiver"] = {
                    "head": head,
                    "base_sha": base_sha,
                    "policy_revision": policy_revision,
                    "reason": waiver_reason,
                    "observed_at": utcnow(),
                }
                _record_local_check(
                    store,
                    committed_task,
                    name="a38",
                    command="A38 author report waiver",
                    passed=None,
                    output=waiver_reason,
                    head=head,
                )
                if coordinator_check_command is not None:
                    _record_local_check(
                        store,
                        committed_task,
                        name="coordinator-check",
                        command=coordinator_check_command,
                        passed=None,
                        output=f"skipped by A38 inventory waiver: {waiver_reason}",
                        head=head,
                    )
                evidence = committed_c.setdefault("evidence", {})
                if isinstance(evidence, dict):
                    evidence.pop("tests_pass", None)
                    evidence["tests_waiver"] = waiver_reason
                    evidence["tests_head"] = head
                checklist_evidence = f"A38 waiver on {head}: {waiver_reason}"

            committed_a38.pop("post_suite_checkpoint", None)
            set_checklist(
                store,
                committed_task,
                "local_check_pass",
                "ja",
                checklist_evidence,
                source="script",
            )
            if committed_c.get("pr_number"):
                set_checklist(
                    store,
                    committed_task,
                    "pushed",
                    "ja",
                    f"draft PR {committed_c.get('pr_number')} head {head}",
                    source="script",
                )
            _promote_base_evidence(
                store, committed_task, head=head, base_sha=base_sha
            )
            if live_state in ("MERGED", "CLOSED"):
                committed_c["phase"] = "await_merge"
            else:
                committed_c["phase"] = "pr_gates_grok"
                committed_task["state"] = "pr-review"
            save_task(store, committed_task)
    task.clear()
    task.update(committed_task)


def phase_tests(
    store: Store, worker: WorkerConfig, task: dict[str, Any], runner: Runner
) -> list[str]:
    """Run the optional trusted precheck, complete A38 suite, and publish its report."""
    if not worker.a38:
        raise CoordinatorError("A38 tests phase called for a non-A38 worker")
    api = scoped(store, worker.session_id, runner)
    if post_suite_checkpoint_pending(task):
        return _resume_post_suite_report(store, worker, task, runner, api)
    checkpoint_state = copy.deepcopy(_report_checkpoint_state(task))
    c = coord(task)
    source = c["source"]
    cfg = repo_cfg(worker, str(source["repo"]), task)
    worktree = str(c["worktree"])
    head = verify_signed_clean_head(store, worker, runner, worktree)
    a38 = _a38_state(c)
    number = as_int(c.get("pr_number") or task.get("ref"))
    if number is None:
        raise CoordinatorError("A38 measurement requires a pull request number")
    try:
        base_sha, changed_paths, markdown_only, guard_docs_only = _pull_a38_context(
            api,
            repo=cfg.repo,
            number=number,
            expected_head=head,
            expected_base=cfg.base,
        )
    except _PullRequestNotOpen as exc:
        c["phase"] = "await_merge"
        save_task(store, task)
        return [
            f"PR {exc.state.lower()} before A38 measurement; verifying the merge"
        ]
    except _PullRequestBaseUnavailable:
        return ["PR state temporarily unavailable; retrying"]
    # Policy migration is intentionally not implemented by this coordinator.
    # Never publish evidence against obsolete base policy or workflow controls.
    # Guard-docs alone has the one standard exception for its integration file.
    unsupported_policy_paths = [
        path
        for path in changed_paths
        if path in (POLICY_PATH, PR_GUARD_CONFIG_PATH)
        or (
            path.startswith(".github/workflows/")
            and not (guard_docs_only and path == GUARD_WORKFLOW_PATH)
        )
    ]
    if unsupported_policy_paths:
        raise NonRecoverableCoordinatorError(
            "A38 policy migrations are not supported by coordinator A38 mode: "
            + ", ".join(sorted(unsupported_policy_paths))
        )
    policy_revision = base_sha
    _checkpoint_report_context(
        store,
        task,
        expected_state=checkpoint_state,
        head=head,
        base_sha=base_sha,
    )
    c = coord(task)
    a38 = _a38_state(c)
    _reconcile_pending_base(store, task, base_sha)
    c = coord(task)
    a38 = _a38_state(c)
    saved_base = evidence_base_sha(c, a38=True)
    if is_sha(saved_base) and saved_base != base_sha:
        invalidate_base_evidence(store, task, base_sha)
        c = coord(task)
        a38 = _a38_state(c)
    elif saved_base != base_sha:
        a38["pending_base_sha"] = base_sha
    fetched = git(
        store,
        worker,
        runner,
        worktree,
        "fetch",
        "--no-tags",
        "origin",
        f"+refs/heads/{cfg.base}:refs/remotes/origin/{cfg.base}",
    )
    require_git_ok(fetched, "fetch A38 base")
    exact_base = git(
        store,
        worker,
        runner,
        worktree,
        "cat-file",
        "-e",
        f"{base_sha}^{{commit}}",
    )
    require_git_ok(exact_base, "verify observed A38 base")
    waiver_reason = (
        "markdown-only change set"
        if markdown_only
        else "guard-docs change set" if guard_docs_only else ""
    )
    if waiver_reason:
        _discard_report_intent(
            store,
            task,
            reason="A38 report intent replaced by an inventory waiver",
        )
        c = coord(task)
        finalization_state = copy.deepcopy(_report_checkpoint_state(task))

        def current_waiver_context() -> tuple[
            dict[str, Any] | None, list[str] | None
        ]:
            live = _stable_live_pr_context(task, api)
            if live is None:
                return None, [
                    "PR state temporarily unavailable before A38 inventory waiver "
                    "finalization; retrying"
                ]
            identity_mismatch = (
                live.get("repo") != cfg.repo
                or live.get("number") != number
            )
            head_mismatch = live.get("head") != head
            base_mismatch = (
                live.get("base_sha") != base_sha
                or live.get("base_name") != cfg.base
            )
            if not (identity_mismatch or head_mismatch or base_mismatch):
                return live, None
            state = str(live.get("state") or "")
            reason = "A38 inventory waiver invalidated by live pull request metadata"
            if state == "OPEN":
                _retire_post_suite_checkpoint(
                    store,
                    task,
                    reason=reason,
                    phase="tests",
                    head=str(live["head"]) if head_mismatch else None,
                    base_sha=str(live["base_sha"]) if base_mismatch else None,
                )
                return None, [
                    "PR metadata changed after A38 inventory measurement; "
                    "invalidating the waiver and remeasuring"
                ]
            _retire_post_suite_checkpoint(
                store,
                task,
                reason=reason,
                phase="await_merge",
            )
            return None, [
                f"PR {state.lower()} with changed A38 inventory metadata; "
                "verifying the merge without promoting the waiver"
            ]

        live, context_lines = current_waiver_context()
        if context_lines is not None:
            return context_lines
        assert live is not None
        live_state = str(live["state"])
        if live_state in ("MERGED", "CLOSED"):
            _finalize_report_evidence(
                store,
                worker,
                task,
                expected_state=finalization_state,
                head=head,
                base_sha=base_sha,
                policy_revision=policy_revision,
                live_state=live_state,
                waiver_reason=waiver_reason,
                coordinator_check_command=(
                    check_label(c, cfg) if cfg.check_argv else None
                ),
            )
            return [
                f"PR {live_state.lower()} during the A38 inventory waiver; "
                "verifying the merge"
            ]
        draft_lines = ensure_draft(store, worker, task, runner)
        if str(coord(task).get("head_sha") or "") != head:
            raise CoordinatorError(
                "published pull request head differs from the A38 waiver head"
            )
        live, context_lines = current_waiver_context()
        if context_lines is not None:
            return context_lines
        assert live is not None
        live_state = str(live["state"])
        _finalize_report_evidence(
            store,
            worker,
            task,
            expected_state=finalization_state,
            head=head,
            base_sha=base_sha,
            policy_revision=policy_revision,
            live_state=live_state,
            waiver_reason=waiver_reason,
            coordinator_check_command=(check_label(c, cfg) if cfg.check_argv else None),
        )
        if live_state in ("MERGED", "CLOSED"):
            return [
                f"PR {live_state.lower()} during the A38 inventory waiver; "
                "verifying the merge"
            ]
        return [f"A38 report waived on {head[:7]} ({waiver_reason})", *draft_lines]
    a38.pop("report_waiver", None)
    evidence = c.get("evidence")
    if isinstance(evidence, dict):
        evidence.pop("tests_waiver", None)
    default_activity_id = _report_activity_id(
        str(task["id"]),
        head=head,
        base_sha=base_sha,
        policy_revision=policy_revision,
    )
    intent = a38.get("report_intent")
    intent_matches = _report_intent_matches(
        intent,
        head=head,
        base_sha=base_sha,
        policy_revision=policy_revision,
    )
    if intent is not None and not intent_matches:
        _discard_report_intent(
            store,
            task,
            reason="A38 report intent no longer matches the current head and base",
        )
        c = coord(task)
        a38 = _a38_state(c)
        intent = None
    activity_id = str(intent["activity_id"]) if intent_matches else default_activity_id
    activity = store.row("activity", activity_id)
    if isinstance(activity, dict) and not _own_report_activity(store, worker, task, activity, head):
        raise CoordinatorError("A38 report activity id is held by a row this worker did not write")
    policy_result = None
    policy = None
    visibility = None
    invalid_recovery_intent = False
    if isinstance(activity, dict):
        payload = activity.get("payload")
        body = payload.get("body") if isinstance(payload, dict) else None
        if intent_matches:
            policy_result, policy = _load_report_policy(
                store, worker, runner, worktree, base_sha
            )
            visibility = _repository_visibility(api, cfg.repo)
        expected_recovery_payload = (
            _report_activity_payload(
                task,
                repo=target_repo(task),
                number=number,
                body=body,
                head=head,
                base_sha=base_sha,
                policy_revision=policy_revision,
                body_sha256=str(intent.get("body_sha256") or ""),
                private=visibility,
            )
            if isinstance(body, str)
            and isinstance(visibility, bool)
            and isinstance(intent, dict)
            else None
        )
        if (
            not intent_matches
            or not isinstance(body, str)
            or payload != expected_recovery_payload
            or _report_body_digest(body) != intent.get("body_sha256")
            or visibility is not intent.get("private")
        ):
            # A deterministic id alone is not evidence. Ignore the row and
            # measure again; the verified body will use a digest-bound fallback
            # id if this id remains occupied by different content.
            activity = None
            invalid_recovery_intent = intent_matches
        else:
            assert policy is not None
            assert visibility is not None
            try:
                _validate_report_body(
                    body,
                    policy=policy,
                    repo=cfg.repo,
                    head=head,
                    base_sha=base_sha,
                    private=visibility,
                    changed_paths=changed_paths,
                )
            except CoordinatorError:
                activity = None
                invalid_recovery_intent = True
    if invalid_recovery_intent:
        _discard_report_intent(
            store,
            task,
            reason="A38 report intent payload failed current validation",
        )
        c = coord(task)
        a38 = _a38_state(c)
        intent = None
        intent_matches = False
        activity_id = default_activity_id
    if isinstance(activity, dict):
        assert isinstance(body, str)
        assert isinstance(intent, dict)
        assert isinstance(visibility, bool)
        _checkpoint_post_suite_report(
            store,
            task,
            expected_state=copy.deepcopy(_report_checkpoint_state(task)),
            checkpoint={
                "repo": cfg.repo,
                "number": number,
                "head": head,
                "base_sha": base_sha,
                "policy_revision": policy_revision,
                "body": body,
                "body_sha256": str(intent["body_sha256"]),
                "private": visibility,
            },
        )
        return _resume_post_suite_report(store, worker, task, runner, api)

    if cfg.check_argv:
        from .coordinator_common import coordinator_env

        precheck = run_bounded(
            list(cfg.check_argv),
            timeout=worker.check_timeout,
            cwd=worktree,
            env={
                **coordinator_env(c, worker, cfg),
                "AGENT_COORDINATOR_BASE": base_sha,
            },
            clear_ambient_github=True,
        )
        pre_output = (precheck.stdout or "") + "\n" + (precheck.stderr or "")
        if precheck.returncode != 0:
            _record_local_check(
                store,
                task,
                name="coordinator-check",
                command=check_label(c, cfg),
                passed=False,
                output=pre_output,
                head=head,
            )
            c["findings"] = f"Trusted precheck failed on {head[:7]}:\n{redact(pre_output)}"
            evidence = c.setdefault("evidence", {})
            if isinstance(evidence, dict):
                evidence["tests_pass"] = False
                evidence["tests_head"] = head
            c["phase"] = "implement"
            task["state"] = "implementing"
            invalidate_head_evidence(c, head)
            save_task(store, task)
            return [f"trusted precheck failed on {head[:7]}; routing to implementer"]
        _record_local_check(
            store,
            task,
            name="coordinator-check",
            command=check_label(c, cfg),
            passed=True,
            output=pre_output,
            head=head,
        )

    if policy_result is None or policy is None:
        policy_result, policy = _load_report_policy(
            store, worker, runner, worktree, base_sha
        )
    if visibility is None:
        visibility = _repository_visibility(api, cfg.repo)

    run_dir = control_dir(worker, str(task["id"])) / "a38" / head
    try:
        if run_dir.exists():
            if run_dir.is_symlink() or not run_dir.is_dir():
                raise CoordinatorError("A38 run path is not a managed directory")
            shutil.rmtree(run_dir)
        run_dir.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        raise CoordinatorError("cannot create a fresh A38 run directory") from exc
    policy_path = run_dir / "policy.json"
    report_path = run_dir / "report.md"
    logs_dir = run_dir / "logs"
    policy_path.write_text(policy_result.stdout or "", encoding="utf-8")

    visibility_flag = "--private" if visibility else "--public"
    steps = [
        ["policy", "--file", str(policy_path)],
        [
            "run",
            "--repo",
            worktree,
            "--policy",
            str(policy_path),
            "--base-sha",
            base_sha,
            "--repository",
            cfg.repo,
            "--output",
            str(report_path),
            "--logs-dir",
            str(logs_dir),
            visibility_flag,
        ],
        [
            "verify",
            "--policy",
            str(policy_path),
            "--file",
            str(report_path),
            "--repo",
            cfg.repo,
            "--head",
            head,
            visibility_flag,
        ],
    ]
    results = []
    for index, argv in enumerate(steps):
        if index == 2 and not report_path.is_file():
            break
        results.append(_run_cli(argv, worker=worker, cwd=run_dir))
        if index == 0 and results[-1].returncode != 0:
            break
    passed = len(results) == 3 and all(result.returncode == 0 for result in results)
    output = "\n".join(
        redact((result.stdout or "") + "\n" + (result.stderr or ""), limit=2000)
        for result in results
    )
    _record_local_check(
        store,
        task,
        name="a38",
        command="agent a38 policy; agent a38 run; agent a38 verify",
        passed=passed,
        output=output,
        head=head,
    )
    live_state = None
    if as_int(c.get("pr_number") or task.get("ref")) is not None:
        live_state = pr_state(store, worker, task, runner)
    if not passed:
        if live_state in ("MERGED", "CLOSED"):
            c["phase"] = "await_merge"
            save_task(store, task)
            return [f"PR {live_state.lower()} during the A38 suite; verifying the merge"]
        details = _a38_failure_summary(report_path, logs_dir, output or "A38 failed")
        c["findings"] = f"A38 failed on {head[:7]}:\n{details}"
        evidence = c.setdefault("evidence", {})
        if isinstance(evidence, dict):
            evidence["tests_pass"] = False
            evidence["tests_head"] = head
        c["phase"] = "implement"
        task["state"] = "implementing"
        invalidate_head_evidence(c, head)
        save_task(store, task)
        return [f"A38 failed on {head[:7]}; routing to implementer"]

    try:
        report_body = report_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise CoordinatorError("verified A38 report cannot be read") from exc
    _validate_report_body(
        report_body,
        policy=policy,
        repo=cfg.repo,
        head=head,
        base_sha=base_sha,
        private=visibility,
        changed_paths=changed_paths,
    )
    body_sha256 = _report_body_digest(report_body)
    _checkpoint_post_suite_report(
        store,
        task,
        expected_state=copy.deepcopy(_report_checkpoint_state(task)),
        checkpoint={
            "repo": cfg.repo,
            "number": number,
            "head": head,
            "base_sha": base_sha,
            "policy_revision": policy_revision,
            "body": report_body,
            "body_sha256": body_sha256,
            "private": visibility,
        },
    )
    return _resume_post_suite_report(store, worker, task, runner, api)


def _summary(task: Mapping[str, Any], language: str) -> str:
    key = "change_summary_en" if language == "en" else "change_summary_de"
    value = task.get(key)
    if not isinstance(value, str) or not value.strip() or "\n" in value:
        raise CoordinatorError(f"task {language.upper()} summary is missing")
    return value.strip()


def _machine_block(payload: Mapping[str, Any]) -> str:
    return (
        f"{REVIEW_BEGIN}\n```json\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=False, separators=(",", ":"))
        + f"\n```\n{REVIEW_END}"
    )


def _render_review_comment(
    task: Mapping[str, Any],
    payload: dict[str, Any],
    *,
    rules_bound: bool,
    reviewer: str | None = None,
    findings: str = "",
) -> str:
    lanes = payload["lanes"]
    model = lanes[0]["model"]
    second = lanes[0]["id"] == "conformity-b"
    if second:
        defects = int(payload["defects"])
        blocks = bool(payload["blocks_merge"])
        if defects == 0:
            en_status, de_status = "No defects.", "Keine Mängel."
        elif blocks:
            en_status = "There are defects that must be fixed."
            de_status = "Es gibt Mängel, die behoben werden müssen."
        else:
            en_status = "There are defects that do not block the merge."
            de_status = "Es gibt Mängel, die den Merge nicht blockieren."
        visible = (
            f"EN:\nSecond review with {model}\n{en_status}\n\n"
            f"DE:\nZweiter Review mit {model}\n{de_status}\n\n"
        )
        detail_prefix = (
            f"{_summary(task, 'en')}\n\n{_summary(task, 'de')}\n\n"
            f"Second review by @{reviewer}.\n"
        )
        if findings:
            detail_prefix += f"\n{findings.strip()}\n"
    else:
        passes = int(payload["passes"])
        visible = (
            f"EN:\nFirst review with {model}\nReady after {passes} review passes.\n"
            f"{_summary(task, 'en')}\n\n"
            f"DE:\nErster Review mit {model}\nBereit nach {passes} Review-Durchläufen.\n"
            f"{_summary(task, 'de')}\n\n"
        )
        detail_prefix = ""
    try:
        record = render_review_record(payload, rules_bound=rules_bound)
    except ReviewError as exc:
        raise CoordinatorError(f"A38 review comment validation failed: {exc}") from exc
    body = (
        visible
        + "<details>\n<summary>Details</summary>\n\n"
        + detail_prefix
        + ("\n" if detail_prefix and not detail_prefix.endswith("\n\n") else "")
        + record
        + _machine_block(payload)
        + "\n\n</details>"
    )
    reasons = [
        *validate_declaration(
            payload,
            head=str(payload["head"]),
            markdown_only=False,
            rules_bound=rules_bound,
        ),
        *validate_visible(body, int(payload["passes"])),
        *validate_review_record(body, payload, rules_bound=rules_bound),
    ]
    if reasons:
        raise CoordinatorError("A38 review comment validation failed: " + "; ".join(dict.fromkeys(reasons)))
    if len(body) + ACTIVITY_MARKER_LENGTH >= GITHUB_COMMENT_LIMIT:
        raise CoordinatorError(
            f"A38 review comment must be shorter than {GITHUB_COMMENT_LIMIT} characters"
        )
    return body


def _post_comment(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
    *,
    activity_id: str,
    body: str,
) -> dict[str, Any]:
    c = coord(task)
    number = as_int(c.get("pr_number") or task.get("ref"))
    if number is None:
        raise CoordinatorError("A38 comment requires a pull request number")
    payload = {
        "repo": target_repo(task),
        "number": number,
        "body": body,
        "target": "pr",
    }
    row = store.row("activity", activity_id)
    if isinstance(row, dict) and not _own_comment_activity(
        store, worker, row, payload
    ):
        raise CoordinatorError(
            "A38 review comment activity id is held by a row this worker did not write"
        )
    queue_activity(
        store,
        activity_id=activity_id,
        session_id=worker.session_id,
        typ="comment.post",
        payload=payload,
    )
    row = store.row("activity", activity_id)
    if not isinstance(row, dict) or not _own_comment_activity(
        store, worker, row, payload
    ):
        raise CoordinatorError("A38 review comment activity intent is missing or foreign")
    execute_github(store, runner, activity_ids=(activity_id,))
    row = store.row("activity", activity_id)
    if not isinstance(row, dict) or not _own_comment_activity(
        store, worker, row, payload
    ):
        raise CoordinatorError("A38 review comment result is missing or foreign")
    if row.get("execution_status") != "done":
        raise CoordinatorError("A38 review comment was not verified published")
    result = row.get("result") if isinstance(row.get("result"), dict) else {}
    if not isinstance(result.get("url"), str) or not result["url"]:
        raise CoordinatorError("A38 review comment URL was not verified")
    return result


def _own_comment_activity(
    store: Store,
    worker: WorkerConfig,
    activity: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> bool:
    """Only the exact local comment intent may occupy its deterministic id."""
    return _own_row_matches(
        store,
        activity,
        {
            "session_id": worker.session_id,
            "type": "comment.post",
            "payload": payload,
        },
    )


def _own_row_matches(
    store: Store,
    row: Mapping[str, Any],
    stable_fields: Mapping[str, Any],
) -> bool:
    """Match row ownership and every stable field used by a durable intent."""
    return row.get("_origin_device_id") == store.device_id() and all(
        row.get(key) == value for key, value in stable_fields.items()
    )


def _first_comment_body(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
    *,
    head: str,
) -> tuple[str, int]:
    """Rebuild the deterministic first-review comment for a saved head."""
    c = coord(task)
    gates = latest_gates(store, str(task["id"]))
    models = task.get("models") if isinstance(task.get("models"), dict) else {}
    vendor = models.get("first")
    if not isinstance(vendor, str):
        raise CoordinatorError("A38 first review provider is not pinned")
    for dimension in ("quality", "logic"):
        gate = gates.get(("review-1", dimension))
        if gate is None or gate.get("verdict") != "approved" or gate.get("head_sha") != head or gate.get("vendor") != vendor:
            raise CoordinatorError(f"missing first review gate review-1/{dimension} on {head[:7]}")
    a38 = _a38_state(c)
    lanes = []
    for dimension, lane_id in (("quality", "conformity-a"), ("logic", "logic-a")):
        item = gate_prompt_evidence(
            store,
            gates.get(("review-1", dimension)),
            head=head,
            dimension=dimension,
            vendor=vendor,
        )
        if item is None:
            raise CoordinatorError(
                f"first review {dimension} prompt evidence missing"
            )
        provider = item["provider"]
        model = item["model"]
        prompt = public_prompt(item["prompt"], worker, task)
        lanes.append(
            {
                "id": lane_id,
                "result": "pass",
                "status": "complete",
                "provider": PROVIDER_DISPLAY[provider],
                "model": model,
                "model_number": model,
                "prompt": prompt,
            }
        )
    if lanes[0]["model"] != lanes[1]["model"]:
        raise CoordinatorError("first review lanes do not share one model")
    payload = {
        "schema": "a38-review/v2",
        "head": head,
        "passes": max(1, int(a38.get("first_review_passes") or 0)),
        "defects": 0,
        "set_aside": "none",
        "lanes": lanes,
    }
    rules_bound = review_rules_bound(store, worker, runner, task)
    body = _render_review_comment(task, payload, rules_bound=rules_bound)
    return body, int(payload["passes"])


def _validated_first_comment_passes(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
    body: str,
    head: str,
    *,
    checkpoint_passes: int | None = None,
) -> int:
    """Validate every persisted representation of a first-review comment."""
    try:
        payload = parse_review_block(body)
    except ReviewError as exc:
        raise CoordinatorError("saved A38 first comment payload is invalid") from exc
    passes = payload.get("passes")
    lanes = payload.get("lanes")
    lane_ids = (
        {lane.get("id") for lane in lanes if isinstance(lane, Mapping)}
        if isinstance(lanes, list)
        else set()
    )
    if (
        payload.get("head") != head
        or type(passes) is not int
        or passes < 1
        or (checkpoint_passes is not None and passes != checkpoint_passes)
        or lane_ids != {"conformity-a", "logic-a"}
    ):
        raise CoordinatorError("saved A38 first comment payload is invalid")
    rules_bound = review_rules_bound(store, worker, runner, task)
    reasons = [
        *validate_declaration(
            payload,
            head=head,
            markdown_only=False,
            rules_bound=rules_bound,
        ),
        *validate_visible(body, passes),
        *validate_review_record(body, payload, rules_bound=rules_bound),
    ]
    if reasons:
        raise CoordinatorError(
            "saved A38 first comment payload is invalid: "
            + "; ".join(dict.fromkeys(reasons))
        )
    return passes


def _upgrade_first_comment_intent(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
    first: Mapping[str, Any],
    head: str,
) -> dict[str, Any]:
    """Checkpoint body/passes on first-comment intents from older clients."""
    expected_id = str(
        uuid5(NAMESPACE_URL, f"coordinator-a38-first:{task['id']}:{head}")
    )
    saved_id = first.get("activity_id")
    if saved_id not in (None, expected_id):
        raise CoordinatorError("saved A38 first comment activity id is invalid")
    upgraded = dict(first)
    body = upgraded.get("body")
    passes = upgraded.get("passes")
    if isinstance(body, str) and body:
        if type(passes) is not int or passes < 1:
            raise CoordinatorError("saved A38 first comment payload is invalid")
        _validated_first_comment_passes(
            store,
            worker,
            task,
            runner,
            body,
            head,
            checkpoint_passes=passes,
        )
        upgraded.update(head=head, activity_id=expected_id)
        _a38_state(coord(task))["first_comment"] = upgraded
        return upgraded

    row = store.row("activity", expected_id)
    if isinstance(row, dict):
        payload = row.get("payload")
        number = as_int(coord(task).get("pr_number") or task.get("ref"))
        row_body = payload.get("body") if isinstance(payload, dict) else None
        expected_payload = {
            "repo": target_repo(task),
            "number": number,
            "body": row_body,
            "target": "pr",
        }
        if (
            number is None
            or not isinstance(row_body, str)
            or not row_body
            or not _own_comment_activity(store, worker, row, expected_payload)
        ):
            raise CoordinatorError(
                "saved A38 first comment activity is foreign or mismatched"
            )
        body = row_body
        passes = _validated_first_comment_passes(
            store, worker, task, runner, body, head
        )
    else:
        body, passes = _first_comment_body(
            store, worker, task, runner, head=head
        )

    upgraded.update(
        head=head,
        activity_id=expected_id,
        body=body,
        passes=passes,
    )
    _a38_state(coord(task))["first_comment"] = upgraded
    return upgraded


def phase_first_comment(
    store: Store, worker: WorkerConfig, task: dict[str, Any], runner: Runner
) -> list[str]:
    if not worker.a38:
        raise CoordinatorError("A38 first-comment phase called for a non-A38 worker")
    c = coord(task)
    head = verify_signed_clean_head(store, worker, runner, str(c.get("worktree") or ""))
    if head != str(c.get("head_sha") or ""):
        invalidate_head_evidence(c, head)
        c["head_sha"] = head
        c["phase"] = "tests"
        save_task(store, task)
        return ["head changed before first A38 review comment"]
    a38 = _a38_state(c)
    existing = a38.get("first_comment")
    same_head_intent = (
        dict(existing)
        if isinstance(existing, dict) and existing.get("head") == head
        else {}
    )
    try:
        same_head_intent = _upgrade_first_comment_intent(
            store, worker, task, runner, same_head_intent, head
        )
    except CoordinatorError as exc:
        if str(exc).startswith("first review ") and str(exc).endswith(
            " prompt evidence missing"
        ):
            c["phase"] = "pr_gates_grok"
            save_task(store, task)
            return [f"{exc}; re-running gate"]
        raise
    activity_id = str(same_head_intent["activity_id"])
    body = str(same_head_intent["body"])
    passes = int(same_head_intent["passes"])
    request_key = _second_review_request_key(c, head, passes)
    _requested, request_plan = _prepare_second_review_request(
        store,
        worker,
        task,
        force=head_review_blocks(c, head),
        request_key=request_key,
    )
    comment_intent = same_head_intent
    comment_intent.update(
        head=head,
        activity_id=activity_id,
        body=body,
        passes=passes,
        request_key=request_key,
        request_occurrence=int(request_plan["occurrence"]),
    )
    a38["first_comment"] = comment_intent
    # Save both outside-effect intents before either the GitHub comment or a ping
    # is sent. Their deterministic ids make the incomplete steps resumable.
    save_task(store, task)
    completed_result = _first_comment_result_verified(
        store, worker, task, runner, comment_intent, head
    )
    already_posted = completed_result is not None
    result = completed_result or _post_comment(
        store, worker, task, runner, activity_id=activity_id, body=body
    )
    comment_intent.update(id=result.get("id"), url=result.get("url"))
    # Checkpoint the external comment result as soon as the executor has verified it.
    save_task(store, task)
    _write_second_review_request_pings(store, task, request_plan)
    c["phase"] = "await_second_review"
    save_task(store, task)
    verb = "already posted" if already_posted else "posted"
    return [f"first A38 review comment {verb} on {head[:7]}"]


def _ping_id(task_id: str, head: str, login: str, occurrence: int) -> str:
    return str(uuid5(NAMESPACE_URL, f"coordinator-a38-review-request:{task_id}:{head}:{login}:{occurrence}"))


def _second_review_request_note(*, url: str, session_id: str) -> str:
    prefix = "Second A38 review requested. PR: "
    suffix = (
        f". Return the result by session mail to worker session `{session_id}`; "
        "use the payload documented in `docs/issue-coordinator.md`."
    )
    available = SECOND_REVIEW_NOTE_LIMIT - len(prefix) - len(suffix)
    if available < 1:
        raise CoordinatorError("second review request template exceeds 1000 characters")
    rendered_url = url
    if len(rendered_url) > available:
        rendered_url = rendered_url[: max(0, available - 1)] + "…"
    return prefix + rendered_url + suffix


def head_review_blocks(c: dict[str, Any], head: str) -> bool:
    state = _a38_state(c).get("second_review")
    reviews = state.get("reviews") if isinstance(state, dict) else None
    return (
        isinstance(state, dict)
        and state.get("head") == head
        and isinstance(reviews, list)
        and newest_reviews_block(reviews)
    )


def _second_review_request_key(c: dict[str, Any], head: str, passes: int) -> str:
    base = f"first-comment:{head}:{passes}"
    state = _a38_state(c).get("second_review")
    reviews = state.get("reviews") if isinstance(state, dict) else None
    entries = [review for review in reviews or [] if isinstance(review, dict)]
    if not entries or not newest_reviews_block(entries):
        return base
    latest = entries[-1]
    identity = latest.get("mail_id") or latest.get("activity_id")
    if not isinstance(identity, str) or not identity:
        identity = "|".join(
            str(latest.get(key) or "") for key in ("at", "reviewer", "id", "url")
        )
    if not identity.strip("|"):
        raise CoordinatorError("blocking second review has no durable identity")
    return f"{base}:blocking-review:{identity}"


def send_second_review_requests(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    *,
    force: bool = False,
    request_key: str | None = None,
) -> bool:
    requested, plan = _prepare_second_review_request(
        store,
        worker,
        task,
        force=force,
        request_key=request_key,
    )
    # The task intent must exist before the first ping row. On a retry the same
    # plan fills only missing deterministic ping ids.
    save_task(store, task)
    _write_second_review_request_pings(store, task, plan)
    return requested


def _prepare_second_review_request(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    *,
    force: bool = False,
    request_key: str | None = None,
) -> tuple[bool, dict[str, Any]]:
    c = coord(task)
    head = str(c.get("head_sha") or "")
    if not is_sha(head):
        raise CoordinatorError("second review request requires current head")
    a38 = _a38_state(c)
    prior = a38.get("second_review")
    same_head = isinstance(prior, dict) and prior.get("head") == head
    same_request = (
        same_head
        and request_key is not None
        and prior.get("request_key") == request_key
    )
    requested = not same_head or (force and not same_request)
    # After a blocking review drops the state, continue the counter so new ping ids are fresh.
    occurrence = (
        int(prior.get("request_count") or 0)
        if same_head
        else int(a38.get("second_review_next_occurrence") or 0)
    )
    if force and requested:
        occurrence += 1
    now = utcnow()
    requested_at = (prior.get("requested_at") or now) if same_head else now
    # A re-ping of the same head keeps every review already posted for it.
    state = dict(prior) if same_head else {}
    if requested:
        state.update(
            head=head,
            requested_at=requested_at,
            last_requested_at=now,
            request_count=occurrence,
            pinged=list(worker.second_reviewers),
        )
        if request_key is None:
            state.pop("request_key", None)
        else:
            state["request_key"] = request_key
    else:
        # Upgrade older request state to a complete durable intent without
        # changing its timeout or occurrence.
        state.setdefault("head", head)
        state.setdefault("requested_at", now)
        state.setdefault("last_requested_at", state["requested_at"])
        state.setdefault("request_count", occurrence)
        state.setdefault("pinged", list(worker.second_reviewers))
        if request_key is not None:
            state.setdefault("request_key", request_key)
    state.setdefault("status", "requested")
    saved_plan = state.get("request_plan")
    if (
        not requested
        and isinstance(saved_plan, dict)
        and saved_plan.get("head") == head
        and int(saved_plan.get("occurrence") or 0) == occurrence
    ):
        a38["second_review"] = state
        return requested, dict(saved_plan)

    number = as_int(c.get("pr_number") or task.get("ref"))
    if number is None:
        raise CoordinatorError("second review request requires pull request number")
    url = str(c.get("pr_url") or f"https://github.com/{target_repo(task)}/pull/{number}")
    note = _second_review_request_note(url=url, session_id=worker.session_id)
    sender = store.meta("github_login")
    if not isinstance(sender, str) or not sender:
        raise CoordinatorError("device must be paired before sending second-review pings")
    recipients = (
        list(worker.second_reviewers)
        if requested
        else list(state.get("pinged") or worker.second_reviewers)
    )
    plan = {
        "head": head,
        "occurrence": occurrence,
        "note": note,
        "sender": sender.casefold(),
        "created_at": str(state.get("last_requested_at") or now),
        "recipients": recipients,
    }
    state["request_plan"] = dict(plan)
    a38["second_review"] = state
    return requested, plan


def _write_second_review_request_pings(
    store: Store,
    task: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> None:
    head = str(plan["head"])
    occurrence = int(plan["occurrence"])
    for login in plan["recipients"]:
        pid = _ping_id(str(task["id"]), head, login, occurrence)
        payload = _second_review_ping_payload(task, plan, login=login, ping_id=pid)
        existing = store.row("ping", pid)
        if existing is not None:
            stable_fields = {
                key: value for key, value in payload.items() if key != "acked_at"
            }
            if not _own_row_matches(store, existing, stable_fields):
                raise CoordinatorError(
                    "A38 second-review ping id is held by a foreign or mismatched row"
                )
            continue
        store.write(
            "ping",
            "insert",
            pid,
            payload,
        )


def _second_review_ping_payload(
    task: Mapping[str, Any],
    plan: Mapping[str, Any],
    *,
    login: str,
    ping_id: str,
) -> dict[str, Any]:
    return {
        "id": ping_id,
        "from_login": plan["sender"],
        "to_login": login,
        "kind": "review-request",
        "task_id": task["id"],
        "body": plan["note"],
        "created_at": plan["created_at"],
        "acked_at": None,
    }


def _stored_second_review_request_plan(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any] | None:
    """Return the already-saved ping occurrence without advancing it."""
    c = coord(task)
    head = str(c.get("head_sha") or "")
    plan = state.get("request_plan")
    if isinstance(plan, dict):
        plan = dict(plan)
    elif isinstance(state.get("pinged"), list) and state.get("last_requested_at"):
        number = as_int(c.get("pr_number") or task.get("ref"))
        if number is None:
            raise CoordinatorError("saved second review request has no pull request number")
        sender = store.meta("github_login")
        if not isinstance(sender, str) or not sender:
            raise CoordinatorError(
                "device must be paired before reconciling second-review pings"
            )
        url = str(
            c.get("pr_url")
            or f"https://github.com/{target_repo(task)}/pull/{number}"
        )
        plan = {
            "head": head,
            "occurrence": int(state.get("request_count") or 0),
            "note": _second_review_request_note(
                url=url, session_id=worker.session_id
            ),
            "sender": sender.casefold(),
            "created_at": str(state["last_requested_at"]),
            "recipients": list(state["pinged"]),
        }
        state["request_plan"] = dict(plan)
    else:
        return None

    recipients = plan.get("recipients")
    if (
        plan.get("head") != head
        or "occurrence" not in plan
        or int(plan.get("occurrence") or 0)
        != int(state.get("request_count") or 0)
        or not isinstance(plan.get("sender"), str)
        or not isinstance(plan.get("note"), str)
        or not isinstance(plan.get("created_at"), str)
        or not isinstance(recipients, list)
        or any(not isinstance(login, str) or not login for login in recipients)
    ):
        raise CoordinatorError("saved second review request plan is invalid")
    return plan


def _first_comment_result_verified(
    store: Store,
    worker: WorkerConfig,
    task: Mapping[str, Any],
    runner: Runner,
    first: Mapping[str, Any],
    head: str,
) -> dict[str, Any] | None:
    """Return the exact verified comment result; reject any occupied mismatch."""
    expected_id = str(
        uuid5(NAMESPACE_URL, f"coordinator-a38-first:{task['id']}:{head}")
    )
    activity_id = first.get("activity_id")
    if activity_id != expected_id:
        raise CoordinatorError("saved A38 first comment activity id is invalid")
    body = first.get("body")
    number = as_int(coord(dict(task)).get("pr_number") or task.get("ref"))
    if not isinstance(body, str) or not body or number is None:
        raise CoordinatorError("saved A38 first comment payload is invalid")
    saved_passes = first.get("passes")
    if type(saved_passes) is not int:
        raise CoordinatorError("saved A38 first comment payload is invalid")
    _validated_first_comment_passes(
        store,
        worker,
        dict(task),
        runner,
        body,
        head,
        checkpoint_passes=saved_passes,
    )
    payload = {
        "repo": target_repo(dict(task)),
        "number": number,
        "body": body,
        "target": "pr",
    }
    row = store.row("activity", activity_id)
    if row is None:
        return None
    if row.get("_origin_device_id") != store.device_id():
        raise CoordinatorError(
            "saved A38 first comment activity id is held by a row this worker did not write"
        )
    if not _own_comment_activity(store, worker, row, payload):
        raise CoordinatorError(
            "saved A38 first comment activity is foreign or mismatched"
        )
    if row.get("execution_status") != "done":
        return None
    result = row.get("result") if isinstance(row, dict) else None
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("url"), str)
        or not result["url"]
    ):
        raise CoordinatorError("saved A38 first comment result is invalid")
    return result


def _saved_second_review_request_needs_reconcile(
    store: Store,
    task: Mapping[str, Any],
    head: str,
    state: Mapping[str, Any],
) -> bool:
    """Check deterministic ping ids without reconstructing a legacy plan.

    In particular, this check must not require device pairing.  Pairing and
    legacy plan reconstruction are needed only after a missing or invalid row
    proves that the saved occurrence actually needs recovery.
    """
    saved_plan = state.get("request_plan")
    # ``pinged`` and ``last_requested_at`` predate durable occurrences and say
    # only that a request was made.  The saved plan is the recovery intent that
    # binds recipients to deterministic ids.  A pending first-comment intent
    # may still upgrade the legacy shape through
    # ``_stored_second_review_request_plan`` below.
    if not isinstance(saved_plan, dict):
        return False
    if (
        saved_plan.get("head") != head
        or "occurrence" not in saved_plan
        or not isinstance(saved_plan.get("sender"), str)
        or not isinstance(saved_plan.get("note"), str)
        or not isinstance(saved_plan.get("created_at"), str)
    ):
        raise CoordinatorError("saved second review request plan is invalid")
    occurrence = int(saved_plan.get("occurrence") or 0)
    recipients = saved_plan.get("recipients")
    if not isinstance(recipients, list) or any(
        not isinstance(login, str) or not login for login in recipients
    ):
        raise CoordinatorError("saved second review request plan is invalid")
    for login in recipients:
        ping_id = _ping_id(str(task["id"]), head, login, occurrence)
        row = store.row("ping", ping_id)
        payload = _second_review_ping_payload(
            task, saved_plan, login=login, ping_id=ping_id
        )
        stable_fields = {key: value for key, value in payload.items() if key != "acked_at"}
        if not isinstance(row, dict) or not _own_row_matches(store, row, stable_fields):
            return True
    return False


def reconcile_a38_publication_intents(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
) -> list[str]:
    """Finish a saved first-comment/ping occurrence without creating another.

    This is intentionally narrower than :func:`phase_first_comment` and
    :func:`send_second_review_requests`: it never prepares an occurrence.  It
    only verifies/publishes the deterministic saved comment and fills missing
    ping ids from the saved plan, including after the pull request was merged.
    """
    if not worker.a38:
        return []
    c = coord(task)
    head = str(c.get("head_sha") or "")
    a38 = _a38_state(c)
    first = a38.get("first_comment")
    second = a38.get("second_review")
    if isinstance(first, dict) and first.get("head") == head:
        legacy_first = not (
            isinstance(first.get("body"), str)
            and bool(first.get("body"))
            and type(first.get("passes")) is int
            and first.get("passes") >= 1
        )
        first = _upgrade_first_comment_intent(
            store, worker, task, runner, first, head
        )
        if legacy_first:
            # Reconciliation may be the first upgraded-client path to observe
            # this legacy intent.  Checkpoint its exact payload before effects.
            save_task(store, task)
    first_saved = (
        isinstance(first, dict)
        and first.get("head") == head
        and isinstance(first.get("activity_id"), str)
        and bool(first.get("activity_id"))
    )
    first_result = (
        _first_comment_result_verified(store, worker, task, runner, first, head)
        if first_saved
        else None
    )
    first_pending = first_saved and first_result is None
    pings_pending = (
        isinstance(second, dict)
        and second.get("head") == head
        and _saved_second_review_request_needs_reconcile(store, task, head, second)
    )
    if not first_pending and not pings_pending:
        return []
    plan = (
        _stored_second_review_request_plan(store, worker, task, second)
        if isinstance(second, dict) and second.get("head") == head
        else None
    )
    if first_pending and plan is None:
        raise CoordinatorError(
            "saved A38 first comment has no saved second-review request occurrence"
        )

    lines: list[str] = []
    if first_pending:
        expected_id = str(first["activity_id"])
        body = first.get("body")
        if not isinstance(body, str) or not body:
            raise CoordinatorError("saved A38 first comment payload is invalid")
        result = _post_comment(
            store,
            worker,
            task,
            runner,
            activity_id=expected_id,
            body=body,
        )
        first.update(id=result.get("id"), url=result.get("url"))
        save_task(store, task)
        lines.append(f"first A38 review comment reconciled on {head[:7]}")
    elif pings_pending and plan is not None:
        # Persist a legacy plan upgrade before filling its deterministic ids.
        save_task(store, task)

    if pings_pending and plan is not None:
        _write_second_review_request_pings(store, task, plan)
        lines.append(
            f"second-review request occurrence {int(plan['occurrence'])} reconciled"
        )
    return lines


def _mark_message_read(store: Store, worker: WorkerConfig, message_id: str) -> None:
    # The device id keeps a replica row from another device off the local receipt's id.
    ack_id = str(
        uuid5(
            NAMESPACE_URL,
            f"coordinator-a38-message-read:{store.device_id()}:{worker.session_id}:{message_id}",
        )
    )
    existing = store.row("activity", ack_id)
    if existing is not None:
        if (
            existing.get("_origin_device_id") == store.device_id()
            and existing.get("execution_status") == "done"
        ):
            return
        if message_id in _read_message_ids(store, worker):
            return
        # Another device holds this id; a fresh id keeps the local receipt writable.
        ack_id = str(uuid4())
    store.write(
        "activity",
        "insert",
        ack_id,
        {
            "id": ack_id,
            "session_id": worker.session_id,
            "type": "message.read",
            "payload": {"message_id": message_id},
            "execution_status": "done",
        },
    )


def _read_message_ids(store: Store, worker: WorkerConfig) -> set[str]:
    ids: set[str] = set()
    device_id = store.device_id()
    for row in store.rows("activity"):
        if (
            row.get("type") != "message.read"
            or row.get("session_id") != worker.session_id
            or row.get("execution_status") != "done"
            or row.get("_origin_device_id") != device_id
        ):
            continue
        payload = row.get("payload")
        if isinstance(payload, dict) and isinstance(payload.get("message_id"), str):
            ids.add(payload["message_id"])
    return ids


def fresh_pr_head(
    store: Store, worker: WorkerConfig, task: Mapping[str, Any], runner: Runner
) -> str | None:
    c = coord(dict(task)) if not isinstance(task, dict) else coord(task)
    number = as_int(c.get("pr_number") or task.get("ref"))
    if number is None:
        raise CoordinatorError("second review intake requires pull request number")
    api = scoped(store, worker.session_id, runner)
    try:
        view = gh_json(
            api,
            [
                "gh",
                "pr",
                "view",
                str(number),
                "--repo",
                target_repo(dict(task)),
                "--json",
                "headRefOid",
            ],
        )
    except CoordinatorError:
        return None
    head = str(view.get("headRefOid") or "").lower() if isinstance(view, dict) else ""
    if not is_sha(head):
        raise CoordinatorError("cannot read current pull request head")
    return head


def pr_state(
    store: Store, worker: WorkerConfig, task: Mapping[str, Any], runner: Runner
) -> str | None:
    """Return OPEN, CLOSED or MERGED for the task's pull request, or None when unreadable."""
    c = coord(dict(task)) if not isinstance(task, dict) else coord(task)
    number = as_int(c.get("pr_number") or task.get("ref"))
    if number is None:
        return None
    api = scoped(store, worker.session_id, runner)
    try:
        view = gh_json(
            api,
            [
                "gh",
                "pr",
                "view",
                str(number),
                "--repo",
                target_repo(dict(task)),
                "--json",
                "state",
            ],
        )
    except CoordinatorError:
        return None
    state = str(view.get("state") or "").upper() if isinstance(view, dict) else ""
    return state if state in ("OPEN", "CLOSED", "MERGED") else None


def public_prompt(prompt: str, worker: WorkerConfig, task: Mapping[str, Any]) -> str:
    """Replace this host's local paths in a lane prompt before it is published.

    The review comment quotes the prompt; the control directory, the task
    worktree, the workspace root and the home directory are host details.
    """
    from pathlib import Path

    c = coord(dict(task)) if not isinstance(task, dict) else coord(task)
    replacements = [
        (str(control_dir(worker, str(task["id"]))), "<control-dir>"),
        (str(c.get("worktree") or ""), "<worktree>"),
        (str(worker.workspace_root), "<workspace>"),
        (str(Path.home()), "~"),
    ]
    text = prompt
    for raw, placeholder in replacements:
        if not raw or raw in ("/", "."):
            continue
        variants = {raw, str(Path(raw).resolve()) if Path(raw).is_absolute() else raw}
        # Longest first, so a resolved path is replaced before its shorter symlink form.
        for variant in sorted(variants, key=len, reverse=True):
            text = text.replace(variant, placeholder)
    return text


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def intake_second_reviews(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
    current: str,
) -> list[str] | None:
    if not worker.a38:
        raise CoordinatorError("external second-review intake called for a non-A38 worker")
    c = coord(task)
    read = _read_message_ids(store, worker)
    # Oldest first by replica time with a deterministic tie-break; the last review
    # posted for the head decides, as the guard reads the newest comment.
    messages = sorted(
        store.rows("activity"),
        key=lambda row: (str(row.get("_updated_at") or ""), str(row.get("id") or "")),
    )
    lines: list[str] = []
    for row in messages:
        message_id = str(row.get("id") or "")
        if not message_id or message_id in read or row.get("type") != "message":
            continue
        outer = row.get("payload")
        if not isinstance(outer, dict) or outer.get("to_session") != worker.session_id:
            continue
        raw = outer.get("payload")
        if not isinstance(raw, Mapping):
            continue
        if raw.get("kind") != SECOND_REVIEW_KIND or raw.get("task_id") != task["id"]:
            continue
        login = row.get("_github_login")
        if not isinstance(login, str) or not login:
            _observation(c, "second review mail ignored: sender GitHub login is unavailable")
            save_task(store, task)
            _mark_message_read(store, worker, message_id)
            continue
        login = login.casefold()
        if login not in worker.second_reviewers:
            _observation(c, f"second review mail from unauthorized @{login} ignored")
            save_task(store, task)
            _mark_message_read(store, worker, message_id)
            continue
        try:
            payload = validate_second_review_payload(raw)
        except CoordinatorError as exc:
            _observation(c, f"malformed second review mail ignored: {exc}")
            save_task(store, task)
            _mark_message_read(store, worker, message_id)
            continue
        if payload["head"] != current:
            _observation(c, "second review for stale head ignored")
            save_task(store, task)
            _mark_message_read(store, worker, message_id)
            continue
        # The guard decides by the newest posted comment, so a review older than one
        # already posted for this head is not posted: posting order stays time order.
        posted_state = _a38_state(c).get("second_review")
        posted_reviews = posted_state.get("reviews") if isinstance(posted_state, dict) else None
        at = str(row.get("_updated_at") or "")
        # A post that may have happened before a crash counts as posted (its watermark).
        pending_post = posted_state.get("pending_post") if isinstance(posted_state, dict) else None
        known = [review for review in posted_reviews or [] if isinstance(review, dict)]
        if isinstance(pending_post, dict) and pending_post.get("mail_id") != str(message_id):
            known.append(pending_post)
        if (
            isinstance(posted_state, dict)
            and posted_state.get("head") == current
            and any(
                (str(review.get("at") or ""), str(review.get("mail_id") or ""))
                > (at, str(message_id))
                for review in known
            )
        ):
            _observation(c, f"second review by @{login} superseded by a newer posted review")
            save_task(store, task)
            _mark_message_read(store, worker, message_id)
            continue
        declaration = {
            "schema": "a38-review/v2",
            "head": current,
            "passes": 1,
            "defects": payload["defects"],
            "set_aside": "none",
            "blocks_merge": payload["blocks_merge"],
            "lanes": payload["lanes"],
        }
        findings = redact(payload["findings"], limit=20000) if payload["defects"] else ""
        rules_bound = review_rules_bound(store, worker, runner, task)
        try:
            body = _render_review_comment(
                task,
                declaration,
                rules_bound=rules_bound,
                reviewer=login,
                findings=findings,
            )
        except CoordinatorError as exc:
            _observation(c, f"malformed second review mail ignored: {exc}")
            save_task(store, task)
            _mark_message_read(store, worker, message_id)
            continue
        activity_id = str(
            uuid5(NAMESPACE_URL, f"coordinator-a38-second:{task['id']}:{current}:{message_id}")
        )
        a38 = _a38_state(c)
        state = a38.get("second_review")
        if not isinstance(state, dict) or state.get("head") != current:
            state = {"head": current, "pinged": list(worker.second_reviewers)}
            a38["second_review"] = state
        # Persist the ordering watermark before posting, so a crash after the post
        # cannot let an older mail be posted after it on the retry.
        state["pending_post"] = {"at": at, "mail_id": str(message_id), "activity_id": activity_id}
        save_task(store, task)
        result = _post_comment(store, worker, task, runner, activity_id=activity_id, body=body)
        state.pop("pending_post", None)
        reviews = state.get("reviews")
        if not isinstance(reviews, list):
            reviews = []
            prior_activity_id = state.get("activity_id")
            if state.get("status") == "posted" and isinstance(prior_activity_id, str):
                reviews.append(
                    {
                        "reviewer": state.get("reviewer"),
                        "activity_id": prior_activity_id,
                        "id": state.get("id"),
                        "url": state.get("url"),
                        "blocks_merge": False,
                    }
                )
            state["reviews"] = reviews
        first_posted = state.get("status") != "posted"
        if first_posted:
            state.update(
                {
                    "status": "posted",
                    "reviewer": login,
                    "activity_id": activity_id,
                    "id": result.get("id"),
                    "url": result.get("url"),
                }
            )
        if not any(
            isinstance(review, dict) and review.get("activity_id") == activity_id
            for review in reviews
        ):
            reviews.append(
                {
                    "reviewer": login,
                    "activity_id": activity_id,
                    "id": result.get("id"),
                    "url": result.get("url"),
                    "blocks_merge": payload["blocks_merge"],
                    "at": str(row.get("_updated_at") or ""),
                    # Same sort key as intake, so equal times still compare in mail order.
                    "mail_id": str(message_id),
                }
            )
        if payload["blocks_merge"]:
            live_state = pr_state(store, worker, task, runner)
            if live_state is None:
                # Unknown state: keep the mail unread and retry; the comment post is
                # idempotent by activity id.
                save_task(store, task)
                lines.append("PR state temporarily unavailable; retrying the blocking second review")
                return lines
            if live_state != "OPEN":
                # A human merged meanwhile; the posted result must not reopen work.
                c["phase"] = "await_merge"
                save_task(store, task)
                _mark_message_read(store, worker, message_id)
                lines.append(
                    f"blocking external second review by @{login} arrived after the pull request "
                    "left the open state; verifying the merge"
                )
                # Keep reading: after the merge every waiting review is taken in, and
                # await_merge decides by the newest one.
                continue
            # Keep reading the queued mail first: a newer passing review of the same
            # head supersedes this block; the saved newest review decides after the loop.
            at = str(row.get("_updated_at") or "")
            saved_block = c.get("second_review_block")
            # A late delivery of an older blocking review keeps the newer block's findings.
            if not (
                isinstance(saved_block, dict)
                and saved_block.get("head") == current
                and str(saved_block.get("at") or "") > at
            ):
                c["second_review_block"] = {
                    "head": current, "findings": findings, "reviewer": login, "at": at
                }
            save_task(store, task)
            _mark_message_read(store, worker, message_id)
            lines.append(f"blocking external second review by @{login} recorded")
            continue
        # Merge-deadline checks use this durable review-record timestamp, not
        # the later coordinator tick that happens to ingest the mail.
        state["requirement_satisfied_at"] = at
        evidence = (
            f"{EXTERNAL_SECOND_REVIEW_PREFIX}{login} at {current}: "
            f"{result['url']}"
        )
        # Every passing review records its evidence, so a pass after an earlier
        # block on the same head (for example after the merge) still closes the keys.
        # This pass is now the last posted review, so it is the deciding one.
        set_checklist(store, task, "review_2_quality", "ja", evidence, source="script")
        set_checklist(store, task, "review_2_logic", "ja", evidence, source="script")
        if c.get("phase") == "await_second_review":
            c["phase"] = "ci"
        save_task(store, task)
        _mark_message_read(store, worker, message_id)
        lines.append(f"external second review by @{login} posted on {current[:7]}")
    routed = _route_newest_blocking_review(store, worker, task, runner, current)
    if routed is not None:
        lines.append(routed)
    return lines or None


def _route_newest_blocking_review(
    store: Store, worker: WorkerConfig, task: dict[str, Any], runner: Runner, current: str
) -> str | None:
    """Route to the implementer when the saved newest review of this head blocks.

    Decided from durable state, so a crash between marking mail read and routing
    cannot lose the block, and only while the pull request is still open."""
    c = coord(task)
    state = _a38_state(c).get("second_review")
    reviews = state.get("reviews") if isinstance(state, dict) else None
    block = c.get("second_review_block")
    if (
        not isinstance(state, dict)
        or state.get("head") != current
        or not isinstance(reviews, list)
        or not newest_reviews_block(reviews)
        or not isinstance(block, dict)
        or block.get("head") != current
    ):
        return None
    if pr_state(store, worker, task, runner) != "OPEN":
        return None
    c["findings"] = block.get("findings") or ""
    c.pop("second_review_block", None)
    # Keep the already-published first comment and review history of this head.
    # A no-change implementation pass must reuse the immutable comment payload;
    # a new head drops both with the rest of the evidence.
    first_comment = _a38_state(c).get("first_comment")
    second_review = _a38_state(c).get("second_review")
    invalidate_head_evidence(c, "")
    if isinstance(first_comment, dict):
        _a38_state(c)["first_comment"] = first_comment
    if isinstance(second_review, dict):
        _a38_state(c)["second_review"] = second_review
    c["phase"] = "implement"
    task["state"] = "implementing"
    save_task(store, task)
    return f"blocking external second review by @{block.get('reviewer')}; routing to implementer"


def phase_await_second_review(
    store: Store, worker: WorkerConfig, task: dict[str, Any], runner: Runner
) -> list[str]:
    if not worker.a38:
        raise CoordinatorError("external second-review phase called for a non-A38 worker")
    c = coord(task)
    current = fresh_pr_head(store, worker, task, runner)
    if current is None:
        return ["PR state temporarily unavailable; retrying"]
    expected = str(c.get("head_sha") or "")
    if current != expected:
        c["head_sha"] = current
        invalidate_head_evidence(c, current)
        c["phase"] = "tests"
        save_task(store, task)
        return ["PR head changed while awaiting second review; invalidating evidence"]
    state = pr_state(store, worker, task, runner)
    if state is None:
        return ["PR state temporarily unavailable; retrying"]
    if state == "MERGED":
        # A merge must not strand a saved first-comment or partial ping
        # occurrence. This only completes current deterministic ids.
        reconcile_a38_publication_intents(store, worker, task, runner)
        # The guard may make the PR Ready before the second review; verify the merge.
        c["phase"] = "await_merge"
        save_task(store, task)
        return ["PR merged while awaiting second review; verifying the merge"]
    if state == "CLOSED":
        # A closed pull request gets no further review comments.
        raise CoordinatorError("pull request is not open while awaiting second review")
    reconcile_a38_publication_intents(store, worker, task, runner)
    lines = intake_second_reviews(store, worker, task, runner, current)
    if lines is not None:
        return lines

    a38 = _a38_state(c)
    state = a38.get("second_review")
    last = _parse_iso(state.get("last_requested_at")) if isinstance(state, dict) else None
    now = _parse_iso(utcnow())
    if now is None:
        raise CoordinatorError("coordinator clock returned an invalid timestamp")
    if last is not None and (now - last).total_seconds() >= worker.second_review_timeout_seconds:
        if send_second_review_requests(store, worker, task, force=True):
            _observation(c, "second review request timed out; review-request pings re-sent")
            save_task(store, task)
            return ["second review still pending; review-request pings re-sent"]
    return ["awaiting external second review"]


def _checklist_maps(store: Store, task_id: str) -> tuple[dict[str, str], dict[str, str]]:
    statuses: dict[str, str] = {}
    evidence: dict[str, str] = {}
    for row in store.rows("checklist_item"):
        if row.get("task_id") != task_id:
            continue
        key = str(row.get("key") or "")
        statuses[key] = str(row.get("status") or "")
        evidence[key] = str(row.get("evidence") or "")
    return statuses, evidence


def require_ready_evidence(store: Store, task: dict[str, Any], head: str) -> bool:
    c = coord(task)
    a38 = _a38_state(c)
    if a38.get("pending_base_sha"):
        raise CoordinatorError(f"A38 base evidence is still pending on {head[:7]}")
    base_sha = evidence_base_sha(c, a38=True)
    report = a38.get("report")
    waiver = a38.get("report_waiver")
    legacy_report = (
        isinstance(report, dict)
        and report.get("head") == head
        and (
            not is_sha(str(report.get("base_sha") or ""))
            or not is_sha(str(report.get("policy_revision") or ""))
        )
    )
    if legacy_report:
        _drop_base_evidence(c)
        c["phase"] = "tests"
        c.pop("resume_phase", None)
        c.pop("question_activity_id", None)
        c.pop("blocker", None)
        save_task(store, task)
        return False
    report_bound = (
        is_sha(base_sha)
        and isinstance(report, dict)
        and report.get("head") == head
        and report.get("base_sha") == base_sha
        and report.get("policy_revision") == base_sha
    )
    waiver_bound = (
        is_sha(base_sha)
        and isinstance(waiver, dict)
        and waiver.get("head") == head
        and waiver.get("base_sha") == base_sha
        and waiver.get("policy_revision") == base_sha
        and waiver.get("reason") in {
            "markdown-only change set",
            "guard-docs change set",
        }
    )
    if report_bound == waiver_bound:
        raise CoordinatorError(f"A38 report or waiver evidence is missing on {head[:7]}")
    for key in ("first_comment", "second_review"):
        value = a38.get(key)
        if not isinstance(value, dict) or value.get("head") != head:
            raise CoordinatorError(f"A38 {key} evidence is missing on {head[:7]}")
    if a38["second_review"].get("status") != "posted":
        raise CoordinatorError("external second review comment is not posted")
    statuses, evidence = _checklist_maps(store, str(task["id"]))
    snapshot = {
        **task,
        "checklist_evidence": evidence,
        "gates": list(latest_gates(store, str(task["id"])).values()),
    }
    if not external_second_review_ok(snapshot, statuses, evidence):
        raise CoordinatorError("external second review checklist evidence is missing")
    return True


def _drop_base_evidence(c: dict[str, Any]) -> None:
    """Drop report/test evidence without claiming a replacement base revision."""
    evidence = c.get("evidence")
    if isinstance(evidence, dict):
        evidence.pop("tests_pass", None)
        evidence.pop("tests_waiver", None)
        evidence.pop("tests_head", None)
        evidence.pop("readiness_head", None)
        evidence.pop("readiness_base", None)
        evidence.pop("readiness_at", None)
    a38 = _a38_state(c)
    for key in (
        "report",
        "report_intent",
        "report_waiver",
        "post_suite_checkpoint",
        "guard_status",
    ):
        a38.pop(key, None)
    a38.pop("policy_revision", None)


def invalidate_base_evidence(
    store: Store, task: dict[str, Any], base_sha: str
) -> None:
    """Drop report/test evidence whose intent was bound to the previous PR base."""
    if not is_sha(base_sha):
        raise CoordinatorError("current pull request base revision is not a 40-hex SHA")
    candidate_state = _report_checkpoint_state(task)
    with store.exclusive(_report_task_lock(task["id"])):
        fresh = store.row("task", str(task["id"]))
        if fresh is None:
            raise CoordinatorError("A38 report task no longer exists")
        if _report_checkpoint_state(fresh) != candidate_state:
            raise A38ReportCheckpointConflict(
                "A38 report task changed before base invalidation"
            )
        committed_task = copy.deepcopy(fresh)
        committed_c = coord(committed_task)
        intent = _a38_state(committed_c).get("report_intent")
        activity_id = intent.get("activity_id") if isinstance(intent, dict) else None
        lock_key = a38_report_activity_lock(str(activity_id or "missing"))
        with store.exclusive(lock_key), store.conn.transaction():
            _retire_pending_report_activity(
                store,
                intent,
                reason="A38 report intent invalidated by a pull request base change",
            )
            _drop_base_evidence(committed_c)
            _a38_state(committed_c)["pending_base_sha"] = base_sha
            reopen_base_bound_checklist(store, committed_task)
            save_task(store, committed_task)
    task.clear()
    task.update(committed_task)


def _reconcile_pending_base(
    store: Store, task: dict[str, Any], observed_base: str
) -> bool:
    """Replace a stale pending base after a stable live-context read."""
    if not is_sha(observed_base):
        raise CoordinatorError("current pull request base revision is not a 40-hex SHA")
    pending = str(_a38_state(coord(task)).get("pending_base_sha") or "").lower()
    if pending and pending != observed_base:
        invalidate_base_evidence(store, task, observed_base)
        return True
    return False


def _bound_base_evidence(
    a38: Mapping[str, Any], *, head: str, base_sha: str
) -> bool:
    report = a38.get("report")
    waiver = a38.get("report_waiver")
    report_bound = (
        isinstance(report, Mapping)
        and report.get("head") == head
        and report.get("base_sha") == base_sha
        and report.get("policy_revision") == base_sha
    )
    waiver_bound = (
        isinstance(waiver, Mapping)
        and waiver.get("head") == head
        and waiver.get("base_sha") == base_sha
        and waiver.get("policy_revision") == base_sha
        and waiver.get("reason")
        in {"markdown-only change set", "guard-docs change set"}
    )
    return report_bound != waiver_bound


def _local_gate_recorded(
    store: Store, task: Mapping[str, Any], *, head: str
) -> bool:
    checklist_closed = any(
        row.get("task_id") == task["id"]
        and str(row.get("key") or "") == "local_check_pass"
        and row.get("status") == "ja"
        for row in store.rows("checklist_item")
    )
    check_recorded = any(
        row.get("task_id") == task["id"]
        and row.get("name") == "a38"
        and row.get("head_sha") == head
        and row.get("result") in {"pass", "skip"}
        for row in store.rows("local_check")
    )
    return checklist_closed and check_recorded


def _promote_base_evidence(
    store: Store, task: dict[str, Any], *, head: str, base_sha: str
) -> None:
    """Promote a measured base only after its report/waiver and local gate exist."""
    c = coord(task)
    a38 = _a38_state(c)
    pending = str(a38.get("pending_base_sha") or "").lower()
    if pending and pending != base_sha:
        raise CoordinatorError("A38 pending base differs from measured evidence")
    if not _bound_base_evidence(a38, head=head, base_sha=base_sha):
        raise CoordinatorError("A38 base promotion requires bound report or waiver evidence")
    if not local_test_gate_satisfied(task, head) or not _local_gate_recorded(
        store, task, head=head
    ):
        raise CoordinatorError("A38 base promotion requires the recorded local gate")
    a38["base_sha"] = base_sha
    a38["policy_revision"] = base_sha
    a38.pop("pending_base_sha", None)


def merged_base_evidence_satisfied(
    store: Store, task: dict[str, Any], *, head: str, base_sha: str
) -> bool:
    """Return whether completion evidence is fully bound to the merged base."""
    c = coord(task)
    a38 = _a38_state(c)
    return (
        not a38.get("pending_base_sha")
        and a38.get("base_sha") == base_sha
        and _bound_base_evidence(a38, head=head, base_sha=base_sha)
        and _local_gate_recorded(store, task, head=head)
    )


def merged_report_visibility_satisfied(
    store: Store,
    worker: WorkerConfig,
    task: Mapping[str, Any],
    api: Runner,
) -> bool:
    """Compare a finalized report body with current repository visibility.

    Inventory waivers have no local-CI report body and therefore no stored
    visibility assertion to revalidate here.
    """
    candidate = dict(task)
    c = coord(candidate)
    report = _a38_state(c).get("report")
    if not isinstance(report, Mapping):
        return True
    activity_id = report.get("activity_id")
    activity = (
        store.row("activity", activity_id)
        if isinstance(activity_id, str) and activity_id
        else None
    )
    if not isinstance(activity, dict) or not _own_report_activity(
        store,
        worker,
        candidate,
        activity,
        str(report.get("head") or ""),
    ):
        activity = None
    payload = activity.get("payload") if isinstance(activity, Mapping) else None
    activity_body = payload.get("body") if isinstance(payload, Mapping) else None
    body = report.get("body")
    if isinstance(activity_body, str) and activity_body:
        if isinstance(body, str) and body and body != activity_body:
            raise NonRecoverableCoordinatorError(
                "task-done refused: stored A38 report bodies differ"
            )
        body = activity_body
    if not isinstance(body, str) or not body:
        raise NonRecoverableCoordinatorError(
            "task-done refused: stored A38 report body is unavailable"
        )
    live_visibility = _repository_visibility(api, target_repo(candidate))
    return _report_body_private(body) is live_visibility


def mark_base_moved_after_evidence(
    c: dict[str, Any], *, old_base: str, new_base: str
) -> None:
    """Invalidate evidence after merge without trusting an unmeasured new base."""
    if not is_sha(old_base) or not is_sha(new_base) or old_base == new_base:
        raise CoordinatorError("merged pull request base movement is invalid")
    _drop_base_evidence(c)
    c["base_moved_after_evidence"] = {
        "old_base": old_base,
        "new_base": new_base,
    }


def local_test_gate_satisfied(task: Mapping[str, Any], head: str) -> bool:
    """A38 local gate is an executed pass or an exact-head inventory waiver."""
    c = coord(dict(task)) if not isinstance(task, dict) else coord(task)
    evidence = c.get("evidence") if isinstance(c.get("evidence"), dict) else {}
    if evidence.get("tests_pass") is True and evidence.get("tests_head") == head:
        return True
    waiver = _a38_state(c).get("report_waiver")
    return (
        evidence.get("tests_head") == head
        and isinstance(waiver, dict)
        and waiver.get("head") == head
        and evidence.get("tests_waiver") == waiver.get("reason")
        and waiver.get("reason")
        in {"markdown-only change set", "guard-docs change set"}
    )


def phase_await_ready(
    store: Store, worker: WorkerConfig, task: dict[str, Any], runner: Runner
) -> list[str]:
    if not worker.a38:
        raise CoordinatorError("await-ready phase called for a non-A38 worker")
    c = coord(task)
    head = str(c.get("head_sha") or "")
    number = as_int(c.get("pr_number") or task.get("ref"))
    if number is None or not is_sha(head):
        raise CoordinatorError("await-ready requires pull request number and head")
    api = scoped(store, worker.session_id, runner)
    try:
        view = gh_json(
            api,
            [
                "gh",
                "pr",
                "view",
                str(number),
                "--repo",
                target_repo(task),
                "--json",
                "isDraft,headRefOid,baseRefOid,state",
            ],
        )
    except CoordinatorError:
        return ["PR state temporarily unavailable; retrying"]
    observed = str(view.get("headRefOid") or "").lower() if isinstance(view, dict) else ""
    if observed != head:
        if not is_sha(observed):
            raise CoordinatorError("cannot read pull request head while awaiting Ready")
        c["head_sha"] = observed
        invalidate_head_evidence(c, observed)
        c["phase"] = "tests"
        save_task(store, task)
        return ["PR head changed while awaiting guard Ready; invalidating evidence"]
    pr_state = str(view.get("state") or "").upper()
    if pr_state in ("MERGED", "CLOSED"):
        # A human may merge (or close) before this poll sees Ready; await_merge
        # verifies the merge or publishes the closed-unmerged blocker.
        c["phase"] = "await_merge"
        save_task(store, task)
        verb = "merged" if pr_state == "MERGED" else "closed"
        return [f"PR {verb} while awaiting guard Ready; verifying the merge"]
    if pr_state != "OPEN":
        raise CoordinatorError("pull request is not open while awaiting guard Ready")
    is_draft = view.get("isDraft")
    if is_draft is not True and is_draft is not False:
        raise CoordinatorError("pull request draft state is unavailable")
    a38 = _a38_state(c)
    pending_base = str(a38.get("pending_base_sha") or "").lower()
    saved_base = evidence_base_sha(c, a38=True)
    observed_base = (
        str(view.get("baseRefOid") or "").lower()
        if isinstance(view, dict)
        else ""
    )
    if (pending_base or is_sha(saved_base)) and not is_sha(observed_base):
        return ["PR state temporarily unavailable; retrying"]
    if pending_base and _reconcile_pending_base(store, task, observed_base):
        c = coord(task)
        c["phase"] = "tests"
        save_task(store, task)
        return ["PR base changed while awaiting guard Ready; invalidating A38 report evidence"]
    if is_sha(saved_base) and observed_base != saved_base:
        invalidate_base_evidence(store, task, observed_base)
        c = coord(task)
        c["phase"] = "tests"
        save_task(store, task)
        return ["PR base changed while awaiting guard Ready; invalidating A38 report evidence"]
    try:
        status_data = gh_json(
            api,
            [
                "gh",
                "api",
                "--paginate",
                "--slurp",
                f"repos/{target_repo(task)}/commits/{head}/statuses",
            ],
        )
    except CoordinatorError:
        return ["PR state temporarily unavailable; retrying"]
    if not isinstance(status_data, list):
        raise CoordinatorError("expected JSON array")
    if status_data and all(isinstance(page, list) for page in status_data):
        statuses = [item for page in status_data for item in page]
    else:
        statuses = status_data
    source = c.get("source")
    base = source.get("base") if isinstance(source, dict) else None
    if not isinstance(base, str) or not base:
        raise CoordinatorError("await-ready requires the pinned base branch")
    expected_context = status_context_enforce(base)
    newest = next(
        (
            item
            for item in statuses
            if isinstance(item, dict)
            and str(item.get("context") or "") == expected_context
        ),
        None,
    )
    a38 = _a38_state(c)
    a38["guard_status"] = {
        "head": head,
        "context": newest.get("context") if isinstance(newest, dict) else None,
        "state": newest.get("state") if isinstance(newest, dict) else None,
        "description": newest.get("description") if isinstance(newest, dict) else "A38 / report status absent",
        "observed_at": utcnow(),
    }
    if is_draft is False:
        if not isinstance(newest, dict) or str(newest.get("state") or "").casefold() != "success":
            observation = (
                "PR left draft without a successful A38 status on "
                f"{head[:7]}"
            )
            _observation(c, observation)
            save_task(store, task)
            return [observation]
        if not require_ready_evidence(store, task, head):
            return [
                f"legacy A38 report on {head[:7]} is not base-bound; re-running tests"
            ]
        allowed = evaluate_allow(
            "pr-ready",
            session_id=worker.session_id,
            task_id=str(task["id"]),
            session_tasks=[
                {
                    "id": task["id"],
                    "session_id": task.get("session_id"),
                    "state": task.get("state"),
                }
            ],
        )
        if not allowed.allowed:
            raise CoordinatorError(f"pr-ready denied: {allowed.reason}")
        evidence = c.setdefault("evidence", {})
        if isinstance(evidence, dict):
            evidence["ready_head"] = head
        c["phase"] = "await_merge"
        task["state"] = "pr-review"
        save_task(store, task)
        return [f"guard set Ready on {head[:7]}; awaiting human merge"]
    save_task(store, task)
    description = a38["guard_status"]["description"] or "A38 / report status has no description"
    return [f"awaiting guard Ready on {head[:7]}: {redact(str(description), limit=500)}"]
