"""Concrete runtime for the script-owned issue coordinator.

Checkpoints live in task.payload['coordinator'] and activity result fields.
There is no second hub state machine and no new store table. GitHub HTTP,
Git, tests, readiness, and lane starts are script work; model text is never a
transition. This module does not claim universal sandbox enforcement.
"""

from __future__ import annotations

import uuid
import hashlib
import json
from dataclasses import asdict
import re
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from .ai_accounts import AccountError as AIAccountError
from .ai_accounts import load_ai_accounts
from .allow import CHECKLIST_KEYS
from .coordinator_common import (
    REQUIRED_LANE_SLOTS,
    REQUIRED_REVIEW_SKILLS,
    REQUIRED_WORKER_SKILLS,
    CoordinatorError,
    LaneRunner,
    Runner,
    account_for,
    as_int,
    coord,
    coordinator_env,
    gh_list,
    harden_grok_write_argv,
    owned_session,
    parse_model_result,
    redact,
    review_is_approved,
    save_task,
    scoped,
    source_key,
    strip_row,
)
from .coordinator_config import WorkerConfig
from .coordinator_exec import run_bounded
from .coordinator_git import (
    ensure_draft,
    phase_checkout,
    phase_publish_draft,
    repo_cfg,
    stage_sign_commit_if_changes,
    verify_signed_clean_head,
)
from .coordinator_github import (
    phase_accept,
    phase_await_merge,
    phase_blocked,
    phase_ci,
    phase_formal_approve,
    phase_leave_draft,
    phase_read_replies,
    phase_readiness,
    post_issue_comment,
    publish_blocker,
    verify_issue_assigned,
)
from .coordinator_lanes import (
    _IMPLEMENTER_RESULTS,
    _REVIEWER_RESULTS,
    invalidate_head_evidence,
    launch_lane,
    phase_pr_gates_codex,
    phase_pr_gates_grok,
    set_checklist,
    write_review_diff,
)
from .github_accounts import AccountError, load_accounts
from .skills import has_skill
from .store import Store, StoreError, utcnow

# Re-exports for coordinator.py and tests.
__all__ = [
    "REQUIRED_LANE_SLOTS",
    "CoordinatorError",
    "advance_one",
    "discover_assignments",
    "harden_grok_write_argv",
    "parse_model_result",
    "preflight_worker",
    "redact",
    "review_is_approved",
    "verify_issue_assigned",
]


def preflight_worker(store: Store, worker: WorkerConfig, runner: Runner) -> None:
    """Refuse work when sessions, skills, AI slots, or GitHub accounts are missing."""
    session = owned_session(store, worker.session_id)
    for skill in REQUIRED_WORKER_SKILLS:
        if not has_skill(session, skill):
            raise CoordinatorError(f"worker session missing skill {skill}")
    review = owned_session(store, worker.review_session)
    for skill in REQUIRED_REVIEW_SKILLS:
        if not has_skill(review, skill):
            raise CoordinatorError(f"review_session missing skill {skill}")
    try:
        accounts = load_accounts(store.home)
        worker_account = accounts.for_session(worker.session_id)
        review_account = accounts.for_session(worker.review_session)
    except AccountError as exc:
        raise CoordinatorError(str(exc)) from exc
    if worker_account.login.casefold() == review_account.login.casefold():
        raise CoordinatorError("formal review_session must use a different GitHub login")
    if worker_account.git_identity is None:
        raise CoordinatorError("worker GitHub account requires git identity for signed commits")
    try:
        worker_account.runner(runner, require_git=True)
        review_account.runner(runner)
    except AccountError as exc:
        raise CoordinatorError(str(exc)) from exc
    try:
        ai = load_ai_accounts(store.home)
        for slot in REQUIRED_LANE_SLOTS:
            vendor, role = slot.split(":", 1)
            selected = ai.for_lane(worker.session_id, role, vendor)
            if slot == "grok:implementer" and selected.access != "workspace-write":
                raise CoordinatorError("implementer lane requires workspace-write access")
            if role != "implementer" and selected.access != "read-only":
                raise CoordinatorError(f"{slot} requires read-only access")
    except AIAccountError as exc:
        raise CoordinatorError(str(exc)) from exc
    if not worker.repositories:
        raise CoordinatorError("worker has no repositories configured")
    if not worker.workspace_root.is_absolute():
        raise CoordinatorError("workspace_root must be absolute")


