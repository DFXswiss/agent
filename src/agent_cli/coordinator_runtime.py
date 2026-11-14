"""Concrete runtime for the script-owned issue coordinator.

Checkpoints live in task.payload['coordinator'] and activity result fields.
There is no second hub state machine and no new store table. GitHub HTTP,
Git, tests, readiness, and lane starts are script work; model text is never a
transition. This module does not claim universal sandbox enforcement.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from collections.abc import Callable
from dataclasses import asdict
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from .a38_review import COMPLIANCE_PROMPT
from .ai_accounts import AccountError as AIAccountError
from .ai_accounts import AIAccounts, load_ai_accounts
from .model_slots import (
    LEGACY_RECORDED_SLOTS,
    SLOT_STAGES,
    ModelSlotsError,
    canonical_key,
    canonical_stage,
    implementer_for_new_record,
    model_pins,
    models_for_new_record,
)
from .coordinator_checks import TaskFacts, checks_satisfied, evaluate_action
from .allow import CHECKLIST_KEYS
from .coordinator_common import (
    FINDINGS_SPEC_BOUND,
    ORDER_KIND,
    ROUND_FINDINGS_BOUND,
    SLOT_LANE_KINDS,
    TASK_TEXT_BOUND,
    TASK_TEXT_RECORD_BOUND,
    check_label,
    clip_middle,
    REQUIRED_REVIEW_SKILLS,
    required_lane_slots,
    REQUIRED_WORKER_SKILLS,
    CiObservedFailureError,
    CoordinatorError,
    DraftHeadNotVerifiedError,
    LaneFailedError,
    NonRecoverableCoordinatorError,
    LaneRunner,
    Runner,
    account_for,
    as_int,
    comment_target,
    coord,
    coordinator_env,
    gh_json,
    gh_list,
    harden_grok_write_argv,
    infra_failure,
    is_order,
    order_id,
    owned_session,
    parse_change_summaries,
    parse_model_result,
    parse_surface_list,
    read_task_text,
    redact,
    redact_tail,
    review_is_approved,
    save_task,
    scoped,
    seconds_since,
    source_kind,
    source_key,
    strip_row,
    task_text_path,
    task_text_reference,
    write_task_text,
)
from .coordinator_config import RepositoryConfig, WorkerConfig, resolve_round_limit
from .coordinator_a38 import (
    A38ReportCheckpointConflict,
    fresh_pr_head,
    intake_second_reviews,
    post_suite_checkpoint_pending,
    pr_state,
    reconcile_a38_publication_intents,
    phase_await_ready as phase_a38_await_ready,
    phase_await_second_review,
    phase_first_comment as phase_a38_first_comment,
    phase_second_comment as phase_a38_second_comment,
    phase_tests as phase_a38_tests,
)
from .coordinator_exec import run_bounded
from .coordinator_git import (
    close_pull_request,
    ensure_draft,
    phase_checkout,
    phase_publish_draft,
    phase_start_draft,
    refresh_draft_body,
    repo_cfg,
    stage_sign_commit_if_changes,
    verify_signed_clean_head,
)
from .coordinator_github import (
    clear_reply_recovery,
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
    GateOrderingUnavailableError,
    _IMPLEMENTER_RESULTS,
    _REVIEWER_RESULTS,
    _coordinator_path_replacements,
    cleanup_finished_reviewer_spools,
    check_command_unavailable,
    clear_reviewer_replay_outcomes,
    implementer_replay_fields,
    invalidate_head_evidence,
    gate_ordering_unavailable,
    launch_lane,
    lane_retry,
    phase_pr_gates_codex,
    phase_pr_gates_grok,
    reconcile_rejection_review_intents,
    recover_working_lanes,
    reviewer_replay_outcomes,
    retry_remaining,
    review_rules_bound,
    set_checklist,
    write_review_context,
    write_review_diff,
)
from .coordinator_orders import (
    consume_order_intents,
    find_order,
    pending_order_intents,
    phase_accept_order,
    reconcile_order_events,
    sweep_terminal_orders,
)
from .preconditions import PHASE_ACTION
from .github_accounts import AccountError, load_accounts
from .skills import has_skill
from .store import Store, StoreError, utcnow
from .timestamps import parse_utc_timestamp

# Re-exports for coordinator.py and tests.
__all__ = [
    "CoordinatorError",
    "advance_one",
    "discover_assignments",
    "harden_grok_write_argv",
    "parse_model_result",
    "preflight_worker",
    "require_single_first_model",
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
    if worker.conflict_remedy is not None and not has_skill(session, "conflicts"):
        raise CoordinatorError("worker session missing skill conflicts")
    try:
        accounts = load_accounts(store.home)
        worker_account = accounts.for_session(worker.session_id)
        review_account = None
        if not worker.a38:
            if worker.review_session is None:
                raise CoordinatorError("non-A38 worker requires review_session")
            review = owned_session(store, worker.review_session)
            for skill in REQUIRED_REVIEW_SKILLS:
                if not has_skill(review, skill):
                    raise CoordinatorError(f"review_session missing skill {skill}")
            review_account = accounts.for_session(worker.review_session)
    except AccountError as exc:
        raise CoordinatorError(str(exc)) from exc
    if review_account is not None and worker_account.login.casefold() == review_account.login.casefold():
        raise CoordinatorError("formal review_session must use a different GitHub login")
    if worker.a38 and worker_account.login.casefold() in worker.second_reviewers:
        raise CoordinatorError("second_reviewers must not contain the worker GitHub login")
    if worker_account.git_identity is None:
        raise CoordinatorError("worker GitHub account requires git identity for signed commits")
    try:
        worker_account.runner(runner, require_git=True)
        if review_account is not None:
            review_account.runner(runner)
    except AccountError as exc:
        raise CoordinatorError(str(exc)) from exc
    try:
        ai = load_ai_accounts(store.home)
        _refuse_codex_first(ai.models.first, ai.implementer)
        _require_second_for_worker(
            ai.models.second, worker.a38, worker.a38_second_review
        )
        for slot in required_lane_slots(ai.models):
            vendor, role = slot.split(":", 1)
            selected = ai.for_lane(
                worker.session_id, role, vendor, require_execution=False
            )
            if role == "implementer" and selected.access != "workspace-write":
                raise CoordinatorError("implementer lane requires workspace-write access")
            if role != "implementer" and selected.access != "read-only":
                raise CoordinatorError(f"{slot} requires read-only access")
        if worker.orders:
            check_slot = f"{ai.models.first}:check"
            check_role = ai.for_lane(
                worker.session_id,
                "check",
                ai.models.first,
                require_execution=False,
            )
            if check_role.access != "read-only":
                raise CoordinatorError(f"{check_slot} requires read-only access")
            require_single_first_model(ai, worker)
    except AIAccountError as exc:
        raise CoordinatorError(str(exc)) from exc
    if not worker.repositories and not worker.repository_owners:
        raise CoordinatorError("worker has no repositories configured")
    if not worker.workspace_root.is_absolute():
        raise CoordinatorError("workspace_root must be absolute")
    try:
        ai.require_lane_execution()
    except AIAccountError as exc:
        raise CoordinatorError(str(exc)) from exc


def execution_binding(
    store: Store,
    worker: WorkerConfig,
    repo: str,
    ai: AIAccounts | None = None,
    repository_config: RepositoryConfig | None = None,
    owner_scoped: bool = False,
) -> str:
    """Pin selected execution metadata, never credential contents or unrelated profiles.

    ``ai`` lets a caller hash the same manifest read it pins ``models`` from.
    """
    ai = ai if ai is not None else load_ai_accounts(store.home)
    selected = _execution_binding_fields(
        store,
        worker,
        repo,
        ai,
        repository_config=repository_config,
        owner_scoped=owner_scoped,
    )
    encoded = json.dumps(selected, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _execution_binding_fields(
    store: Store,
    worker: WorkerConfig,
    repo: str,
    ai: AIAccounts,
    *,
    repository_config: RepositoryConfig | None = None,
    owner_scoped: bool = False,
) -> dict[str, Any]:
    """Return the current binding fields from one AI-manifest snapshot."""
    cfg = repository_config or repo_cfg(worker, repo)
    owner = repo.split("/", 1)[0].casefold() if "/" in repo else ""
    if owner_scoped and owner not in worker.repository_owners:
        raise CoordinatorError(f"repository owner {owner or '?'} is no longer configured")
    github = load_accounts(store.home)
    lanes = [
        asdict(
            ai.for_lane(
                worker.session_id,
                slot.split(":", 1)[1],
                slot.split(":", 1)[0],
                require_execution=False,
            )
        )
        for slot in required_lane_slots(ai.models)
    ]
    selected = {
        "repository": asdict(cfg),
        "workspace": str(worker.workspace_root),
        "worker_session": worker.session_id,
        "reply_logins": worker.reply_logins,
        **({"repository_owner": owner} if owner_scoped else {}),
        **({"require_hub_assignment": True} if worker.require_hub_assignment else {}),
        "github": [
            asdict(github.for_session(sid))
            for sid in (
                (worker.session_id,)
                if worker.a38
                else (worker.session_id, worker.review_session)
            )
        ],
        "models": ai.models.as_dict(),
        "lane_execution": asdict(ai.require_lane_execution()),
        "lanes": lanes,
    }
    if worker.a38:
        selected["a38"] = {
            "enabled": True,
            "second_reviewers": worker.second_reviewers,
            "second_review_timeout_seconds": worker.second_review_timeout_seconds,
        }
        if worker.a38_second_review == "model":
            selected["a38"]["second_review_mode"] = "model"
    else:
        selected["review_session"] = worker.review_session
    return selected


def _legacy_execution_binding(
    store: Store,
    worker: WorkerConfig,
    repo: str,
    ai: AIAccounts,
    *,
    repository_config: RepositoryConfig | None = None,
    owner_scoped: bool = False,
) -> str:
    """Reproduce the pre-lane-execution hash for accepted-task migration only.

    The old formula omitted ``lane_execution`` and omitted ``models`` when the
    historical grok/codex mapping was selected. All other fields remain pinned.
    """
    selected = _execution_binding_fields(
        store,
        worker,
        repo,
        ai,
        repository_config=repository_config,
        owner_scoped=owner_scoped,
    )
    selected.pop("lane_execution")
    if ai.models == LEGACY_RECORDED_SLOTS:
        selected.pop("models")
    encoded = json.dumps(selected, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def refresh_execution_binding(
    store: Store, worker: WorkerConfig, task: dict[str, Any]
) -> None:
    """Validate a task binding and migrate only the preceding hash formula."""
    checkpoint = coord(task)
    source = checkpoint.get("source") or {}
    repo = str(source.get("repo") or "")
    ai = load_ai_accounts(store.home)
    cfg = repo_cfg(worker, repo, task)
    owner_scoped = bool(source.get("owner_scoped"))
    current = execution_binding(
        store,
        worker,
        repo,
        ai,
        repository_config=cfg,
        owner_scoped=owner_scoped,
    )
    stored = checkpoint.get("execution_binding")
    if stored == current:
        return
    if stored != _legacy_execution_binding(
        store,
        worker,
        repo,
        ai,
        repository_config=cfg,
        owner_scoped=owner_scoped,
    ):
        raise CoordinatorError("task execution configuration changed or is unpinned")
    # Accepted tasks keep working across the binding-formula upgrade. Repin
    # immediately so a later real models/lane/device change stops.
    checkpoint["execution_binding"] = current
    save_task(store, task)


CODEX_FIRST_REFUSED = (
    "models.first = codex is not supported by the coordinator: the codex "
    "implementer lane cannot be restricted from running commands"
)
SEPARATE_IMPLEMENTER_REFUSED = (
    "a separate implementer is not supported by the coordinator: implementer must equal "
    "models.first"
)


def _refuse_codex_first(first: str, implementer: str | None = None) -> None:
    """codex exec --sandbox workspace-write can run shell commands; the coordinator's
    implementer must not execute software. Used by preflight, task creation and launch."""
    if first == "codex":
        raise CoordinatorError(CODEX_FIRST_REFUSED)
    if implementer is not None and implementer != first:
        raise CoordinatorError(SEPARATE_IMPLEMENTER_REFUSED)


def _require_second_for_worker(
    second: str | None, a38: bool, second_review: str = "external"
) -> None:
    """Only an A38 worker with an external second review runs without a second review vendor."""
    if a38 and second_review == "model":
        if second is None:
            raise CoordinatorError(
                "A38 workers with a38_second_review = model require models.second"
            )
        return
    if a38 and second is not None:
        raise CoordinatorError("A38 workers require models.second = null")
    if not a38 and second is None:
        raise CoordinatorError("models.second = null is allowed only for A38 workers")


def require_single_first_model(ai: AIAccounts, worker: WorkerConfig) -> str:
    """The one model identifier of the implementer and the first-review lanes of an order worker."""
    first = ai.models.first
    try:
        implementer = ai.for_lane(
            worker.session_id,
            "implementer",
            first,
            require_execution=False,
        ).model
        for kind in SLOT_LANE_KINDS["first"]:
            model = ai.for_lane(
                worker.session_id,
                kind,
                first,
                require_execution=False,
            ).model
            if model != implementer:
                raise CoordinatorError(
                    "order workers require one model for the implementer and the "
                    f"first review: {first}:{kind} uses {model}, "
                    f"{first}:implementer uses {implementer}"
                )
    except AIAccountError as exc:
        raise CoordinatorError(str(exc)) from exc
    return implementer


def _first_slot_vendor(task: dict[str, Any]) -> str:
    """Provider of model slot ``first``, pinned on the task when it was created."""
    try:
        # Lane start: an old task without a pin must be recreated before new work.
        first = models_for_new_record(task).first
        implementer = implementer_for_new_record(task)
    except ModelSlotsError as exc:
        raise CoordinatorError(str(exc)) from exc
    _refuse_codex_first(first, implementer)
    return first


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
        canonical_key(str(item.get("key")))
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


def _discard_task_text(worker: WorkerConfig, task_id: str) -> None:
    """Remove a task text written for a task this call does not own."""
    path = task_text_path(worker, task_id)
    for candidate in (path, path.with_suffix(".tmp")):
        try:
            candidate.unlink()
        except OSError:
            pass
    try:
        path.parent.rmdir()
    except OSError:
        pass


def _create_implement_task(
    store: Store,
    worker: WorkerConfig,
    *,
    repo: str,
    number: int,
    title: str,
    assigned_id: str,
    repository_config: RepositoryConfig | None,
    body: str,
) -> dict[str, Any] | None:
    """Insert task under device-wide source admission lock. None if another owner won.

    Runs inside the caller's store transaction: an error takes the inserted
    row back and removes the task text.
    """
    session = owned_session(store, worker.session_id)
    tid = str(uuid.uuid4())
    # One manifest read for both the binding hash and the models pin.
    try:
        ai = load_ai_accounts(store.home)
    except AIAccountError as exc:
        raise CoordinatorError(str(exc)) from exc
    _refuse_codex_first(ai.models.first, ai.implementer)
    _require_second_for_worker(
        ai.models.second, worker.a38, worker.a38_second_review
    )
    try:
        implementer_model = ai.for_lane(
            worker.session_id,
            "implementer",
            ai.models.first,
            require_execution=False,
        ).model
    except AIAccountError as exc:
        raise CoordinatorError(str(exc)) from exc
    source = {
        "repo": repo,
        "number": number,
        "assigned_id": assigned_id,
        "title": title,
        "body": redact(body, limit=2000),
    }
    if repository_config is not None:
        source.update(
            publication_repo=repository_config.publication_repo,
            base=repository_config.base,
        )
    else:
        source["owner_scoped"] = True
        source["require_hub_assignment"] = worker.require_hub_assignment
    c = {
        "phase": "accept",
        "source": source,
        "branch": f"task-{tid[:8]}",
        **({"execution_binding": execution_binding(
            store, worker, repo, ai, repository_config
        )} if repository_config is not None else {}),
        "worker_session": worker.session_id,
        "worker_login": account_for(store, worker.session_id).login.casefold(),
        "round_limit": resolve_round_limit(worker, implementer_model),
        **(
            {
                "a38": {
                    "enabled": True,
                    **(
                        {"second_review_mode": "model"}
                        if worker.a38_second_review == "model"
                        else {}
                    ),
                }
            }
            if worker.a38
            else {}
        ),
    }
    held: dict[str, Any] = {"existing": None}

    def _skip() -> bool:
        existing = _find_task_for_issue_device_wide(store, repo, number)
        if existing is None:
            return False
        held["existing"] = existing
        return True

    try:
        write_task_text(worker, tid, body)
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
                "payload": {
                    "coordinator": c,
                    "source_issue": {"repo": repo, "number": number},
                },
                "state": "open",
                "current_round": 0,
                **model_pins(ai.models, ai.implementer),
                "created_at": utcnow(),
                "updated_at": utcnow(),
                "change_summary_en": None,
                "change_summary_de": None,
            },
            lock_key=f"coordinator-source:{repo.casefold()}:{number}",
            skip=_skip,
        )
        if held["existing"] is not None:
            _discard_task_text(worker, tid)
            return None
        task = store.row("task", tid)
        if task is None:
            # Lost the race to another insert with same logical source.
            _discard_task_text(worker, tid)
            return _find_task_for_issue_device_wide(store, repo, number)
        _ensure_checklist_rows(store, task, session)
    except BaseException:
        _discard_task_text(worker, tid)
        raise
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
    primary_id = str(
        uuid5(
            NAMESPACE_URL,
            f"coordinator-assigned:{repo.casefold()}:{number}:{assignee.casefold()}",
        )
    )
    fallback_id = str(
        uuid5(
            NAMESPACE_URL,
            "coordinator-assigned:"
            f"{store.device_id()}:{repo.casefold()}:{number}:{assignee.casefold()}",
        )
    )
    stable_payload = {
        "repo": repo,
        "number": number,
        "url": url,
        "title": title,
        "body": redact(body, limit=2000),
        "assignee": assignee,
        "mandate": "github-assignment",
        "source": "coordinator",
    }

    def _matches(row: dict[str, Any], activity_id: str) -> bool:
        payload = row.get("payload")
        if not isinstance(payload, dict):
            return False
        stored_assigned_at = payload.get("assigned_at")
        observed_at = payload.get("assignment_observed_at")
        expected_payload = {
            **stable_payload,
            "assigned_at": stored_assigned_at,
            "assignment_observed_at": observed_at,
        }
        return (
            row.get("_origin_device_id") == store.device_id()
            and row.get("id") == activity_id
            and row.get("session_id") == worker.session_id
            and row.get("type") == "issue.assigned"
            and row.get("execution_status") == "done"
            and parse_utc_timestamp(stored_assigned_at) is not None
            and parse_utc_timestamp(observed_at) is not None
            and payload == expected_payload
        )

    for activity_id in (primary_id, fallback_id):
        existing = store.row("activity", activity_id)
        if existing is not None:
            if _matches(existing, activity_id):
                return activity_id
            if activity_id == fallback_id:
                raise CoordinatorError(
                    "device-scoped assignment activity id is occupied by a "
                    "mismatched row"
                )
            continue

        collision: dict[str, dict[str, Any] | None] = {"row": None}

        def _skip(candidate_id: str = activity_id) -> bool:
            current = store.row("activity", candidate_id)
            collision["row"] = current
            return current is not None

        store.write_with_advisory(
            "activity",
            "insert",
            activity_id,
            {
                "id": activity_id,
                "session_id": worker.session_id,
                "type": "issue.assigned",
                "payload": {
                    **stable_payload,
                    "assigned_at": assigned_at,
                    "assignment_observed_at": utcnow(),
                },
                "execution_status": "done",
            },
            lock_key=f"coordinator-source:{repo.casefold()}:{number}",
            skip=_skip,
        )
        written = collision["row"] or store.row("activity", activity_id)
        if isinstance(written, dict) and _matches(written, activity_id):
            return activity_id
    raise CoordinatorError("assignment activity write is missing or mismatched")


def discover_assignments(store: Store, worker: WorkerConfig, runner: Runner) -> list[str]:
    """Paginated discovery of currently assigned open issues. No silent first-run ignore."""
    if has_runnable_task(store, worker):
        return []
    lines: list[str] = []
    scoped_runner = scoped(store, worker.session_id, runner)
    account = account_for(store, worker.session_id)
    login = account.login
    candidates: list[tuple[str, dict[str, Any], RepositoryConfig | None]] = []
    explicit_names = {repo.casefold() for repo in worker.repositories}
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
        candidates.extend((repo, issue, repo_cfg_item) for issue in issues if isinstance(issue, dict))
    for owner in worker.repository_owners:
        try:
            owner_row = gh_json(scoped_runner, ["gh", "api", f"users/{owner}"])
            owner_type = owner_row.get("type") if isinstance(owner_row, dict) else None
            if owner_type == "Organization":
                owner_qualifier = "org"
            elif owner_type == "User":
                owner_qualifier = "user"
            else:
                raise CoordinatorError(
                    f"repository owner {owner} has unsupported GitHub account type"
                )
            search_result = gh_json(
                scoped_runner,
                [
                    "gh", "api", "--paginate", "--slurp",
                    f"search/issues?q=is:issue+is:open+assignee:{login}+"
                    f"archived:false+{owner_qualifier}:{owner}&per_page=100",
                ],
            )
            if isinstance(search_result, dict):
                pages = [search_result]
            elif isinstance(search_result, list) and all(
                isinstance(page, dict) for page in search_result
            ):
                pages = search_result
            else:
                raise CoordinatorError("owner issue search returned no page object")
            if not pages:
                raise CoordinatorError(
                    "owner issue search returned incomplete results (no pages)"
                )
            found: list[tuple[dict[str, Any], str]] = []
            identities: set[str] = set()
            total_count: int | None = None
            for page in pages:
                if page.get("incomplete_results") is not False:
                    raise CoordinatorError("owner issue search returned incomplete results")
                page_total = page.get("total_count")
                if isinstance(page_total, bool) or not isinstance(page_total, int):
                    raise CoordinatorError("owner issue search page has no total_count")
                if page_total < 0:
                    raise CoordinatorError(
                        "owner issue search page has negative total_count"
                    )
                if total_count is None:
                    total_count = page_total
                elif page_total != total_count:
                    raise CoordinatorError(
                        "owner issue search returned inconsistent total_count across pages"
                    )
                items = page.get("items")
                if not isinstance(items, list):
                    raise CoordinatorError("owner issue search page has no items array")
                for issue in items:
                    if not isinstance(issue, dict):
                        raise CoordinatorError(
                            "owner issue search returned incomplete results "
                            "(malformed issue identity)"
                        )
                    repository_url = issue.get("repository_url")
                    match = re.fullmatch(
                        r"https://api\.github\.com/repos/([^/]+)/([^/]+)",
                        str(repository_url or ""),
                    )
                    number = as_int(issue.get("number"))
                    if match is None or number is None or number <= 0:
                        raise CoordinatorError(
                            "owner issue search returned incomplete results "
                            "(malformed issue identity)"
                        )
                    repo = f"{match.group(1)}/{match.group(2)}"
                    identity = source_key(repo, number)
                    if identity in identities:
                        raise CoordinatorError(
                            "owner issue search returned incomplete results "
                            f"(duplicate issue identity {identity})"
                        )
                    identities.add(identity)
                    found.append((issue, repo))
            if total_count is None:
                raise CoordinatorError(
                    "owner issue search returned incomplete results (no pages)"
                )
            if total_count > len(identities):
                raise CoordinatorError(
                    "owner issue search was truncated "
                    f"({len(identities)} of {total_count} results)"
                )
            if total_count < len(identities):
                raise CoordinatorError(
                    "owner issue search returned incomplete results "
                    f"({len(identities)} unique identities exceed total_count {total_count})"
                )
        except CoordinatorError as exc:
            lines.append(f"discover owner {owner}: {redact(str(exc))}")
            continue
        for issue, repo in found:
            if str(issue.get("state") or "").casefold() != "open":
                continue
            repository = issue.get("repository")
            if isinstance(repository, dict) and repository.get("archived") is True:
                continue
            if repo.casefold() in explicit_names:
                continue
            if repo.partition("/")[0].casefold() not in worker.repository_owners:
                continue
            candidates.append((repo, issue, None))
    seen: set[str] = set()
    for repo, issue, repo_cfg_item in candidates:
        source_id = f"{repo.casefold()}#{issue.get('number')}"
        if source_id in seen:
            continue
        seen.add(source_id)
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
        source_lock = f"coordinator-source:{repo.casefold()}:{number}"
        with store.advisory_transaction(source_lock):
            # Recheck admission while holding the same transaction lock that
            # covers both source evidence and task creation. A losing worker
            # therefore cannot leave an orphan assignment activity behind.
            task = _find_task_for_issue_device_wide(store, repo, number)
            if task is None:
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
                    repository_config=repo_cfg_item,
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
        break
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


def record_round_findings(
    store: Store, tr: dict[str, Any], c: dict[str, Any]
) -> None:
    """Keep the findings this round's implementer is started with on its round row."""
    text = str(c.get("findings") or "")
    stored = (
        clip_middle(redact(text, limit=None), ROUND_FINDINGS_BOUND)
        if text
        else None
    )
    if tr.get("findings") == stored:
        return
    committed = copy.deepcopy(tr)
    committed["findings"] = stored
    store.write("task_round", "update", committed["id"], strip_row(committed))
    tr.clear()
    tr.update(committed)


