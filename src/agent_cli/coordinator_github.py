"""GitHub activities, CI observation, formal approve, Ready, and merge."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from .ai_accounts import load_ai_accounts
from .allow import GATE_PAIRS, both_second_reviews_na, evaluate_allow
from .model_slots import (
    ModelSlots,
    ModelSlotsError,
    canonical_checklist,
    canonical_stage,
    implementer_from_snapshot,
    models_from_snapshot,
)
from .coordinator_common import (
    ACCEPT_BODY,
    ACCEPT_MARKER_PREFIX,
    CI_ACTION_REQUIRED,
    CI_PENDING,
    CI_SUCCESS,
    CiInventoryProtocolError,
    CiObservationProtocolError,
    CiObservationTransportError,
    CiObservedFailureError,
    CoordinatorError,
    QUESTION_MARKER_PREFIX,
    STATUS_MARKER_PREFIX,
    Runner,
    account_for,
    as_int,
    coord,
    coordinator_env,
    gh_json,
    gh_list,
    is_sha,
    redact,
    save_task,
    scoped,
    strip_row,
    target_repo,
    text,
)
from .coordinator_config import RepositoryConfig, WorkerConfig, validate_base
from .coordinator_exec import run_bounded
from .coordinator_git import (
    execute_github,
    queue_activity,
    repo_cfg,
    verify_signed_clean_head,
)
from .coordinator_lanes import invalidate_head_evidence, latest_gates, set_checklist
from .github_act import (
    ACTIVITY_MARKER,
    REVIEW_APPROVE_COMMIT_MISMATCH,
    REVIEW_APPROVE_NON_APPROVED,
)
from .github_accounts import AccountError
from .hub import Hub, HubError
from .pr_guard_config import PrGuardLookupError, fetch_trusted_pr_guard, one_review_for
from .pr_lifecycle import (
    a38_accepts_skipped_or_neutral,
    a38_passed_job_names_from_comment,
)
from .store import Store, StoreError, utcnow

# Executor error strings that prove an observed same-marker APPROVE rejection.
_FORMAL_OBSERVED_REJECTION_ERRORS = frozenset(
    {REVIEW_APPROVE_NON_APPROVED, REVIEW_APPROVE_COMMIT_MISMATCH}
)

# Fixed JSON contract for configured readiness_argv (trusted operator script).
# Tied to exact HEAD and base. Not model/repo input and not a policy DSL.
READINESS_CONTRACT = (
    '{"head":"<40-hex>","base":"<40-hex-pinned-base>",'
    '"contributing_ok":true,'
    '"deviation":{"declared":false}'
    "|{\"declared\":true,\"granted\":true,\"granted_by\":\"<reply_login>\","
    '"evidence":"<human grant provenance>"}}'
)

HUB_ASSIGNMENT_GRACE_SECONDS = 120
HUB_CONFIGURATION_BLOCKER = (
    "Hub assignment verification is refused by the hub or the device is not paired; "
    "fix the device pairing or hub configuration, then reply here."
)
HUB_ASSIGNMENT_QUERY_CAP = 500
HUB_ASSIGNMENT_RETRY_PUBLISH_SECONDS = 3600
FORK_VISIBILITY_GRACE_SECONDS = 300
MANDATE_DEVIATION_BLOCKER = (
    'mandate has no explicit deviation statement; issue the assignment through '
    'the hub with "no deviation authorized"'
)


@dataclass(frozen=True)
class HubAssignmentMandate:
    row_id: str
    actor: str
    origin: str
    issue_key: str
    deviation: object


def _task_models(task: dict[str, Any]) -> ModelSlots:
    """Model slots used to inspect recorded gates, including legacy tasks."""
    try:
        # Recorded-state read: readiness checks inspect gates already on the task.
        return models_from_snapshot(task)
    except ModelSlotsError as exc:
        raise CoordinatorError(str(exc)) from exc



def acceptance_marker(repo: str, number: int, session_id: str) -> str:
    return f"{ACCEPT_MARKER_PREFIX}{repo}#{number}:{session_id} -->"


def post_issue_comment(
    store: Store,
    worker: WorkerConfig,
    runner: Runner,
    *,
    repo: str,
    number: int,
    body: str,
    kind: str,
    occurrence: str = "",
) -> str:
    """Publish a deterministic idempotent source-issue status/question comment."""
    safe = redact(body, limit=2000)
    activity_id = str(
        uuid5(
            NAMESPACE_URL,
            f"coordinator-{kind}:{worker.session_id}:{repo}:{number}:{occurrence}:{safe}",
        )
    )
    marker = f"{STATUS_MARKER_PREFIX}{kind}:{activity_id} -->"
    if kind == "question":
        marker = f"{QUESTION_MARKER_PREFIX}{activity_id} -->"
    queue_activity(
        store,
        activity_id=activity_id,
        session_id=worker.session_id,
        typ="comment.post",
        payload={"repo": repo, "number": number, "body": f"{safe}\n{marker}", "target": "issue"},
    )
    execute_github(store, runner, activity_ids=(activity_id,))
    row = store.row("activity", activity_id)
    if row is None or row.get("execution_status") != "done":
        err = ""
        if row is not None:
            err = redact(str(row.get("execution_error") or row.get("execution_status") or "pending"))
        raise CoordinatorError(f"issue comment {kind} not verified published: {err}")
    return activity_id


def reply_checkpoint_eligible(task: dict[str, Any], c: dict[str, Any] | None = None) -> bool:
    """True when task state + resume_phase make a reply checkpoint recoverable.

    Terminal implementer ``RESULT: blocked`` (failed), terminal ``done``, and
    uncertain-lane outcomes stay ineligible. A missed ``reply_checkpoint=True``
    must not wedge a path that already pinned a safe ``resume_phase``.
    """
    inner = c if isinstance(c, dict) else coord(task)
    if inner.get("uncertain_lane"):
        return False
    if task.get("state") in ("failed", "done"):
        return False
    resume = inner.get("resume_phase")
    if not isinstance(resume, str) or not resume or resume in ("ask", "blocked", "done"):
        return False
    return True


def clear_reply_recovery(c: dict[str, Any]) -> None:
    """Drop stale question/checkpoint/resume fields for non-recoverable outcomes."""
    c.pop("question_activity_id", None)
    c.pop("resume_phase", None)
    c.pop("replies_consumed_through", None)


def publish_blocker(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
    message: str,
    *,
    kind: str = "blocker",
    reply_checkpoint: bool = False,
    preserve_reply_checkpoint: bool = False,
    binding_notice: bool = False,
    occurrence: str | None = None,
) -> list[str]:
    """Publish a source-issue blocker.

    Recoverable blockers pin ``question_activity_id`` so ``phase_read_replies``
    can resume the exact ``resume_phase``. Eligibility follows task state and
    ``resume_phase`` so a missed boolean cannot silently wedge another
    recoverable path. Status re-publishes while waiting keep an existing
    checkpoint and must not reset consumed replies for that same checkpoint.
    Execution-binding notices use ``preserve_reply_checkpoint`` because they
    recover without an authorized reply when the pinned binding is restored.
    ``binding_notice`` keeps their publication state separate from the phase
    blocker publication state.
    """
    c = coord(task)
    source = c.get("source") if isinstance(c.get("source"), dict) else None
    lines = [f"blocked: {redact(message)}"]
    if source is None:
        return lines
    eligible = reply_checkpoint_eligible(task, c)
    prior = c.get("question_activity_id")
    has_prior = isinstance(prior, str) and bool(prior)
    # Explicit request (re)pins when eligible; otherwise auto-pin only when a
    # recoverable resume_phase is set and no checkpoint exists yet.
    should_pin = (
        not preserve_reply_checkpoint
        and eligible
        and (reply_checkpoint or not has_prior)
    )
    resolved_occurrence = occurrence or ""
    if occurrence is None and should_pin and c.get("resume_phase") == "formal_approve":
        # Every invalidated approval needs a reply after its own blocker, even
        # when a later dismissal has the same wording as an earlier one.
        resolved_occurrence = (
            f"{task['id']}:{c.get('head_sha')}:{c.get('formal_approve_attempt', 0)}"
        )
    try:
        activity_id = post_issue_comment(
            store,
            worker,
            runner,
            repo=str(source["repo"]),
            number=int(source["number"]),
            body=f"Blocked: {redact(message)}",
            kind=kind,
            occurrence=resolved_occurrence,
        )
        changed = False
        if binding_notice:
            c["binding_blocker_activity_id"] = activity_id
            changed = True
        elif c.get("blocker_publish_once"):
            c["blocker_activity_id"] = activity_id
            changed = True
        if should_pin:
            prior = c.get("question_activity_id")
            c["question_activity_id"] = activity_id
            # Same idempotent blocker activity must not re-open already consumed
            # replies; a new checkpoint activity starts a fresh reply window.
            if prior != activity_id:
                c["replies_consumed_through"] = None
            changed = True
        if changed:
            save_task(store, task)
        lines.append(f"blocker published on {source['repo']}#{source['number']}")
    except (CoordinatorError, StoreError) as exc:
        # These subclass SystemExit — must not be treated as successful publication.
        lines.append(f"blocker publish failed locally visible: {redact(str(exc))}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"blocker publish failed locally visible: {redact(str(exc))}")
    return lines


def verify_issue_assigned(runner: Runner, repo: str, number: int, login: str) -> dict[str, Any]:
    data = gh_json(runner, ["gh", "api", f"repos/{repo}/issues/{number}"])
    if not isinstance(data, dict):
        raise CoordinatorError("issue lookup failed")
    if data.get("pull_request") is not None:
        raise CoordinatorError("target is a pull request, not an issue")
    state = str(data.get("state") or "").lower()
    if state != "open":
        raise CoordinatorError(f"issue {repo}#{number} is {state or 'unknown'}")
    assignees = data.get("assignees")
    if not isinstance(assignees, list):
        raise CoordinatorError("issue assignees missing")
    logins = {
        str(a.get("login")).casefold()
        for a in assignees
        if isinstance(a, dict) and isinstance(a.get("login"), str)
    }
    if login.casefold() not in logins:
        raise CoordinatorError(f"issue {repo}#{number} is no longer assigned to configured login")
    return data


def _gh_jq_text(runner: Runner, argv: list[str], label: str) -> str:
    completed = runner(argv)
    if completed.returncode != 0:
        raise CoordinatorError(redact(completed.stderr or completed.stdout or f"{label} failed"))
    value = (completed.stdout or "").strip()
    if not value or any(char in value for char in "\0\r\n"):
        raise CoordinatorError(f"{label} returned no usable value")
    return value


def _fork_repository(
    runner: Runner,
    *,
    repo: str,
    publication_repo: str,
    create_if_missing: bool = True,
    before_create: Callable[[], None] | None = None,
    after_create: Callable[[], None] | None = None,
) -> bool:
    """Check the publication fork once, optionally requesting it when absent."""
    lookup = ["gh", "api", f"repos/{publication_repo}"]
    completed = runner(lookup)
    if completed.returncode != 0:
        missing_detail = (completed.stderr or completed.stdout or "").casefold()
        if "404" not in missing_detail and "not found" not in missing_detail:
            raise CoordinatorError(redact(missing_detail or "fork lookup failed"))
        if create_if_missing:
            if before_create is not None:
                before_create()
            created = runner(["gh", "api", "-X", "POST", f"repos/{repo}/forks"])
            if created.returncode != 0:
                detail = created.stderr or created.stdout or "fork creation failed"
                folded = detail.casefold()
                if not any(
                    marker in folded
                    for marker in ("already exists", "fork exists", "in progress")
                ):
                    raise CoordinatorError(redact(detail))
            if after_create is not None:
                after_create()
        return False
    try:
        data = json.loads((completed.stdout or "").strip())
    except json.JSONDecodeError as exc:
        raise CoordinatorError("fork lookup returned invalid JSON") from exc
    parent = data.get("parent") if isinstance(data, dict) else None
    parent_name = parent.get("full_name") if isinstance(parent, dict) else None
    if not isinstance(parent_name, str) or parent_name.casefold() != repo.casefold():
        raise CoordinatorError("publication fork has a different parent repository")
    return True


def _owner_repository_config(
    worker: WorkerConfig,
    runner: Runner,
    *,
    repo: str,
    login: str,
    fork_request_done: bool = False,
    before_fork_create: Callable[[], None] | None = None,
    after_fork_create: Callable[[], None] | None = None,
) -> RepositoryConfig | None:
    base = validate_base(_gh_jq_text(
        runner,
        ["gh", "api", f"repos/{repo}", "--jq", ".default_branch"],
        "default branch lookup",
    ))
    can_push = _gh_jq_text(
        runner,
        ["gh", "api", f"repos/{repo}", "--jq", ".permissions.push"],
        "push permission lookup",
    ).casefold()
    if can_push not in {"true", "false"}:
        raise CoordinatorError("push permission lookup returned neither true nor false")
    publication = repo
    if can_push == "false":
        publication = f"{login}/{repo.split('/', 1)[1]}"
        if not _fork_repository(
            runner,
            repo=repo,
            publication_repo=publication,
            create_if_missing=not fork_request_done,
            before_create=before_fork_create,
            after_create=after_fork_create,
        ):
            return None
    return RepositoryConfig(
        repo=repo,
        base=base,
        publication_repo=publication,
        check_argv=worker.owner_check_argv,
        readiness_argv=worker.owner_readiness_argv or (),
    )


def _hub_assignment_row(
    store: Store,
    *,
    repo: str,
    number: int,
    agent: str,
    assignment_actor: str,
    reply_logins: tuple[str, ...],
    canonical_repo: str | None = None,
) -> tuple[HubAssignmentMandate | None, str | None]:
    """Return (verified mandate, retry reason); neither means a definite no-match."""
    hub_url = store.meta("hub_url")
    token = store.meta("device_token")
    if not hub_url or not token:
        return None, "device is not paired with a hub"
    issue_key = f"{repo}#{number}"
    canonical_issue_key = f"{canonical_repo or repo}#{number}"
    issue_keys = list(
        dict.fromkeys((issue_key, issue_key.casefold(), canonical_issue_key))
    )
    folded_issue_keys = {candidate.casefold() for candidate in issue_keys}
    hub = Hub(hub_url, token)
    try:
        try:
            result = hub.query(
                {
                    "type": "assignment.created",
                    "payload.issue_key": {"in": issue_keys},
                }
            )
        except HubError as exc:
            return None, redact(str(exc))
        except Exception as exc:  # malformed/transport client responses retry acceptance
            return None, redact(str(exc) or type(exc).__name__)
    finally:
        hub.close()
    rows = result.get("rows") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        return None, "hub assignment query returned an invalid response"
    if len(rows) >= HUB_ASSIGNMENT_QUERY_CAP:
        return None, "hub assignment query truncated"
    allowed = {login.casefold() for login in reply_logins}
    newest: tuple[tuple[str, str], HubAssignmentMandate] | None = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        payload = row.get("payload")
        inner = payload.get("payload") if isinstance(payload, dict) else None
        envelope_login = row.get("github_login")
        origin_device_id = row.get("origin_device_id")
        inner_actor = inner.get("actor") if isinstance(inner, dict) else None
        inner_issue_key = inner.get("issue_key") if isinstance(inner, dict) else None
        if not isinstance(inner, dict) or not isinstance(envelope_login, str):
            continue
        if (
            str(inner.get("repo") or "").casefold() == repo.casefold()
            and as_int(inner.get("number")) == number
            and str(inner.get("agent") or "").casefold() == agent.casefold()
            and isinstance(inner_issue_key, str)
            and inner_issue_key.casefold() in folded_issue_keys
            and origin_device_id == f"web:{envelope_login}"
            and isinstance(inner_actor, str)
            and inner_actor.casefold() == envelope_login.casefold()
            and envelope_login.casefold() == assignment_actor.casefold()
            and envelope_login.casefold() in allowed
        ):
            row_id = row.get("row_id")
            if isinstance(row_id, str) and row_id:
                mandate = HubAssignmentMandate(
                    row_id=row_id,
                    actor=envelope_login,
                    origin=origin_device_id,
                    issue_key=inner_issue_key,
                    deviation=inner.get("deviation"),
                )
                # The newest hub row decides; its statement alone closes or blocks.
                order = (str(row.get("updated_at") or ""), row_id)
                if newest is None or order > newest[0]:
                    newest = (order, mandate)
    return (newest[1] if newest is not None else None), None


def _mandate_provenance(source: Any) -> dict[str, Any]:
    """Only the assignment's provenance goes into team-visible evidence."""
    if not isinstance(source, dict):
        return {}
    keys = ("repo", "number", "assignment_actor", "assignment_event_id", "assignment_at", "hub_assignment_row_id")
    return {key: source[key] for key in keys if key in source}


