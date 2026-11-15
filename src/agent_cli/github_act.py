"""Execute pending pr.open, pr.body, comment.post, review.post, and issue.write activities via gh."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from contextlib import ExitStack
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from .coordinator_common import redact
from .github_accounts import AccountError, load_accounts
from .runtime import Completed
from .store import Store
from .text_sanitize import redact_terminal_interleaved_secret
from .timestamps import parse_utc_timestamp

Runner = Callable[[list[str]], Completed]

ACTIVITY_MARKER = "<!-- agent-activity:{id} -->"
A38_REPORT_BINDING_KEY = "_coordinator_a38_report"
COMMENT_KIND_KEY = "_agent_comment_kind"
A38_MODEL_SECOND_REVIEW_COMMENT = "a38-model-second-review"

# Typed executor facts for APPROVE discover-before-POST fail-closed outcomes.
# Coordinator recovery must match these exactly — never infer dismissal from
# arbitrary transport/log text.
REVIEW_APPROVE_NON_APPROVED = (
    "review.post APPROVE marker matches a non-APPROVED review"
)
REVIEW_APPROVE_COMMIT_MISMATCH = (
    "review.post APPROVE marker commit_id does not match payload"
)

_URL_RE = re.compile(
    r"https://github\.com/[^/\s]+/[^/\s]+/(?:pulls?|issues)/(\d+)"
)
_COMMENT_URL_RE = re.compile(
    r"^https://github\.com/([^/\s]+)/([^/\s]+)/(?:issues|pull)/(\d+)"
    r"#issuecomment-(\d+)$"
)
# Full commit SHA for optional review.post commit_id (exact-head APPROVE binding).
_FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


class _GhError(Exception):
    """Per-row gh failure; never escapes scan_github."""


def _strip(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if not k.startswith("_")}


def a38_report_activity_lock(activity_id: str) -> str:
    """Cross-process serialization key shared by report publication/invalidation."""
    return f"coordinator-a38-report-activity:{activity_id}"


def a38_report_task_lock(task_id: object) -> str:
    """Cross-process task lock acquired before any report activity lock."""
    return f"coordinator-a38-report-task:{task_id}"


def coordinator_a38_report_activity_id(
    task_id: str,
    *,
    head: str,
    base_sha: str | None = None,
    policy_revision: str | None = None,
) -> str:
    """Return the current or legacy deterministic coordinator report id."""
    if base_sha is None and policy_revision is None:
        name = f"coordinator-a38-report:{task_id}:{head}"
    elif base_sha is not None and policy_revision is not None:
        name = (
            f"coordinator-a38-report:{task_id}:{head}:"
            f"{base_sha}:{policy_revision}"
        )
    else:
        raise ValueError("base and policy revision must be supplied together")
    return str(uuid5(NAMESPACE_URL, name))


def _task_a38_report_activity_ids(task: dict[str, Any]) -> set[str]:
    """Report ids referenced by or reproducible from one coordinator task."""
    task_id = str(task.get("id") or "")
    task_payload = task.get("payload")
    coordinator = (
        task_payload.get("coordinator") if isinstance(task_payload, dict) else None
    )
    if not task_id or not isinstance(coordinator, dict):
        return set()
    stored_a38 = coordinator.get("a38")
    a38 = stored_a38 if isinstance(stored_a38, dict) else {}

    ids: set[str] = set()
    heads: set[str] = set()
    head = coordinator.get("head_sha")
    if isinstance(head, str) and _FULL_SHA_RE.fullmatch(head):
        heads.add(head)

    for key in ("report_intent", "report"):
        reference = a38.get(key)
        if not isinstance(reference, dict):
            continue
        activity_id = reference.get("activity_id")
        if isinstance(activity_id, str) and activity_id:
            ids.add(activity_id)
        reference_head = reference.get("head")
        base_sha = reference.get("base_sha")
        policy_revision = reference.get("policy_revision")
        if isinstance(reference_head, str) and _FULL_SHA_RE.fullmatch(reference_head):
            heads.add(reference_head)
            if (
                isinstance(base_sha, str)
                and _FULL_SHA_RE.fullmatch(base_sha)
                and isinstance(policy_revision, str)
                and _FULL_SHA_RE.fullmatch(policy_revision)
            ):
                ids.add(
                    coordinator_a38_report_activity_id(
                        task_id,
                        head=reference_head,
                        base_sha=base_sha,
                        policy_revision=policy_revision,
                    )
                )

    base_sha = a38.get("base_sha")
    policy_revision = a38.get("policy_revision")
    if (
        isinstance(head, str)
        and _FULL_SHA_RE.fullmatch(head)
        and isinstance(base_sha, str)
        and _FULL_SHA_RE.fullmatch(base_sha)
        and isinstance(policy_revision, str)
        and _FULL_SHA_RE.fullmatch(policy_revision)
    ):
        ids.add(
            coordinator_a38_report_activity_id(
                task_id,
                head=head,
                base_sha=base_sha,
                policy_revision=policy_revision,
            )
        )
    pending_base = a38.get("pending_base_sha")
    if (
        isinstance(head, str)
        and _FULL_SHA_RE.fullmatch(head)
        and isinstance(pending_base, str)
        and _FULL_SHA_RE.fullmatch(pending_base)
    ):
        ids.add(
            coordinator_a38_report_activity_id(
                task_id,
                head=head,
                base_sha=pending_base,
                policy_revision=pending_base,
            )
        )
    ids.update(
        coordinator_a38_report_activity_id(task_id, head=candidate_head)
        for candidate_head in heads
    )
    return ids


def is_coordinator_a38_report_activity_for_task(
    row: dict[str, Any], task: dict[str, Any]
) -> bool:
    """Return whether a row is bound to one specific coordinator task."""
    payload = row.get("payload")
    task_id = str(task.get("id") or "")
    if isinstance(payload, dict):
        binding = payload.get(A38_REPORT_BINDING_KEY)
        if isinstance(binding, dict) and binding.get("task_id") == task_id:
            return True
    return str(row.get("id") or "") in _task_a38_report_activity_ids(task)


def _is_a38_report_activity(store: Store, row: dict[str, Any]) -> bool:
    payload = row.get("payload")
    if isinstance(payload, dict) and A38_REPORT_BINDING_KEY in payload:
        return True
    return any(
        is_coordinator_a38_report_activity_for_task(row, task)
        for task in store.rows("task")
    )


def _a38_report_task_id(store: Store, row: dict[str, Any]) -> str | None:
    """Resolve the one coordinator task whose lock protects this report row."""
    payload = row.get("payload")
    binding = payload.get(A38_REPORT_BINDING_KEY) if isinstance(payload, dict) else None
    task_id = binding.get("task_id") if isinstance(binding, dict) else None
    if isinstance(task_id, str) and task_id:
        return task_id
    matches = [
        str(task["id"])
        for task in store.rows("task")
        if (_task_report_intent(task) or ({}, {}))[1].get("activity_id")
        == row.get("id")
    ]
    return matches[0] if len(matches) == 1 else None


def _task_report_intent(
    task: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    task_payload = task.get("payload")
    coordinator = (
        task_payload.get("coordinator") if isinstance(task_payload, dict) else None
    )
    a38 = coordinator.get("a38") if isinstance(coordinator, dict) else None
    intent = a38.get("report_intent") if isinstance(a38, dict) else None
    if not isinstance(coordinator, dict) or not isinstance(intent, dict):
        return None
    return coordinator, intent


def _a38_report_target_error(
    row: dict[str, Any], task: dict[str, Any], coordinator: dict[str, Any]
) -> str | None:
    payload = row.get("payload")
    assert isinstance(payload, dict)
    source = coordinator.get("source")
    expected_repo = (
        source.get("repo")
        if isinstance(source, dict) and isinstance(source.get("repo"), str)
        else task.get("repo")
    )
    expected_number_raw = (
        coordinator.get("pr_number")
        if coordinator.get("pr_number") is not None
        else task.get("ref")
    )
    expected_number = _as_int(expected_number_raw)
    if (
        not isinstance(expected_repo, str)
        or not expected_repo
        or expected_number is None
        or payload.get("repo") != expected_repo
        or _as_int(payload.get("number")) != expected_number
    ):
        return "A38 report activity target does not match its task"
    return None


def _a38_report_checkpoint_error(
    coordinator: dict[str, Any], intent: dict[str, Any], binding: dict[str, Any]
) -> str | None:
    """Require the executor binding to remain current for task head and base."""
    a38 = coordinator.get("a38")
    active_base = (
        a38.get("pending_base_sha") or a38.get("base_sha")
        if isinstance(a38, dict)
        else None
    )
    if (
        coordinator.get("head_sha") != binding.get("head")
        or active_base != binding.get("base_sha")
        or intent.get("head") != binding.get("head")
        or intent.get("base_sha") != binding.get("base_sha")
    ):
        return "A38 report activity is stale for the task head or active base"
    return None


def _retire_a38_report_activity(
    store: Store, row: dict[str, Any], *, error: str
) -> None:
    """Retire a failed row and its matching task intent in one transaction.

    The caller holds the report activity lock, which serializes this update with
    coordinator report publication and checkpoint invalidation.
    """
    payload = row.get("payload")
    binding = payload.get(A38_REPORT_BINDING_KEY) if isinstance(payload, dict) else None
    task_id = binding.get("task_id") if isinstance(binding, dict) else None
    candidates = (
        [store.row("task", task_id)]
        if isinstance(task_id, str) and task_id
        else store.rows("task")
    )
    matched_task: dict[str, Any] | None = None
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        current = _task_report_intent(candidate)
        if current is not None and current[1].get("activity_id") == row.get("id"):
            matched_task = candidate
            break
    updated_row = _strip(row)
    updated_row["execution_status"] = "error"
    updated_row["execution_error"] = error
    with store.conn.transaction():
        store.write("activity", "update", str(row["id"]), updated_row)
        if matched_task is not None:
            updated_task = _strip(matched_task)
            task_payload = updated_task.get("payload")
            coordinator = (
                task_payload.get("coordinator")
                if isinstance(task_payload, dict)
                else None
            )
            a38 = coordinator.get("a38") if isinstance(coordinator, dict) else None
            if isinstance(a38, dict):
                a38.pop("report_intent", None)
            store.write("task", "update", str(updated_task["id"]), updated_task)


def _a38_report_intent_error(store: Store, row: dict[str, Any]) -> str | None:
    """Fail closed when a pending A38 report no longer has its task intent."""
    payload = row.get("payload")
    if not _is_a38_report_activity(store, row):
        return None
    if not isinstance(payload, dict):
        return "A38 report activity payload does not match its current intent"
    binding = payload.get(A38_REPORT_BINDING_KEY)
    if binding is None:
        matches: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
        for task in store.rows("task"):
            current = _task_report_intent(task)
            if current is None:
                continue
            coordinator, intent = current
            if intent.get("activity_id") == str(row.get("id") or ""):
                matches.append((task, coordinator, intent))
        if len(matches) != 1:
            return "A38 report activity has no matching current report intent"
        task, coordinator, intent = matches[0]
        body = payload.get("body")
        digest = intent.get("body_sha256")
        if (
            not isinstance(body, str)
            or not isinstance(digest, str)
            or hashlib.sha256(body.encode("utf-8")).hexdigest() != digest
            or payload.get("target") != "pr"
            or payload.get("exact_body") is not True
            or not isinstance(intent.get("head"), str)
            or _FULL_SHA_RE.fullmatch(intent["head"]) is None
            or not isinstance(intent.get("base_sha"), str)
            or _FULL_SHA_RE.fullmatch(intent["base_sha"]) is None
            or not isinstance(intent.get("policy_revision"), str)
            or _FULL_SHA_RE.fullmatch(intent["policy_revision"]) is None
            or not isinstance(intent.get("private"), bool)
        ):
            return "A38 report activity payload does not match its current intent"
        checkpoint_error = _a38_report_checkpoint_error(
            coordinator, intent, intent
        )
        return checkpoint_error or _a38_report_target_error(row, task, coordinator)
    expected_keys = {
        "task_id",
        "head",
        "base_sha",
        "policy_revision",
        "body_sha256",
        "private",
    }
    if not isinstance(binding, dict) or set(binding) != expected_keys:
        return "A38 report activity binding is invalid"
    task_id = binding.get("task_id")
    body = payload.get("body")
    digest = binding.get("body_sha256")
    if (
        not isinstance(task_id, str)
        or not task_id
        or not isinstance(binding.get("head"), str)
        or _FULL_SHA_RE.fullmatch(binding["head"]) is None
        or not isinstance(binding.get("base_sha"), str)
        or _FULL_SHA_RE.fullmatch(binding["base_sha"]) is None
        or not isinstance(binding.get("policy_revision"), str)
        or _FULL_SHA_RE.fullmatch(binding["policy_revision"]) is None
        or not isinstance(binding.get("private"), bool)
        or not isinstance(body, str)
        or not isinstance(digest, str)
        or hashlib.sha256(body.encode("utf-8")).hexdigest() != digest
        or payload.get("target") != "pr"
        or payload.get("exact_body") is not True
    ):
        return "A38 report activity payload does not match its binding"
    task = store.row("task", task_id)
    current = _task_report_intent(task) if isinstance(task, dict) else None
    coordinator, intent = current if current is not None else ({}, {})
    expected_intent = {
        "head": binding.get("head"),
        "base_sha": binding.get("base_sha"),
        "policy_revision": binding.get("policy_revision"),
        "activity_id": str(row.get("id") or ""),
        "body_sha256": digest,
        "private": binding.get("private"),
    }
    if not isinstance(intent, dict) or any(
        intent.get(key) != value for key, value in expected_intent.items()
    ):
        return "A38 report activity has no matching current report intent"
    checkpoint_error = _a38_report_checkpoint_error(coordinator, intent, binding)
    return checkpoint_error or _a38_report_target_error(row, task, coordinator)


def _safe_execution_error(store: Store, row: dict[str, Any], error: object) -> str:
    """Redact raw executor detail, including selected account paths, then bound it."""
    safe = str(error)
    try:
        account = load_accounts(store.home).for_session(
            str(row.get("session_id") or "")
        )
    except AccountError:
        account = None
    configured_paths: list[tuple[str, str]] = []
    if account is not None:
        configured_paths.append((account.gh_config_dir, "<gh_config_dir>"))
        if account.git_identity is not None:
            configured_paths.append(
                (account.git_identity["signing_key"], "<signing_key>")
            )
    for path, replacement in configured_paths:
        safe = redact_terminal_interleaved_secret(
            safe, path, replacement=replacement
        )
    safe = redact(safe, limit=None)
    for path, replacement in configured_paths:
        safe = redact_terminal_interleaved_secret(
            safe, path, replacement=replacement
        )
    return redact(safe, limit=500)


def _mark(
    store: Store,
    row: dict[str, Any],
    *,
    status: str,
    error: str | None = None,
    result: dict[str, Any] | None = None,
) -> None:
    current = store.row("activity", str(row.get("id") or ""))
    if not isinstance(current, dict) or current.get("execution_status") != "pending":
        return
    updated = _strip(current)
    updated["execution_status"] = status
    if error is None:
        updated.pop("execution_error", None)
    else:
        updated["execution_error"] = _safe_execution_error(store, current, error)
    if result is not None:
        updated["result"] = result
    store.write("activity", "update", updated["id"], updated)


def _repo_ok(repo: Any) -> str | None:
    if not isinstance(repo, str) or repo.count("/") != 1:
        return None
    owner, name = repo.split("/", 1)
    if not owner or not name:
        return None
    return repo


def _as_int(raw: Any) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return None


def _nonempty_str(raw: Any) -> str | None:
    if isinstance(raw, str) and raw != "":
        return raw
    return None


def _optional_str_field(
    payload: dict[str, Any], key: str, *, nonempty: bool = False
) -> str | None:
    """Return str value, None if missing/null; raise _GhError if present but not str."""
    if key not in payload or payload[key] is None:
        return None
    raw = payload[key]
    if not isinstance(raw, str):
        raise _GhError(f"{key} must be a string")
    if nonempty and raw == "":
        raise _GhError(f"{key} must be a non-empty string")
    return raw


def _with_marker(body: str, activity_id: str) -> str:
    marker = ACTIVITY_MARKER.format(id=activity_id)
    if marker in body:
        return body
    if body:
        return f"{body}\n{marker}"
    return marker


def _parse_url_number(stdout: str) -> tuple[str, int]:
    match = _URL_RE.search(stdout)
    if match is None:
        raise _GhError("gh stdout has no github pull/issue URL")
    return match.group(0), int(match.group(1))


def _gh_json_result(argv: list[str], runner: Runner) -> tuple[Any | None, str | None]:
    try:
        completed = runner(argv)
    except OSError as exc:
        return None, f"gh is not available: {exc}"
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "gh failed").strip()
        return None, detail or "gh failed"
    raw = completed.stdout.strip()
    if raw == "":
        return None, "gh returned empty output"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None, "gh returned invalid JSON"
    if not isinstance(data, (dict, list)):
        return None, "gh output is not a JSON object or array"
    return data, None


def _gh_json(argv: list[str], runner: Runner) -> Any:
    data, err = _gh_json_result(argv, runner)
    if err is not None:
        raise _GhError(err)
    return data


def _gh_text(argv: list[str], runner: Runner) -> str:
    try:
        completed = runner(argv)
    except OSError as exc:
        raise _GhError(f"gh is not available: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "gh failed").strip()
        raise _GhError(detail or "gh failed")
    return completed.stdout or ""


def _posted_comment_created_at(
    payload: dict[str, Any],
    *,
    repo: str,
    number: int,
    url: str,
    runner: Runner,
) -> tuple[int | None, str | None]:
    """Read GitHub's timestamp for comment kinds whose post time is a gate fact."""
    if payload.get(COMMENT_KIND_KEY) != A38_MODEL_SECOND_REVIEW_COMMENT:
        return None, None
    match = _COMMENT_URL_RE.fullmatch(url)
    if match is None:
        return None, None
    owner, name = repo.split("/", 1)
    url_owner, url_name, url_number, comment_id = match.groups()
    if (
        url_owner.casefold() != owner.casefold()
        or url_name.casefold() != name.casefold()
        or int(url_number) != number
    ):
        return None, None
    try:
        comment = _gh_json(
            [
                "gh",
                "api",
                f"repos/{owner}/{name}/issues/comments/{comment_id}",
            ],
            runner,
        )
    except _GhError:
        return int(comment_id), None
    if not isinstance(comment, dict):
        return int(comment_id), None
    created_at = comment.get("created_at")
    if (
        not isinstance(created_at, str)
        or parse_utc_timestamp(created_at) is None
    ):
        return int(comment_id), None
    return int(comment_id), created_at