def _implementer_instruction(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
) -> str:
    """Implementer task, plus the surface list when the base has REVIEW.md."""
    prefix = (
        "Implement the order. Edit files only.\n"
        if is_order(task)
        else "Implement the assigned issue. Edit files only.\n"
    )
    text = prefix + (
        "Before you write a new one, search this repository for an existing "
        "helper, utility, base class, configuration key, fixture, or nearby "
        "feature built the same way that already does the job. Read and search "
        "the files with your file tools; Bash stays prohibited. The job is the "
        "purpose the caller already has, and behavior that differs does not "
        "make a different job. For a new endpoint, a user-interface control, a "
        "visible error text, a clock or time window, or a permission check, "
        "reuse or extend what the search finds rather than writing a second "
        "one: one of those goes in only when no existing element in this "
        "repository does that job. For a helper the caller does not see, reuse "
        "is the better change and not a requirement."
    )
    if not review_rules_bound(store, worker, runner, task):
        return text
    return (
        text
        + "\nThe base revision has REVIEW.md. After the summary lines, include "
        "one or more REUSED: lines and one or more ADDED: lines. Each line names "
        "a file and a line, for example REUSED: `src/pay.ts:40` existing gift invoice. "
        "These lines report that search; they are not a guess written afterwards. "
        "An ADDED: line for one of those named kinds that passes over an "
        "existing element names that element and the different job it has, on "
        "the line itself. Any other ADDED: line needs no different job. "
        "When that section is empty, write REUSED: none — nothing existing does this job. "
        "The script posts these lines into the pull request body. Do not call GitHub."
    )