def _states_no_deviation(deviation: Any, actor: str) -> bool:
    return (
        isinstance(deviation, dict)
        and set(deviation) == {"declared", "stated_by"}
        and deviation.get("declared") is False
        and deviation.get("stated_by") == actor
    )


def _canonical_issue_repo(expected_repo: str, *responses: object) -> str | None:
    """Return GitHub's canonical owner/name from issue or event responses."""
    for response in responses:
        items = response if isinstance(response, list) else [response]
        for item in items:
            if not isinstance(item, dict):
                continue
            repository = item.get("repository")
            candidate = (
                repository.get("full_name") if isinstance(repository, dict) else None
            )
            if not isinstance(candidate, str) or not candidate:
                repository_url = item.get("repository_url")
                match = re.fullmatch(
                    r"https://api\.github\.com/repos/([^/]+)/([^/]+)",
                    str(repository_url or ""),
                )
                candidate = (
                    f"{match.group(1)}/{match.group(2)}" if match is not None else None
                )
            if candidate is None:
                continue
            if candidate.casefold() != expected_repo.casefold():
                raise CoordinatorError(
                    "issue response repository does not match configured repository"
                )
            return candidate
    return None


def _parse_coordinator_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _seconds_since(value: object) -> float | None:
    then = _parse_coordinator_time(value)
    now = _parse_coordinator_time(utcnow())
    if then is None or now is None:
        return None
    return (now - then).total_seconds()


def _hub_assignment_configuration_error(reason: str) -> bool:
    folded = reason.casefold()
    return (
        reason == "device is not paired with a hub"
        or reason == "hub assignment query truncated"
        or "http 400" in folded
        or "http 401" in folded
        or "http 403" in folded
    )


def phase_accept(store: Store, worker: WorkerConfig, task: dict[str, Any], runner: Runner) -> list[str]:
    c = coord(task)
    source = c["source"]
    repo = str(source["repo"])
    number = int(source["number"])
    scoped_runner = scoped(store, worker.session_id, runner)
    account = account_for(store, worker.session_id)
    try:
        issue = verify_issue_assigned(scoped_runner, repo, number, account.login)
    except CoordinatorError as exc:
        c["phase"] = "blocked"
        c["resume_phase"] = "accept"
        c["blocker"] = str(exc)
        save_task(store, task)
        return publish_blocker(
            store, worker, task, runner, str(exc), kind="unassigned", reply_checkpoint=True
        )
    events = gh_list(scoped_runner, ["gh", "api", "--paginate", "--slurp", f"repos/{repo}/issues/{number}/events"])
    assignments = [event for event in events if isinstance(event, dict)
                   and event.get("event") == "assigned"
                   and str((event.get("assignee") or {}).get("login", "")).casefold() == account.login.casefold()
                   and isinstance(event.get("id"), int) and not isinstance(event.get("id"), bool)]
    latest = max(assignments, key=lambda event: event["id"], default={})
    actor = latest.get("actor") or {}
    if latest.get("id", 0) <= 0 or actor.get("type") != "User" or not text(actor.get("login")):
        raise CoordinatorError("assignment has no verified human mandate")
    source["assignment_actor"] = actor["login"]
    source["assignment_event_id"] = latest["id"]
    source["assignment_at"] = latest.get("created_at")
    owner_a38_default = bool(
        source.get("owner_scoped") and worker.a38 and not worker.owner_readiness_argv
    )
    if worker.require_hub_assignment or owner_a38_default:
        canonical_repo = _canonical_issue_repo(repo, issue, events)
        if canonical_repo is None:
            issue_response = gh_json(
                scoped_runner, ["gh", "api", f"repos/{repo}/issues/{number}"]
            )
            canonical_repo = _canonical_issue_repo(repo, issue_response)
        if canonical_repo is None:
            raise CoordinatorError("issue response has no canonical repository name")
        mandate, retry = _hub_assignment_row(
            store,
            repo=repo,
            number=number,
            agent=account.login,
            assignment_actor=str(actor["login"]),
            reply_logins=worker.reply_logins,
            canonical_repo=canonical_repo,
        )
        if retry is not None:
            # The detail can name the hub host; the task (replicated) and GitHub get only
            # fixed texts, and the detail stays in this tick's local output line.
            c["hub_assignment_retry"] = (
                "hub configuration error"
                if _hub_assignment_configuration_error(retry)
                else "hub temporarily unavailable"
            )
            if _hub_assignment_configuration_error(retry):
                public = HUB_CONFIGURATION_BLOCKER
                if c.get("blocker") != public:
                    c.pop("blocker_activity_id", None)
                c["phase"] = "blocked"
                c["resume_phase"] = "accept"
                c["blocker"] = public
                c["blocker_publish_once"] = True
                c["blocker_publish_kind"] = "configuration-binding"
                save_task(store, task)
                return publish_blocker(
                    store,
                    worker,
                    task,
                    runner,
                    public,
                    kind="configuration-binding",
                    reply_checkpoint=True,
                )
            elapsed = _seconds_since(c.get("hub_assignment_retry_published_at"))
            line = f"hub assignment query unavailable; retrying accept: {retry}"
            if elapsed is None or elapsed >= HUB_ASSIGNMENT_RETRY_PUBLISH_SECONDS:
                published_at = utcnow()
                c["hub_assignment_retry_published_at"] = published_at
                save_task(store, task)
                return [
                    line,
                    *publish_blocker(
                        store,
                        worker,
                        task,
                        runner,
                        "Hub assignment verification is temporarily unavailable; "
                        "the worker retries acceptance.",
                        kind="hub-assignment-retry",
                        occurrence=published_at,
                    ),
                ]
            save_task(store, task)
            return [line]
        if mandate is None:
            age = _seconds_since(source.get("assignment_at"))
            if age is not None and age < HUB_ASSIGNMENT_GRACE_SECONDS:
                c["hub_assignment_retry"] = "hub assignment event not yet visible"
                save_task(store, task)
                return ["hub assignment event not yet visible; retrying accept"]
            message = (
                MANDATE_DEVIATION_BLOCKER
                if owner_a38_default
                else "assignment was not issued through the hub"
            )
            if c.get("blocker") != message:
                c.pop("blocker_activity_id", None)
            c["blocker"] = message
            c["resume_phase"] = "accept"
            c["phase"] = "blocked"
            c["blocker_publish_once"] = True
            c["blocker_publish_kind"] = "hub-assignment"
            save_task(store, task)
            return publish_blocker(
                store,
                worker,
                task,
                runner,
                message,
                kind="hub-assignment",
                reply_checkpoint=True,
            )
        if not isinstance(mandate, HubAssignmentMandate):
            raise CoordinatorError("hub assignment verification returned invalid evidence")
        source["hub_assignment_row_id"] = mandate.row_id
        source["hub_assignment_mandate"] = {
            "row_id": mandate.row_id,
            "actor": mandate.actor,
            "origin": mandate.origin,
            "issue_key": mandate.issue_key,
            "deviation": mandate.deviation,
        }
        if owner_a38_default and not _mandate_states_no_deviation(
            source, text(source.get("assignment_actor"))
        ):
            # Check the statement at accept, so a corrected hub assignment is found by
            # the next query instead of failing only at readiness.
            source.pop("hub_assignment_mandate", None)
            source.pop("hub_assignment_row_id", None)
            if c.get("blocker") != MANDATE_DEVIATION_BLOCKER:
                c.pop("blocker_activity_id", None)
            c["blocker"] = MANDATE_DEVIATION_BLOCKER
            c["resume_phase"] = "accept"
            c["phase"] = "blocked"
            c["blocker_publish_once"] = True
            c["blocker_publish_kind"] = "hub-assignment"
            save_task(store, task)
            return publish_blocker(
                store,
                worker,
                task,
                runner,
                MANDATE_DEVIATION_BLOCKER,
                kind="hub-assignment",
                reply_checkpoint=True,
            )
        c.pop("hub_assignment_retry", None)
        c.pop("hub_assignment_retry_published_at", None)
        c.pop("blocker", None)
        c.pop("blocker_publish_once", None)
        c.pop("blocker_publish_kind", None)
        c.pop("blocker_activity_id", None)
        clear_reply_recovery(c)
    if source.get("owner_scoped") and not isinstance(source.get("repository_config"), dict):
        def persist_fork_intent() -> None:
            if source.get("fork_requested_at") is None:
                source["fork_requested_at"] = utcnow()
                save_task(store, task)

        def persist_fork_request_done() -> None:
            if source.get("fork_request_done_at") is None:
                source["fork_request_done_at"] = utcnow()
                save_task(store, task)

        cfg = _owner_repository_config(
            worker,
            scoped_runner,
            repo=repo,
            login=account.login.casefold(),
            fork_request_done=source.get("fork_request_done_at") is not None,
            before_fork_create=persist_fork_intent,
            after_fork_create=persist_fork_request_done,
        )
        if cfg is None:
            fork_request_done_at = source.get("fork_request_done_at")
            if fork_request_done_at is None:
                save_task(store, task)
                return ["publication fork request not completed; retrying accept"]
            age = _seconds_since(fork_request_done_at)
            if age is not None and age < FORK_VISIBILITY_GRACE_SECONDS:
                save_task(store, task)
                return ["publication fork not yet visible; retrying accept"]
            message = (
                "publication fork did not become visible within "
                f"{FORK_VISIBILITY_GRACE_SECONDS} seconds"
            )
            if c.get("blocker") != message:
                c.pop("blocker_activity_id", None)
            c["blocker"] = message
            c["resume_phase"] = "accept"
            c["phase"] = "blocked"
            c["blocker_publish_once"] = True
            c["blocker_publish_kind"] = "publication-fork"
            save_task(store, task)
            return publish_blocker(
                store,
                worker,
                task,
                runner,
                message,
                kind="publication-fork",
                reply_checkpoint=True,
            )
        from .coordinator_runtime import execution_binding

        # Check and bind before pinning anything, so a refusal leaves accept re-runnable.
        ai = load_ai_accounts(store.home)
        if ai.models.as_dict() != task.get("models") or worker.a38 != bool(c.get("a38")):
            raise CoordinatorError("task model configuration changed before acceptance")
        binding = execution_binding(
            store, worker, repo, ai, repository_config=cfg, owner_scoped=True
        )
        source["repository_config"] = {
            "repo": cfg.repo,
            "base": cfg.base,
            "publication_repo": cfg.publication_repo,
        }
        source["base"] = cfg.base
        source["publication_repo"] = cfg.publication_repo
        c["execution_binding"] = binding
    marker = acceptance_marker(repo, number, worker.session_id)
    activity_id = str(uuid5(NAMESPACE_URL, f"coordinator-accept:{worker.session_id}:{repo}:{number}"))
    body = f"{ACCEPT_BODY}\n{marker}"
    queue_activity(
        store,
        activity_id=activity_id,
        session_id=worker.session_id,
        typ="comment.post",
        payload={"repo": repo, "number": number, "body": body, "target": "issue"},
    )
    execute_github(store, runner, activity_ids=(activity_id,))
    row = store.row("activity", activity_id)
    if row is None or row.get("execution_status") != "done":
        err = ""
        if row is not None:
            err = redact(str(row.get("execution_error") or "acceptance comment not verified"))
        raise CoordinatorError(f"acceptance comment failed before model start: {err}")
    c["acceptance_activity_id"] = activity_id
    set_checklist(store, task, "session_registered", "ja",
                  f"session {worker.session_id} active", source="script")
    set_checklist(store, task, "spec_written", "ja",
                  f"GitHub assignment event {latest['id']} by {actor['login']} on {repo}#{number}",
                  source="human")
    c["phase"] = "checkout"
    save_task(store, task)
    return [f"acceptance published {repo}#{number}"]


