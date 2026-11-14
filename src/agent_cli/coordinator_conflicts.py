"""Conflict remedy for ``pr_mergeable`` on order tasks.

The coordinator script orchestrates the remedy; the conflict resolution in
``conflict_mandate.py`` performs the work.
"""

from __future__ import annotations

from typing import Any

from .preconditions import REMEDY_ACTIONS
from .conflict_mandate import discard_script_mandate, run_script_mandate
from .conflict_mandate_config import ConflictRepository, ConflictWorker
from .conflict_mandate_state import Request
from .coordinator_checks import (
    CheckContext,
    CheckValue,
    Deferred,
    _commit,
    current_phase,
    head_of,
    one_line,
    question_text,
    record_check,
    resolve_action,
)
from .coordinator_common import (
    CoordinatorError,
    as_int,
    coord,
    is_sha,
    order_id,
    redact,
    target_repo,
)
from .coordinator_config import WorkerConfig
from .coordinator_git import adopt_published_head, control_dir
from .coordinator_github import publish_blocker
from .coordinator_lanes import invalidate_head_evidence, lane_retry
from .store import utcnow


REMEDY_DIR = "conflict-remedy"
REASON_BOUND = 600


def remedy_state(
    task: dict[str, Any], key: str = "pr_mergeable"
) -> dict[str, Any]:
    """Return a shallow copy of one remedy marker."""
    remedies = coord(task).get("remedies")
    state = remedies.get(key) if isinstance(remedies, dict) else None
    return dict(state) if isinstance(state, dict) else {}


def remedy_starts(task: dict[str, Any]) -> int:
    """Requests used since the most recent authorized human reply."""
    state = remedy_state(task)
    replies = len(coord(task).get("authorized_replies") or [])
    if replies > int(state.get("replies") or 0):
        return 0
    return int(state.get("starts") or 0)


def conflict_worker(worker: WorkerConfig, task: dict[str, Any]) -> ConflictWorker:
    """Build the isolated conflict worker for one coordinator task."""
    repo = target_repo(task)
    remedy = worker.conflict_remedy
    return ConflictWorker(
        session_id=worker.session_id,
        workspace_root=control_dir(worker, str(task["id"])) / REMEDY_DIR,
        repositories={
            repo: ConflictRepository(repo=repo, check_argv=remedy.check_argv)
        },
        request_logins=(),
        poll_seconds=worker.poll_seconds,
        lane_timeout=worker.lane_timeout,
        check_timeout=worker.check_timeout,
        max_rebase_steps=remedy.max_rebase_steps,
    )


def conflict_request(task: dict[str, Any], state: dict[str, Any]) -> Request:
    """Build the conflict engine request identified by the remedy marker."""
    return Request(
        repo=target_repo(task),
        number=as_int(coord(task).get("pr_number")),
        comment_id=int(state["request"]),
        created_at=str(state["mandated_at"]),
    )


def resolve_conflict_mandate(ctx: CheckContext) -> CheckValue:
    """Resolve the order itself as the script-side conflict mandate."""
    task = ctx.task
    worker = ctx.worker
    c = coord(task)
    head = head_of(task)
    state = remedy_state(task)
    running = state.get("state") == "running" and state.get("head") == head
    if worker.conflict_remedy is None:
        return CheckValue(
            "nein",
            "conflict resolution is not configured for this worker",
            head=head or None,
        )
    used = remedy_starts(task) - (1 if running else 0)
    if used >= worker.conflict_remedy.max_attempts:
        return CheckValue(
            "nein",
            f"conflict resolution ran {used} times without a human decision",
            head=head or None,
        )
    number = (
        int(state["request"])
        if running
        else int(state.get("request") or 0) + 1
    )
    source = c.get("source") if isinstance(c.get("source"), dict) else {}
    return CheckValue(
        "ja",
        f"order {order_id(task)} submitted by {source.get('submitted_by')}: "
        f"conflict resolution {number} of pull request "
        f"#{as_int(c.get('pr_number'))} on {head[:7]}",
        head=head,
    )


def _ask(
    ctx: CheckContext, evidence: str, *, keep_running: bool = False
) -> list[str]:
    phase = current_phase(ctx.task)
    store = ctx.store
    worker = ctx.worker
    task = ctx.task
    runner = ctx.runner
    check = ctx.check
    value = CheckValue("nein", evidence, head=head_of(task) or None)
    record_check(store, task, check, value, action=ctx.action)
    text = question_text(worker, task, check, value)

    def change(changed: dict[str, Any]) -> None:
        cp = coord(changed)
        cp["phase"] = "blocked"
        cp["resume_phase"] = phase
        cp["failed_check"] = check.key
        cp["blocker"] = f"Check {check.key} failed: {evidence}"
        cp["question_text"] = redact(text, limit=1500)
        cp.pop("gate_route", None)
        marker = cp.setdefault("remedies", {}).setdefault(
            check.key,
            {
                "name": "conflict_resolution",
                "request": 0,
                "starts": 0,
                "replies": len(cp.get("authorized_replies") or []),
                "blocks": 0,
            },
        )
        marker["blocks"] = int(marker.get("blocks") or 0) + 1
        marker["reason"] = one_line(evidence, REASON_BOUND)
        marker["at"] = utcnow()
        if not keep_running:
            marker["state"] = "blocked"

    _commit(store, task, change)
    marker = remedy_state(task)
    return publish_blocker(
        store,
        worker,
        task,
        runner,
        text,
        kind=check.block_kind or f"check-{check.key}",
        reply_checkpoint=True,
        occurrence=(
            f"{task['id']}:conflict:{marker.get('request', 0)}:"
            f"{marker['blocks']}"
        ),
    )