def _spec_context(
    c: dict[str, Any], extra: str = "", *, text: str | None = None
) -> str:
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
    source_line = (
        f"Source order: {source.get('order_id')}\n"
        if source_kind(c) == ORDER_KIND
        else f"Source issue: {source.get('repo')}#{source.get('number')}\n"
    )
    return source_line + (
        f"Title: {redact(str(title))}\n"
        f"Body (untrusted):\n{redact(text, limit=TASK_TEXT_BOUND) if text is not None else redact(str(body), limit=1500)}\n"
        f"{reply_text}\n"
        f"{('Findings to address:\\n' + redact(str(findings), limit=FINDINGS_SPEC_BOUND)) if findings else ''}\n"
        f"{extra}\n"
    )


def _held_surface_list(task: dict[str, Any]) -> tuple[list[str], list[str]] | None:
    """Lines captured from the full lane stdout, while that capture is current."""
    c = coord(task)
    if c.get("surface_list_held") is not True:
        return None
    reused = task.get("reused_lines")
    added = task.get("added_lines")
    if not (
        isinstance(reused, list)
        and reused
        and isinstance(added, list)
        and added
        and all(isinstance(line, str) and line for line in [*reused, *added])
    ):
        return None
    return (reused, added)


def _replayed_surface_list(
    outcome: dict[str, Any] | None,
) -> tuple[list[str], list[str]] | None:
    """Validate structured surface lines from a durable lane checkpoint."""
    if not isinstance(outcome, dict):
        return None
    reused = outcome.get("reused_lines")
    added = outcome.get("added_lines")
    if not isinstance(reused, list) or not isinstance(added, list):
        return None
    if not all(isinstance(line, str) for line in [*reused, *added]):
        return None
    replay_lines = [
        *(f"REUSED: {line}" for line in reused),
        *(f"ADDED: {line}" for line in added),
    ]
    replay_text = "\n".join(replay_lines)
    return parse_surface_list(replay_text)


def _hold_surface_list(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
    stdout: str,
) -> None:
    """Remember a reused and added list from the full implementer stdout.

    The lane outcome carries the same structured lines for crash recovery.
    Holding them on the task also makes them available to draft publication;
    this function does not post them.
    """
    if not review_rules_bound(store, worker, runner, task):
        return
    listed = parse_surface_list(stdout)
    c = coord(task)
    if listed is None:
        c.pop("surface_list_held", None)
        return
    task["reused_lines"] = listed[0]
    task["added_lines"] = listed[1]
    c["surface_list_held"] = True


NO_CHANGE_CLOSE_BODY = (
    "Closing this draft: the implementation lane reported that no change is needed, "
    "so the pull request contains only its empty start commit."
)
NO_CHANGE_BLOCKER = (
    "The implementation lane reported no change; the empty draft pull request was closed."
)