def _rollup_state(check: dict[str, Any]) -> str:
    for key in ("conclusion", "state", "status"):
        raw = check.get(key)
        if isinstance(raw, str) and raw:
            return raw.lower()
    return ""


def _paginate_workflow_runs(runner: Runner, repo: str, head: str) -> list[dict[str, Any]]:
    """Paginate Actions runs for an exact head; fail closed on truncation/unknown shape.

    Shape / missing ``workflow_runs`` / pagination truncation raise
    ``CiInventoryProtocolError`` (recoverable blocked + ``resume_phase=ci``).
    Transient ``gh_json`` transport failures raise ``CiObservationTransportError``
    so callers can retry the same static phase without a reply gate.
    """
    owner, name = repo.split("/", 1)
    page = 1
    runs: list[dict[str, Any]] = []
    while page <= 20:
        try:
            raw = gh_json(
                runner,
                [
                    "gh",
                    "api",
                    f"repos/{owner}/{name}/actions/runs?head_sha={head}&per_page=100&page={page}",
                ],
            )
        except CoordinatorError as exc:
            raise CiObservationTransportError(
                str(exc) or "workflow inventory temporarily unavailable"
            ) from exc
        if not isinstance(raw, dict):
            raise CiInventoryProtocolError("workflow inventory has unexpected shape")
        batch = raw.get("workflow_runs")
        if not isinstance(batch, list):
            raise CiInventoryProtocolError("workflow inventory missing workflow_runs")
        for item in batch:
            if isinstance(item, dict):
                runs.append(item)
        total = as_int(raw.get("total_count"))
        if total is not None and len(runs) >= total:
            break
        if len(batch) < 100:
            break
        page += 1
    else:
        raise CiInventoryProtocolError("workflow inventory pagination truncated")
    return runs


def _fetch_pr_json(runner: Runner, target: str, number: int, fields: str) -> Any:
    """Fetch ``gh pr view`` JSON; transport failures are typed, not generic blockers."""
    try:
        return gh_json(
            runner,
            ["gh", "pr", "view", str(number), "--repo", target, "--json", fields],
        )
    except CoordinatorError as exc:
        raise CiObservationTransportError(
            str(exc) or "PR view temporarily unavailable"
        ) from exc


@dataclass(frozen=True)
class _CiObservation:
    """Deterministic CI observation classification shared by phase_ci and rechecks."""

    kind: str
    detail: str
    failures: tuple[str, ...] = ()
    action_required: tuple[str, ...] = ()
    new_head: str | None = None
    rollup: tuple[Any, ...] | None = None
    latest: dict[str, dict[str, Any]] | None = None


def _a38_ci_evidence(
    store: Store, worker: WorkerConfig, task: dict[str, Any], head: str
) -> frozenset[str]:
    """Return passing jobs from the verified exact-head author A38 report."""
    c = coord(task)
    base = str(c.get("base_sha") or "")
    a38 = c.get("a38") if isinstance(c.get("a38"), dict) else {}
    report = a38.get("report") if isinstance(a38, dict) else None
    if not (
        is_sha(base)
        and isinstance(report, dict)
        and report.get("head") == head
        and report.get("base_sha") == base
        and report.get("policy_revision") == base
    ):
        return frozenset()
    activity_id = report.get("activity_id")
    activity = store.row("activity", activity_id) if isinstance(activity_id, str) else None
    from .coordinator_a38 import _own_report_activity

    payload = activity.get("payload") if isinstance(activity, dict) else None
    body = payload.get("body") if isinstance(payload, dict) else None
    if (
        not isinstance(activity, dict)
        or not _own_report_activity(store, worker, task, activity, head)
        or activity.get("execution_status") != "done"
        or not isinstance(body, str)
    ):
        return frozenset()
    return a38_passed_job_names_from_comment(body, head)


def _rollup_workflow_run_id(check: dict[str, Any]) -> int | None:
    """Read the Actions run identity from a rollup link, never its display name."""
    for key in ("detailsUrl", "details_url", "link", "url"):
        value = check.get(key)
        if not isinstance(value, str):
            continue
        match = re.search(r"/actions/runs/([1-9][0-9]*)(?:[/#?]|$)", value)
        if match is not None:
            return int(match.group(1))
    return None


def _classify_rollup_and_inventory(
    rollup: list[Any],
    latest: dict[str, dict[str, Any]],
    *,
    passed_a38: frozenset[str] = frozenset(),
) -> _CiObservation:
    """Classify fetched rollup+inventory facts. Never guesses from log text."""
    pending = False
    failures: list[str] = []
    action_required: list[str] = []
    successes = 0
    inventory_keys_by_run: dict[int, list[str]] = {}
    for key, run in latest.items():
        run_id = as_int(run.get("id"))
        if run_id is not None and run_id > 0:
            inventory_keys_by_run.setdefault(run_id, []).append(key)
    report_covered_inventory_keys: set[str] = set()

    for check in rollup:
        if not isinstance(check, dict):
            return _CiObservation("protocol", "PR check rollup entry malformed")
        state = _rollup_state(check)
        name = str(check.get("name") or check.get("context") or "check")
        if state in CI_PENDING or state == "":
            pending = True
        elif state in CI_ACTION_REQUIRED:
            action_required.append(name)
        elif state in CI_SUCCESS:
            successes += 1
        elif a38_accepts_skipped_or_neutral(
            name,
            state,
            passed_names=passed_a38,
        ):
            successes += 1
            run_id = _rollup_workflow_run_id(check)
            matching_keys = inventory_keys_by_run.get(run_id or 0, [])
            if len(matching_keys) == 1:
                report_covered_inventory_keys.add(matching_keys[0])
        else:
            # Cancelled, failed, pending, and uncovered skipped/neutral stay strict.
            failures.append(f"{name}:{state or 'unknown'}")

    for key, run in latest.items():
        status = str(run.get("status") or "").lower()
        conclusion = str(run.get("conclusion") or "").lower()
        path = str(run.get("path") or key)
        if status != "completed":
            pending = True
            continue
        if conclusion in CI_ACTION_REQUIRED:
            action_required.append(path)
        elif conclusion in CI_SUCCESS:
            successes += 1
        elif conclusion in {"skipped", "neutral"} and (
            key in report_covered_inventory_keys
            or a38_accepts_skipped_or_neutral(
                path, conclusion, passed_names=passed_a38
            )
        ):
            successes += 1
        else:
            failures.append(f"{path}:{conclusion or 'unknown'}")

    if action_required:
        return _CiObservation(
            "action_required",
            "CI action_required (external authorization)",
            action_required=tuple(action_required),
            rollup=tuple(rollup),
            latest=latest,
        )
    if not rollup and not latest:
        return _CiObservation("pending", "no checks yet", rollup=tuple(rollup), latest=latest)
    if pending and not failures:
        return _CiObservation("pending", "checks pending", rollup=tuple(rollup), latest=latest)
    if failures:
        return _CiObservation(
            "failed",
            "CI failed",
            failures=tuple(failures),
            rollup=tuple(rollup),
            latest=latest,
        )
    rollup_ok = any(
        isinstance(check, dict)
        and (
            _rollup_state(check) in CI_SUCCESS
            or a38_accepts_skipped_or_neutral(
                str(check.get("name") or check.get("context") or "check"),
                _rollup_state(check),
                passed_names=passed_a38,
            )
        )
        for check in rollup
    )
    inventory_ok = any(
        str(run.get("status") or "") == "completed"
        and (
            str(run.get("conclusion") or "") in CI_SUCCESS
            or (
                str(run.get("conclusion") or "") in {"skipped", "neutral"}
                and key in report_covered_inventory_keys
            )
            or a38_accepts_skipped_or_neutral(
                str(run.get("path") or ""),
                str(run.get("conclusion") or ""),
                passed_names=passed_a38,
            )
        )
        for key, run in latest.items()
    )
    if (
        not rollup
        or not latest
        or not rollup_ok
        or not inventory_ok
        or successes <= 0
    ):
        return _CiObservation(
            "pending",
            "incomplete rollup/inventory success evidence",
            rollup=tuple(rollup),
            latest=latest,
        )
    return _CiObservation("green", "CI green", rollup=tuple(rollup), latest=latest)


def _observe_exact_head_ci(
    runner: Runner,
    target: str,
    number: int,
    head: str,
    *,
    fields: str,
    passed_a38: frozenset[str] = frozenset(),
) -> _CiObservation:
    """Shared exact-head CI observation for ``phase_ci`` and Ready-side rechecks."""
    try:
        pr = _fetch_pr_json(runner, target, number, fields)
    except CiObservationTransportError:
        return _CiObservation("transport", "PR view temporarily unavailable")
    if not isinstance(pr, dict):
        return _CiObservation("protocol", "PR view failed")
    observed_head = str(pr.get("headRefOid") or "")
    if observed_head.lower() != head.lower():
        return _CiObservation(
            "head_changed",
            "PR head changed",
            new_head=(observed_head or head).lower(),
        )
    rollup = pr.get("statusCheckRollup")
    # Absent rollup must not skip head workflow inventory: action_required (and
    # actual failures) may exist only in inventory. Empty rollup cannot be
    # green, but inventory facts remain visible to classification.
    if rollup is None:
        rollup = []
    elif not isinstance(rollup, list):
        return _CiObservation("protocol", "PR check rollup malformed")
    try:
        runs = _paginate_workflow_runs(runner, target, head)
    except CiInventoryProtocolError as exc:
        return _CiObservation("protocol", f"CI inventory: {exc}")
    except CiObservationTransportError:
        return _CiObservation("transport", "inventory temporarily unavailable")
    except CoordinatorError:
        # Defensive: any other typed command failure stays transport, not reply-gated.
        return _CiObservation("transport", "inventory temporarily unavailable")
    latest = _latest_run_attempts(runs, head)
    return _classify_rollup_and_inventory(
        rollup,
        latest,
        passed_a38=passed_a38,
    )


def _latest_run_attempts(runs: list[dict[str, Any]], head: str) -> dict[str, dict[str, Any]]:
    """Disambiguate by workflow path + event; keep highest attempt/id per key."""
    latest: dict[str, dict[str, Any]] = {}
    for run in runs:
        if str(run.get("head_sha") or "").lower() != head.lower():
            continue
        path = run.get("path")
        event = run.get("event") or ""
        if not isinstance(path, str) or not path:
            continue
        key = f"{path}|{event}"
        prev = latest.get(key)
        run_attempt = as_int(run.get("run_attempt")) or 0
        run_id = as_int(run.get("id")) or 0
        if prev is None:
            latest[key] = run
            continue
        prev_attempt = as_int(prev.get("run_attempt")) or 0
        prev_id = as_int(prev.get("id")) or 0
        if run_attempt > prev_attempt or (run_attempt == prev_attempt and run_id > prev_id):
            latest[key] = run
    return latest