def _gh_not_found(completed: Completed) -> bool:
    """True only when gh failed because this pull request is missing."""
    if completed.returncode == 0:
        return False
    text = f"{completed.stderr or ''}{completed.stdout or ''}".casefold()
    if "no pull request" in text:
        return True
    if "pull request" in text and ("not found" in text or "could not find" in text):
        return True
    return False


def _is_draft(raw: Any) -> bool:
    if raw is True:
        return True
    if isinstance(raw, str) and raw == "true":
        return True
    return False


def _flatten_comment_pages(data: Any) -> list[Any]:
    if not isinstance(data, list):
        raise _GhError("comments api is not an array")
    if data and all(isinstance(el, list) for el in data):
        flat: list[Any] = []
        for page in data:
            if not all(isinstance(item, dict) for item in page):
                raise _GhError("comments api has unexpected shape")
            flat.extend(page)
        return flat
    if all(isinstance(el, dict) for el in data):
        return data
    raise _GhError("comments api has unexpected shape")


def _run_pr_open(store: Store, runner: Runner, row: dict[str, Any], *, expected_login: str | None = None) -> str:
    rid = str(row["id"])
    payload = row.get("payload")
    if not isinstance(payload, dict):
        _mark(store, row, status="error", error="payload must be an object")
        return f"pr.open {rid} error"
    repo = _repo_ok(payload.get("repo"))
    title = _nonempty_str(payload.get("title"))
    head = _nonempty_str(payload.get("head"))
    if repo is None or title is None or head is None:
        _mark(store, row, status="error", error="pr.open requires repo, title, head")
        return f"pr.open {rid} error"
    try:
        body_opt = _optional_str_field(payload, "body")
        body = "" if body_opt is None else body_opt
        base = _optional_str_field(payload, "base", nonempty=True)
    except _GhError as exc:
        _mark(store, row, status="error", error=str(exc))
        return f"pr.open {rid} error"
    try:
        view_argv = [
            "gh",
            "pr",
            "view",
            head,
            "--repo",
            repo,
            "--json",
            "number,url,state,isDraft" + (",author" if expected_login else ""),
        ]
        try:
            completed = runner(view_argv)
        except OSError as exc:
            raise _GhError(f"gh is not available: {exc}") from exc
        viewed: dict[str, Any] | None
        if completed.returncode != 0:
            # Not-found → create; any other view failure must not create.
            if _gh_not_found(completed):
                viewed = None
            else:
                detail = (completed.stderr or completed.stdout or "gh failed").strip()
                raise _GhError(detail or "gh failed")
        else:
            raw = completed.stdout.strip()
            if raw == "":
                raise _GhError("gh returned empty output")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise _GhError("gh returned invalid JSON") from exc
            if not isinstance(data, dict):
                raise _GhError("gh output is not a JSON object or array")
            viewed = data
        if isinstance(viewed, dict):
            if expected_login:
                author = viewed.get("author")
                login = author.get("login") if isinstance(author, dict) else None
                if not isinstance(login, str) or login.casefold() != expected_login.casefold():
                    raise _GhError("Existing pull request author differs from the configured account")
            state = str(viewed.get("state") or "").upper()
            number = _as_int(viewed.get("number"))
            url = viewed.get("url")
            if number is None or not isinstance(url, str) or url == "":
                raise _GhError("pr view missing number/url")
            if state != "OPEN":
                raise _GhError("existing pull request is not open")
            if not _is_draft(viewed.get("isDraft")):
                raise _GhError("existing pull request is not a draft")
            result = {"repo": repo, "number": number, "url": url, "draft": True}
            _mark(store, row, status="done", result=result)
            return f"pr.open {rid} done number={number}"
        create_body = _with_marker(body, rid)
        argv = [
            "gh",
            "pr",
            "create",
            "--draft",
            "--repo",
            repo,
            "--title",
            title,
            "--body",
            create_body,
            "--head",
            head,
        ]
        if base is not None:
            argv.extend(["--base", base])
        stdout = _gh_text(argv, runner)
        url, number = _parse_url_number(stdout)
        result = {"repo": repo, "number": number, "url": url, "draft": True}
        _mark(store, row, status="done", result=result)
        return f"pr.open {rid} done number={number}"
    except _GhError as exc:
        _mark(store, row, status="error", error=str(exc))
        return f"pr.open {rid} error"