def run_conflict_remedy(ctx: CheckContext, value: CheckValue) -> list[str]:
    """Start or continue the conflict remedy for one order task."""
    del value
    store = ctx.store
    worker = ctx.worker
    task = ctx.task
    runner = ctx.runner
    check = ctx.check
    phase = current_phase(task)
    head = head_of(task)
    source = coord(task).get("source")
    source = source if isinstance(source, dict) else {}
    base = str(source.get("base") or "the base branch")
    prefix = f"pull request has merge conflicts with {base}"
    repo = target_repo(task)
    number = as_int(coord(task).get("pr_number"))

    state = remedy_state(task)
    running = state.get("state") == "running"
    resolved = resolve_action(
        store,
        worker,
        task,
        runner,
        ctx.lane_runner,
        action=REMEDY_ACTIONS[check.remedy],
    )
    if isinstance(resolved, Deferred):
        return list(resolved.lines)
    refused = next(
        (
            value.evidence or f"check {candidate.key} is {value.status}"
            for candidate, value in resolved
            if value.status != "ja"
        ),
        "",
    )
    if running and state.get("head") != head:
        refused = "the recorded conflict resolution belongs to another head"
    if refused:
        if running and worker.conflict_remedy is not None:
            discard_script_mandate(
                conflict_worker(worker, task), conflict_request(task, state)
            )
            evidence = (
                f"{prefix}; conflict resolution {state['request']} was stopped: "
                f"{refused}"
            )
        else:
            evidence = f"{prefix}; conflict resolution did not start: {refused}"
        return _ask(ctx, evidence)

    if not running:
        now = utcnow()
        marker = {
            "name": "conflict_resolution",
            "state": "running",
            "request": int(state.get("request") or 0) + 1,
            "starts": remedy_starts(task) + 1,
            "replies": len(coord(task).get("authorized_replies") or []),
            "blocks": int(state.get("blocks") or 0),
            "head": head,
            "mandated_at": now,
            "at": utcnow(),
        }

        def start(changed: dict[str, Any]) -> None:
            coord(changed).setdefault("remedies", {})["pr_mergeable"] = marker

        _commit(store, task, start)
        state = remedy_state(task)
        lines = [
            f"conflict resolution {state['request']} started for "
            f"{repo}#{number} on {head[:7]}"
        ]
    else:
        lines = []

    k = state["request"]
    record_check(
        store,
        task,
        check,
        CheckValue(
            "nein",
            f"{prefix}; conflict resolution {k} is running",
            head=head,
        ),
        action=ctx.action,
    )
    cw = conflict_worker(worker, task)
    request = conflict_request(task, state)
    run = run_script_mandate(
        store,
        cw,
        request,
        mandated_head=head,
        runner=runner,
        lane_runner=ctx.lane_runner,
    )
    lines += [one_line(line, 300) for line in run.lines]

    if run.outcome == "blocked":
        return lines + _ask(
            ctx,
            f"{prefix}; conflict resolution {k} ended with a blocker: "
            f"{one_line(run.reason, REASON_BOUND)}",
        )
    if run.outcome == "pending":
        if run.decided:
            return lines + [
                f"conflict resolution {k} is finishing on {head[:7]}; "
                f"retrying {phase}"
            ]
        retry = lane_retry(
            store,
            worker,
            task,
            phase=phase,
            reason=(
                f"conflict resolution {k}: {one_line(run.reason, 200)}"
            ),
        )
        if retry is not None:
            return lines + retry
        discard_script_mandate(cw, request)
        return lines + _ask(
            ctx,
            f"{prefix}; conflict resolution {k} did not complete after "
            f"{worker.lane_retries + 1} attempts: "
            f"{one_line(run.reason, REASON_BOUND)}",
        )

    try:
        adopted = adopt_published_head(
            store,
            worker,
            task,
            runner,
            expected_old=head,
            expected_new=run.new_head if run.old_head == head else None,
            base_sha=run.base_sha,
        )
    except CoordinatorError as exc:
        return lines + _ask(
            ctx,
            f"conflict resolution {k} finished, but the checkout could not "
            f"adopt the published head: "
            f"{one_line(redact(str(exc)), REASON_BOUND)}",
            keep_running=True,
        )
    if adopted is None:

        def finish_without_push(changed: dict[str, Any]) -> None:
            marker = coord(changed)["remedies"]["pr_mergeable"]
            marker["state"] = "finished"
            marker["result"] = "no-conflicts"
            marker["at"] = utcnow()

        _commit(store, task, finish_without_push)
        return lines + [
            f"conflict resolution {k} found no conflicts on {head[:7]}; "
            f"retrying {phase}"
        ]

    def adopt(changed: dict[str, Any]) -> None:
        cp = coord(changed)
        cp["head_sha"] = adopted
        cp["published_head"] = adopted
        cp.pop("pending_published_head", None)
        if is_sha(str(run.base_sha or "")):
            cp["rebase_base_sha"] = run.base_sha
        invalidate_head_evidence(cp, adopted)
        cp["phase"] = "tests"
        cp.pop("gate_route", None)
        marker = cp["remedies"]["pr_mergeable"]
        marker.update(
            state="finished",
            result="resolved",
            new_head=adopted,
            base_sha=run.base_sha,
            at=utcnow(),
        )

    _commit(store, task, adopt)
    return lines + [
        f"conflict resolution {k} rebased {head[:7]}; adopted head "
        f"{adopted[:7]}; routing to tests"
    ]