def close_empty_draft(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
) -> list[str]:
    """Close a draft that holds only its start commit and end the task as failed (no-change)."""

    def _commit(
        staged: dict[str, Any], staged_round: dict[str, Any] | None = None
    ) -> None:
        with store.conn.transaction():
            if staged_round is not None:
                store.write(
                    "task_round",
                    "update",
                    staged_round["id"],
                    strip_row(staged_round),
                )
            save_task(store, staged)
        task.clear()
        task.update(staged)

    if coord(task).get("closing_draft") != "no-change":
        staged = copy.deepcopy(task)
        coord(staged)["closing_draft"] = "no-change"
        _commit(staged)

    outcome = "CLOSED"
    if checks_satisfied(TaskFacts(store, task), task, "close_draft"):
        outcome = close_pull_request(
            store,
            worker,
            task,
            runner,
            activity_id=str(
                uuid5(
                    NAMESPACE_URL,
                    f"coordinator-no-change-close:{task['id']}",
                )
            ),
            body=NO_CHANGE_CLOSE_BODY,
        )

    staged = copy.deepcopy(task)
    staged_c = coord(staged)
    staged_c.pop("closing_draft", None)
    if outcome == "MERGED":
        staged_c["phase"] = "await_merge"
        _commit(staged)
        return ["PR merged during the no-change close; verifying the merge"]

    staged_c.pop("lane_outcome", None)
    staged_c["phase"] = "blocked"
    clear_reply_recovery(staged_c)
    staged_c["failed_kind"] = "no-change"
    staged_c["blocker"] = "implementation lane reported no change"
    staged["state"] = "failed"
    staged_round = None
    try:
        round_row = _find_round(
            store,
            str(task["id"]),
            int(task.get("current_round") or 0),
        )
    except CoordinatorError:
        pass
    else:
        if round_row.get("implementer_verdict") is None:
            staged_round = copy.deepcopy(round_row)
            staged_round["implementer_verdict"] = "blocked"
            staged_round["finished_at"] = utcnow()
    _commit(staged, staged_round)

    lines = ["no change on the start commit; draft closed"]
    if not is_order(task):
        lines.extend(
            publish_blocker(
                store,
                worker,
                task,
                runner,
                NO_CHANGE_BLOCKER,
                kind="no-change",
            )
        )
    return lines


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
    replay: dict[str, Any] | None,
    sha: str | None,
    draft_lines: list[str],
) -> list[str]:
    """Apply a persisted implementer outcome. Never relaunches the model."""
    c = coord(task)

    def _mark_applied(target: dict[str, Any]) -> None:
        outcome = target.get("lane_outcome")
        if isinstance(outcome, dict):
            outcome["applied"] = True
            target["lane_outcome"] = outcome

    def _commit_early_outcome(
        *,
        task_state: str,
        blocker: str,
        resume: bool,
        provider_unavailable: bool = False,
    ) -> None:
        """Commit an early round verdict and its consumed checkpoint together."""
        committed_task = copy.deepcopy(task)
        committed_tr = copy.deepcopy(tr)
        committed_c = coord(committed_task)
        committed_tr["implementer_verdict"] = "blocked"
        committed_tr["finished_at"] = utcnow()
        committed_c["phase"] = "blocked"
        if resume:
            committed_c["resume_phase"] = "implement"
        else:
            committed_c.pop("resume_phase", None)
            committed_c.pop("question_activity_id", None)
            committed_c.pop("replies_consumed_through", None)
        committed_c["blocker"] = blocker
        if provider_unavailable:
            committed_c["blocker_publish_once"] = True
            committed_c["blocker_publish_kind"] = "provider-incomplete"
            committed_c.pop("uncertain_lane", None)
        _mark_applied(committed_c)
        committed_task["state"] = task_state
        with store.conn.transaction():
            store.write(
                "task_round",
                "update",
                committed_tr["id"],
                strip_row(committed_tr),
            )
            save_task(store, committed_task)
        task.clear()
        task.update(committed_task)
        tr.clear()
        tr.update(committed_tr)

    def _commit_replay_blocker(blocker: str) -> dict[str, Any]:
        """Consume a replay checkpoint only after its blocker commits."""
        committed_task = copy.deepcopy(task)
        committed_c = coord(committed_task)
        committed_c["phase"] = "blocked"
        committed_c["resume_phase"] = "implement"
        committed_c["blocker"] = blocker
        _mark_applied(committed_c)
        committed_task["state"] = "open"
        with store.conn.transaction():
            save_task(store, committed_task)
        task.clear()
        task.update(committed_task)
        return coord(task)

    if status != "complete":
        retry = lane_retry(
            store,
            worker,
            task,
            phase="implement",
            reason=f"implementer status={status}",
            before_save=lambda cp: cp.pop("lane_outcome", None),
        )
        if retry is not None:
            return retry + draft_lines
        provider_unavailable = status == "unavailable"
        _commit_early_outcome(
            task_state="open",
            blocker=f"implementer status={status}",
            resume=True,
            provider_unavailable=provider_unavailable,
        )
        return publish_blocker(
            store,
            worker,
            task,
            runner,
            (
                "Implementer provider output is unavailable"
                if provider_unavailable
                else f"Implementer returned incomplete status={status}"
            ),
            kind=(
                "provider-incomplete"
                if provider_unavailable
                else "implementer-incomplete"
            ),
            reply_checkpoint=True,
        ) + draft_lines

    if model_result not in _IMPLEMENTER_RESULTS:
        retry = lane_retry(
            store,
            worker,
            task,
            phase="implement",
            reason=f"implementer RESULT={model_result or 'empty'}",
            before_save=lambda cp: cp.pop("lane_outcome", None),
        )
        if retry is not None:
            return retry + draft_lines
        _commit_early_outcome(
            task_state="open",
            blocker=f"implementer invalid RESULT={model_result or 'empty'}",
            resume=True,
        )
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
        pending_q = c.get("pending_question")
        if isinstance(pending_q, str) and pending_q:
            question = pending_q
        try:
            target = comment_target(task)
            if target is None:
                raise CoordinatorError("question has no comment target")
            qid = post_issue_comment(
                store,
                worker,
                runner,
                repo=target[0],
                number=target[1],
                body=f"Question:\n{question}",
                kind="question",
                occurrence=f"{task['id']}:{round_num}",
                target=target[2],
            )
        except (CoordinatorError, StoreError) as exc:
            # Preserve the actual question across draft/publication failures.
            committed_task = copy.deepcopy(task)
            committed_c = coord(committed_task)
            committed_c["pending_question"] = question
            committed_c["phase"] = "blocked"
            committed_c["resume_phase"] = "implement"
            committed_c["blocker"] = f"ask publish failed: {exc}"
            committed_task["state"] = "open"
            with store.conn.transaction():
                save_task(store, committed_task)
            task.clear()
            task.update(committed_task)
            return publish_blocker(
                store,
                worker,
                task,
                runner,
                f"Failed to publish implementer question: {redact(str(exc))}",
                kind="ask-publish",
                reply_checkpoint=True,
            ) + draft_lines
        committed_task = copy.deepcopy(task)
        committed_tr = copy.deepcopy(tr)
        committed_c = coord(committed_task)
        committed_c.pop("pending_question", None)
        committed_c["phase"] = "ask"
        if committed_c.get("question_activity_id") != qid:
            committed_c["replies_consumed_through"] = None
        committed_c["question_activity_id"] = qid
        if is_order(task):
            committed_c["question_text"] = redact(question, limit=1500)
        committed_c["resume_phase"] = "implement"
        outcome = committed_c.get("lane_outcome")
        if isinstance(outcome, dict):
            outcome["applied"] = True
            committed_c["lane_outcome"] = outcome
        committed_task["state"] = "open"
        # Keep the round open while publication is uncertain. Its occurrence ID
        # must remain stable if the process stops after GitHub posts the question.
        committed_tr["implementer_verdict"] = "blocked"
        committed_tr["finished_at"] = utcnow()
        with store.conn.transaction():
            store.write(
                "task_round", "update", committed_tr["id"], strip_row(committed_tr)
            )
            save_task(store, committed_task)
        task.clear()
        task.update(committed_task)
        tr.clear()
        tr.update(committed_tr)
        return [f"ask posted on {target[0]}#{target[1]}"] + draft_lines

    if model_result == "blocked":
        # review-loop: implementer blocked → task failed. Stop.
        # Distinct from RESULT ask (reply-recoverable) and from external
        # blockers (CI authorization / incomplete reviews) which keep a
        # non-failed task and resume_phase + question checkpoint.
        _commit_early_outcome(
            task_state="failed",
            blocker="implementer blocked",
            resume=False,
        )
        return publish_blocker(
            store,
            worker,
            task,
            runner,
            redact(stdout or "implementer blocked"),
            kind="implementer-blocked",
        ) + draft_lines

    if model_result == "no-change":
        if (
            c.get("start_commit")
            and c.get("head_sha") == c["start_commit"]
            and not sha
        ):
            return close_empty_draft(store, worker, task, runner) + draft_lines
        if not c.get("pr_number") and not sha:
            c = _commit_replay_blocker("no-change and no pull request")
            return publish_blocker(
                store,
                worker,
                task,
                runner,
                "No code change and no pull request. Stopping without an empty PR.",
                kind="no-change",
                reply_checkpoint=True,
            ) + draft_lines

    structured_summaries = None
    if isinstance(replay, dict):
        replay_en = replay.get("summary_en")
        replay_de = replay.get("summary_de")
        if isinstance(replay_en, str) and isinstance(replay_de, str):
            structured_summaries = parse_change_summaries(
                f"SUMMARY_EN: {replay_en}\nSUMMARY_DE: {replay_de}\n"
            )
    summaries = structured_summaries or parse_change_summaries(stdout)
    unchanged = model_result == "no-change" and not sha
    if summaries is None and unchanged:
        kept_en = task.get("change_summary_en")
        kept_de = task.get("change_summary_de")
        if (
            isinstance(kept_en, str)
            and kept_en
            and isinstance(kept_de, str)
            and kept_de
        ):
            summaries = (kept_en, kept_de)
    if summaries is None:
        c = _commit_replay_blocker(
            "completed patch lacks concrete English/German change summaries"
        )
        return publish_blocker(
            store,
            worker,
            task,
            runner,
            c["blocker"],
            kind="change-summary",
            reply_checkpoint=True,
        )
    summary_en = redact(summaries[0], limit=800)
    summary_de = redact(summaries[1], limit=800)
    listed: tuple[list[str], list[str]] | None = None
    rules_bound = review_rules_bound(store, worker, runner, task)
    if rules_bound:
        # Structured checkpoint data and a held list both outlive the prefix.
        if _held_surface_list(task) is None:
            listed = _replayed_surface_list(replay) or parse_surface_list(stdout)
            if listed is None and unchanged:
                listed = _replayed_surface_list(
                    {
                        "reused_lines": task.get("reused_lines"),
                        "added_lines": task.get("added_lines"),
                    }
                )
            if listed is None:
                c = _commit_replay_blocker(
                    "completed patch lacks a reused and added list with file and line"
                )
                return publish_blocker(
                    store,
                    worker,
                    task,
                    runner,
                    c["blocker"],
                    kind="surface-list",
                    reply_checkpoint=True,
                )

    committed_task = copy.deepcopy(task)
    committed_tr = copy.deepcopy(tr)
    committed_c = coord(committed_task)
    committed_task["change_summary_en"] = summary_en
    committed_task["change_summary_de"] = summary_de
    if listed is not None:
        committed_task["reused_lines"] = listed[0]
        committed_task["added_lines"] = listed[1]
    if rules_bound:
        refresh_draft_body(store, worker, committed_task, runner)
        committed_c.pop("surface_list_held", None)
    committed_tr["implementer_verdict"] = "done"
    committed_task["state"] = "reviewing"
    committed_c["phase"] = "inner_review"
    committed_c.pop("lane_outcome", None)
    with store.conn.transaction():
        store.write(
            "task_round", "update", committed_tr["id"], strip_row(committed_tr)
        )
        set_checklist(
            store,
            committed_task,
            "implementer_done",
            "ja",
            f"round {round_num} implementer done",
            source="script",
        )
        save_task(store, committed_task)
    task.clear()
    task.update(committed_task)
    tr.clear()
    tr.update(committed_tr)
    return [f"implementer done round={round_num}"] + draft_lines