def fetch_failure_logs(
    runner: Runner,
    repo: str,
    latest: dict[str, dict[str, Any]],
    failures: list[str],
) -> str:
    """Fetch plain-text failed-job logs. Never treat ZIP archive bytes as text."""
    chunks: list[str] = []
    inaccessible = False
    failed_runs: list[tuple[str, dict[str, Any]]] = []
    for key, run in latest.items():
        path = str(run.get("path") or key)
        conclusion = str(run.get("conclusion") or "").lower()
        status = str(run.get("status") or "").lower()
        if status == "completed" and conclusion and conclusion not in CI_SUCCESS:
            failed_runs.append((path, run))
            continue
        # Also match rollup failure names when inventory path differs.
        if any(path in f or key in f or path.rsplit("/", 1)[-1] in f for f in failures):
            failed_runs.append((path, run))
    if not failed_runs and failures:
        # Rollup reported failures but inventory had no matching failed run.
        inaccessible = True
        chunks.append("failing workflow logs inaccessible")
    seen_ids: set[int] = set()
    for path, run in failed_runs:
        run_id = as_int(run.get("id"))
        if run_id is None or run_id in seen_ids:
            if run_id is None:
                inaccessible = True
                chunks.append(f"{path}: logs inaccessible")
            continue
        seen_ids.add(run_id)
        attempt = as_int(run.get("run_attempt")) or 1
        argv = [
            "gh",
            "run",
            "view",
            str(run_id),
            "--repo",
            repo,
            "--log-failed",
            "--attempt",
            str(attempt),
        ]
        try:
            completed = runner(argv)
        except OSError:
            inaccessible = True
            chunks.append(f"{path}: logs inaccessible")
            continue
        if completed.returncode != 0:
            inaccessible = True
            chunks.append(f"{path}: logs inaccessible")
            continue
        raw = completed.stdout or ""
        if not raw.strip():
            inaccessible = True
            chunks.append(f"{path}: logs inaccessible (empty output)")
            continue
        # ZIP / binary archives must not be fed to the implementer as "logs".
        if raw.startswith("PK") or "\x00" in raw[:200]:
            inaccessible = True
            chunks.append(f"{path}: logs inaccessible (archive, not plain text)")
            continue
        chunks.append(redact(raw[:1500]))
    if inaccessible and not any("logs inaccessible" not in c for c in chunks):
        return "failing workflow logs inaccessible"
    if not chunks:
        return "failing workflow logs inaccessible"
    return "\n".join(chunks)


def phase_ci(store: Store, worker: WorkerConfig, task: dict[str, Any], runner: Runner) -> list[str]:
    """Exact-head PR check rollup AND head workflow inventory. Fail closed.

    This core observes cumulative GitHub CI targets only. Target-repository
    policy / A38 live join belongs to configured readiness_argv. A verified
    exact-head A38 report may cover matching skipped/neutral required checks;
    there is no generic cancelled/failed/pending bypass. Empty or malformed evidence is not green.
    Absent ``statusCheckRollup`` is treated as ``[]`` and inventory is still
    inspected so inventory-only ``action_required`` / failures stay visible.
    action_required is an authorization blocker, not a source-code failure.
    Pending / absent-yet evidence and transient observation transport stay on
    static ``phase=ci`` with no idle model and no reply gate. Hard protocol
    faults use recoverable blocked + ``resume_phase=ci`` + checkpoint.
    """
    c = coord(task)
    target = target_repo(task)
    number = as_int(c.get("pr_number") or task.get("ref"))
    head = str(c.get("head_sha") or "")
    if number is None or not head:
        raise CoordinatorError("CI observation requires PR number and head")
    scoped_runner = scoped(store, worker.session_id, runner)
    passed_a38 = (
        _a38_ci_evidence(store, worker, task, head)
        if worker.a38 else frozenset()
    )
    obs = _observe_exact_head_ci(
        scoped_runner,
        target,
        number,
        head,
        fields="statusCheckRollup,headRefOid,state,isDraft,author,baseRefName,mergeable",
        passed_a38=passed_a38,
    )
    if obs.kind == "transport":
        # Transient PR-view / inventory transport: same static ci phase.
        return [f"CI pending on {head[:7]} ({obs.detail})"]
    if obs.kind == "pending":
        return [f"CI pending on {head[:7]} ({obs.detail})"]
    if obs.kind == "head_changed":
        c["head_sha"] = str(obs.new_head or head).lower()
        invalidate_head_evidence(c, c["head_sha"])
        c["phase"] = "tests"
        save_task(store, task)
        return ["PR head changed; invalidating evidence"]
    if obs.kind == "protocol":
        # Hard rollup/inventory shape/truncation: recoverable blocked + ci resume.
        c["phase"] = "blocked"
        c["resume_phase"] = "ci"
        c["blocker"] = obs.detail
        save_task(store, task)
        kind = "ci-inventory" if obs.detail.startswith("CI inventory:") else "ci-rollup"
        return publish_blocker(
            store,
            worker,
            task,
            runner,
            obs.detail,
            kind=kind,
            reply_checkpoint=True,
        )
    if obs.kind == "action_required":
        names = list(obs.action_required)
        if worker.a38:
            return [
                f"CI authorization pending on {head[:7]} (guard-owned): "
                + ", ".join(names[:5])
            ]
        c["phase"] = "blocked"
        c["resume_phase"] = "ci"
        c["blocker"] = "CI action_required (external authorization)"
        save_task(store, task)
        return publish_blocker(
            store,
            worker,
            task,
            runner,
            "GitHub CI reports action_required (authorization), not a code failure: "
            + ", ".join(names[:5]),
            kind="ci-action-required",
            reply_checkpoint=True,
        )
    if obs.kind == "failed":
        failures = list(obs.failures)
        latest = obs.latest or {}
        logs = fetch_failure_logs(scoped_runner, target, latest, failures)
        if "inaccessible" in logs and not any(
            line for line in logs.splitlines() if "inaccessible" not in line and line.strip()
        ):
            c["phase"] = "blocked"
            c["resume_phase"] = "ci"
            c["blocker"] = "CI failed but logs inaccessible"
            save_task(store, task)
            return publish_blocker(
                store,
                worker,
                task,
                runner,
                f"CI failed on {head[:7]} but workflow logs are inaccessible",
                kind="ci-logs",
                reply_checkpoint=True,
            )
        c["findings"] = redact(f"CI failed on {head[:7]}:\n" + "\n".join(failures) + "\n" + logs)
        c["phase"] = "implement"
        task["state"] = "implementing"
        evidence = c.setdefault("evidence", {})
        if isinstance(evidence, dict):
            evidence["ci_green"] = False
            evidence["ci_head"] = head
        invalidate_head_evidence(c, "" if worker.a38 else head)
        save_task(store, task)
        return [f"CI failed on {head[:7]}; routing to implementer"]
    if obs.kind != "green":
        raise CoordinatorError(f"CI observation inconclusive ({obs.kind}: {obs.detail})")
    evidence = c.setdefault("evidence", {})
    if not isinstance(evidence, dict):
        evidence = {}
        c["evidence"] = evidence
    evidence["ci_green"] = True
    evidence["ci_head"] = head
    evidence["ci_observed_at"] = utcnow()
    c["phase"] = "readiness"
    save_task(store, task)
    return [f"CI green on {head[:7]}"]


def _fresh_ci_still_green(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
    head: str,
) -> str | None:
    """Re-observe CI on this tick; do not trust a stale ci_green flag.

    Returns ``None`` when still green. Returns a same-phase retry message for
    pending / absent-yet evidence and transient observation transport — callers
    must not manufacture a reply gate or idle a model, and must not proceed to
    APPROVE/Ready. Raises ``CiObservedFailureError`` for an observed actual
    failed CI so ``advance_one`` can route to existing ``phase_ci`` log/implement
    handling without a same-phase retry setter or human reply gate. Raises for
    hard protocol faults, action_required, and head change.
    """
    c = coord(task)
    target = target_repo(task)
    number = as_int(c.get("pr_number") or task.get("ref"))
    if number is None:
        raise CoordinatorError("CI recheck requires PR number")
    scoped_runner = scoped(store, worker.session_id, runner)
    passed_a38 = (
        _a38_ci_evidence(store, worker, task, head)
        if worker.a38 else frozenset()
    )
    obs = _observe_exact_head_ci(
        scoped_runner,
        target,
        number,
        head,
        fields="statusCheckRollup,headRefOid",
        passed_a38=passed_a38,
    )
    if obs.kind == "green":
        return None
    if obs.kind in ("pending", "transport"):
        return f"CI {obs.detail} on {head[:7]}; retrying without Ready/APPROVE"
    if obs.kind == "head_changed":
        raise CoordinatorError("PR head changed during readiness")
    if obs.kind == "protocol":
        raise CiObservationProtocolError(obs.detail)
    if obs.kind == "action_required" and worker.a38:
        return (
            f"CI authorization pending on {head[:7]} (guard-owned); "
            "retrying without Ready"
        )
    if obs.kind == "action_required":
        raise CoordinatorError(
            "CI action_required on recheck: " + ", ".join(obs.action_required[:5])
        )
    if obs.kind == "failed":
        detail = ", ".join(obs.failures[:5]) if obs.failures else obs.detail
        raise CiObservedFailureError(f"CI not green on recheck ({detail})")
    raise CoordinatorError(f"CI observation inconclusive on recheck ({obs.kind})")


def _parse_readiness_result(stdout: str, *, head: str, base: str, base_name: str) -> dict[str, Any]:
    """Parse trusted readiness_argv JSON. Fail closed on missing/mismatched proof."""
    raw = (stdout or "").strip()
    if not raw:
        raise CoordinatorError(
            "readiness produced no JSON; required contract: " + READINESS_CONTRACT
        )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CoordinatorError(
            "readiness stdout is not JSON; required contract: " + READINESS_CONTRACT
        ) from exc
    if not isinstance(data, dict):
        raise CoordinatorError("readiness JSON must be an object")
    result_head = str(data.get("head") or "").lower()
    if not is_sha(result_head) or result_head != head.lower():
        raise CoordinatorError("readiness JSON head does not match exact clean signed HEAD")
    result_base = str(data.get("base") or "")
    base_ok = is_sha(result_base.lower()) and result_base.lower() == base.lower()
    if not base_ok:
        raise CoordinatorError("readiness JSON base does not match pinned base")
    if data.get("contributing_ok") is not True:
        raise CoordinatorError("readiness JSON contributing_ok is not true")
    deviation = data.get("deviation")
    if not isinstance(deviation, dict):
        raise CoordinatorError("readiness JSON requires deviation object")
    return data


def _apply_readiness_checklist(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    readiness: dict[str, Any],
    *,
    head: str,
) -> None:
    """Close contributing/deviation from trusted readiness before Ready. No inferred grants."""
    if readiness.get("owner_a38_default") is True:
        gate = latest_gates(store, task["id"]).get(("review-1", "quality"))
        if (
            gate is None
            or gate.get("verdict") != "approved"
            or gate.get("head_sha") != head
        ):
            raise CoordinatorError(
                "owner A38 readiness requires approved review-1/quality gate on current head"
            )
        source_raw = coord(task).get("source")
        source = source_raw if isinstance(source_raw, dict) else {}
        assignment_actor_raw = text(source.get("assignment_actor"))
        assignment_actor = assignment_actor_raw.strip() if assignment_actor_raw else None
        assignment_event_id = source.get("assignment_event_id")
        if (
            assignment_actor is None
            or not isinstance(assignment_event_id, int)
            or isinstance(assignment_event_id, bool)
            or assignment_event_id <= 0
        ):
            raise CoordinatorError(
                "owner A38 default requires a verified assignment actor and event; "
                "configure owner_readiness_argv"
            )
        set_checklist(
            store,
            task,
            "contributing_ok",
            "ja",
            f"conformity-a approved at {head}",
            source="script",
        )
        mandate = source.get("hub_assignment_mandate")
        row_id = mandate.get("row_id") if isinstance(mandate, dict) else None
        mandate_actor = mandate.get("actor") if isinstance(mandate, dict) else None
        explicit_no_deviation = _mandate_states_no_deviation(source, assignment_actor)
        if not explicit_no_deviation:
            raise CoordinatorError(MANDATE_DEVIATION_BLOCKER)
        mandate_evidence = (
            f"hub mandate {row_id}: {mandate_actor} stated deviation declared=false"
        )
        set_checklist(
            store,
            task,
            "deviation_declared",
            "n_a",
            mandate_evidence,
            source="human",
        )
        set_checklist(
            store,
            task,
            "deviation_granted",
            "n_a",
            mandate_evidence,
            source="human",
        )
        return
    set_checklist(
        store,
        task,
        "contributing_ok",
        "ja",
        f"trusted readiness_argv contributing_ok on {head}",
        source="script",
    )
    deviation = readiness["deviation"]
    declared = deviation.get("declared")
    if declared is False:
        set_checklist(
            store,
            task,
            "deviation_declared",
            "n_a",
            (
                "trusted readiness_argv reported no deviation; "
                f"human assignment mandate {_mandate_provenance(coord(task).get('source', {}))}"
            ),
            source="human",
        )
        set_checklist(
            store,
            task,
            "deviation_granted",
            "n_a",
            "no deviation declared; not claiming a grant",
            source="human",
        )
        return
    if declared is not True:
        raise CoordinatorError("readiness deviation.declared must be boolean")
    if deviation.get("granted") is not True:
        raise CoordinatorError("declared deviation without granted=true; refusing inferred grant")
    granted_by = str(deviation.get("granted_by") or "").casefold()
    if not granted_by or granted_by not in {x.casefold() for x in worker.reply_logins}:
        raise CoordinatorError("deviation grant must cite an authorized reply_logins login")
    evidence = text(deviation.get("evidence"))
    if evidence is None:
        raise CoordinatorError("deviation grant requires explicit human grant evidence")
    set_checklist(
        store,
        task,
        "deviation_declared",
        "ja",
        f"trusted readiness + human grant by {granted_by}: {redact(evidence)}",
        source="human",
    )
    set_checklist(
        store,
        task,
        "deviation_granted",
        "ja",
        f"granted_by={granted_by}; {redact(evidence)}",
        source="human",
    )