def _run_pr_body(store: Store, runner: Runner, row: dict[str, Any]) -> str:
    """Replace a draft pull request body. The model does not call gh."""
    rid = str(row["id"])
    payload = row.get("payload")
    if not isinstance(payload, dict):
        _mark(store, row, status="error", error="payload must be an object")
        return f"pr.body {rid} error"
    repo = _repo_ok(payload.get("repo"))
    number = _as_int(payload.get("number"))
    try:
        body = _optional_str_field(payload, "body", nonempty=True)
    except _GhError as exc:
        _mark(store, row, status="error", error=str(exc))
        return f"pr.body {rid} error"
    if repo is None or number is None or body is None:
        _mark(store, row, status="error", error="pr.body requires repo, number, body")
        return f"pr.body {rid} error"
    try:
        _gh_text(
            ["gh", "pr", "edit", str(number), "--repo", repo, "--body", body],
            runner,
        )
        _mark(store, row, status="done", result={"repo": repo, "number": number})
        return f"pr.body {rid} done number={number}"
    except _GhError as exc:
        _mark(store, row, status="error", error=str(exc))
        return f"pr.body {rid} error"


def _run_issue_write(store: Store, runner: Runner, row: dict[str, Any]) -> str:
    rid = str(row["id"])
    payload = row.get("payload")
    if not isinstance(payload, dict):
        _mark(store, row, status="error", error="payload must be an object")
        return f"issue.write {rid} error"
    repo = _repo_ok(payload.get("repo"))
    title = _nonempty_str(payload.get("title"))
    if repo is None or title is None:
        _mark(store, row, status="error", error="issue.write requires repo, title")
        return f"issue.write {rid} error"
    try:
        body_opt = _optional_str_field(payload, "body")
        body = "" if body_opt is None else body_opt
    except _GhError as exc:
        _mark(store, row, status="error", error=str(exc))
        return f"issue.write {rid} error"
    marker = ACTIVITY_MARKER.format(id=rid)
    try:
        listed = _gh_json(
            [
                "gh",
                "issue",
                "list",
                "--repo",
                repo,
                "--state",
                "open",
                "--limit",
                "100",
                "--json",
                "number,title,url,body",
            ],
            runner,
        )
        if not isinstance(listed, list):
            raise _GhError("issue list is not an array")
        for issue in listed:
            if not isinstance(issue, dict):
                continue
            issue_body = issue.get("body")
            if not isinstance(issue_body, str) or marker not in issue_body:
                continue
            number = _as_int(issue.get("number"))
            url = issue.get("url")
            if number is None or not isinstance(url, str) or url == "":
                raise _GhError("issue list entry missing number/url")
            result = {"repo": repo, "number": number, "url": url}
            _mark(store, row, status="done", result=result)
            return f"issue.write {rid} done number={number}"
        if len(listed) == 100:
            raise _GhError("issue list truncated")
        create_body = _with_marker(body, rid)
        stdout = _gh_text(
            [
                "gh",
                "issue",
                "create",
                "--repo",
                repo,
                "--title",
                title,
                "--body",
                create_body,
            ],
            runner,
        )
        url, number = _parse_url_number(stdout)
        result = {"repo": repo, "number": number, "url": url}
        _mark(store, row, status="done", result=result)
        return f"issue.write {rid} done number={number}"
    except _GhError as exc:
        _mark(store, row, status="error", error=str(exc))
        return f"issue.write {rid} error"


