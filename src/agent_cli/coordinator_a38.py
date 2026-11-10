"""A38 phases for the script-owned issue coordinator.

The worker account publishes author evidence. The external second reviewer
communicates only through existing hub pings and session mail. This module
never submits a GitHub review approval and never changes draft state.
"""

from __future__ import annotations

import json
import shutil
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from uuid import NAMESPACE_URL, uuid5, uuid4

from .a38_guard import status_context_enforce
from .a38_review import (
    REVIEW_BEGIN,
    REVIEW_END,
    ReviewError,
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
    Runner,
    as_int,
    coord,
    gh_json,
    is_sha,
    redact,
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
from .github_act import ACTIVITY_MARKER
from .local_ci import LocalCiError, parse_comment
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
    passed: bool,
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
            "result": "pass" if passed else "fail",
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


def phase_tests(
    store: Store, worker: WorkerConfig, task: dict[str, Any], runner: Runner
) -> list[str]:
    """Run the optional trusted precheck, complete A38 suite, and publish its report."""
    if not worker.a38:
        raise CoordinatorError("A38 tests phase called for a non-A38 worker")
    c = coord(task)
    source = c["source"]
    cfg = repo_cfg(worker, str(source["repo"]), task)
    worktree = str(c["worktree"])
    head = verify_signed_clean_head(store, worker, runner, worktree)
    c["head_sha"] = head
    a38 = _a38_state(c)
    activity_id = str(uuid5(NAMESPACE_URL, f"coordinator-a38-report:{task['id']}:{head}"))
    activity = store.row("activity", activity_id)
    if isinstance(activity, dict) and not _own_report_activity(store, worker, task, activity, head):
        raise CoordinatorError("A38 report activity id is held by a row this worker did not write")
    if isinstance(activity, dict):
        payload = activity.get("payload")
        if activity.get("execution_status") != "done":
            if (
                activity.get("type") != "comment.post"
                or not isinstance(payload, dict)
                or payload.get("exact_body") is not True
                or not isinstance(payload.get("body"), str)
                or not payload["body"]
            ):
                raise CoordinatorError("existing A38 report activity is invalid")
            queue_activity(
                store,
                activity_id=activity_id,
                session_id=worker.session_id,
                typ="comment.post",
                payload=payload,
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
        a38["report"] = {
            "head": head,
            "activity_id": activity_id,
            "id": result.get("id"),
            "url": result["url"],
        }
        evidence = c.setdefault("evidence", {})
        if isinstance(evidence, dict):
            evidence["tests_pass"] = True
            evidence["tests_head"] = head
        set_checklist(store, task, "local_check_pass", "ja", f"A38 pass on {head}", source="script")
        c["phase"] = "pr_gates_grok"
        task["state"] = "pr-review"
        save_task(store, task)
        return [f"A38 report already posted on {head[:7]}"]

    if cfg.check_argv:
        from .coordinator_common import coordinator_env

        precheck = run_bounded(
            list(cfg.check_argv),
            timeout=worker.check_timeout,
            cwd=worktree,
            env=coordinator_env(c, worker, cfg),
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
    base_result = git(store, worker, runner, worktree, "rev-parse", f"refs/remotes/origin/{cfg.base}")
    require_git_ok(base_result, "resolve A38 base")
    base_sha = (base_result.stdout or "").strip().lower()
    if not is_sha(base_sha):
        raise CoordinatorError("A38 base revision is not a 40-hex SHA")
    a38["base_sha"] = base_sha
    policy_result = git(store, worker, runner, worktree, "show", f"{base_sha}:.github/a38.json")
    require_git_ok(policy_result, "load A38 policy from base")

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

    api = scoped(store, worker.session_id, runner)
    visibility = gh_json(api, ["gh", "api", f"repos/{cfg.repo}", "--jq", ".private"])
    if not isinstance(visibility, bool):
        raise CoordinatorError("target repository visibility lookup did not return a boolean")
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
    if as_int(c.get("pr_number") or task.get("ref")) is not None:
        # A merge during the suite, passed or failed, goes to the merge check first.
        live_state = pr_state(store, worker, task, runner)
        if live_state in ("MERGED", "CLOSED"):
            c["phase"] = "await_merge"
            save_task(store, task)
            return [f"PR {live_state.lower()} during the A38 suite; verifying the merge"]
    if not passed:
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
    draft_lines = ensure_draft(store, worker, task, runner)
    if str(coord(task).get("head_sha") or "") != head:
        raise CoordinatorError("published pull request head differs from verified A38 head")
    if c.get("pr_number"):
        try:
            set_checklist(
                store,
                task,
                "pushed",
                "ja",
                f"draft PR {c.get('pr_number')} head {head}",
                source="script",
            )
        except CoordinatorError:
            pass
    number = as_int(c.get("pr_number") or task.get("ref"))
    if number is None:
        raise CoordinatorError("A38 report comment requires a pull request number")
    queue_activity(
        store,
        activity_id=activity_id,
        session_id=worker.session_id,
        typ="comment.post",
        payload={
            "repo": target_repo(task),
            "number": number,
            "body": report_body,
            "target": "pr",
            "exact_body": True,
        },
    )
    execute_github(store, runner, activity_ids=(activity_id,))
    row = store.row("activity", activity_id)
    if row is None or row.get("execution_status") != "done":
        raise CoordinatorError("A38 report comment was not verified published")
    result = row.get("result") if isinstance(row.get("result"), dict) else {}
    if not isinstance(result.get("url"), str) or not result["url"]:
        raise CoordinatorError("A38 report comment URL was not verified")
    a38["report"] = {
        "head": head,
        "activity_id": activity_id,
        "id": result.get("id"),
        "url": result.get("url"),
    }
    evidence = c.setdefault("evidence", {})
    if isinstance(evidence, dict):
        evidence["tests_pass"] = True
        evidence["tests_head"] = head
    set_checklist(store, task, "local_check_pass", "ja", f"A38 pass on {head}", source="script")
    c["phase"] = "pr_gates_grok"
    task["state"] = "pr-review"
    save_task(store, task)
    return [f"A38 passed and report posted on {head[:7]}", *draft_lines]


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
    try:
        body, passes = _first_comment_body(
            store, worker, task, runner, head=head
        )
    except CoordinatorError as exc:
        if str(exc).startswith("first review ") and str(exc).endswith(
            " prompt evidence missing"
        ):
            c["phase"] = "pr_gates_grok"
            save_task(store, task)
            return [f"{exc}; re-running gate"]
        raise
    activity_id = str(uuid5(NAMESPACE_URL, f"coordinator-a38-first:{task['id']}:{head}"))
    request_key = _second_review_request_key(c, head, passes)
    _requested, request_plan = _prepare_second_review_request(
        store,
        worker,
        task,
        force=head_review_blocks(c, head),
        request_key=request_key,
    )
    comment_intent = (
        dict(existing)
        if isinstance(existing, dict) and existing.get("head") == head
        else {}
    )
    comment_intent.update(
        head=head,
        activity_id=activity_id,
        request_key=request_key,
        request_occurrence=int(request_plan["occurrence"]),
    )
    a38["first_comment"] = comment_intent
    # Save both outside-effect intents before either the GitHub comment or a ping
    # is sent. Their deterministic ids make the incomplete steps resumable.
    save_task(store, task)
    existing_row = store.row("activity", activity_id)
    already_posted = (
        isinstance(existing_row, dict)
        and existing_row.get("execution_status") == "done"
    )
    result = _post_comment(store, worker, task, runner, activity_id=activity_id, body=body)
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
        payload = {
            "id": pid,
            "from_login": plan["sender"],
            "to_login": login,
            "kind": "review-request",
            "task_id": task["id"],
            "body": plan["note"],
            "created_at": plan["created_at"],
            "acked_at": None,
        }
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


def _first_comment_result_verified(store: Store, first: Mapping[str, Any]) -> bool:
    """A saved comment intent is complete only after the local executor verified it."""
    activity_id = first.get("activity_id")
    if not isinstance(activity_id, str) or not activity_id:
        return False
    row = store.row("activity", activity_id)
    result = row.get("result") if isinstance(row, dict) else None
    return (
        isinstance(row, dict)
        and row.get("_origin_device_id") == store.device_id()
        and row.get("execution_status") == "done"
        and isinstance(result, dict)
        and isinstance(result.get("url"), str)
        and bool(result["url"])
    )


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
    if saved_plan.get("head") != head or "occurrence" not in saved_plan:
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
        if not isinstance(row, dict) or not _own_row_matches(
            store,
            row,
            {
                "id": ping_id,
                "to_login": login,
                "kind": "review-request",
                "task_id": task["id"],
            },
        ):
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
    first_saved = (
        isinstance(first, dict)
        and first.get("head") == head
        and isinstance(first.get("activity_id"), str)
        and bool(first.get("activity_id"))
    )
    first_pending = first_saved and not _first_comment_result_verified(store, first)
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
        expected_id = str(
            uuid5(NAMESPACE_URL, f"coordinator-a38-first:{task['id']}:{head}")
        )
        if first["activity_id"] != expected_id:
            raise CoordinatorError("saved A38 first comment activity id is invalid")
        body, _passes = _first_comment_body(
            store, worker, task, runner, head=head
        )
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


def pr_open_unmerged(
    store: Store, worker: WorkerConfig, task: Mapping[str, Any], runner: Runner
) -> bool | None:
    """Return whether the task's pull request is still open, or None when unreadable."""
    state = pr_state(store, worker, task, runner)
    return None if state is None else state == "OPEN"


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
    # Keep the review history of this head: a merge during the implementer's work must
    # still be decided by it. A new head drops it with the rest of the evidence.
    second_review = _a38_state(c).get("second_review")
    invalidate_head_evidence(c, "")
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


def require_ready_evidence(store: Store, task: dict[str, Any], head: str) -> None:
    c = coord(task)
    a38 = _a38_state(c)
    for key in ("report", "first_comment", "second_review"):
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
                "isDraft,headRefOid,state",
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
        require_ready_evidence(store, task, head)
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