def _readiness_for_head(
    worker: WorkerConfig,
    task: dict[str, Any],
    cfg: RepositoryConfig,
    *,
    head: str,
    base_sha: str,
) -> dict[str, Any]:
    """Obtain owner-default or configured trusted readiness for one exact head."""
    c = coord(task)
    source = c["source"]
    if source.get("owner_scoped") and worker.a38 and not cfg.readiness_argv:
        return {
            "head": head,
            "base": base_sha,
            "contributing_ok": True,
            "deviation": {"declared": False},
            "owner_a38_default": True,
        }
    completed = run_bounded(
        list(cfg.readiness_argv),
        timeout=worker.check_timeout,
        cwd=str(c["worktree"]),
        env=coordinator_env(c, worker, cfg),
        clear_ambient_github=True,
    )
    if completed.returncode != 0:
        raise CoordinatorError(
            redact(
                f"readiness failed: {(completed.stderr or completed.stdout or '')[:500]}"
            )
        )
    return _parse_readiness_result(
        completed.stdout or "",
        head=head,
        base=base_sha,
        base_name=cfg.base,
    )


def phase_readiness(store: Store, worker: WorkerConfig, task: dict[str, Any], runner: Runner) -> list[str]:
    c = coord(task)
    source = c["source"]
    cfg = repo_cfg(worker, str(source["repo"]), task)
    worktree = str(c["worktree"])
    head = verify_signed_clean_head(store, worker, runner, worktree)
    if head != str(c.get("head_sha") or "").lower():
        invalidate_head_evidence(c, head)
        c["head_sha"] = head
        c["phase"] = "tests"
        save_task(store, task)
        return ["head changed before readiness"]

    base_sha = str(c.get("base_sha") or "")
    readiness = _readiness_for_head(
        worker,
        task,
        cfg,
        head=head,
        base_sha=base_sha,
    )

    # Re-verify clean signed head AFTER readiness (command may have touched files).
    head_after = verify_signed_clean_head(store, worker, runner, worktree)
    if head_after != head:
        invalidate_head_evidence(c, head_after)
        c["head_sha"] = head_after
        c["phase"] = "tests"
        save_task(store, task)
        return ["head modified by readiness; invalidating evidence"]

    target = target_repo(task)
    number = as_int(c.get("pr_number"))
    if number is None:
        raise CoordinatorError("readiness requires PR number")
    scoped_runner = scoped(store, worker.session_id, runner)
    try:
        pr = _fetch_pr_json(
            scoped_runner,
            target,
            number,
            "headRefOid,baseRefOid,baseRefName,author,isDraft,state,mergeable",
        )
    except CiObservationTransportError:
        # Unavailable fetch ≠ known mismatched identity/base/signature.
        # Keep phase off formal_approve so nested Ready-side callers return.
        c["phase"] = "readiness"
        save_task(store, task)
        return [f"PR metadata temporarily unavailable on {head[:7]}; retrying readiness"]
    if not isinstance(pr, dict):
        raise CoordinatorError("PR view failed")
    account = account_for(store, worker.session_id)
    if str(pr.get("headRefOid") or "").lower() != head:
        raise CoordinatorError("PR head does not match clean signed head")
    if worker.a38:
        observed_base = str(pr.get("baseRefOid") or "").lower()
        saved_base = str(c.get("base_sha") or "").lower()
        if is_sha(saved_base) and observed_base != saved_base:
            from .coordinator_a38 import invalidate_base_evidence

            invalidate_base_evidence(c, observed_base)
            c["phase"] = "tests"
            save_task(store, task)
            return ["PR base changed before readiness; invalidating A38 report evidence"]
    author = pr.get("author") if isinstance(pr.get("author"), dict) else {}
    if str(author.get("login") or "").casefold() != account.login.casefold():
        raise CoordinatorError("PR author does not match configured worker login")
    if str(pr.get("baseRefName") or "") != cfg.base:
        raise CoordinatorError("PR base mismatch")
    if str(pr.get("state") or "").upper() != "OPEN":
        raise CoordinatorError("PR is not open")
    if str(pr.get("mergeable") or "").upper() != "MERGEABLE":
        raise CoordinatorError(f"PR not mergeable ({pr.get('mergeable')})")
    if worker.a38:
        if pr.get("isDraft") not in (True, False):
            raise CoordinatorError("PR draft state is unavailable before readiness completes")
    elif pr.get("isDraft") is not True:
        raise CoordinatorError("PR must still be draft before readiness completes")

    evidence = c.get("evidence") if isinstance(c.get("evidence"), dict) else {}
    if worker.a38:
        from .coordinator_a38 import local_test_gate_satisfied

        tests_satisfied = local_test_gate_satisfied(task, head)
    else:
        tests_satisfied = bool(
            evidence.get("tests_pass") and evidence.get("tests_head") == head
        )
    if not tests_satisfied:
        raise CoordinatorError("tests not green on current head")
    retry = _fresh_ci_still_green(store, worker, task, runner, head)
    if retry is not None:
        # Pending/transient CI observation: no reply gate, no APPROVE/Ready.
        # Never leave phase=formal_approve on an incomplete readiness tick.
        c["phase"] = "readiness"
        save_task(store, task)
        return [retry]
    latest = latest_gates(store, task["id"])
    models = _task_models(task)
    if worker.a38:
        from .coordinator_a38 import require_ready_evidence

        if not require_ready_evidence(store, task, head):
            return [
                f"legacy A38 report on {head[:7]} is not base-bound; re-running tests"
            ]
        gate_pairs = tuple(pair for pair in GATE_PAIRS if pair[0] == "review-1")
    else:
        skip_second = _second_review_skip(
            store, worker, task, runner, phase="readiness", head=head
        )
        if isinstance(skip_second, list):
            return skip_second
        gate_pairs = tuple(
            pair for pair in GATE_PAIRS if not (pair[0] == "review-2" and skip_second)
        )
    for stage, dimension, slot in gate_pairs:
        vendor = models.for_slot(slot)
        g = latest.get((stage, dimension))
        if g is None or g.get("verdict") != "approved" or g.get("head_sha") != head or g.get("vendor") != vendor:
            raise CoordinatorError(f"missing approved gate {stage}/{dimension} on {head[:7]}")
    # Close policy/deviation from trusted readiness BEFORE Ready — never after merge.
    _apply_readiness_checklist(store, worker, task, readiness, head=head)
    evidence = c.setdefault("evidence", {})
    if isinstance(evidence, dict):
        evidence["readiness_head"] = head
        evidence["readiness_base"] = base_sha or cfg.base
        evidence["readiness_at"] = utcnow()
    c["phase"] = "await_ready" if worker.a38 else "formal_approve"
    save_task(store, task)
    return [f"readiness ok on {head[:7]}"]


def _inspect_formal_approve(
    runner: Runner,
    *,
    repo: str,
    number: int,
    marker: str,
    head: str,
    login: str,
) -> tuple[str, dict[str, Any] | None]:
    """Classify same-marker formal review facts from a successful reviews list.

    Returns ``("approved", payload)``, ``("invalid", reason_payload)``, or
    ``("absent", None)``. Absence alone is not proof of human dismissal —
    only an observed same-marker non-APPROVED / misbound review is.
    Transport failures raise from ``gh_list`` and must not invalidate.
    """
    owner, name = repo.split("/", 1)
    reviews = gh_list(
        runner,
        ["gh", "api", "--paginate", "--slurp", f"repos/{owner}/{name}/pulls/{number}/reviews"],
    )
    invalid: dict[str, Any] | None = None
    for review in reviews:
        if not isinstance(review, dict):
            continue
        body = review.get("body")
        if not isinstance(body, str) or marker not in body:
            continue
        user = review.get("user") if isinstance(review.get("user"), dict) else {}
        if str(user.get("login") or "").casefold() != login.casefold():
            continue
        state = str(review.get("state") or "").upper()
        commit = str(review.get("commit_id") or "")
        rev_id = as_int(review.get("id"))
        url = text(review.get("html_url") or review.get("url"))
        if (
            state == "APPROVED"
            and commit
            and commit.lower() == head.lower()
            and rev_id is not None
            and rev_id > 0
            and url is not None
        ):
            return (
                "approved",
                {
                    "id": rev_id,
                    "url": url,
                    "commit_id": commit,
                    "login": login.casefold(),
                    "state": "APPROVED",
                },
            )
        # Same marker + login observed, but not a valid APPROVED on this head.
        if state != "APPROVED":
            invalid = {"state": state or "missing", "commit_id": commit, "reason": "not APPROVED"}
        elif not commit or commit.lower() != head.lower():
            invalid = {
                "state": state,
                "commit_id": commit,
                "reason": "commit_id mismatch",
            }
        else:
            invalid = {
                "state": state,
                "commit_id": commit,
                "reason": "missing id or url",
            }
    if invalid is not None:
        return ("invalid", invalid)
    return ("absent", None)


def phase_formal_approve(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
) -> list[str]:
    """APPROVE via review.post activity + commit_id transport; verify strictly."""
    if worker.a38:
        raise CoordinatorError("A38 mode never posts a formal APPROVE")
    if worker.review_session is None:
        raise CoordinatorError("formal approve requires review_session")
    fresh = phase_readiness(store, worker, task, runner)
    if coord(task).get("phase") != "formal_approve":
        return fresh
    c = coord(task)
    target = target_repo(task)
    number = as_int(c.get("pr_number"))
    worktree = str(c.get("worktree") or "")
    head = verify_signed_clean_head(store, worker, runner, worktree)
    c["head_sha"] = head
    if number is None or not is_sha(head):
        raise CoordinatorError("formal approve requires PR and head")

    # Fresh exact-head gate/CI checks before action.
    evidence = c.get("evidence") if isinstance(c.get("evidence"), dict) else {}
    if not evidence.get("tests_pass") or evidence.get("tests_head") != head:
        raise CoordinatorError("tests not green before formal approve")
    retry = _fresh_ci_still_green(store, worker, task, runner, head)
    if retry is not None:
        c["phase"] = "formal_approve"
        save_task(store, task)
        return [retry]
    latest = latest_gates(store, task["id"])
    skip_second = _second_review_skip(store, worker, task, runner, phase="formal_approve", head=head)
    if isinstance(skip_second, list):
        return skip_second
    models = _task_models(task)
    for stage, dimension, slot in GATE_PAIRS:
        if stage == "review-2" and skip_second:
            continue
        vendor = models.for_slot(slot)
        g = latest.get((stage, dimension))
        if g is None or g.get("verdict") != "approved" or g.get("head_sha") != head or g.get("vendor") != vendor:
            raise CoordinatorError(f"missing approved gate {stage}/{dimension} before formal approve")

    try:
        review_account = account_for(store, worker.review_session)
        scoped_review = review_account.runner(runner)
    except AccountError as exc:
        raise CoordinatorError(str(exc)) from exc

    # Occurrence advances when stale formal evidence is cleared after dismissal so
    # a resumed attempt gets a new durable activity id (and marker). Same attempt
    # stays idempotent across crash/retry / transient transport.
    attempt = as_int(c.get("formal_approve_attempt")) or 0
    activity_id = str(
        uuid5(NAMESPACE_URL, f"coordinator-formal-approve:{task['id']}:{head}:{attempt}")
    )
    marker = ACTIVITY_MARKER.format(id=activity_id)
    body = f"Formal approval for head `{head[:7]}` after script-verified gates and CI.\n{marker}"

    queue_activity(
        store,
        activity_id=activity_id,
        session_id=worker.review_session,
        typ="review.post",
        payload={
            "repo": target,
            "number": number,
            "body": body,
            "event": "APPROVE",
            "commit_id": head,
        },
    )
    execute_github(store, runner, activity_ids=(activity_id,))
    recorded = store.row("activity", activity_id)
    try:
        status, discovered = _inspect_formal_approve(
            scoped_review,
            repo=target,
            number=number,
            marker=marker,
            head=head,
            login=review_account.login,
        )
    except CoordinatorError:
        # Transient discovery transport: preserve attempt/activity; no reply gate.
        return [
            f"formal approval not yet verified on {head[:7]}; retrying same attempt"
        ]
    if status == "invalid":
        # Observed same-marker revoked / non-APPROVED / misbound — new reply + attempt.
        reason = "not APPROVED"
        if isinstance(discovered, dict):
            reason = str(discovered.get("reason") or reason)
        _invalidate_stale_formal_approval(task, reason=reason)
        raise CoordinatorError(
            f"formal review is not currently APPROVED on the reviewed head ({reason})"
        )
    if status == "approved" and isinstance(discovered, dict):
        # Live APPROVED on this attempt: reconcile through the executor when the
        # activity row is not yet done (lost response / prior transport error).
        if recorded is None or recorded.get("execution_status") != "done":
            queue_activity(
                store,
                activity_id=activity_id,
                session_id=worker.review_session,
                typ="review.post",
                payload={
                    "repo": target,
                    "number": number,
                    "body": body,
                    "event": "APPROVE",
                    "commit_id": head,
                },
            )
            execute_github(store, runner, activity_ids=(activity_id,))
            recorded = store.row("activity", activity_id)
        if recorded is None or recorded.get("execution_status") != "done":
            # Do not count unknown delivery as approval; retry same attempt.
            return [
                f"formal APPROVE observed on {target}#{number}; "
                f"reconciling activity {activity_id[:8]} on same attempt"
            ]
        recorded["result"] = {"repo": target, "number": number, **discovered}
        store.write("activity", "update", activity_id, strip_row(recorded))
        c["formal_approve_id"] = activity_id
        evidence = c.setdefault("evidence", {})
        if isinstance(evidence, dict):
            evidence["formal_head"] = head
        c["phase"] = "leave_draft"
        save_task(store, task)
        return [f"formal APPROVE on {target}#{number} at {head[:7]}"]

    # Absent: only typed executor rejection facts may invalidate. Transient POST /
    # transport / not-yet-visible results preserve the durable attempt id.
    if recorded is not None and recorded.get("execution_status") == "error":
        err = str(recorded.get("execution_error") or "")
        if err in _FORMAL_OBSERVED_REJECTION_ERRORS:
            reason = (
                "not APPROVED"
                if err == REVIEW_APPROVE_NON_APPROVED
                else "commit_id mismatch"
            )
            _invalidate_stale_formal_approval(task, reason=reason)
            raise CoordinatorError(
                f"formal review is not currently APPROVED on the reviewed head ({reason})"
            )
    return [
        f"formal approval not yet verified on {head[:7]}; retrying same attempt"
    ]