def _login(runner: Runner) -> str | None:
    """The account gh is authenticated as, or None when it cannot be determined."""
    data, _err = _gh_json_result(["gh", "api", "user"], runner)
    if isinstance(data, dict):
        login = data.get("login")
        if isinstance(login, str) and login != "":
            return login
    return None


def _optional_full_sha(payload: dict[str, Any], key: str) -> str | None:
    """Return a validated 40-hex commit SHA, or None when the field is absent/null."""
    if key not in payload or payload[key] is None:
        return None
    raw = payload[key]
    if not isinstance(raw, str) or not _FULL_SHA_RE.fullmatch(raw):
        raise _GhError(f"{key} must be a full 40-hex commit SHA")
    return raw


def _run_review_post(store: Store, runner: Runner, row: dict[str, Any]) -> str:
    """Submit a pull-request review of type COMMENT carrying the findings.

    A rejected gate's findings are a review, not a remark: posting them as a review
    puts them where an author looks and makes the work visible to anything counting
    review artefacts. COMMENT rather than REQUEST_CHANGES, so a bot cannot hold a
    merge closed through branch protection.
    """
    rid = str(row["id"])
    payload = row.get("payload")
    if not isinstance(payload, dict):
        _mark(store, row, status="error", error="payload must be an object")
        return f"review.post {rid} error"
    repo = _repo_ok(payload.get("repo"))
    number = _as_int(payload.get("number"))
    body = _nonempty_str(payload.get("body"))
    if repo is None or number is None or number <= 0 or body is None:
        _mark(store, row, status="error", error="review.post requires repo, number, body")
        return f"review.post {rid} error"
    # COMMENT reports; APPROVE is a merge authorisation and is only inserted once the
    # gates on this head are approved. REQUEST_CHANGES is refused here rather than left
    # to convention: it would let this account hold a merge closed through branch
    # protection, which is a different tool from the one this is.
    event = payload.get("event", "COMMENT")
    if event not in ("COMMENT", "APPROVE"):
        _mark(store, row, status="error", error="review.post event must be COMMENT or APPROVE")
        return f"review.post {rid} error"
    try:
        commit_id = _optional_full_sha(payload, "commit_id")
    except _GhError as exc:
        _mark(store, row, status="error", error=str(exc))
        return f"review.post {rid} error"
    marker = ACTIVITY_MARKER.format(id=rid)
    owner, name = repo.split("/", 1)
    try:
        reviews_raw = _gh_json(
            ["gh", "api", "--paginate", "--slurp", f"repos/{owner}/{name}/pulls/{number}/reviews"],
            runner,
        )
        me: str | None = None
        asked_who_we_are = False
        for review in _flatten_comment_pages(reviews_raw):
            if not isinstance(review, dict):
                continue
            rbody = review.get("body")
            if not isinstance(rbody, str) or marker not in rbody:
                continue
            # Only now: on the ordinary path no review carries this marker, and asking
            # gh who we are would be a round trip to answer a question nobody asked.
            if not asked_who_we_are:
                me = _login(runner)
                asked_who_we_are = True
            if me is None:
                # A candidate exists but the identity is unknown. Retrying later is
                # right; posting a second review because a lookup flaked is not.
                raise _GhError("cannot determine the authenticated account")
            # The marker is visible to anyone reading the pull request. Without the
            # author check a copied marker would suppress this review entirely.
            author = review.get("user")
            login = author.get("login") if isinstance(author, dict) else None
            # Case-insensitive, as DESIGN.md requires of a GitHub login and as
            # watch.py compares one: the same account can be spelled either way.
            if not isinstance(login, str) or login.lower() != me.lower():
                continue
            if event == "APPROVE":
                # Dismissed / revoked same-marker approvals must not satisfy the
                # activity and must not be treated as delivery. Fail closed so a
                # fresh authorized activity (new id/marker) can POST instead.
                state = str(review.get("state") or "").upper()
                if state != "APPROVED":
                    raise _GhError(REVIEW_APPROVE_NON_APPROVED)
                rev_commit = str(review.get("commit_id") or "")
                if commit_id is not None and rev_commit.lower() != commit_id.lower():
                    raise _GhError(REVIEW_APPROVE_COMMIT_MISMATCH)
            url = review.get("html_url") or review.get("url")
            if not isinstance(url, str) or url == "":
                raise _GhError("review missing url")
            result: dict[str, Any] = {"repo": repo, "number": number, "url": url}
            rev_id = _as_int(review.get("id"))
            if rev_id is not None and rev_id > 0:
                result["id"] = rev_id
            _mark(store, row, status="done", result=result)
            return f"review.post {rid} done"
        post_argv = [
            "gh",
            "api",
            "-X",
            "POST",
            f"repos/{owner}/{name}/pulls/{number}/reviews",
            "-f",
            f"body={_with_marker(body, rid)}",
            "-f",
            f"event={event}",
        ]
        if commit_id is not None:
            post_argv.extend(["-f", f"commit_id={commit_id}"])
        created = _gh_json(post_argv, runner)
        if not isinstance(created, dict):
            raise _GhError("review response is not an object")
        url = created.get("html_url") or created.get("url")
        if not isinstance(url, str) or url == "":
            raise _GhError("review missing url")
        result = {"repo": repo, "number": number, "url": url}
        new_id = _as_int(created.get("id"))
        if new_id is not None and new_id > 0:
            result["id"] = new_id
        _mark(store, row, status="done", result=result)
        return f"review.post {rid} done"
    except _GhError as exc:
        _mark(store, row, status="error", error=str(exc))
        return f"review.post {rid} error"