def implement_commit_message(task: dict[str, Any]) -> str:
    return "Implement the order." if is_order(task) else "Implement assigned issue."


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
        if is_order(task):
            gate = evaluate_action(
                store,
                worker,
                task,
                runner,
                lane_runner,
                action="commit_push",
            )
            if gate is not None:
                return gate
            c = coord(task)
        sha = stage_sign_commit_if_changes(
            store,
            worker,
            runner,
            worktree,
            implement_commit_message(task),
        )
        draft_lines: list[str] = []
        if sha:
            c["head_sha"] = sha
            invalidate_head_evidence(c, sha)
            draft_lines = ensure_draft(store, worker, task, runner)
            if not coord(task).get("pr_number"):
                committed_task = copy.deepcopy(task)
                committed_c = coord(committed_task)
                committed_outcome = copy.deepcopy(pending_outcome)
                committed_c["phase"] = "publish_draft"
                committed_c["resume_phase"] = "implement"
                committed_outcome["sha"] = sha
                committed_c["lane_outcome"] = committed_outcome
                with store.conn.transaction():
                    save_task(store, committed_task)
                task.clear()
                task.update(committed_task)
                return ["implementer outcome recovery; draft pending"] + draft_lines
        else:
            draft_lines = ensure_draft(store, worker, task, runner)
            if not coord(task).get("pr_number") and coord(task).get("phase") == "publish_draft":
                c = coord(task)
                c["resume_phase"] = "implement"
                c["lane_outcome"] = pending_outcome
                save_task(store, task)
                return ["implementer outcome recovery; draft pending"] + draft_lines
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
            replay=pending_outcome,
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
            replay=None,
            sha=None,
            draft_lines=[],
        )

    first_vendor = _first_slot_vendor(task)
    instruction = _implementer_instruction(store, worker, task, runner)
    task_text = read_task_text(worker, task)
    record_body = (
        _spec_context(c, instruction, text=task_text_reference(task_text))
        if len(task_text) > TASK_TEXT_RECORD_BOUND
        else None
    )
    record_round_findings(store, tr, c)
    agent, result = launch_lane(
        store,
        worker,
        task,
        role="implementer",
        vendor=first_vendor,
        round_num=round_num,
        spec_body=_spec_context(c, instruction, text=task_text),
        record_spec_body=record_body,
        runner=runner,
        lane_runner=lane_runner,
    )
    c = coord(task)
    status = result.status
    model_result = result.protocol_result
    # Keep the launch checkpoint intact while supporting mocked/external launch
    # implementations that return without writing it themselves.
    checkpoint = c.get("lane_outcome")
    if not isinstance(checkpoint, dict) or any(
        checkpoint.get(key) != expected
        for key, expected in (
            ("role", "implementer"),
            ("vendor", first_vendor),
            ("round", round_num),
            ("agent_id", agent["id"]),
        )
    ):
        checkpoint = {}
    replay_keys = ("summary_en", "summary_de", "reused_lines", "added_lines")
    if all(key in checkpoint for key in replay_keys):
        replay_fields = {key: checkpoint[key] for key in replay_keys}
    else:
        replay_fields = implementer_replay_fields(
            result.stdout or "",
            config_dir=getattr(result, "config_dir", None),
            path_replacements=_coordinator_path_replacements(store, worker, task),
        )
    for key, value in replay_fields.items():
        if key not in checkpoint:
            checkpoint[key] = value
    checkpoint.update(
        {
            "role": "implementer",
            "vendor": first_vendor,
            "round": round_num,
            "agent_id": agent["id"],
            "status": status,
            "result": model_result,
            "returncode": result.returncode,
            "stdout": redact(result.stdout or ""),
            "applied": False,
        }
    )
    c["lane_outcome"] = checkpoint
    if review_rules_bound(store, worker, runner, task):
        replayed_surfaces = _replayed_surface_list(checkpoint)
        if replayed_surfaces is not None:
            task["reused_lines"] = replayed_surfaces[0]
            task["added_lines"] = replayed_surfaces[1]
            c["surface_list_held"] = True
        else:
            _hold_surface_list(store, worker, task, runner, result.stdout or "")
    save_task(store, task)

    worktree = str(c["worktree"])
    if is_order(task):
        gate = evaluate_action(
            store,
            worker,
            task,
            runner,
            lane_runner,
            action="commit_push",
        )
        if gate is not None:
            return gate
        c = coord(task)
    sha = stage_sign_commit_if_changes(
        store,
        worker,
        runner,
        worktree,
        implement_commit_message(task),
    )
    draft_lines = []
    if worker.a38 and as_int(c.get("pr_number")) is not None:
        # A merge during the lane keeps the published head and its review state.
        left = _a38_pr_left_open(store, worker, task, runner, "implement")
        if left is not None:
            return left
    if sha:
        c["head_sha"] = sha
        invalidate_head_evidence(c, sha)
        draft_lines = ensure_draft(store, worker, task, runner)
        if not coord(task).get("pr_number"):
            committed_task = copy.deepcopy(task)
            committed_c = coord(committed_task)
            # Resume implement after draft so persisted lane_outcome is applied
            # (including ask publish) rather than skipping straight to inner_review.
            committed_c["phase"] = "publish_draft"
            committed_c["resume_phase"] = "implement"
            if status == "complete" and model_result == "ask":
                committed_c["pending_question"] = redact(
                    result.stdout or "Question from implementer."
                )
            with store.conn.transaction():
                save_task(store, committed_task)
            task.clear()
            task.update(committed_task)
            return ["implementer committed; draft pending"] + draft_lines
    else:
        draft_lines = ensure_draft(store, worker, task, runner)
        if not coord(task).get("pr_number") and coord(task).get("phase") == "publish_draft":
            c = coord(task)
            c["resume_phase"] = "implement"
            if status == "complete" and model_result == "ask":
                c["pending_question"] = redact(result.stdout or "Question from implementer.")
            save_task(store, task)
            return ["implementer committed; draft pending"] + draft_lines

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
        replay=(
            c.get("lane_outcome")
            if isinstance(c.get("lane_outcome"), dict)
            else None
        ),
        sha=sha,
        draft_lines=draft_lines,
    )


def _reject_inner_and_reopen(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    tr: dict[str, Any],
    findings: str,
    *,
    agent_id: str,
) -> list[str]:
    committed_task = copy.deepcopy(task)
    committed_tr = copy.deepcopy(tr)
    c = coord(committed_task)
    committed_tr["reviewer_verdict"] = "rejected"
    committed_tr["finished_at"] = utcnow()
    c["findings"] = redact(findings)
    n = int(committed_task.get("current_round") or 0) + 1
    rid = str(uuid.uuid4())
    committed_task["current_round"] = n
    committed_task["state"] = "implementing"
    c["phase"] = "implement"
    with store.conn.transaction():
        store.write(
            "task_round", "update", committed_tr["id"], strip_row(committed_tr)
        )
        store.write(
            "task_round",
            "insert",
            rid,
            {
                "id": rid,
                "task_id": committed_task["id"],
                "round": n,
                "implementer_verdict": None,
                "reviewer_verdict": None,
                "started_at": utcnow(),
                "finished_at": None,
            },
        )
        clear_reviewer_replay_outcomes(committed_task, agent_ids={agent_id})
        save_task(store, committed_task)
    task.clear()
    task.update(committed_task)
    tr.clear()
    tr.update(committed_tr)
    return [f"inner reviewer rejected; new round={n}"]


def _apply_inner_reviewer_replay(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
    tr: dict[str, Any],
    outcome: dict[str, Any],
) -> list[str]:
    """Apply one durable inner-review result without launching another model."""
    status = str(outcome.get("status") or "")
    model_result = str(outcome.get("result") or "")
    stdout = str(outcome.get("stdout") or "")
    stderr = str(outcome.get("stderr") or "")
    agent_id = str(outcome["agent_id"])
    round_num = int(task.get("current_round") or 0)
    if (
        status in ("timeout", "partial", "unavailable")
        or model_result not in _REVIEWER_RESULTS
    ):
        retry = lane_retry(
            store,
            worker,
            task,
            phase="inner_review",
            reason=(
                f"inner reviewer status={status} "
                f"result={model_result or 'empty'}"
            ),
            before_save=lambda cp: clear_reviewer_replay_outcomes(
                {"payload": {"coordinator": cp}}, agent_ids={agent_id}
            ),
        )
        if retry is not None:
            return retry
        committed_task = copy.deepcopy(task)
        committed_c = coord(committed_task)
        committed_c["phase"] = "blocked"
        committed_c["resume_phase"] = "inner_review"
        committed_c["blocker"] = (
            f"inner reviewer incomplete (status={status} "
            f"result={model_result or 'empty'})"
        )
        provider_unavailable = status == "unavailable"
        if provider_unavailable:
            committed_c["blocker_publish_once"] = True
            committed_c["blocker_publish_kind"] = "provider-incomplete"
            committed_c.pop("uncertain_lane", None)
        with store.conn.transaction():
            clear_reviewer_replay_outcomes(committed_task, agent_ids={agent_id})
            save_task(store, committed_task)
        task.clear()
        task.update(committed_task)
        return publish_blocker(
            store,
            worker,
            task,
            runner,
            f"Inner reviewer incomplete (status={status}, "
            f"result={model_result or 'empty'}); not a code rejection.",
            kind=(
                "provider-incomplete"
                if provider_unavailable
                else "reviewer-incomplete"
            ),
            reply_checkpoint=True,
        )
    if not review_is_approved(status, model_result):
        return _reject_inner_and_reopen(
            store,
            worker,
            task,
            tr,
            stdout or stderr or "rejected",
            agent_id=agent_id,
        )

    committed_task = copy.deepcopy(task)
    committed_tr = copy.deepcopy(tr)
    committed_c = coord(committed_task)
    committed_tr["reviewer_verdict"] = "approved"
    committed_tr["finished_at"] = utcnow()
    committed_task["state"] = "local-check"
    committed_c["phase"] = "tests"
    with store.conn.transaction():
        store.write(
            "task_round", "update", committed_tr["id"], strip_row(committed_tr)
        )
        set_checklist(
            store,
            committed_task,
            "reviewer_approved",
            "ja",
            f"round {round_num} approved",
            source="script",
        )
        clear_reviewer_replay_outcomes(committed_task, agent_ids={agent_id})
        save_task(store, committed_task)
    task.clear()
    task.update(committed_task)
    tr.clear()
    tr.update(committed_tr)
    return [f"inner reviewer approved round={round_num}"]