def _task_snapshot(store: Store, tid: str) -> dict[str, Any]:
    task = store.row("task", tid)
    assert task is not None
    checklist_rows = [r for r in store.rows("checklist_item") if r.get("task_id") == tid]
    checklist = canonical_checklist(
        {str(r["key"]): str(r["status"]) for r in checklist_rows}
    )
    checklist_evidence = canonical_checklist({
        str(r["key"]): r.get("evidence") if isinstance(r.get("evidence"), str) else ""
        for r in checklist_rows
    })
    checks = [c for c in store.rows("local_check") if c.get("task_id") == tid]
    ordered_checks = sorted(checks, key=lambda c: c.get("ran_at") or "")
    latest_by_name: dict[str, dict[str, Any]] = {}
    for item in ordered_checks:
        name = item.get("name")
        if name is None:
            continue
        latest_by_name[str(name)] = item
    local_checks = [{"name": name, "result": item.get("result")} for name, item in latest_by_name.items()]
    gates_raw = _ordered_snapshot_gates(store, tid)
    gates = [
        {
            "stage": canonical_stage(str(g.get("stage"))),
            "dimension": g.get("dimension"),
            "vendor": g.get("vendor"),
            "verdict": g.get("verdict"),
            "head_sha": g.get("head_sha") or "",
        }
        for g in gates_raw
    ]
    return {
        "id": task["id"],
        "session_id": task.get("session_id"),
        "workflow": task.get("workflow"),
        "state": task.get("state"),
        "payload": task.get("payload"),
        "checklist": checklist,
        "checklist_evidence": checklist_evidence,
        "summaries": {
            "en": task.get("change_summary_en") or "",
            "de": task.get("change_summary_de") or "",
        },
        "gates": gates,
        "local_checks": local_checks,
        "models": _task_models(task).as_dict(),
        "implementer": implementer_from_snapshot(task),
    }


def _ordered_snapshot_gates(store: Store, tid: str) -> list[dict[str, Any]]:
    """Order gate histories durably and fail closed when legacy order is unknowable."""
    raw = [g for g in store.rows("review_gate") if g.get("task_id") == tid]
    insert_sequences = store.row_insert_sequences("review_gate")
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for gate in raw:
        key = (
            canonical_stage(str(gate.get("stage"))),
            str(gate.get("dimension") or ""),
        )
        grouped.setdefault(key, []).append(gate)

    ordered: list[dict[str, Any]] = []
    for key in sorted(grouped):
        rows = grouped[key]
        durable: list[tuple[int, dict[str, Any]]] = []
        origins: set[str] = set()
        for row in rows:
            row_id = str(row.get("id") or "")
            sequence = insert_sequences.get(row_id)
            if sequence is None or sequence[0] != str(
                row.get("_origin_device_id") or ""
            ):
                durable = []
                break
            origins.add(sequence[0])
            durable.append((sequence[1], row))
        if (
            len(durable) == len(rows)
            and len(origins) == 1
            and len({seq for seq, _ in durable}) == len(rows)
        ):
            ordered.extend(
                row for _seq, row in sorted(durable, key=lambda item: item[0])
            )
            continue

        by_time: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_time.setdefault(str(row.get("recorded_at") or ""), []).append(row)
        for recorded_at in sorted(by_time):
            tied = sorted(
                by_time[recorded_at], key=lambda row: str(row.get("id") or "")
            )
            ordered.extend(tied)
            outcomes = {
                (
                    str(row.get("vendor") or ""),
                    str(row.get("verdict") or ""),
                    str(row.get("head_sha") or ""),
                )
                for row in tied
            }
            if len(outcomes) > 1:
                fail_closed = dict(tied[-1])
                fail_closed["verdict"] = "unavailable"
                fail_closed["head_sha"] = ""
                ordered.append(fail_closed)
    return ordered


def _both_second_reviews_na(store: Store, task_id: str) -> bool:
    rows = canonical_checklist(
        {
            str(row.get("key")): row
            for row in store.rows("checklist_item")
            if row.get("task_id") == task_id
        }
    )
    statuses: dict[str, Any] = {}
    evidence: dict[str, Any] = {}
    for key in ("review_2_quality", "review_2_logic"):
        row = rows.get(key)
        if not isinstance(row, dict):
            return False
        statuses[key] = row.get("status")
        evidence[key] = row.get("evidence")
    return both_second_reviews_na(statuses, evidence)


def _trusted_done_review(
    store: Store, worker: WorkerConfig, task: dict[str, Any], runner: Runner
) -> tuple[str, dict[str, Any] | None]:
    """Live opener and trusted config for a done decision.

    One review is enough only when the pull request opener is listed in
    one_review_authors in the trusted .github/pr-guard.json on the default
    branch. A missing file, a missing key, or an empty list keeps two reviews
    required. An entry that exists only on the pull request head does not count.
    A stored payload copy is not this decision. A lookup failure raises
    ``CiObservationTransportError``.
    """
    if not _both_second_reviews_na(store, task["id"]):
        return "", None
    target = target_repo(task)
    number = as_int(coord(task).get("pr_number"))
    if number is None or not target:
        return "", None
    scoped_runner = scoped(store, worker.session_id, runner)
    pr = _fetch_pr_json(scoped_runner, target, number, "author")
    author = pr.get("author") if isinstance(pr, dict) and isinstance(pr.get("author"), dict) else {}
    login = str(author.get("login") or "")
    if not login:
        return "", None
    try:
        config = fetch_trusted_pr_guard(scoped_runner, target)
    except PrGuardLookupError as exc:
        raise CiObservationTransportError("trusted pr-guard file could not be read") from exc
    return login, config


def _opener_one_review(
    store: Store, worker: WorkerConfig, task: dict[str, Any], runner: Runner
) -> bool:
    """Two reviews are required before Ready.

    One review is enough only when the pull request opener is listed in
    one_review_authors in the trusted .github/pr-guard.json on the default
    branch. A missing file, a missing key, or an empty list keeps two reviews
    required. An entry that exists only on the pull request head does not count.
    """
    login, config = _trusted_done_review(store, worker, task, runner)
    return one_review_for(config, login)


def _second_review_skip(store: Store, worker: WorkerConfig, task: dict[str, Any], runner: Runner, *, phase: str, head: str) -> bool | list[str]:
    """True when the second review may be skipped. A list is a retry message. Two reviews are the standard. One review is enough only when the pull request opener is listed in one_review_authors in the trusted .github/pr-guard.json on the default branch. A missing file, a missing key, or an empty list keeps two reviews required. An entry that exists only on the pull request head does not count."""
    if not _both_second_reviews_na(store, task["id"]):
        return False
    try:
        return _opener_one_review(store, worker, task, runner)
    except CiObservationTransportError:
        c = coord(task)
        c["phase"] = phase
        save_task(store, task)
        return [f"trusted review list temporarily unavailable on {head[:7]}; retrying {phase}"]


def _invalidate_stale_formal_approval(task: dict[str, Any], *, reason: str) -> None:
    """Clear stale formal evidence and pin recovery to formal_approve.

    Human dismissal is not permission to silently re-APPROVE. A later tick must
    wait for an authorized NEW reply, then use a new durable activity occurrence.
    """
    c = coord(task)
    evidence = c.get("evidence") if isinstance(c.get("evidence"), dict) else None
    if isinstance(evidence, dict):
        evidence.pop("formal_head", None)
    c.pop("formal_approve_id", None)
    attempt = as_int(c.get("formal_approve_attempt")) or 0
    c["formal_approve_attempt"] = attempt + 1
    c["resume_phase"] = "formal_approve"
    c["blocker"] = (
        f"formal APPROVE no longer valid on exact head ({reason}); "
        "authorized reply required before a new approval attempt"
    )


def _fresh_formal_still_approved(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
    *,
    head: str,
) -> bool:
    """Fresh GET: stored formal_head is not current GitHub proof.

    Returns True when APPROVED on the exact head. Returns False when the
    successful list has no same-marker hit yet (retry same attempt; do not
    invalidate). Observed same-marker non-APPROVED / misbound invalidates and
    raises so recovery requires a new authorized reply.
    """
    c = coord(task)
    target = target_repo(task)
    number = as_int(c.get("pr_number"))
    if number is None:
        raise CoordinatorError("formal recheck requires PR number")
    if worker.review_session is None:
        raise CoordinatorError("formal recheck requires review_session")
    try:
        review_account = account_for(store, worker.review_session)
        scoped_review = review_account.runner(runner)
    except AccountError as exc:
        raise CoordinatorError(str(exc)) from exc
    activity_id = c.get("formal_approve_id")
    marker = ACTIVITY_MARKER.format(id=activity_id) if isinstance(activity_id, str) else ""
    try:
        status, discovered = _inspect_formal_approve(
            scoped_review,
            repo=target,
            number=number,
            marker=marker or f"Formal approval for head `{head[:7]}`",
            head=head,
            login=review_account.login,
        )
    except CoordinatorError:
        # Transient discovery transport: retry leave-draft without invalidating.
        return False
    if status == "approved" and isinstance(discovered, dict):
        return True
    if status == "invalid":
        reason = "not APPROVED"
        if isinstance(discovered, dict):
            reason = str(discovered.get("reason") or reason)
        _invalidate_stale_formal_approval(task, reason=reason)
        raise CoordinatorError(
            f"formal APPROVE no longer valid on exact head ({reason})"
        )
    # Absence alone is not proof of human dismissal.
    return False


def _require_formal_head_evidence(task: dict[str, Any], head: str, *, when: str) -> None:
    evidence = coord(task).get("evidence") if isinstance(coord(task).get("evidence"), dict) else {}
    if evidence.get("formal_head") != head:
        label = when.strip() or "before leave-draft"
        _invalidate_stale_formal_approval(task, reason=f"formal_head stale ({label})")
        raise CoordinatorError(f"formal approve not on current head{when}")


def _leave_draft_comment(head: str, *, one_review: bool) -> str:
    """Ready evidence comment.

    One review is enough only when the pull request opener is listed in
    one_review_authors in the trusted .github/pr-guard.json on the default
    branch. A missing file, a missing key, or an empty list keeps two reviews
    required. An entry that exists only on the pull request head does not count.
    """
    if one_review:
        lead = (
            f"Ready for review: one review run is approved on `{head[:7]}` "
            f"and CI green, because this pull request opener is listed in one_review_authors. "
        )
    else:
        lead = (
            f"Ready for review: two review runs are required and approved on `{head[:7]}` "
            f"and CI green. "
        )
    return (
        lead
        + "One review is enough only when the pull request opener is listed in "
        "one_review_authors in the trusted .github/pr-guard.json on the default branch. "
        "A missing file, a missing key, or an empty list keeps two reviews required. "
        "An entry that exists only on the pull request head does not count. "
        "Still not merge; a human merges."
    )


def _revoked_exception_comment(head: str) -> str:
    """Correction when the one-review exception disappears before Ready.

    One review is enough only when the pull request opener is listed in
    one_review_authors in the trusted .github/pr-guard.json on the default
    branch. A missing file, a missing key, or an empty list keeps two reviews
    required. An entry that exists only on the pull request head does not count.
    """
    return (
        f"Ready was not set on `{head[:7]}`. "
        "The one-review exception was no longer present when leave-draft was about to "
        "mark the pull request ready. Two review runs are required. "
        "One review is enough only when the pull request opener is listed in "
        "one_review_authors in the trusted .github/pr-guard.json on the default branch. "
        "A missing file, a missing key, or an empty list keeps two reviews required. "
        "An entry that exists only on the pull request head does not count. "
        "Still not merge; a human merges."
    )


def _raise_revoked_one_review(
    store: Store,
    worker: WorkerConfig,
    runner: Runner,
    task: dict[str, Any],
    *,
    target: str,
    number: int,
    head: str,
) -> None:
    """Post the correction once, then refuse Ready.

    One review is enough only when the pull request opener is listed in
    one_review_authors in the trusted .github/pr-guard.json on the default
    branch. A missing file, a missing key, or an empty list keeps two reviews
    required. An entry that exists only on the pull request head does not count.
    """
    withdrawn_id = str(
        uuid5(NAMESPACE_URL, f"coordinator-ready-withdrawn:{task['id']}:{head}")
    )
    existing = store.row("activity", withdrawn_id)
    if existing is None or existing.get("execution_status") != "done":
        queue_activity(
            store,
            activity_id=withdrawn_id,
            session_id=worker.session_id,
            typ="comment.post",
            payload={
                "repo": target,
                "number": number,
                "body": _revoked_exception_comment(head),
                "target": "pr",
            },
        )
        execute_github(store, runner, activity_ids=(withdrawn_id,))
        withdrawn = store.row("activity", withdrawn_id)
        if withdrawn is None or withdrawn.get("execution_status") != "done":
            raise CoordinatorError("revoked one-review comment not verified")
    raise CoordinatorError(
        "one-review exception revoked before leave-draft; two reviews are required"
    )