def _run_comment_post(
    store: Store,
    runner: Runner,
    row: dict[str, Any],
    *,
    expected_login: str,
    before_post: Callable[[], str | None] | None = None,
) -> str:
    rid = str(row["id"])
    payload = row.get("payload")
    if not isinstance(payload, dict):
        _mark(store, row, status="error", error="payload must be an object")
        return f"comment.post {rid} error"
    repo = _repo_ok(payload.get("repo"))
    number = _as_int(payload.get("number"))
    body = _nonempty_str(payload.get("body"))
    if repo is None or number is None or number <= 0 or body is None:
        _mark(
            store,
            row,
            status="error",
            error="comment.post requires repo, number, body",
        )
        return f"comment.post {rid} error"
    target_raw = payload.get("target")
    exact_body = payload.get("exact_body", False)
    if not isinstance(exact_body, bool):
        _mark(store, row, status="error", error="comment.post exact_body must be boolean")
        return f"comment.post {rid} error"
    target = "issue"
    if target_raw is not None:
        if target_raw not in ("pr", "issue"):
            _mark(store, row, status="error", error="comment.post target must be pr|issue")
            return f"comment.post {rid} error"
        target = str(target_raw)
    marker = ACTIVITY_MARKER.format(id=rid)
    owner, name = repo.split("/", 1)
    try:
        comments_raw = _gh_json(
            [
                "gh",
                "api",
                "--paginate",
                "--slurp",
                f"repos/{owner}/{name}/issues/{number}/comments",
            ],
            runner,
        )
        comments = _flatten_comment_pages(comments_raw)
        for comment in comments:
            if not isinstance(comment, dict):
                continue
            cbody = comment.get("body")
            if not isinstance(cbody, str):
                continue
            matches = cbody == body if exact_body else marker in cbody
            if not matches:
                continue
            # Only the posting account's own comment proves the activity; a copied marker does not.
            user = comment.get("user")
            login = user.get("login") if isinstance(user, dict) else None
            if not isinstance(login, str) or login.casefold() != expected_login.casefold():
                continue
            cid = comment.get("id")
            url = comment.get("html_url") or comment.get("url")
            if not isinstance(url, str) or url == "":
                raise _GhError("comment missing url")
            result: dict[str, Any] = {"repo": repo, "number": number, "url": url}
            if cid is not None:
                result["id"] = cid
            created_at = comment.get("created_at")
            if (
                isinstance(created_at, str)
                and parse_utc_timestamp(created_at) is not None
            ):
                result["created_at"] = created_at
            _mark(store, row, status="done", result=result)
            return f"comment.post {rid} done"
        post_body = body if exact_body else _with_marker(body, rid)
        if target == "pr":
            argv = [
                "gh",
                "pr",
                "comment",
                str(number),
                "--repo",
                repo,
                "--body",
                post_body,
            ]
        else:
            argv = [
                "gh",
                "issue",
                "comment",
                str(number),
                "--repo",
                repo,
                "--body",
                post_body,
            ]
        if before_post is not None:
            report_error = before_post()
            if report_error is not None:
                _mark(store, row, status="error", error=report_error)
                return f"comment.post {rid} error"
        stdout = _gh_text(argv, runner)
        stripped = stdout.strip()
        if stripped.startswith("{"):
            try:
                data = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise _GhError("gh returned invalid JSON") from exc
            if not isinstance(data, dict):
                raise _GhError("gh comment JSON is not an object")
            url = data.get("html_url") or data.get("url")
            if not isinstance(url, str) or url == "":
                raise _GhError("comment response missing url")
            result = {"repo": repo, "number": number, "url": url}
            if data.get("id") is not None:
                result["id"] = data["id"]
            created_at = data.get("created_at")
            if (
                isinstance(created_at, str)
                and parse_utc_timestamp(created_at) is not None
            ):
                result["created_at"] = created_at
            elif isinstance(url, str):
                comment_id, looked_up_created_at = _posted_comment_created_at(
                    payload,
                    repo=repo,
                    number=number,
                    url=url,
                    runner=runner,
                )
                if comment_id is not None and "id" not in result:
                    result["id"] = comment_id
                if looked_up_created_at is not None:
                    result["created_at"] = looked_up_created_at
            _mark(store, row, status="done", result=result)
            return f"comment.post {rid} done"
        if stripped.startswith("https://github.com/"):
            result = {"repo": repo, "number": number, "url": stripped}
            comment_id, created_at = _posted_comment_created_at(
                payload,
                repo=repo,
                number=number,
                url=stripped,
                runner=runner,
            )
            if comment_id is not None:
                result["id"] = comment_id
            if created_at is not None:
                result["created_at"] = created_at
            _mark(store, row, status="done", result=result)
            return f"comment.post {rid} done"
        raise _GhError("gh stdout has no github pull/issue URL")
    except _GhError as exc:
        _mark(store, row, status="error", error=str(exc))
        return f"comment.post {rid} error"