def phase_inner_review(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
    lane_runner: LaneRunner | None,
) -> list[str]:
    cleanup_finished_reviewer_spools(store, worker, task)
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
    head = str(c.get("head_sha") or "")
    replay = reviewer_replay_outcomes(
        store,
        task,
        vendor=_first_slot_vendor(task),
        round_num=round_num,
        head=head,
        stage="inner-review",
    )
    if replay:
        if len(replay) != 1 or replay[0].get("dimension") != "inner":
            raise CoordinatorError("inner reviewer lane outcome checkpoint is invalid")
        return _apply_inner_reviewer_replay(
            store, worker, task, runner, tr, replay[0]
        )
    task["state"] = "reviewing"
    save_task(store, task)
    diff_note = ""
    if head and c.get("base_sha"):
        try:
            diff_path = write_review_diff(store, worker, task, runner, head=head)
            excerpt_path = diff_path.with_suffix(".excerpt.txt")
            excerpt = excerpt_path.read_text(encoding="utf-8")
            base_note = write_review_context(store, worker, task, runner)
            diff_note = (
                f"Script-generated diff artifact: {diff_path}\n"
                f"{COMPLIANCE_PROMPT} Read the attached skills at the base "
                "revision, not from the pull request head.\n"
                f"{base_note}"
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
    agent, _result = launch_lane(
        store,
        worker,
        task,
        role="reviewer",
        vendor=_first_slot_vendor(task),
        round_num=round_num,
        spec_body=_spec_context(
            c,
            "Review the current worktree. Read-only. Do not run Git; use the script diff.\n"
            + diff_note,
        ),
        runner=runner,
        lane_runner=lane_runner,
    )
    if agent.get("status") == "working":
        return ["inner reviewer finished; database checkpoint will be retried"]
    c = coord(task)
    head = str(c.get("head_sha") or "")
    replay = reviewer_replay_outcomes(
        store,
        task,
        vendor=_first_slot_vendor(task),
        round_num=round_num,
        head=head,
        stage="inner-review",
    )
    if len(replay) != 1 or replay[0].get("agent_id") != agent.get("id"):
        raise CoordinatorError("finished inner reviewer has no durable outcome")
    return _apply_inner_reviewer_replay(
        store, worker, task, runner, tr, replay[0]
    )


def phase_tests(store: Store, worker: WorkerConfig, task: dict[str, Any], runner: Runner) -> list[str]:
    if worker.a38:
        return phase_a38_tests(store, worker, task, runner)
    c = coord(task)
    source = c["source"]
    cfg = repo_cfg(worker, str(source["repo"]), task)
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
    if infra_failure(completed.returncode):
        return check_command_unavailable(
            store,
            worker,
            task,
            runner,
            head=head,
            returncode=completed.returncode,
        )
    output = redact_tail((completed.stdout or "") + "\n" + (completed.stderr or ""))
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
            "command": check_label(c, cfg),
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


ALWAYS_PARKED = frozenset(
    {"queued", "ask", "blocked", "await_merge", "await_ready", "await_second_review"}
)
PARK_KEY_PREFIX = "coordinator-park:"


def parked(task: dict[str, Any], worker: WorkerConfig) -> bool:
    """True while the task only waits for an outside event."""
    c = coord(task)
    phase = str(c.get("phase") or "accept")
    return (
        phase in ALWAYS_PARKED
        or (phase == "ci" and c.get("ci_wait") is True)
        or bool(c.get("binding_blocker"))
        or c.get("lane_wait") is True
        or retry_remaining(c, worker) > 0
    )


def observation_due(
    store: Store, task: dict[str, Any], worker: WorkerConfig, *, has_intent: bool = False
) -> bool:
    """True when a waiting task may be observed in this tick."""
    if has_intent or worker.park_poll_seconds == 0:
        return True
    stored = store.sync_get(PARK_KEY_PREFIX + str(task["id"]))
    if stored is None:
        return True
    elapsed = seconds_since(stored)
    return elapsed is None or elapsed >= worker.park_poll_seconds


def mark_observed(store: Store, task: dict[str, Any], worker: WorkerConfig) -> None:
    if worker.park_poll_seconds == 0:
        return
    store.sync_set(PARK_KEY_PREFIX + str(task["id"]), utcnow())


def runnable_count(store: Store, worker: WorkerConfig) -> int:
    """Open coordinator tasks of the worker that are not waiting, plus queued ones."""
    origin = store.device_id()
    count = 0
    for row in store.rows("task"):
        if row.get("_origin_device_id") != origin:
            continue
        if row.get("session_id") != worker.session_id:
            continue
        if _terminal(row):
            continue
        payload = row.get("payload")
        if not isinstance(payload, dict) or not isinstance(payload.get("coordinator"), dict):
            continue
        phase = str(payload["coordinator"].get("phase") or "accept")
        if phase == "queued" or not parked(row, worker):
            count += 1
    return count


def has_runnable_task(store: Store, worker: WorkerConfig) -> bool:
    return runnable_count(store, worker) > 0


# A38 phases in which the guard may already have made the PR Ready, so a human
# can merge before the worker reaches await_merge.
def _a38_pr_left_open(
    store: Store, worker: WorkerConfig, task: dict[str, Any], runner: Runner, phase: str
) -> list[str] | None:
    """Route an A38 task whose PR was merged or closed meanwhile to the merge check."""
    live_state = pr_state(store, worker, task, runner)
    # An unreadable state continues: the push path refuses a PR that is not open.
    if live_state in ("MERGED", "CLOSED"):
        coord(task)["phase"] = "await_merge"
        save_task(store, task)
        verb = "merged" if live_state == "MERGED" else "closed"
        return [f"PR {verb} during {phase}; verifying the merge"]
    return None


A38_MERGEABLE_WAIT_PHASES = frozenset({"await_second_review", "ci", "readiness", "await_ready"})
A38_PUBLISHED_RERUN_PHASES = frozenset(
    {"implement", "inner_review", "tests", "publish_draft", "pr_gates_grok", "pr_gates_codex", "a38_first_comment", "a38_second_comment"}
)
NON_REPEATABLE_REJECTION_CHECKPOINT_ERRORS = frozenset(
    {
        "rejection review checkpoint is invalid",
        "rejection review intent is missing or foreign",
    }
)


def _second_review_intake_allowed(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    c: dict[str, Any],
    runner: Runner,
    phase: str,
) -> bool | None:
    """Intake runs on an open PR (in await_merge only while still assigned), and in
    await_merge also after the merge, which closes the issue; a blocking result
    there never reopens work. None means the state is unreadable."""
    state = pr_state(store, worker, task, runner)
    if state is None:
        return None
    if state == "OPEN":
        return phase != "await_merge" or _source_still_assigned(store, worker, c, runner)
    return phase == "await_merge" and state == "MERGED"


def _verify_source_assigned(
    store: Store, worker: WorkerConfig, c: dict[str, Any], runner: Runner
) -> None:
    if source_kind(c) == ORDER_KIND:
        return
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


def _source_still_assigned(
    store: Store, worker: WorkerConfig, c: dict[str, Any], runner: Runner
) -> bool:
    """await_merge skips the revocation stop; a revoked task posts no new review comments."""
    try:
        _verify_source_assigned(store, worker, c, runner)
    except CoordinatorError:
        return False
    return True


def phase_handlers(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
    lane_runner: LaneRunner | None,
) -> dict[str, Callable[[], list[str]]]:
    """The handler of each coordinator phase, bound to one task."""
    return {
        "accept": lambda: (
            phase_accept_order(store, worker, task, runner)
            if is_order(task)
            else phase_accept(store, worker, task, runner)
        ),
        "checkout": lambda: phase_checkout(store, worker, task, runner),
        "start_draft": lambda: phase_start_draft(store, worker, task, runner),
        "implement": lambda: phase_implement(store, worker, task, runner, lane_runner),
        "inner_review": lambda: phase_inner_review(store, worker, task, runner, lane_runner),
        "tests": lambda: phase_tests(store, worker, task, runner),
        "publish_draft": lambda: phase_publish_draft(store, worker, task, runner),
        "pr_gates_grok": lambda: phase_pr_gates_grok(store, worker, task, runner, lane_runner),
        "pr_gates_codex": lambda: phase_pr_gates_codex(store, worker, task, runner, lane_runner),
        "a38_first_comment": lambda: phase_a38_first_comment(store, worker, task, runner),
        "a38_second_comment": lambda: phase_a38_second_comment(
            store, worker, task, runner
        ),
        "await_second_review": lambda: phase_await_second_review(store, worker, task, runner),
        "ci": lambda: phase_ci(store, worker, task, runner),
        "readiness": lambda: phase_readiness(store, worker, task, runner),
        "await_ready": lambda: phase_a38_await_ready(store, worker, task, runner),
        "formal_approve": lambda: phase_formal_approve(store, worker, task, runner),
        "leave_draft": lambda: phase_leave_draft(store, worker, task, runner),
        "await_merge": lambda: phase_await_merge(store, worker, task, runner),
        "ask": lambda: phase_read_replies(store, worker, task, runner),
        "blocked": lambda: phase_blocked(store, worker, task, runner),
        "done": lambda: ["done"],
    }


_LANE_PHASES = ("implement", "inner_review", "pr_gates_grok", "pr_gates_codex")


def _phase_of_newest_lane(store: Store, task: dict[str, Any]) -> str:
    """The phase that starts the lane whose outcome was uncertain."""
    lanes = [
        row
        for row in store.rows("agent")
        if row.get("task_id") == task["id"]
        and (
            row.get("role") in ("implementer", "reviewer")
            or str(row.get("role") or "").startswith("pr-reviewer")
        )
    ]
    if not lanes:
        stored = str(coord(task).get("resume_phase") or "")
        return stored if stored in _LANE_PHASES else "implement"
    row = max(lanes, key=lambda value: str(value.get("started_at") or ""))
    role = str(row.get("role") or "")
    if role == "implementer":
        return "implement"
    if role == "reviewer":
        return "inner_review"
    stage = canonical_stage(str(row.get("stage") or ""))
    return (
        "pr_gates_codex"
        if stage == SLOT_STAGES["second"]
        else "pr_gates_grok"
    )


def _recover_uncertain_lane(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
) -> list[str]:
    """Recover a task that an earlier version stopped with the flag uncertain_lane."""
    if recover_working_lanes(store, worker, task) == "waiting":
        return ["a lane of an earlier tick is still running; waiting"]
    resume = _phase_of_newest_lane(store, task)

    def clear(cp: dict[str, Any]) -> None:
        cp.pop("uncertain_lane", None)
        cp.pop("resume_phase", None)
        cp.pop("blocker", None)

    lines = lane_retry(
        store,
        worker,
        task,
        phase=resume,
        reason="lane outcome was uncertain",
        before_save=clear,
    )
    if lines is not None:
        return lines
    c = coord(task)
    clear(c)
    c["phase"] = "blocked"
    c["resume_phase"] = resume
    c["blocker"] = f"Model lane interrupted in {resume}; not a code rejection."
    save_task(store, task)
    return publish_blocker(
        store,
        worker,
        task,
        runner,
        c["blocker"],
        kind="lane-failed",
        reply_checkpoint=True,
    )


def _advance_task(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    *,
    runner: Runner,
    lane_runner: LaneRunner | None,
    intents=(),
    intents_only: bool = False,
) -> list[str]:
    if is_order(task):
        if intents:
            consumed = consume_order_intents(
                store, worker, task, runner, list(intents)
            )
            if consumed is not None:
                return consumed
        if intents_only:
            return []
    c = coord(task)
    if str(c.get("phase") or "accept") == "queued":
        return [f"order queued {order_id(task)}"]
    if c.get("uncertain_lane"):
        return _recover_uncertain_lane(store, worker, task, runner)
    if c.get("closing_draft"):
        return close_empty_draft(store, worker, task, runner)
    phase = str(c.get("phase") or "accept")
    source = c.get("source") or {}
    ordering_error = gate_ordering_unavailable(store, task, phase=phase)
    if ordering_error is not None:
        return [ordering_error]
    pending_post_suite = worker.a38 and post_suite_checkpoint_pending(task)
    # A rejection comment is a durable task intent, not an implementation-phase
    # side effect. Reconcile it before every dispatch or early PR-state branch,
    # including checkpoint recovery and its unreadable-state retry.
    rejection_recovery_error = False
    try:
        rejection_publication_lines = reconcile_rejection_review_intents(
            store, worker, task, runner
        )
    except (CoordinatorError, StoreError) as exc:
        message = str(exc)
        if message in NON_REPEATABLE_REJECTION_CHECKPOINT_ERRORS:
            c["rejection_review_checkpoint_audit"] = {
                "checkpoint": copy.deepcopy(c.get("rejection_review_activity_ids")),
                "error": message,
            }
            c.pop("rejection_review_activity_ids", None)
        if not c.get("resume_phase") and phase not in ("done", "blocked", "ask"):
            c["resume_phase"] = phase
        c["blocker"] = message
        if phase != "done":
            c["phase"] = "blocked"
            phase = "blocked"
        save_task(store, task)
        blocker_lines = publish_blocker(
            store,
            worker,
            task,
            runner,
            message,
            kind="phase-error",
            reply_checkpoint=True,
        )
        lane_observations = c.pop("lane_observations", [])
        if not isinstance(lane_observations, list):
            lane_observations = []
        elif lane_observations:
            save_task(store, task)
        rejection_publication_lines = [
            *(str(item) for item in lane_observations),
            *blocker_lines,
        ]
        rejection_recovery_error = True
        if not pending_post_suite:
            return rejection_publication_lines
    pending_post_suite_state: str | None = None
    if pending_post_suite:
        # A saved suite may resume publication only after a fresh state read.
        # Open pull requests still require the current execution binding and
        # source assignment; merge/close is the only no-assignment finalization.
        pending_post_suite_state = pr_state(store, worker, task, runner)
        if pending_post_suite_state is None:
            return [
                *rejection_publication_lines,
                "PR state temporarily unavailable before A38 checkpoint recovery; retrying"
            ]
        if phase == "blocked" and pending_post_suite_state in ("MERGED", "CLOSED"):
            # A checkpointed suite has publish-or-retire work that does not need
            # a reply once the pull request leaves OPEN.
            phase = "tests"
            c["phase"] = "tests"
    if rejection_recovery_error and phase == "blocked":
        return rejection_publication_lines
    pending_provider_checkpoint = (
        phase == "blocked"
        and c.get("blocker_publish_once") is True
        and c.get("blocker_publish_kind") == "provider-incomplete"
    )
    final_blocker = phase == "blocked" and (
        bool(c.get("uncertain_lane"))
        or not c.get("resume_phase")
        or pending_provider_checkpoint
    )
    if (
        worker.a38
        and as_int(c.get("pr_number")) is not None
        and phase not in ("await_merge", "done")
        and not final_blocker
        and not pending_post_suite
    ):
        # Once the pull request exists the guard may make it Ready and a human may
        # merge it in any phase, also while asking or recoverably blocked; check that
        # first. A non-recoverable or uncertain blocker stays final even if stale
        # recovery metadata is present. A provider-incomplete checkpoint also stays
        # blocked until its queued publication is verified and an authorized reply
        # consumes the checkpoint; otherwise a restart could strand or reorder it.
        live_state = pr_state(store, worker, task, runner)
        if live_state in ("MERGED", "CLOSED"):
            if live_state == "MERGED" and _source_still_assigned(
                store, worker, c, runner
            ):
                reconcile_a38_publication_intents(
                    store, worker, task, runner
                )
            c["phase"] = "await_merge"
            save_task(store, task)
            verb = "merged" if live_state == "MERGED" else "closed"
            return [
                *rejection_publication_lines,
                f"PR {verb} during {phase}; verifying the merge",
            ]
        if live_state is None and phase in A38_PUBLISHED_RERUN_PHASES:
            return [
                *rejection_publication_lines,
                f"PR state temporarily unavailable before {phase}; retrying",
            ]
    try:
        if pending_post_suite and pending_post_suite_state in ("MERGED", "CLOSED"):
            pass
        elif (
            source.get("owner_scoped")
            and (
                phase == "accept"
                or (phase == "blocked" and c.get("resume_phase") == "accept")
            )
            and not c.get("execution_binding")
        ):
            if source.get("require_hub_assignment") is not worker.require_hub_assignment:
                raise CoordinatorError("task hub-assignment requirement changed before acceptance")
            # Owner-scoped acceptance has no repository configuration yet, so it
            # is bound once the accepted task pins its publication route.
            pass
        else:
            refresh_execution_binding(store, worker, task)
    except (CoordinatorError, StoreError) as exc:
        message = str(exc)
        binding_blocker_activity_id = c.get("binding_blocker_activity_id")
        if (
            c.get("binding_blocker") == message
            and isinstance(binding_blocker_activity_id, str)
            and binding_blocker_activity_id
        ):
            return [*rejection_publication_lines, f"blocked: {redact(message)}"]
        if c.get("binding_blocker") != message:
            c.pop("binding_blocker_activity_id", None)
        c["binding_blocker"] = message
        save_task(store, task)
        return [
            *rejection_publication_lines,
            *publish_blocker(
                store,
                worker,
                task,
                runner,
                message,
                kind="configuration-binding",
                preserve_reply_checkpoint=True,
                binding_notice=True,
            ),
        ]
    if "binding_blocker" in c or "binding_blocker_activity_id" in c:
        restored = bool(c.get("binding_blocker"))
        c.pop("binding_blocker", None)
        c.pop("binding_blocker_activity_id", None)
        save_task(store, task)
        if restored and phase not in ALWAYS_PARKED and phase != "ci":
            return list(rejection_publication_lines)
    # Once an A38 pull request exists the guard may make it Ready (also before a rerun
    # after a blocking review), so every later phase checks for a human merge first.
    mergeable_wait = worker.a38 and (
        phase in A38_MERGEABLE_WAIT_PHASES
        or (phase in A38_PUBLISHED_RERUN_PHASES and as_int(c.get("pr_number")) is not None)
    )
    # Stop new effects when assignment is revoked. await_merge may continue
    # observation only; formal_approve / leave_draft must not proceed.
    resume_post_suite_after_assignment = False
    if (
        phase not in ("await_merge", "done", "blocked", "ask")
        and (not pending_post_suite or pending_post_suite_state == "OPEN")
    ):
        try:
            _verify_source_assigned(store, worker, c, runner)
        except CoordinatorError as exc:
            if mergeable_wait:
                live_state = pr_state(store, worker, task, runner)
                if live_state is None:
                    # A merge also unassigns the closed issue; decide only on a readable state.
                    return [
                        *rejection_publication_lines,
                        f"PR state temporarily unavailable during {phase}; retrying",
                    ]
                if live_state in ("MERGED", "CLOSED"):
                    if pending_post_suite:
                        # The saved suite must publish or retire against its live
                        # context before merge verification can consume it.
                        pending_post_suite_state = live_state
                        phase = "tests"
                        c["phase"] = "tests"
                        resume_post_suite_after_assignment = True
                    else:
                        # A merge closes the issue; await_merge verifies the human merge
                        # (and blocks a pull request that was closed without one).
                        c["phase"] = "await_merge"
                        save_task(store, task)
                        return [
                            *rejection_publication_lines,
                            f"PR left open state during {phase}; verifying the merge",
                        ]
            if not resume_post_suite_after_assignment:
                c["phase"] = "blocked"
                c["resume_phase"] = (
                    phase
                    if phase not in ("blocked", "ask", "done")
                    else "implement"
                )
                c["blocker"] = str(exc)
                save_task(store, task)
                return [
                    *rejection_publication_lines,
                    *publish_blocker(
                        store,
                        worker,
                        task,
                        runner,
                        str(exc),
                        kind="stopped",
                        reply_checkpoint=True,
                    ),
                ]
    handlers = phase_handlers(store, worker, task, runner, lane_runner)
    handler = handlers.get(phase)
    if handler is None:
        raise CoordinatorError(f"unknown coordinator phase {phase}")
    try:
        intake_lines: list[str] | None = None
        if worker.a38 and phase in {
            "ci",
            "readiness",
            "await_ready",
            "leave_draft",
            "await_merge",
        }:
            a38 = c.get("a38")
            second = a38.get("second_review") if isinstance(a38, dict) else None
            current = str(c.get("head_sha") or "")
            # Intake runs on an open PR and, in await_merge, also after the merge;
            # a result that arrives after a merge or close never reopens work.
            # Validate late mail against the live PR head: after a push the phase handler
            # invalidates evidence, and a result for the old head must not reroute the task.
            # An unreadable head or state retries the tick: skipping intake would let the
            # phase decide on a stored review while a newer one is still waiting.
            unreadable = [f"PR state temporarily unavailable before second-review intake in {phase}; retrying"]
            if isinstance(second, dict) and second.get("head") == current:
                live = fresh_pr_head(store, worker, task, runner)
                if live is None:
                    return unreadable
                if live != current:
                    second = None
            allowed = (
                _second_review_intake_allowed(store, worker, task, c, runner, phase)
                if isinstance(second, dict) and second.get("head") == current
                else False
            )
            if allowed is None:
                return unreadable
            if allowed:
                # A prior tick may have persisted the phase transition before
                # completing a saved comment/ping occurrence.  CLOSED and a
                # revoked OPEN task reach no publication side effect here.
                reconcile_a38_publication_intents(store, worker, task, runner)
                intake_lines = intake_second_reviews(store, worker, task, runner, current)
                if coord(task).get("phase") != phase:
                    return intake_lines or [
                        "blocking external second review; routing to implementer"
                    ]
        finalizing = pending_post_suite and pending_post_suite_state in (
            "MERGED",
            "CLOSED",
        )
        action = None if finalizing else PHASE_ACTION.get(phase)
        if action is not None:
            gate = evaluate_action(
                store,
                worker,
                task,
                runner,
                lane_runner,
                action=action,
            )
            if gate is not None:
                return [
                    *rejection_publication_lines,
                    *(intake_lines or []),
                    *gate,
                ]
        handler_lines = handler()
        c = coord(task)
        if pending_post_suite and c.get("phase") == "await_merge":
            clear_reply_recovery(c)
            save_task(store, task)
        lines = [
            *rejection_publication_lines,
            *(intake_lines or []),
            *handler_lines,
        ]
        lane_observations = c.pop("lane_observations", [])
        if isinstance(lane_observations, list) and lane_observations:
            save_task(store, task)
            return [*(str(item) for item in lane_observations), *lines]
        return lines
    except A38ReportCheckpointConflict:
        # The report checkpoint reloaded a newer task than this tick holds.
        # Replace the caller snapshot and retry without publishing or saving it.
        fresh = store.row("task", str(task["id"]))
        if fresh is not None:
            task.clear()
            task.update(fresh)
        return [
            *rejection_publication_lines,
            "A38 report checkpoint changed concurrently; retrying",
        ]
    except GateOrderingUnavailableError as exc:
        return [str(exc)]
    except CiObservedFailureError as exc:
        # Ready-side observed actual failed CI: hand off to script phase_ci.
        # Do not reply-gate on the Ready phase, and do not let same-phase retry
        # setters overwrite this transition. Next tick fetches logs / routes
        # implement (or preserves logs-inaccessible external blocker).
        c = coord(task)
        evidence = c.get("evidence")
        if isinstance(evidence, dict):
            evidence["ci_green"] = False
        c["phase"] = "ci"
        c["resume_phase"] = None
        c["blocker"] = None
        save_task(store, task)
        lane_observations = c.pop("lane_observations", [])
        if not isinstance(lane_observations, list):
            lane_observations = []
        elif lane_observations:
            save_task(store, task)
        return [
            *(str(item) for item in lane_observations),
            str(exc) or "CI failed on recheck; returning to CI observation",
        ]
    except NonRecoverableCoordinatorError as exc:
        # Unsupported or final coordinator boundaries cannot be made safe by
        # an authorized issue reply.  Persist a final blocker without a reply
        # checkpoint so later ticks remain stopped.
        c = coord(task)
        c["phase"] = "blocked"
        c["blocker"] = str(exc)
        clear_reply_recovery(c)
        save_task(store, task)
        blocker_lines = publish_blocker(
            store,
            worker,
            task,
            runner,
            str(exc),
            kind="non-recoverable",
        )
        lane_observations = c.pop("lane_observations", [])
        if not isinstance(lane_observations, list):
            lane_observations = []
        elif lane_observations:
            save_task(store, task)
        return [
            *(str(item) for item in lane_observations),
            *blocker_lines,
        ]
    except LaneFailedError as exc:
        retry_lines = lane_retry(
            store, worker, task, phase=phase, reason=str(exc)
        )
        c = coord(task)
        if retry_lines is None:
            c["phase"] = "blocked"
            c["resume_phase"] = phase
            c["blocker"] = (
                f"Model lane interrupted in {phase}; not a code rejection."
            )
            save_task(store, task)
            replies = coord(task).get("authorized_replies")
            count = len(replies) if isinstance(replies, list) else 0
            retry_lines = publish_blocker(
                store,
                worker,
                task,
                runner,
                c["blocker"],
                kind="lane-failed",
                reply_checkpoint=True,
                occurrence=f"{task['id']}:{count}",
            )
        lane_observations = c.pop("lane_observations", [])
        if not isinstance(lane_observations, list):
            lane_observations = []
        elif lane_observations:
            save_task(store, task)
        return [
            *(str(item) for item in lane_observations),
            *retry_lines,
        ]
    except (CoordinatorError, StoreError) as exc:
        # Task-specific failures must become GitHub-visible blockers when possible.
        if isinstance(exc, DraftHeadNotVerifiedError):
            retry_lines = lane_retry(
                store, worker, task, phase=phase, reason=str(exc)
            )
            if retry_lines is not None:
                c = coord(task)
                lane_observations = c.pop("lane_observations", [])
                if not isinstance(lane_observations, list):
                    lane_observations = []
                elif lane_observations:
                    save_task(store, task)
                return [
                    *(str(item) for item in lane_observations),
                    *retry_lines,
                ]
        c = coord(task)
        if not c.get("resume_phase") and phase not in ("await_merge", "done", "blocked", "ask"):
            c["resume_phase"] = phase
        c["blocker"] = str(exc)
        if phase not in ("await_merge", "done"):
            c["phase"] = "blocked"
        save_task(store, task)
        blocker_lines = publish_blocker(
            store,
            worker,
            task,
            runner,
            str(exc),
            kind="phase-error",
            reply_checkpoint=True,
        )
        lane_observations = c.pop("lane_observations", [])
        if not isinstance(lane_observations, list):
            lane_observations = []
        elif lane_observations:
            save_task(store, task)
        return [
            *(str(item) for item in lane_observations),
            *blocker_lines,
        ]
    except Exception as exc:  # noqa: BLE001 — never escape as silent tick failure
        c = coord(task)
        if not c.get("resume_phase") and phase not in ("await_merge", "done", "blocked", "ask"):
            c["resume_phase"] = phase
        c["blocker"] = redact(str(exc))
        if phase not in ("await_merge", "done"):
            c["phase"] = "blocked"
        save_task(store, task)
        blocker_lines = publish_blocker(
            store,
            worker,
            task,
            runner,
            redact(str(exc)),
            kind="phase-error",
            reply_checkpoint=True,
        )
        lane_observations = c.pop("lane_observations", [])
        if not isinstance(lane_observations, list):
            lane_observations = []
        elif lane_observations:
            save_task(store, task)
        return [
            *(str(item) for item in lane_observations),
            *blocker_lines,
        ]


def _after_advance(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    before: str,
    *,
    intents_only: bool,
) -> list[str]:
    """Maintain the scheduler flags of one visited task; save only on a change."""
    if not _terminal(task):
        c = coord(task)
        now = str(c.get("phase") or "accept")
        changed = False
        retry = c.get("retry")
        if isinstance(retry, dict) and retry.get("phase") != now:
            c.pop("retry", None)
            changed = True
        if now not in ("ask", "blocked"):
            for key in ("failed_check", "question_text"):
                if key in c:
                    c.pop(key, None)
                    changed = True
        if now == "ci":
            if (
                before == "ci"
                and not intents_only
                and c.get("ci_wait") is not True
            ):
                c["ci_wait"] = True
                changed = True
        elif "ci_wait" in c:
            del c["ci_wait"]
            changed = True
        if changed:
            save_task(store, task)
    if is_order(task):
        reconcile_order_events(store, worker, task)
    return []


def _advance_guarded(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    *,
    runner: Runner,
    lane_runner: LaneRunner | None,
    intents=(),
    intents_only: bool = False,
) -> list[str]:
    """Visit one task; an error of this task must not stop the tick."""
    before = str(coord(task).get("phase") or "accept")
    try:
        if intents or intents_only:
            lines = _advance_task(
                store,
                worker,
                task,
                runner=runner,
                lane_runner=lane_runner,
                intents=intents,
                intents_only=intents_only,
            )
        else:
            lines = _advance_task(
                store, worker, task, runner=runner, lane_runner=lane_runner
            )
    except CoordinatorError as exc:
        lines = [f"blocked: {redact(str(exc))}"]
    except StoreError as exc:
        lines = [f"store error: {redact(str(exc))}"]
    except Exception as exc:  # noqa: BLE001 — one task must not stop the tick
        lines = [f"error: {redact(str(exc))}"]
    try:
        lines = [*lines, *_after_advance(store, worker, task, before, intents_only=intents_only)]
    except StoreError as exc:
        lines = [*lines, f"store error: {redact(str(exc))}"]
    except Exception as exc:  # noqa: BLE001
        lines = [*lines, f"error: {redact(str(exc))}"]
    return lines


def advance_one(
    store: Store,
    worker: WorkerConfig,
    *,
    runner: Runner,
    lane_runner: LaneRunner | None,
) -> list[str]:
    lines: list[str] = []
    tasks = _open_tasks(store, worker.session_id)
    orders_active = worker.orders or any(is_order(candidate) for candidate in tasks)
    if orders_active:
        lines.extend(sweep_terminal_orders(store, worker))
    intents = pending_order_intents(store, worker) if orders_active else {}
    touched: set[str] = set()
    for task in tasks:  # 1. waiting tasks and pending intents
        tid = str(task["id"])
        task_intents = intents.pop(tid, []) if is_order(task) else []
        if not parked(task, worker):
            if task_intents:
                lines.extend(
                    _advance_guarded(
                        store,
                        worker,
                        task,
                        runner=runner,
                        lane_runner=lane_runner,
                        intents=task_intents,
                        intents_only=True,
                    )
                )
                if any(
                    intent["payload"].get("kind") == "cancel"
                    for intent in task_intents
                ):
                    touched.add(tid)
            continue
        idle = (
            coord(task).get("phase") == "queued"
            or retry_remaining(coord(task), worker) > 0
        )
        if idle and not task_intents:
            continue
        if not observation_due(
            store, task, worker, has_intent=bool(task_intents)
        ):
            continue
        lines.extend(
            _advance_guarded(
                store,
                worker,
                task,
                runner=runner,
                lane_runner=lane_runner,
                intents=task_intents,
                intents_only=idle,
            )
        )
        mark_observed(store, task, worker)
        touched.add(tid)
    for leftover_order, leftover in intents.items():  # answers of ended orders
        ended = find_order(store, leftover_order)
        if (
            ended is None
            or not _terminal(ended)
            or ended.get("session_id") != worker.session_id
        ):
            continue
        lines.extend(
            _advance_guarded(
                store,
                worker,
                ended,
                runner=runner,
                lane_runner=lane_runner,
                intents=leftover,
                intents_only=True,
            )
        )

    runnable = [t for t in tasks if not _terminal(t) and not parked(t, worker)]
    if not runnable:
        queued = [
            t
            for t in tasks
            if not _terminal(t) and coord(t).get("phase") == "queued"
        ]
        if queued:
            coord(queued[0])["phase"] = "accept"
            save_task(store, queued[0])
            runnable = [queued[0]]
    candidates = [t for t in runnable if str(t["id"]) not in touched]
    if candidates:  # 2. one phase of the oldest runnable task
        lines.extend(
            _advance_guarded(
                store,
                worker,
                candidates[0],
                runner=runner,
                lane_runner=lane_runner,
            )
        )
    return lines