def phase_leave_draft(store: Store, worker: WorkerConfig, task: dict[str, Any], runner: Runner) -> list[str]:
    if worker.a38:
        raise CoordinatorError("A38 mode never leaves draft; the guard owns Ready")
    c = coord(task)
    target = target_repo(task)
    number = as_int(c.get("pr_number"))
    worktree = str(c.get("worktree") or "")
    head = verify_signed_clean_head(store, worker, runner, worktree)
    if number is None:
        raise CoordinatorError("leave-draft requires PR number")
    cfg = repo_cfg(worker, str(c["source"]["repo"]), task)

    # Recheck allow pr-ready and fresh evidence before transition.
    task["state"] = "pr-review"
    save_task(store, task)
    snap = _task_snapshot(store, task["id"])
    allow = evaluate_allow(
        "pr-ready",
        session_id=worker.session_id,
        task_id=task["id"],
        session_tasks=[snap],
    )
    if not allow.allowed:
        raise CoordinatorError(f"pr-ready denied: {allow.reason}")

    evidence = c.get("evidence") if isinstance(c.get("evidence"), dict) else {}
    _require_formal_head_evidence(task, head, when="")
    if not evidence.get("tests_pass") or evidence.get("tests_head") != head:
        raise CoordinatorError("tests not green before leave-draft")
    if evidence.get("readiness_head") != head:
        raise CoordinatorError("readiness evidence not on current head before leave-draft")
    retry = _fresh_ci_still_green(store, worker, task, runner, head)
    if retry is not None:
        c["phase"] = "leave_draft"
        save_task(store, task)
        return [retry]
    # Fail fast when already dismissed; a second check after readiness is still
    # required because readiness can take up to check_timeout.
    if not _fresh_formal_still_approved(store, worker, task, runner, head=head):
        c["phase"] = "leave_draft"
        save_task(store, task)
        return [f"formal APPROVE not yet visible on {head[:7]}; retrying leave-draft"]
    latest = latest_gates(store, task["id"])
    skip_second = _second_review_skip(store, worker, task, runner, phase="leave_draft", head=head)
    if isinstance(skip_second, list):
        return skip_second
    # A previous tick already published the one-review evidence comment. If the
    # live list no longer grants the exception, correct it before the gate
    # loop. Keys that are no longer n_a are a real two-review pass and still
    # go through the gates. A transport error already returned above.
    if not skip_second and _both_second_reviews_na(store, task["id"]):
        one_id = str(
            uuid5(NAMESPACE_URL, f"coordinator-ready-comment:{task['id']}:{head}:one")
        )
        one_row = store.row("activity", one_id)
        if one_row is not None and one_row.get("execution_status") == "done":
            _raise_revoked_one_review(
                store,
                worker,
                runner,
                task,
                target=target,
                number=number,
                head=head,
            )
    models = _task_models(task)
    for stage, dimension, slot in GATE_PAIRS:
        if stage == "review-2" and skip_second:
            continue
        vendor = models.for_slot(slot)
        g = latest.get((stage, dimension))
        if g is None or g.get("verdict") != "approved" or g.get("head_sha") != head or g.get("vendor") != vendor:
            raise CoordinatorError(f"missing approved gate {stage}/{dimension} before leave-draft")

    scoped_runner = scoped(store, worker.session_id, runner)
    try:
        pr = _fetch_pr_json(
            scoped_runner,
            target,
            number,
            "headRefOid,baseRefName,author,isDraft,state,mergeable",
        )
    except CiObservationTransportError:
        c["phase"] = "leave_draft"
        save_task(store, task)
        return [f"PR metadata temporarily unavailable on {head[:7]}; retrying leave-draft"]
    if not isinstance(pr, dict):
        raise CoordinatorError("PR view failed")
    if str(pr.get("headRefOid") or "").lower() != head:
        raise CoordinatorError("PR head mismatch before leave-draft")
    if str(pr.get("mergeable") or "").upper() != "MERGEABLE":
        raise CoordinatorError("PR not mergeable before leave-draft")
    if str(pr.get("baseRefName") or "") != cfg.base:
        raise CoordinatorError("PR base mismatch before leave-draft")
    if pr.get("isDraft") is not True:
        # Already left draft — verify and continue only with matching head.
        if pr.get("isDraft") is False and str(pr.get("state") or "").upper() == "OPEN":
            c["phase"] = "await_merge"
            save_task(store, task)
            return [f"already ready {target}#{number}; awaiting human merge"]
        raise CoordinatorError("PR draft state unexpected before leave-draft")

    # Readiness can take up to check_timeout; a dismissal during that window must
    # still block leave-draft. Formal APPROVE freshness is checked again below,
    # immediately before the Ready mutation — not only before readiness.
    fresh = phase_readiness(store, worker, task, runner)
    if coord(task).get("phase") != "formal_approve":
        return fresh
    head = verify_signed_clean_head(store, worker, runner, worktree)
    evidence = c.get("evidence") if isinstance(c.get("evidence"), dict) else {}
    _require_formal_head_evidence(task, head, when=" after readiness")
    if not _fresh_formal_still_approved(store, worker, task, runner, head=head):
        c["phase"] = "leave_draft"
        save_task(store, task)
        return [f"formal APPROVE not yet visible on {head[:7]}; retrying leave-draft"]
    c["phase"] = "leave_draft"
    save_task(store, task)

    one_review = bool(skip_second)
    body = _leave_draft_comment(head, one_review=one_review)
    # The decision is part of the id. A later pass that no longer has the
    # exception posts its own two-review comment instead of reusing this one.
    decision = "one" if one_review else "two"
    activity_id = str(
        uuid5(NAMESPACE_URL, f"coordinator-ready-comment:{task['id']}:{head}:{decision}")
    )
    queue_activity(
        store,
        activity_id=activity_id,
        session_id=worker.session_id,
        typ="comment.post",
        payload={"repo": target, "number": number, "body": body, "target": "pr"},
    )
    execute_github(store, runner, activity_ids=(activity_id,))
    ready_row = store.row("activity", activity_id)
    if ready_row is None or ready_row.get("execution_status") != "done":
        raise CoordinatorError("Ready evidence comment not verified")

    # Publishing the evidence comment is itself an external call; recheck after it.
    if not _fresh_formal_still_approved(store, worker, task, runner, head=head):
        return [f"formal APPROVE not yet visible on {head[:7]}; retrying leave-draft"]
    retry_after_comment = _fresh_ci_still_green(store, worker, task, runner, head)
    if retry_after_comment is not None:
        c["phase"] = "leave_draft"
        save_task(store, task)
        return [retry_after_comment]
    # The evidence comment is an external call. Re-read the trusted list before
    # Ready. One review is enough only while the opener is still listed.
    if skip_second:
        again = _second_review_skip(store, worker, task, runner, phase="leave_draft", head=head)
        if isinstance(again, list):
            return again
        if not again:
            _raise_revoked_one_review(
                store,
                worker,
                runner,
                task,
                target=target,
                number=number,
                head=head,
            )
    ready = scoped_runner(["gh", "pr", "ready", str(number), "--repo", target])
    if ready.returncode != 0:
        raise CoordinatorError(redact(ready.stderr or ready.stdout or "gh pr ready failed"))
    verify = gh_json(
        scoped_runner,
        ["gh", "pr", "view", str(number), "--repo", target, "--json", "isDraft,headRefOid,state"],
    )
    if verify.get("isDraft") is not False:
        raise CoordinatorError("leave-draft did not clear isDraft")
    if str(verify.get("headRefOid") or "").lower() != head:
        if verify.get("state") == "OPEN" and verify.get("isDraft") is False:
            undone = scoped_runner(["gh", "pr", "ready", str(number), "--repo", target, "--undo"])
            if undone.returncode != 0:
                raise CoordinatorError("PR changed during Ready; returning it to Draft also failed")
        raise CoordinatorError("PR head changed during leave-draft; readiness is not verified")
    if str(verify.get("state") or "").upper() != "OPEN":
        raise CoordinatorError("PR not open after leave-draft")
    c["phase"] = "await_merge"
    c["ready_comment_id"] = activity_id
    if isinstance(evidence, dict):
        evidence["ready_head"] = head
    task["state"] = "pr-review"
    save_task(store, task)
    return [f"left draft {target}#{number}; awaiting human merge"]


def _record_human_merge(
    store: Store,
    worker: WorkerConfig,
    *,
    target: str,
    number: int,
    sha: str,
    merged_at: str,
    login: str,
    merged_type: str,
    expected_head: str,
    url: str,
    pr_open_id: Any,
) -> None:
    """Record the human merge once. This does not mark the task done."""
    mid = str(uuid5(NAMESPACE_URL, f"coordinator-merged:{target}:{number}:{sha}"))
    if store.row("activity", mid) is not None:
        return
    store.write(
        "activity",
        "insert",
        mid,
        {
            "id": mid,
            "session_id": worker.session_id,
            "type": "pr.merged",
            "payload": {
                "repo": target,
                "number": number,
                "url": url,
                "merge_sha": sha,
                "merged_at": merged_at,
                "merged_by": login.casefold(),
                "merged_by_type": merged_type,
                "reviewed_head": expected_head,
                "pr_open_id": pr_open_id,
            },
            "execution_status": "done",
        },
    )


def _mandate_states_no_deviation(source: dict[str, Any], assignment_actor: str | None) -> bool:
    """True when the pinned hub mandate carries the owner's explicit no-deviation statement."""
    mandate = source.get("hub_assignment_mandate")
    if not isinstance(mandate, dict) or not isinstance(assignment_actor, str):
        return False
    row_id = mandate.get("row_id")
    mandate_actor = mandate.get("actor")
    issue_key = mandate.get("issue_key")
    deviation = mandate.get("deviation")
    expected_issue_key = f"{source.get('repo')}#{source.get('number')}"
    return (
        isinstance(row_id, str)
        and bool(row_id)
        and row_id == source.get("hub_assignment_row_id")
        and isinstance(mandate_actor, str)
        and mandate_actor.casefold() == assignment_actor.casefold()
        and mandate.get("origin") == f"web:{mandate_actor}"
        and isinstance(issue_key, str)
        and issue_key.casefold() == expected_issue_key.casefold()
        and _states_no_deviation(deviation, mandate_actor)
    )


def _readiness_keys_closed(store: Store, task: dict[str, Any], head: str) -> bool:
    """Readiness counts as closed only when it ran on this exact head."""
    evidence = coord(task).get("evidence")
    if not isinstance(evidence, dict) or evidence.get("readiness_head") != head:
        return False
    task_id = str(task["id"])
    statuses = {
        str(row.get("key")): str(row.get("status") or "")
        for row in store.rows("checklist_item")
        if row.get("task_id") == task_id
    }
    return (
        statuses.get("contributing_ok") == "ja"
        and statuses.get("deviation_declared") in {"ja", "n_a"}
        and statuses.get("deviation_granted") in {"ja", "n_a"}
    )


def _close_readiness_after_merge(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
    *,
    head: str,
) -> None:
    """Close readiness on an A38 head merged before the readiness phase ran."""
    if _readiness_keys_closed(store, task, head):
        return
    c = coord(task)
    cfg = repo_cfg(worker, str(c["source"]["repo"]), task)
    local_head = verify_signed_clean_head(
        store, worker, runner, str(c.get("worktree") or "")
    )
    if local_head != head:
        raise CoordinatorError("merged PR head does not match clean signed readiness head")
    base_sha = str(c.get("base_sha") or "")
    readiness = _readiness_for_head(
        worker,
        task,
        cfg,
        head=head,
        base_sha=base_sha,
    )
    if verify_signed_clean_head(
        store, worker, runner, str(c.get("worktree") or "")
    ) != head:
        raise CoordinatorError("head modified by merged-head readiness")
    _apply_readiness_checklist(store, worker, task, readiness, head=head)
    evidence = c.setdefault("evidence", {})
    if isinstance(evidence, dict):
        evidence["readiness_head"] = head
        evidence["readiness_base"] = base_sha or cfg.base
        evidence["readiness_at"] = utcnow()
    save_task(store, task)


def _block_merged_readiness(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
    message: str,
) -> list[str]:
    c = coord(task)
    c["phase"] = "blocked"
    c["resume_phase"] = "await_merge"
    c["blocker"] = message
    save_task(store, task)
    return publish_blocker(
        store,
        worker,
        task,
        runner,
        message,
        kind="readiness",
        reply_checkpoint=True,
    )