def execution_binding(store: Store, worker: WorkerConfig, repo: str) -> str:
    """Pin selected execution metadata, never credential contents or unrelated profiles."""
    cfg = repo_cfg(worker, repo)
    github = load_accounts(store.home)
    ai = load_ai_accounts(store.home)
    selected = {
        "repository": asdict(cfg), "workspace": str(worker.workspace_root),
        "worker_session": worker.session_id, "review_session": worker.review_session,
        "reply_logins": worker.reply_logins,
        "github": [asdict(github.for_session(sid)) for sid in (worker.session_id, worker.review_session)],
        "lanes": [asdict(ai.for_lane(worker.session_id, slot.split(":", 1)[1], slot.split(":", 1)[0]))
                  for slot in REQUIRED_LANE_SLOTS],
    }
    return hashlib.sha256(json.dumps(selected, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _terminal(task: dict[str, Any]) -> bool:
    return task.get("state") in ("done", "failed")


def _find_task_for_issue_device_wide(
    store: Store,
    repo: str,
    number: int,
) -> dict[str, Any] | None:
    """Device-wide source identity — first session ownership wins across workers."""
    key = source_key(repo, number)
    origin = store.device_id()
    matches: list[dict[str, Any]] = []
    for row in store.rows("task"):
        if row.get("_origin_device_id") != origin:
            continue
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        inner = payload.get("coordinator")
        if not isinstance(inner, dict):
            continue
        source = inner.get("source")
        if not isinstance(source, dict):
            continue
        try:
            src_num = int(source.get("number"))
        except (TypeError, ValueError):
            continue
        if source_key(str(source.get("repo") or ""), src_num) == key:
            matches.append(row)
    if not matches:
        return None
    matches.sort(key=lambda r: str(r.get("created_at") or ""))
    return matches[0]


def _ensure_checklist_rows(store: Store, task: dict[str, Any], session: dict[str, Any]) -> None:
    """Reconcile checklist after partial crash — never leave keys missing forever."""
    existing = {
        str(item.get("key"))
        for item in store.rows("checklist_item")
        if item.get("task_id") == task["id"]
    }
    source = "runner" if session.get("kind") == "runner" else "human"
    for key in CHECKLIST_KEYS["implement"]:
        if key in existing:
            continue
        cid = str(uuid.uuid4())
        store.write(
            "checklist_item",
            "insert",
            cid,
            {
                "id": cid,
                "task_id": task["id"],
                "key": key,
                "status": "pending",
                "evidence": None,
                "source": source,
                "deviation_declared": False,
                "deviation_granted": False,
                "granted_by": None,
                "updated_at": utcnow(),
            },
        )


def _create_implement_task(
    store: Store,
    worker: WorkerConfig,
    *,
    repo: str,
    number: int,
    title: str,
    assigned_id: str,
    publication_repo: str,
    base: str,
    body: str,
) -> dict[str, Any] | None:
    """Insert task under device-wide source admission lock. None if another owner won."""
    session = owned_session(store, worker.session_id)
    tid = str(uuid.uuid4())
    c = {
        "phase": "accept",
        "source": {
            "repo": repo,
            "number": number,
            "assigned_id": assigned_id,
            "publication_repo": publication_repo,
            "base": base,
            "title": title,
            "body": redact(body, limit=2000),
        },
        "branch": f"task-{tid[:8]}",
        "execution_binding": execution_binding(store, worker, repo),
        "worker_session": worker.session_id,
        "worker_login": account_for(store, worker.session_id).login.casefold(),
    }
    held: dict[str, Any] = {"existing": None}

    def _skip() -> bool:
        existing = _find_task_for_issue_device_wide(store, repo, number)
        if existing is None:
            return False
        held["existing"] = existing
        return True

    store.write_with_advisory(
        "task",
        "insert",
        tid,
        {
            "id": tid,
            "session_id": worker.session_id,
            "workflow": "implement",
            "title": f"{worker.session_id[:8]} - {title[:120]}",
            "repo": repo,  # target repository
            "ref": None,
            "payload": {"coordinator": c, "source_issue": {"repo": repo, "number": number}},
            "state": "open",
            "current_round": 0,
            "created_at": utcnow(),
            "updated_at": utcnow(),
            "change_summary_en": None,
            "change_summary_de": None,
        },
        lock_key=f"coordinator-source:{repo.casefold()}:{number}",
        skip=_skip,
    )
    if held["existing"] is not None:
        return None
    task = store.row("task", tid)
    if task is None:
        # Lost the race to another insert with same logical source.
        return _find_task_for_issue_device_wide(store, repo, number)
    _ensure_checklist_rows(store, task, session)
    return task


def _insert_assigned_activity(
    store: Store,
    worker: WorkerConfig,
    *,
    repo: str,
    number: int,
    title: str,
    body: str,
    url: str,
    assignee: str,
    assigned_at: str,
) -> str:
    activity_id = str(
        uuid5(
            NAMESPACE_URL,
            f"coordinator-assigned:{repo.casefold()}:{number}:{assigned_at}:{assignee}",
        )
    )
    existing = store.row("activity", activity_id)
    if existing is not None:
        return activity_id

    def _skip() -> bool:
        return store.row("activity", activity_id) is not None

    store.write_with_advisory(
        "activity",
        "insert",
        activity_id,
        {
            "id": activity_id,
            "session_id": worker.session_id,
            "type": "issue.assigned",
            "payload": {
                "repo": repo,
                "number": number,
                "url": url,
                "title": title,
                "body": redact(body, limit=2000),
                "assigned_at": assigned_at,
                "assignment_observed_at": utcnow(),
                "assignee": assignee,
                "mandate": "github-assignment",
                "source": "coordinator",
            },
            "execution_status": "done",
        },
        lock_key=f"coordinator-source:{repo.casefold()}:{number}",
        skip=_skip,
    )
    return activity_id


def discover_assignments(store: Store, worker: WorkerConfig, runner: Runner) -> list[str]:
    """Paginated discovery of currently assigned open issues. No silent first-run ignore."""
    lines: list[str] = []
    scoped_runner = scoped(store, worker.session_id, runner)
    account = account_for(store, worker.session_id)
    login = account.login
    for repo, repo_cfg_item in worker.repositories.items():
        try:
            issues = gh_list(
                scoped_runner,
                [
                    "gh",
                    "api",
                    "--paginate",
                    "--slurp",
                    f"repos/{repo}/issues?assignee={login}&state=open&per_page=100",
                ],
            )
        except CoordinatorError as exc:
            lines.append(f"discover {repo}: {redact(str(exc))}")
            continue
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            if issue.get("pull_request") is not None:
                continue
            number = as_int(issue.get("number"))
            if number is None or number <= 0:
                continue
            # Device-wide admission: first session ownership wins.
            existing = _find_task_for_issue_device_wide(store, repo, number)
            if existing is not None:
                if existing.get("session_id") != worker.session_id:
                    lines.append(
                        f"source owned by session {existing.get('session_id')} for {repo}#{number}"
                    )
                    continue
                if not _terminal(existing):
                    continue
                if existing.get("state") == "done":
                    lines.append(f"already completed {repo}#{number}")
                    continue
                if existing.get("state") == "failed":
                    lines.append(
                        f"failed task remains for {repo}#{number}; awaiting authorized recovery"
                    )
                    continue
            try:
                verified = verify_issue_assigned(scoped_runner, repo, number, login)
            except CoordinatorError as exc:
                lines.append(f"skip {repo}#{number}: {redact(str(exc))}")
                continue
            title = verified.get("title") if isinstance(verified.get("title"), str) else ""
            body = verified.get("body") if isinstance(verified.get("body"), str) else ""
            url = verified.get("html_url") if isinstance(verified.get("html_url"), str) else ""
            # updated_at is not assigned_at — record observation honestly.
            observed = utcnow()
            assigned_id = _insert_assigned_activity(
                store,
                worker,
                repo=repo,
                number=number,
                title=title or "",
                body=body or "",
                url=url or "",
                assignee=login.casefold(),
                assigned_at=observed,
            )
            task = _create_implement_task(
                store,
                worker,
                repo=repo,
                number=number,
                title=title or f"Issue {number}",
                assigned_id=assigned_id,
                publication_repo=repo_cfg_item.publication_repo,
                base=repo_cfg_item.base,
                body=body or "",
            )
            if task is None:
                other = _find_task_for_issue_device_wide(store, repo, number)
                if other is not None and other.get("session_id") != worker.session_id:
                    lines.append(
                        f"source owned by session {other.get('session_id')} for {repo}#{number}"
                    )
                continue
            if task.get("session_id") != worker.session_id:
                lines.append(
                    f"source owned by session {task.get('session_id')} for {repo}#{number}"
                )
                continue
            lines.append(f"accepted source {repo}#{number} task={task['id']}")
    return lines


def _ensure_round(store: Store, task: dict[str, Any]) -> int:
    current = int(task.get("current_round") or 0)
    if current > 0:
        for row in store.rows("task_round"):
            if row.get("task_id") != task["id"] or int(row.get("round") or 0) != current:
                continue
            if row.get("implementer_verdict") is None:
                task["state"] = "implementing"
                save_task(store, task)
                return current
            break
    n = current + 1
    task["current_round"] = n
    task["state"] = "implementing"
    rid = str(uuid.uuid4())
    store.write(
        "task_round",
        "insert",
        rid,
        {
            "id": rid,
            "task_id": task["id"],
            "round": n,
            "implementer_verdict": None,
            "reviewer_verdict": None,
            "started_at": utcnow(),
            "finished_at": None,
        },
    )
    save_task(store, task)
    return n


def _find_round(store: Store, task_id: str, round_num: int) -> dict[str, Any]:
    for row in store.rows("task_round"):
        if row.get("task_id") == task_id and int(row.get("round") or 0) == round_num:
            return row
    raise CoordinatorError(f"round {round_num} missing")


def _spec_context(c: dict[str, Any], extra: str = "") -> str:
    source = c.get("source") if isinstance(c.get("source"), dict) else {}
    title = source.get("title") or ""
    body = source.get("body") or ""
    replies = c.get("authorized_replies") or []
    reply_text = ""
    if isinstance(replies, list) and replies:
        reply_text = "\nAuthorized human replies (untrusted spec):\n" + "\n".join(
            redact(str(r)) for r in replies[-5:]
        )
    findings = c.get("findings") or ""
    return (
        f"Source issue: {source.get('repo')}#{source.get('number')}\n"
        f"Title: {redact(str(title))}\n"
        f"Body (untrusted):\n{redact(str(body), limit=1500)}\n"
        f"{reply_text}\n"
        f"{('Findings to address:\\n' + redact(str(findings))) if findings else ''}\n"
        f"{extra}\n"
    )


def _apply_implementer_outcome(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
    *,
    round_num: int,
    tr: dict[str, Any],
    status: str,
    model_result: str,
    stdout: str,
    sha: str | None,
    draft_lines: list[str],
) -> list[str]:
    """Apply a persisted implementer outcome. Never relaunches the model."""
    c = coord(task)

    def _mark_applied() -> None:
        outcome = c.get("lane_outcome")
        if isinstance(outcome, dict):
            outcome["applied"] = True
            c["lane_outcome"] = outcome

    if status != "complete":
        tr["implementer_verdict"] = "blocked"
        tr["finished_at"] = utcnow()
        store.write("task_round", "update", tr["id"], strip_row(tr))
        c["phase"] = "blocked"
        c["resume_phase"] = "implement"
        c["blocker"] = f"implementer status={status}"
        _mark_applied()
        task["state"] = "open"
        save_task(store, task)
        return publish_blocker(
            store,
            worker,
            task,
            runner,
            f"Implementer returned incomplete status={status}",
            kind="implementer-incomplete",
            reply_checkpoint=True,
        ) + draft_lines

    if model_result not in _IMPLEMENTER_RESULTS:
        tr["implementer_verdict"] = "blocked"
        tr["finished_at"] = utcnow()
        store.write("task_round", "update", tr["id"], strip_row(tr))
        c["phase"] = "blocked"
        c["resume_phase"] = "implement"
        c["blocker"] = f"implementer invalid RESULT={model_result or 'empty'}"
        _mark_applied()
        task["state"] = "open"
        save_task(store, task)
        return publish_blocker(
            store,
            worker,
            task,
            runner,
            f"Implementer RESULT must be done|ask|blocked|no-change (got {model_result or 'empty'})",
            kind="implementer-invalid-result",
            reply_checkpoint=True,
        ) + draft_lines

    if model_result == "ask":
        question = redact(stdout or "Question from implementer.")
        source = c["source"]
        pending_q = c.get("pending_question")
        if isinstance(pending_q, str) and pending_q:
            question = pending_q
        try:
            qid = post_issue_comment(
                store,
                worker,
                runner,
                repo=str(source["repo"]),
                number=int(source["number"]),
                body=f"Question:\n{question}",
                kind="question",
                occurrence=f"{task['id']}:{round_num}",
            )
        except (CoordinatorError, StoreError) as exc:
            # Preserve the actual question across draft/publication failures.
            c["pending_question"] = question
            c["phase"] = "blocked"
            c["resume_phase"] = "implement"
            c["blocker"] = f"ask publish failed: {exc}"
            task["state"] = "open"
            save_task(store, task)
            return publish_blocker(
                store,
                worker,
                task,
                runner,
                f"Failed to publish implementer question: {redact(str(exc))}",
                kind="ask-publish",
                reply_checkpoint=True,
            ) + draft_lines
        c.pop("pending_question", None)
        c["phase"] = "ask"
        if c.get("question_activity_id") != qid:
            c["replies_consumed_through"] = None
        c["question_activity_id"] = qid
        c["resume_phase"] = "implement"
        _mark_applied()
        task["state"] = "open"
        # Keep the round open while publication is uncertain. Its occurrence ID
        # must remain stable if the process stops after GitHub posts the question.
        tr["implementer_verdict"] = "blocked"
        tr["finished_at"] = utcnow()
        with store.conn.transaction():
            store.write("task_round", "update", tr["id"], strip_row(tr))
            save_task(store, task)
        return [f"ask posted on {source['repo']}#{source['number']}"] + draft_lines

    if model_result == "blocked":
        # review-loop: implementer blocked → task failed. Stop.
        # Distinct from RESULT ask (reply-recoverable) and from external
        # blockers (CI authorization / incomplete reviews) which keep a
        # non-failed task and resume_phase + question checkpoint.
        tr["implementer_verdict"] = "blocked"
        tr["finished_at"] = utcnow()
        store.write("task_round", "update", tr["id"], strip_row(tr))
        c["phase"] = "blocked"
        c.pop("resume_phase", None)
        c.pop("question_activity_id", None)
        c.pop("replies_consumed_through", None)
        c["blocker"] = "implementer blocked"
        _mark_applied()
        task["state"] = "failed"
        save_task(store, task)
        return publish_blocker(
            store,
            worker,
            task,
            runner,
            redact(stdout or "implementer blocked"),
            kind="implementer-blocked",
        ) + draft_lines

    if model_result == "no-change":
        if not c.get("pr_number") and not sha:
            c["phase"] = "blocked"
            c["resume_phase"] = "implement"
            c["blocker"] = "no-change and no pull request"
            _mark_applied()
            task["state"] = "open"
            save_task(store, task)
            return publish_blocker(
                store,
                worker,
                task,
                runner,
                "No code change and no pull request. Stopping without an empty PR.",
                kind="no-change",
                reply_checkpoint=True,
            ) + draft_lines

    summaries = {}
    for language in ("en", "de"):
        values = re.findall(rf"(?m)^SUMMARY_{language.upper()}: ([^\r\n]+)$", stdout)
        if len(values) != 1 or not values[0].strip().endswith(".") or len(values[0]) > 800:
            c.update(phase="blocked", resume_phase="implement",
                     blocker="completed patch lacks concrete English/German change summaries")
            _mark_applied()
            task["state"] = "open"
            save_task(store, task)
            return publish_blocker(
                store,
                worker,
                task,
                runner,
                c["blocker"],
                kind="change-summary",
                reply_checkpoint=True,
            )
        summaries[language] = redact(values[0].strip(), limit=800)
    task["change_summary_en"] = summaries["en"]
    task["change_summary_de"] = summaries["de"]
    tr["implementer_verdict"] = "done"
    store.write("task_round", "update", tr["id"], strip_row(tr))
    task["state"] = "reviewing"
    c["phase"] = "inner_review"
    set_checklist(
        store,
        task,
        "implementer_done",
        "ja",
        f"round {round_num} implementer done",
        source="script",
    )
    _mark_applied()
    c.pop("lane_outcome", None)
    save_task(store, task)
    return [f"implementer done round={round_num}"] + draft_lines


def phase_implement(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
    lane_runner: LaneRunner | None,
) -> list[str]:
    c = coord(task)
    if c.get("uncertain_lane"):
        return publish_blocker(
            store,
            worker,
            task,
            runner,
            "uncertain prior lane outcome; refusing second model start",
            kind="uncertain-lane",
        )
    if c.get("phase") == "ask":
        return phase_read_replies(store, worker, task, runner)
    # Mid-task account must not silently repoint.
    expected_login = str(c.get("worker_login") or "")
    if expected_login:
        current = account_for(store, worker.session_id).login.casefold()
        if current != expected_login:
            raise CoordinatorError("worker GitHub login changed mid-task; refusing silent repoint")

    round_num = _ensure_round(store, task)
    tr = _find_round(store, task["id"], round_num)

    # Crash recovery: apply a persisted completed lane outcome before signing/publishing
    # rather than starting another implementer.
    pending_outcome = c.get("lane_outcome")
    if (
        isinstance(pending_outcome, dict)
        and pending_outcome.get("role") == "implementer"
        and pending_outcome.get("applied") is not True
        and pending_outcome.get("round") == round_num
    ):
        status = str(pending_outcome.get("status") or "")
        model_result = str(pending_outcome.get("result") or "")
        stdout = str(pending_outcome.get("stdout") or "")
        worktree = str(c.get("worktree") or "")
        sha = stage_sign_commit_if_changes(
            store,
            worker,
            runner,
            worktree,
            "Implement assigned issue.",
        )
        draft_lines: list[str] = []
        if sha:
            c["head_sha"] = sha
            invalidate_head_evidence(c, sha)
            draft_lines = ensure_draft(store, worker, task, runner)
            if not coord(task).get("pr_number"):
                c["phase"] = "publish_draft"
                c["resume_phase"] = "implement"
                pending_outcome["sha"] = sha
                c["lane_outcome"] = pending_outcome
                save_task(store, task)
                return ["implementer outcome recovery; draft pending"] + draft_lines
        else:
            draft_lines = ensure_draft(store, worker, task, runner)
        lines = _apply_implementer_outcome(
            store,
            worker,
            task,
            runner,
            round_num=round_num,
            tr=tr,
            status=status,
            model_result=model_result,
            stdout=stdout,
            sha=sha,
            draft_lines=draft_lines,
        )
        return lines

    # Pending ask that failed to publish after a patch.
    if isinstance(c.get("pending_question"), str) and c.get("pending_question"):
        return _apply_implementer_outcome(
            store,
            worker,
            task,
            runner,
            round_num=round_num,
            tr=tr,
            status="complete",
            model_result="ask",
            stdout=str(c.get("pending_question")),
            sha=None,
            draft_lines=[],
        )

    agent, result = launch_lane(
        store,
        worker,
        task,
        role="implementer",
        vendor="grok",
        round_num=round_num,
        spec_body=_spec_context(c, "Implement the assigned issue. Edit files only."),
        runner=runner,
        lane_runner=lane_runner,
    )
    status, model_result = parse_model_result(result.stdout, result.returncode)
    # Persist completed lane outcome BEFORE signing/publishing so a crash resumes
    # applying this outcome instead of launching another implementer.
    c["lane_outcome"] = {
        "role": "implementer",
        "vendor": "grok",
        "round": round_num,
        "agent_id": agent["id"],
        "status": status,
        "result": model_result,
        "stdout": redact(result.stdout or ""),
        "applied": False,
    }
    save_task(store, task)

    worktree = str(c["worktree"])
    sha = stage_sign_commit_if_changes(
        store,
        worker,
        runner,
        worktree,
        "Implement assigned issue.",
    )
    draft_lines = []
    if sha:
        c["head_sha"] = sha
        invalidate_head_evidence(c, sha)
        draft_lines = ensure_draft(store, worker, task, runner)
        if not coord(task).get("pr_number"):
            c = coord(task)
            # Resume implement after draft so persisted lane_outcome is applied
            # (including ask publish) rather than skipping straight to inner_review.
            c["phase"] = "publish_draft"
            c["resume_phase"] = "implement"
            if status == "complete" and model_result == "ask":
                c["pending_question"] = redact(result.stdout or "Question from implementer.")
            save_task(store, task)
            return ["implementer committed; draft pending"] + draft_lines
    else:
        draft_lines = ensure_draft(store, worker, task, runner)

    return _apply_implementer_outcome(
        store,
        worker,
        task,
        runner,
        round_num=round_num,
        tr=tr,
        status=status,
        model_result=model_result,
        stdout=result.stdout or "",
        sha=sha,
        draft_lines=draft_lines,
    )


def _reject_inner_and_reopen(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    tr: dict[str, Any],
    findings: str,
) -> list[str]:
    c = coord(task)
    tr["reviewer_verdict"] = "rejected"
    tr["finished_at"] = utcnow()
    store.write("task_round", "update", tr["id"], strip_row(tr))
    c["findings"] = redact(findings)
    n = int(task.get("current_round") or 0) + 1
    rid = str(uuid.uuid4())
    store.write(
        "task_round",
        "insert",
        rid,
        {
            "id": rid,
            "task_id": task["id"],
            "round": n,
            "implementer_verdict": None,
            "reviewer_verdict": None,
            "started_at": utcnow(),
            "finished_at": None,
        },
    )
    task["current_round"] = n
    task["state"] = "implementing"
    c["phase"] = "implement"
    save_task(store, task)
    return [f"inner reviewer rejected; new round={n}"]


def phase_inner_review(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
    lane_runner: LaneRunner | None,
) -> list[str]:
    c = coord(task)
    if not c.get("pr_number"):
        # Must not skip publication.
        c["phase"] = "publish_draft"
        c["resume_phase"] = "inner_review"
        save_task(store, task)
        return ["inner review deferred: draft not published"]
    round_num = int(task.get("current_round") or 0)
    tr = _find_round(store, task["id"], round_num)
    if tr.get("implementer_verdict") != "done":
        c["phase"] = "implement"
        save_task(store, task)
        return ["inner review deferred: implementer not done"]
    task["state"] = "reviewing"
    save_task(store, task)
    head = str(c.get("head_sha") or "")
    diff_note = ""
    if head and c.get("base_sha"):
        try:
            diff_path = write_review_diff(store, worker, task, runner, head=head)
            excerpt_path = diff_path.with_suffix(".excerpt.txt")
            excerpt = excerpt_path.read_text(encoding="utf-8")
            diff_note = (
                f"Script-generated diff artifact: {diff_path}\n"
                f"Read CONTRIBUTING.md and attached skills first.\n"
                f"---- diff excerpt ----\n{excerpt}\n---- end excerpt ----\n"
            )
        except (CoordinatorError, OSError) as exc:
            return publish_blocker(
                store,
                worker,
                task,
                runner,
                f"Cannot build inner-review diff: {redact(str(exc))}",
                kind="review-diff",
            )
    agent, result = launch_lane(
        store,
        worker,
        task,
        role="reviewer",
        vendor="grok",
        round_num=round_num,
        spec_body=_spec_context(
            c,
            "Review the current worktree. Read-only. Do not run Git; use the script diff.\n"
            + diff_note,
        ),
        runner=runner,
        lane_runner=lane_runner,
    )
    status, model_result = parse_model_result(result.stdout, result.returncode)
    if (
        status in ("timeout", "partial", "unavailable")
        or model_result not in _REVIEWER_RESULTS
    ):
        c["phase"] = "blocked"
        c["resume_phase"] = "inner_review"
        c["blocker"] = (
            f"inner reviewer incomplete (status={status} result={model_result or 'empty'})"
        )
        save_task(store, task)
        return publish_blocker(
            store,
            worker,
            task,
            runner,
            f"Inner reviewer incomplete (status={status}, result={model_result or 'empty'}); "
            "not a code rejection.",
            kind="reviewer-incomplete",
            reply_checkpoint=True,
        )
    if not review_is_approved(status, model_result):
        return _reject_inner_and_reopen(store, worker, task, tr, result.stdout or "rejected")

    tr["reviewer_verdict"] = "approved"
    tr["finished_at"] = utcnow()
    store.write("task_round", "update", tr["id"], strip_row(tr))
    task["state"] = "local-check"
    c["phase"] = "tests"
    set_checklist(
        store,
        task,
        "reviewer_approved",
        "ja",
        f"round {round_num} approved",
        source="script",
    )
    save_task(store, task)
    return [f"inner reviewer approved round={round_num}"]


def phase_tests(store: Store, worker: WorkerConfig, task: dict[str, Any], runner: Runner) -> list[str]:
    c = coord(task)
    source = c["source"]
    cfg = repo_cfg(worker, str(source["repo"]))
    worktree = str(c["worktree"])
    head = verify_signed_clean_head(store, worker, runner, worktree)
    c["head_sha"] = head
    evidence = c.setdefault("evidence", {})
    if not isinstance(evidence, dict):
        evidence = {}
        c["evidence"] = evidence
    if evidence.get("tests_pass") and evidence.get("tests_head") == head:
        c["phase"] = "pr_gates_grok"
        task["state"] = "pr-review"
        save_task(store, task)
        return [f"tests already green on {head[:7]}"]
    env = coordinator_env(c, worker, cfg)
    completed = run_bounded(
        list(cfg.check_argv),
        timeout=worker.check_timeout,
        cwd=worktree,
        env=env,
        clear_ambient_github=True,
    )
    output = redact((completed.stdout or "") + "\n" + (completed.stderr or ""))
    cid = str(uuid.uuid4())
    passed = completed.returncode == 0
    store.write(
        "local_check",
        "insert",
        cid,
        {
            "id": cid,
            "task_id": task["id"],
            "name": "coordinator-check",
            "command": " ".join(cfg.check_argv),
            "result": "pass" if passed else "fail",
            "output": output,
            "ran_at": utcnow(),
            "head_sha": head,
        },
    )
    if not passed:
        evidence["tests_pass"] = False
        evidence["tests_head"] = head
        c["findings"] = f"Tests failed on {head[:7]}:\n{output}"
        c["phase"] = "implement"
        task["state"] = "implementing"
        invalidate_head_evidence(c, head)
        save_task(store, task)
        return [f"tests failed on {head[:7]}; routing to implementer"]
    evidence["tests_pass"] = True
    evidence["tests_head"] = head
    set_checklist(
        store,
        task,
        "local_check_pass",
        "ja",
        f"pass on {head}",
        source="script",
    )
    draft_lines = ensure_draft(store, worker, task, runner)
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
    c["phase"] = "pr_gates_grok"
    task["state"] = "pr-review"
    save_task(store, task)
    return [f"tests passed on {head[:7]}"] + draft_lines


def _open_tasks(store: Store, session_id: str) -> list[dict[str, Any]]:
    origin = store.device_id()
    tasks = []
    for row in store.rows("task"):
        if row.get("_origin_device_id") != origin:
            continue
        if row.get("session_id") != session_id:
            continue
        if _terminal(row):
            continue
        payload = row.get("payload")
        if not isinstance(payload, dict) or not isinstance(payload.get("coordinator"), dict):
            continue
        # Reconcile checklist if a prior crash left keys missing.
        session = store.row("session", session_id)
        if session is not None:
            _ensure_checklist_rows(store, row, session)
        tasks.append(row)
    tasks.sort(key=lambda r: str(r.get("created_at") or ""))
    return tasks


def advance_one(
    store: Store,
    worker: WorkerConfig,
    *,
    runner: Runner,
    lane_runner: LaneRunner | None,
) -> list[str]:
    tasks = _open_tasks(store, worker.session_id)
    if not tasks:
        return []
    task = tasks[0]
    c = coord(task)
    phase = str(c.get("phase") or "accept")
    source = c.get("source") or {}
    try:
        binding = execution_binding(store, worker, str(source.get("repo") or ""))
        if c.get("execution_binding") != binding:
            raise CoordinatorError("task execution configuration changed or is unpinned")
    except (CoordinatorError, StoreError) as exc:
        return publish_blocker(store, worker, task, runner, str(exc), kind="configuration-binding")
    # Stop new effects when assignment is revoked. await_merge may continue
    # observation only; formal_approve / leave_draft must not proceed.
    if phase not in ("await_merge", "done", "blocked", "ask"):
        try:
            source = c.get("source")
            if isinstance(source, dict):
                scoped_runner = scoped(store, worker.session_id, runner)
                account = account_for(store, worker.session_id)
                verify_issue_assigned(
                    scoped_runner,
                    str(source["repo"]),
                    int(source["number"]),
                    account.login,
                )
        except CoordinatorError as exc:
            c["phase"] = "blocked"
            c["resume_phase"] = phase if phase not in ("blocked", "ask", "done") else "implement"
            c["blocker"] = str(exc)
            save_task(store, task)
            return publish_blocker(
                store, worker, task, runner, str(exc), kind="stopped", reply_checkpoint=True
            )

    handlers = {
        "accept": lambda: phase_accept(store, worker, task, runner),
        "checkout": lambda: phase_checkout(store, worker, task, runner),
        "implement": lambda: phase_implement(store, worker, task, runner, lane_runner),
        "inner_review": lambda: phase_inner_review(store, worker, task, runner, lane_runner),
        "tests": lambda: phase_tests(store, worker, task, runner),
        "publish_draft": lambda: phase_publish_draft(store, worker, task, runner),
        "pr_gates_grok": lambda: phase_pr_gates_grok(store, worker, task, runner, lane_runner),
        "pr_gates_codex": lambda: phase_pr_gates_codex(store, worker, task, runner, lane_runner),
        "ci": lambda: phase_ci(store, worker, task, runner),
        "readiness": lambda: phase_readiness(store, worker, task, runner),
        "formal_approve": lambda: phase_formal_approve(store, worker, task, runner),
        "leave_draft": lambda: phase_leave_draft(store, worker, task, runner),
        "await_merge": lambda: phase_await_merge(store, worker, task, runner),
        "ask": lambda: phase_read_replies(store, worker, task, runner),
        "blocked": lambda: phase_blocked(store, worker, task, runner),
        "done": lambda: ["done"],
    }
    handler = handlers.get(phase)
    if handler is None:
        raise CoordinatorError(f"unknown coordinator phase {phase}")
    try:
        return handler()
    except (CoordinatorError, StoreError) as exc:
        # Task-specific failures must become GitHub-visible blockers when possible.
        c = coord(task)
        if not c.get("resume_phase") and phase not in ("await_merge", "done", "blocked", "ask"):
            c["resume_phase"] = phase
        c["blocker"] = str(exc)
        if phase not in ("await_merge", "done"):
            c["phase"] = "blocked"
        save_task(store, task)
        return publish_blocker(
            store,
            worker,
            task,
            runner,
            str(exc),
            kind="phase-error",
            reply_checkpoint=True,
        )
    except Exception as exc:  # noqa: BLE001 — never escape as silent tick failure
        c = coord(task)
        if not c.get("resume_phase") and phase not in ("await_merge", "done", "blocked", "ask"):
            c["resume_phase"] = phase
        c["blocker"] = redact(str(exc))
        if phase not in ("await_merge", "done"):
            c["phase"] = "blocked"
        save_task(store, task)
        return publish_blocker(
            store,
            worker,
            task,
            runner,
            redact(str(exc)),
            kind="phase-error",
            reply_checkpoint=True,
        )