def scan_github(store: Store, runner: Runner) -> list[str]:
    """Execute owned pending GitHub activities for this device.

    Handles pr.open, pr.body, comment.post, review.post, and issue.write.

    Other pending types (subscription.set, query.request, …) are skipped.
    Returns human-readable status lines, one per handled row.
    """
    lines: list[str] = []

    def execute(row: dict[str, Any], *, a38_report: bool = False) -> str:
        typ = row.get("type")
        account = load_accounts(store.home).for_session(
            str(row.get("session_id") or "")
        )
        binding = {"account": account.name, "login": account.login.casefold()}
        if (
            row.get("execution_account") is not None
            and row["execution_account"] != binding
        ):
            raise AccountError("Activity account binding changed; refusing account switch")
        row = dict(row, execution_account=binding)
        store.write("activity", "update", row["id"], _strip(row))
        scoped_runner = account.runner(runner)
        if typ == "pr.open":
            return _run_pr_open(
                store, scoped_runner, row, expected_login=account.login
            )
        if typ == "pr.body":
            return _run_pr_body(store, scoped_runner, row)
        if typ == "issue.write":
            return _run_issue_write(store, scoped_runner, row)
        if typ == "comment.post":

            def revalidate_report() -> str | None:
                current = store.row("activity", str(row["id"]))
                if (
                    not isinstance(current, dict)
                    or current.get("execution_status") != "pending"
                ):
                    return "A38 report activity is no longer pending"
                report_error = _a38_report_intent_error(store, current)
                if report_error is not None:
                    _retire_a38_report_activity(
                        store, current, error=report_error
                    )
                return report_error

            return _run_comment_post(
                store,
                scoped_runner,
                row,
                expected_login=account.login,
                before_post=revalidate_report if a38_report else None,
            )
        if typ == "review.post":
            return _run_review_post(store, scoped_runner, row)
        raise _GhError("unsupported activity type")

    for row in store.pending_work():
        typ = row.get("type")
        if typ not in {
            "pr.open",
            "pr.body",
            "issue.write",
            "comment.post",
            "review.post",
        }:
            continue
        try:
            if typ == "comment.post" and _is_a38_report_activity(store, row):
                task_id = _a38_report_task_id(store, row)
                with ExitStack() as locks:
                    if task_id is not None:
                        locks.enter_context(
                            store.exclusive(a38_report_task_lock(task_id))
                        )
                    locks.enter_context(
                        store.exclusive(
                            a38_report_activity_lock(str(row.get("id") or ""))
                        )
                    )
                    current = store.row("activity", str(row.get("id") or ""))
                    if (
                        not isinstance(current, dict)
                        or current.get("execution_status") != "pending"
                    ):
                        continue
                    report_error = _a38_report_intent_error(store, current)
                    if report_error is not None:
                        _retire_a38_report_activity(
                            store, current, error=report_error
                        )
                        lines.append(f"comment.post {current.get('id')} error")
                        continue
                    lines.append(execute(current, a38_report=True))
                    continue
            lines.append(execute(row))
        except (Exception, AccountError) as exc:  # noqa: BLE001 — per-row isolation
            rid = str(row.get("id") or "?")
            _mark(store, row, status="error", error=str(exc))
            label = typ if isinstance(typ, str) else "activity"
            lines.append(f"{label} {rid} error")
    return lines