def phase_await_merge(store: Store, worker: WorkerConfig, task: dict[str, Any], runner: Runner) -> list[str]:
    c = coord(task)
    target = target_repo(task)
    number = as_int(c.get("pr_number"))
    expected_head = str(c.get("head_sha") or "")
    if number is None:
        raise CoordinatorError("await_merge requires PR number")
    scoped_runner = scoped(store, worker.session_id, runner)
    info = gh_json(
        scoped_runner,
        ["gh", "api", f"repos/{target}/pulls/{number}"],
    )
    state = str(info.get("state") or "").upper()
    if info.get("merged") is True:
        sha = str(info.get("merge_commit_sha") or "")
        merged_at = str(info.get("merged_at") or "")
        merged_by = info.get("merged_by") if isinstance(info.get("merged_by"), dict) else None
        if not sha or not merged_at:
            return ["merge observed but incomplete metadata"]
        if merged_by is None:
            return ["merge observed but mergedBy missing"]
        # REQUIRE actual type User from REST pulls API. Never default missing type to human.
        merged_type = str(merged_by.get("type") or "")
        login = str(merged_by.get("login") or "")
        if not login:
            return ["merge observed but mergedBy login missing"]
        if merged_type not in ("User",):
            c["phase"] = "blocked"
            c["blocker"] = f"merge by non-human or unknown actor type ({merged_type or 'missing'})"
            # Intentionally non-recoverable: drop stale ask/CI/formal checkpoints
            # so a later authorized comment cannot resume implement.
            clear_reply_recovery(c)
            save_task(store, task)
            return publish_blocker(
                store,
                worker,
                task,
                runner,
                f"Merge was not by a human User (got {merged_type or 'missing type'})",
                kind="nonhuman-merge",
            )
        cfg = repo_cfg(worker, str(c["source"]["repo"]), task)
        if str((info.get("base") or {}).get("ref") or "") != cfg.base:
            raise CoordinatorError("merged PR base mismatch")
        if worker.a38:
            observed_base = str((info.get("base") or {}).get("sha") or "").lower()
            saved_base = str(c.get("base_sha") or "").lower()
            if not is_sha(observed_base):
                return ["merge observed but base SHA missing or invalid"]
            if not is_sha(saved_base) or observed_base != saved_base:
                from .coordinator_a38 import invalidate_base_evidence

                invalidate_base_evidence(c, observed_base)
                c["phase"] = "tests"
                save_task(store, task)
                return [
                    "merged PR base changed; invalidating A38 report evidence "
                    "before task completion"
                ]
        if str((info.get("head") or {}).get("sha") or "") != expected_head:
            raise CoordinatorError("merged PR head differs from reviewed head")
        # Do not manufacture checklist values or boilerplate summaries here.
        # The ordinary route closes readiness before Ready; an early A38 merge
        # closes it below from the same trusted source before task-done.
        if not (task.get("change_summary_en") and task.get("change_summary_de")):
            return [
                "human merge observed; task-done blocked: missing change summaries "
                "describing the actual result"
            ]
        pr_open_id = c.get("pr_open_activity_id")
        try:
            opener, config = _trusted_done_review(store, worker, task, runner)
        except CiObservationTransportError:
            _record_human_merge(
                store,
                worker,
                target=target,
                number=number,
                sha=sha,
                merged_at=merged_at,
                login=login,
                merged_type=merged_type,
                expected_head=expected_head,
                url=str(info.get("html_url") or ""),
                pr_open_id=pr_open_id,
            )
            return ["trusted review list could not be read; retrying await_merge"]
        if worker.a38:
            second_review = _a38_second_review_outcome(c, expected_head)
            if second_review != "passed":
                return _await_second_review_after_merge(
                    store,
                    worker,
                    task,
                    runner,
                    expected_head,
                    blocking=second_review == "blocking",
                )
            if not _readiness_keys_closed(store, task, expected_head):
                try:
                    _close_readiness_after_merge(
                        store,
                        worker,
                        task,
                        runner,
                        head=expected_head,
                    )
                except (CoordinatorError, StoreError) as exc:
                    return _block_merged_readiness(
                        store, worker, task, runner, str(exc)
                    )
        snap = _task_snapshot(store, task["id"])
        snap["pull_opener"] = opener
        snap["trusted_pr_guard"] = config
        allow = evaluate_allow(
            "task-done",
            session_id=worker.session_id,
            task_id=task["id"],
            session_tasks=[snap],
        )
        if not allow.allowed:
            return [f"human merge observed; task-done blocked: {allow.reason} {allow.blocking}"]

        _record_human_merge(
            store,
            worker,
            target=target,
            number=number,
            sha=sha,
            merged_at=merged_at,
            login=login,
            merged_type=merged_type,
            expected_head=expected_head,
            url=str(info.get("html_url") or ""),
            pr_open_id=pr_open_id,
        )
        source = c["source"]
        assigned_id = source.get("assigned_id")
        if isinstance(assigned_id, str) and assigned_id:
            ack_id = str(uuid5(NAMESPACE_URL, f"coordinator-ack:{assigned_id}"))
            if store.row("activity", ack_id) is None:
                store.write(
                    "activity",
                    "insert",
                    ack_id,
                    {
                        "id": ack_id,
                        "session_id": worker.session_id,
                        "type": "issue.assigned.ack",
                        "payload": {
                            "assigned_id": assigned_id,
                            "repo": source["repo"],
                            "number": source["number"],
                        },
                        "execution_status": "done",
                    },
                )
        task["state"] = "done"
        c["phase"] = "done"
        save_task(store, task)
        return [f"human merge verified {target}#{number}; task done"]
    if worker.a38 and state == "OPEN" and info.get("draft") is True:
        c["phase"] = "await_ready"
        save_task(store, task)
        return [f"pull request returned to draft on {expected_head[:7]}; awaiting guard Ready"]
    if state == "CLOSED":
        c["phase"] = "blocked"
        c["blocker"] = "Ready PR closed without merge"
        # Intentionally non-recoverable: drop stale ask/CI/formal checkpoints
        # so a later authorized comment cannot resume implement.
        clear_reply_recovery(c)
        save_task(store, task)
        return publish_blocker(
            store,
            worker,
            task,
            runner,
            "Pull request was closed without a verified human merge.",
            kind="closed-unmerged",
        )
    return [f"awaiting human merge of {target}#{number}"]


def _a38_second_review_outcome(c: dict[str, Any], head: str) -> str:
    """Return "blocking", "passed" or "missing" from the newest posted second review of head."""
    a38 = c.get("a38")
    state = a38.get("second_review") if isinstance(a38, dict) else None
    if not isinstance(state, dict) or state.get("head") != head or state.get("status") != "posted":
        return "missing"
    # The newest posted second review decides (docs/a38.md): a later clearing
    # review lifts an earlier block, a later block overrides an earlier pass.
    from .coordinator_a38 import newest_reviews_block

    reviews = [review for review in state.get("reviews") or [] if isinstance(review, dict)]
    if newest_reviews_block(reviews):
        return "blocking"
    return "passed"


def _await_second_review_after_merge(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
    head: str,
    *,
    blocking: bool,
) -> list[str]:
    """A merge can land before the external second review (the guard sets Ready
    without it). Keep accepting that review for the merged head for one review
    timeout, then stop with a visible, non-recoverable blocker. A blocking review
    of the merged head stops at once."""
    c = coord(task)
    if blocking:
        c["phase"] = "blocked"
        c["blocker"] = f"merged although the external second review blocks the merge on {head}"
        clear_reply_recovery(c)
        save_task(store, task)
        return publish_blocker(
            store,
            worker,
            task,
            runner,
            f"Pull request was merged on {head[:7]} although the external second review blocks the merge.",
            kind="merged-with-blocking-second-review",
        )
    a38 = c.setdefault("a38", {})
    stamp = utcnow()
    now = _parse_coordinator_time(stamp)
    since = _parse_coordinator_time(a38.get("merged_waiting_since"))
    if now is None:
        raise CoordinatorError("coordinator clock returned an invalid timestamp")
    if since is None:
        a38["merged_waiting_since"] = stamp
        save_task(store, task)
        return [f"human merge observed on {head[:7]}; awaiting the external second review"]
    if (now - since).total_seconds() < worker.second_review_timeout_seconds:
        return [f"human merge observed on {head[:7]}; awaiting the external second review"]
    c["phase"] = "blocked"
    c["blocker"] = f"merged without an external second review on {head}"
    clear_reply_recovery(c)
    save_task(store, task)
    return publish_blocker(
        store,
        worker,
        task,
        runner,
        f"Pull request was merged on {head[:7]} without an external second review.",
        kind="merged-without-second-review",
    )


def phase_read_replies(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
) -> list[str]:
    c = coord(task)
    if c.get("uncertain_lane"):
        return [
            "blocked: uncertain prior lane outcome; refusing model start on reply alone"
        ]
    if task.get("state") == "failed":
        return [
            "blocked: task failed; refusing reply resume without eligible recovery"
        ]
    if task.get("state") == "done":
        return [
            "blocked: task done; refusing reply resume without eligible recovery"
        ]
    # Never consume or resume without a safe eligible resume_phase. Missing
    # resume_phase must not default to implement (closed-unmerged / non-human
    # merge / terminal outcomes leave stale checkpoints otherwise).
    if not reply_checkpoint_eligible(task, c):
        source = c.get("source") if isinstance(c.get("source"), dict) else {}
        repo = str(source.get("repo") or "?")
        number = source.get("number") or "?"
        return [
            f"blocked: no eligible resume_phase for reply recovery on {repo}#{number}"
        ]
    source = c["source"]
    repo = str(source["repo"])
    number = int(source["number"])
    scoped_runner = scoped(store, worker.session_id, runner)
    account = account_for(store, worker.session_id)
    comments = gh_list(
        scoped_runner,
        ["gh", "api", "--paginate", "--slurp", f"repos/{repo}/issues/{number}/comments"],
    )
    allowed = set(worker.reply_logins)
    q_activity = c.get("question_activity_id")
    if not isinstance(q_activity, str) or not q_activity:
        return [f"waiting for question checkpoint on {repo}#{number}"]
    q_marker = f"{QUESTION_MARKER_PREFIX}{q_activity} -->"
    # Also accept ACTIVITY_MARKER form if executor rewrote body, and status
    # checkpoints published for reply-recoverable external blockers.
    q_marker_alt = ACTIVITY_MARKER.format(id=q_activity)
    status_needle = f":{q_activity} -->"

    question_id: int | None = None
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        body = comment.get("body") if isinstance(comment.get("body"), str) else ""
        user = comment.get("user") if isinstance(comment.get("user"), dict) else {}
        login = str(user.get("login") or "").casefold()
        if login != account.login.casefold():
            continue
        status_hit = STATUS_MARKER_PREFIX in body and status_needle in body
        if q_marker in body or q_marker_alt in body or status_hit:
            cid = as_int(comment.get("id"))
            if cid is None:
                continue
            question_id = cid
            break
    if question_id is None:
        return [f"waiting for verified own question comment on {repo}#{number}"]

    consumed = c.get("replies_consumed_through")
    consumed_id = as_int(consumed) if consumed is not None else None
    new_replies: list[str] = []
    last_id = consumed_id if consumed_id is not None else question_id
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        cid = as_int(comment.get("id"))
        if cid is None:
            continue
        # Only replies strictly after the verified own question comment id.
        if cid <= question_id:
            continue
        if consumed_id is not None and cid <= consumed_id:
            continue
        # Valid monotonic ids only.
        if last_id is not None and cid <= last_id:
            continue
        user = comment.get("user") if isinstance(comment.get("user"), dict) else {}
        login = str(user.get("login") or "").casefold()
        if login not in allowed:
            continue
        body = comment.get("body") if isinstance(comment.get("body"), str) else ""
        # Untrusted spec only — no control fields; do not treat copied markers as authority.
        new_replies.append(redact(body, limit=2000))
        last_id = cid

    if not new_replies:
        return [f"waiting for authorized reply on {repo}#{number}"]
    existing = c.get("authorized_replies")
    if not isinstance(existing, list):
        existing = []
    existing.extend(new_replies)
    c["authorized_replies"] = existing
    c["replies_consumed_through"] = last_id
    # Resume the exact safe script phase persisted at the blocker — never blindly
    # start implement for CI authorization / checkout / acceptance administrative issues.
    resume = str(c.get("resume_phase") or "")
    c["phase"] = resume
    c.pop("resume_phase", None)
    c.pop("blocker_publish_once", None)
    c.pop("blocker_publish_kind", None)
    c.pop("blocker_activity_id", None)
    if resume == "implement":
        task["state"] = "implementing"
    save_task(store, task)
    return [f"consumed {len(new_replies)} authorized reply(ies); resuming {c['phase']}"]


def phase_blocked(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
) -> list[str]:
    """Externally blocked tasks remain reply-recoverable when outcome is certain."""
    c = coord(task)
    if c.get("uncertain_lane"):
        return publish_blocker(
            store,
            worker,
            task,
            runner,
            str(c.get("blocker") or "uncertain lane outcome"),
            kind="uncertain-lane",
        )
    blocker = redact(str(c.get("blocker") or "blocked"))
    if c.get("blocker_publish_once"):
        blocker_activity_id = c.get("blocker_activity_id")
        if not isinstance(blocker_activity_id, str) or not blocker_activity_id:
            return publish_blocker(
                store,
                worker,
                task,
                runner,
                blocker,
                kind=str(c.get("blocker_publish_kind") or "blocker"),
                reply_checkpoint=True,
            )
    # Try authorized reply recovery without starting a model blindly.
    prior_phase = str(c.get("phase") or "blocked")
    lines = phase_read_replies(store, worker, task, runner)
    resumed = str(coord(task).get("phase") or "")
    if resumed not in ("ask", "blocked", prior_phase) and resumed:
        return lines
    if c.get("blocker_publish_once"):
        return lines
    return publish_blocker(store, worker, task, runner, blocker, kind="status")
