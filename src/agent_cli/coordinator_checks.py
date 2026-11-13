"""Evaluate the coordinator precondition registry in the script; a model never does."""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass
from typing import Any, Callable
from uuid import NAMESPACE_URL, uuid5

from .allow import (
    SECOND_REVIEW_NOT_POSTED,
    both_second_reviews_na,
    evaluate_allow,
    external_second_review_evidence_ok,
)
from .chain import close_allowed
from .coordinator_common import (
    CiObservationTransportError,
    CoordinatorError,
    LaneRunner,
    Runner,
    a38_model_second,
    a38_task,
    as_int,
    comment_target,
    coord,
    evidence_base_sha,
    is_order,
    is_sha,
    order_id,
    owned_session,
    redact,
    save_task,
    scoped,
    source_kind,
    source_label,
    target_repo,
    task_text_path,
)
from .coordinator_config import WorkerConfig, resolve_round_limit
from .coordinator_git import verify_checkout_identity, verify_signed_clean_head
from .coordinator_github import (
    _fetch_pr_json,
    _readiness_keys_closed,
    clear_reply_recovery,
    post_issue_comment,
    publish_blocker,
)
from .coordinator_lanes import (
    _handled_rejected_gate_ids,
    implementer_model_id,
    lane_retry,
    recover_working_lanes,
    set_checklist,
)
from .gate_order import latest_gates
from .model_slots import ModelSlotsError, canonical_key, models_from_snapshot
from .preconditions import ACTIONS, CHECKS, Check
from .store import Store, utcnow


CHECK_EVIDENCE_BOUND = 1000
ROUTE_LOOP_LIMIT = 3


@dataclass(frozen=True)
class CheckValue:
    status: str
    evidence: str = ""
    source: str = "script"
    at: str = ""
    head: str | None = None


@dataclass(frozen=True)
class Deferred:
    lines: list[str]


@dataclass(frozen=True)
class CheckContext:
    store: Store
    worker: WorkerConfig
    task: dict[str, Any]
    runner: Runner
    lane_runner: LaneRunner | None
    check: Check
    action: str


class TaskFacts:
    """Rows of one task, loaded on first use and reloaded after invalidate()."""

    store: Store

    def __init__(self, store: Store, task: dict[str, Any]) -> None:
        self.store = store
        self.task = task
        self._checklist: dict[str, dict[str, Any]] | None = None
        self._gates: dict[tuple[str, str], dict[str, Any]] | None = None
        self._round_loaded = False
        self._round: dict[str, Any] | None = None

    def checklist(self) -> dict[str, dict[str, Any]]:
        if self._checklist is None:
            rows: dict[str, dict[str, Any]] = {}
            for row in self.store.rows("checklist_item"):
                if row.get("task_id") != self.task["id"]:
                    continue
                key = canonical_key(str(row.get("key")))
                if key not in rows:
                    rows[key] = row
            self._checklist = rows
        return self._checklist

    def gates(self) -> dict[tuple[str, str], dict[str, Any]]:
        if self._gates is None:
            self._gates = latest_gates(self.store, str(self.task["id"]))
        return self._gates

    def round(self) -> dict[str, Any] | None:
        if not self._round_loaded:
            self._round_loaded = True
            current = int(self.task.get("current_round") or 0)
            if current:
                for row in self.store.rows("task_round"):
                    if (
                        row.get("task_id") == self.task["id"]
                        and int(row.get("round") or 0) == current
                    ):
                        self._round = row
                        break
        return self._round

    def invalidate(self) -> None:
        self._checklist = None
        self._gates = None
        self._round_loaded = False
        self._round = None


def current_phase(task: dict[str, Any]) -> str:
    return str(coord(task).get("phase") or "accept")


def head_of(task: dict[str, Any]) -> str:
    return str(coord(task).get("head_sha") or "")


def next_round(store: Store, task: dict[str, Any]) -> int:
    """The round number the next implementer start would use."""
    current = int(task.get("current_round") or 0)
    if current > 0:
        for row in store.rows("task_round"):
            if (
                row.get("task_id") == task["id"]
                and int(row.get("round") or 0) == current
            ):
                if row.get("implementer_verdict") is None:
                    return current
                break
    return current + 1


def round_limit_pin(
    store: Store, worker: WorkerConfig, task: dict[str, Any]
) -> dict[str, Any]:
    """The round limit pinned on the task.

    Pins it now for a task created before the pin existed.
    """
    pin = coord(task).get("round_limit")
    if (
        isinstance(pin, dict)
        and isinstance(pin.get("limit"), int)
        and isinstance(pin.get("max_rounds"), int)
    ):
        return pin
    try:
        model = implementer_model_id(store, worker, task)
    except CoordinatorError:
        model = ""
    pin = resolve_round_limit(worker, model)
    _commit(store, task, lambda t: coord(t).__setitem__("round_limit", pin))
    return coord(task)["round_limit"]


def applies(when: str, task: dict[str, Any]) -> bool:
    c = coord(task)
    if when == "always":
        return True
    if when == "a38":
        return a38_task(c)
    if when == "not_a38":
        return not a38_task(c)
    if when == "model_second":
        return a38_model_second(c)
    if when == "external_second":
        return a38_task(c) and not a38_model_second(c)
    if when.startswith("phase:"):
        return current_phase(task) == when.removeprefix("phase:")
    return False


def route_phase(target: str, task: dict[str, Any]) -> str:
    if target != "second_review":
        return target
    try:
        second = models_from_snapshot(task).second
    except ModelSlotsError:
        second = None
    return "pr_gates_codex" if second is not None else "await_second_review"


def registry_row_id(task_id: str, key: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"coordinator-check:{task_id}:{key}"))


def one_line(text: str, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    safe = " ".join(redact(flat, limit=10_000).split())
    if len(safe) <= limit:
        return safe
    return safe[: limit - 2] + " …"


def _commit(
    store: Store,
    task: dict[str, Any],
    change: Callable[[dict[str, Any]], None],
) -> None:
    committed = copy.deepcopy(task)
    change(committed)
    save_task(store, committed)
    task.clear()
    task.update(committed)


def _value(
    status: str,
    evidence: Any = "",
    source: Any = "script",
    at: Any = "",
    head: str | None = None,
) -> CheckValue:
    return CheckValue(
        status,
        one_line(str(evidence or ""), CHECK_EVIDENCE_BOUND),
        str(source or "script"),
        str(at or ""),
        head,
    )


def _checklist_value(facts: TaskFacts, key: str) -> CheckValue:
    row = facts.checklist().get(key)
    if row is None:
        return CheckValue("pending")
    status = str(row.get("status") or "pending")
    if status in ("pending", "unavailable"):
        return CheckValue("pending")
    if status in ("ja", "nein", "n_a"):
        return _value(
            status,
            row.get("evidence"),
            row.get("source"),
            row.get("updated_at"),
        )
    return CheckValue("pending")


def _read_order_active(facts: TaskFacts, task: dict[str, Any]) -> CheckValue:
    state = task.get("state")
    status = "ja" if state not in ("done", "failed") else "nein"
    return _value(status, f"task state {state}")


def _read_session_registered(facts: TaskFacts, task: dict[str, Any]) -> CheckValue:
    return _checklist_value(facts, "session_registered")


def _read_spec_written(facts: TaskFacts, task: dict[str, Any]) -> CheckValue:
    return _checklist_value(facts, "spec_written")


def _read_contributing_ok(facts: TaskFacts, task: dict[str, Any]) -> CheckValue:
    return _checklist_value(facts, "contributing_ok")


def _read_deviation_declared(facts: TaskFacts, task: dict[str, Any]) -> CheckValue:
    return _checklist_value(facts, "deviation_declared")


def _read_deviation_granted(facts: TaskFacts, task: dict[str, Any]) -> CheckValue:
    return _checklist_value(facts, "deviation_granted")


def _read_draft_open(facts: TaskFacts, task: dict[str, Any]) -> CheckValue:
    number = as_int(coord(task).get("pr_number"))
    if number is not None:
        return _value("ja", f"pull request #{number}")
    return CheckValue("pending")


def _read_implementer_outcome(facts: TaskFacts, task: dict[str, Any]) -> CheckValue:
    lo = coord(task).get("lane_outcome")
    current = int(task.get("current_round") or 0)
    if not (
        isinstance(lo, dict)
        and lo.get("role") == "implementer"
        and lo.get("applied") is not True
        and int(lo.get("round") or 0) == current
    ):
        return CheckValue("pending")
    row = facts.store.row("agent", str(lo.get("agent_id") or ""))
    if row is None or row.get("status") != "done":
        return CheckValue("pending")
    return _value(
        "ja",
        f"implementer outcome {row.get('status')}/{row.get('result')} round {current}",
    )


def _read_implementer_done(facts: TaskFacts, task: dict[str, Any]) -> CheckValue:
    row = facts.round()
    if row is None:
        return CheckValue("pending")
    number = int(row.get("round") or task.get("current_round") or 0)
    verdict = row.get("implementer_verdict")
    if verdict == "done":
        return _value("ja", f"round {number} implementer done")
    if verdict == "blocked":
        return _value("nein", f"round {number} implementer blocked")
    return CheckValue("pending")


def _read_reviewer_approved(facts: TaskFacts, task: dict[str, Any]) -> CheckValue:
    row = facts.round()
    if row is None:
        return CheckValue("pending")
    number = int(row.get("round") or task.get("current_round") or 0)
    verdict = row.get("reviewer_verdict")
    if verdict == "approved":
        return _value("ja", f"round {number} approved")
    if verdict == "rejected":
        return _value("nein", f"round {number} rejected")
    return CheckValue("pending")


def _read_tests_green(facts: TaskFacts, task: dict[str, Any]) -> CheckValue:
    c = coord(task)
    head = head_of(task)
    ev = c.get("evidence") if isinstance(c.get("evidence"), dict) else {}
    if head and a38_task(c) and isinstance(c.get("a38"), dict):
        from .coordinator_a38 import local_test_gate_satisfied

        if local_test_gate_satisfied(task, head):
            if ev.get("tests_pass") is True:
                return _value("ja", f"tests pass on {head}")
            return _value(
                "ja", f"tests waived on {head}: {ev.get('tests_waiver')}"
            )
    elif head and ev.get("tests_pass") is True and ev.get("tests_head") == head:
        return _value("ja", f"tests pass on {head}")
    if head and ev.get("tests_pass") is False and ev.get("tests_head") == head:
        return _value("nein", f"tests failed on {head[:7]}")
    return CheckValue("pending")


def _read_head_published(facts: TaskFacts, task: dict[str, Any]) -> CheckValue:
    head = head_of(task)
    if head and coord(task).get("published_head") == head:
        return _value("ja", f"head {head} published")
    return CheckValue("pending")


def _read_report_posted(facts: TaskFacts, task: dict[str, Any]) -> CheckValue:
    c = coord(task)
    head = head_of(task)
    a38 = c.get("a38") if isinstance(c.get("a38"), dict) else {}
    base = evidence_base_sha(c, a38=True)
    if not head or a38.get("pending_base_sha") or not is_sha(base):
        return CheckValue("pending")
    from .coordinator_a38 import _bound_base_evidence

    if not _bound_base_evidence(a38, head=head, base_sha=base):
        return CheckValue("pending")
    report = a38.get("report")
    if isinstance(report, dict) and report.get("head") == head:
        return _value("ja", f"A38 report on {head}")
    waiver = a38.get("report_waiver")
    reason = waiver.get("reason") if isinstance(waiver, dict) else ""
    return _value("ja", f"A38 report waived on {head}: {reason}")


def _gate_value(
    facts: TaskFacts,
    task: dict[str, Any],
    stage: str,
    dimension: str,
    provider: str,
) -> CheckValue:
    gate = facts.gates().get((stage, dimension))
    head = head_of(task)
    if (
        gate is not None
        and head
        and gate.get("verdict") == "approved"
        and gate.get("head_sha") == head
        and gate.get("vendor") == provider
    ):
        return _value("ja", f"{stage}/{dimension} approved on {head}")
    if (
        gate is not None
        and head
        and gate.get("verdict") == "rejected"
        and gate.get("head_sha") == head
        and gate.get("vendor") == provider
        and gate.get("id") not in _handled_rejected_gate_ids(task)
    ):
        return _value("nein", f"{stage}/{dimension} rejected on {head[:7]}")
    return CheckValue("pending")


def _review_1(facts: TaskFacts, task: dict[str, Any], dimension: str) -> CheckValue:
    try:
        models = models_from_snapshot(task)
    except ModelSlotsError as exc:
        return _value("pending", str(exc))
    return _gate_value(facts, task, "review-1", dimension, models.first)


def _read_review_1_quality(facts: TaskFacts, task: dict[str, Any]) -> CheckValue:
    return _review_1(facts, task, "quality")


def _read_review_1_logic(facts: TaskFacts, task: dict[str, Any]) -> CheckValue:
    return _review_1(facts, task, "logic")


def _read_first_comment_posted(facts: TaskFacts, task: dict[str, Any]) -> CheckValue:
    c = coord(task)
    head = head_of(task)
    a38 = c.get("a38") if isinstance(c.get("a38"), dict) else {}
    first = a38.get("first_comment")
    if not (head and isinstance(first, dict) and first.get("head") == head):
        return CheckValue("pending")
    row = facts.store.row("activity", str(first.get("activity_id") or ""))
    if (
        row is not None
        and row.get("_origin_device_id") == facts.store.device_id()
        and row.get("execution_status") == "done"
    ):
        return _value("ja", f"first review comment on {head}")
    return CheckValue("pending")


def _review_2(facts: TaskFacts, task: dict[str, Any], dimension: str) -> CheckValue:
    try:
        models = models_from_snapshot(task)
    except ModelSlotsError as exc:
        return _value("pending", str(exc))
    key = f"review_2_{dimension}"
    if models.second is not None:
        value = _gate_value(facts, task, "review-2", dimension, models.second)
        if value.status == "ja":
            return value
        rows = facts.checklist()
        statuses = {
            name: (rows.get(name) or {}).get("status")
            for name in ("review_2_quality", "review_2_logic")
        }
        evidence = {
            name: (rows.get(name) or {}).get("evidence")
            for name in ("review_2_quality", "review_2_logic")
        }
        if both_second_reviews_na(statuses, evidence):
            return _value("n_a", SECOND_REVIEW_NOT_POSTED)
        return value
    row = facts.checklist().get(key)
    snapshot = {**task, "gates": list(facts.gates().values())}
    if (
        row is not None
        and row.get("status") == "ja"
        and external_second_review_evidence_ok(snapshot, row.get("evidence"))
    ):
        return _value(
            "ja", row.get("evidence"), row.get("source"), row.get("updated_at")
        )
    return CheckValue("pending")


def _read_review_2_quality(facts: TaskFacts, task: dict[str, Any]) -> CheckValue:
    return _review_2(facts, task, "quality")


def _read_review_2_logic(facts: TaskFacts, task: dict[str, Any]) -> CheckValue:
    return _review_2(facts, task, "logic")


def _read_review_2_recorded(facts: TaskFacts, task: dict[str, Any]) -> CheckValue:
    try:
        second = models_from_snapshot(task).second
    except ModelSlotsError as exc:
        return _value("pending", str(exc))
    head = head_of(task)
    if second is None or not head:
        return CheckValue("pending")
    for dimension in ("quality", "logic"):
        gate = facts.gates().get(("review-2", dimension))
        if not (
            gate is not None
            and gate.get("head_sha") == head
            and gate.get("vendor") == second
            and gate.get("verdict") in ("approved", "rejected")
        ):
            return CheckValue("pending")
    return _value("ja", f"review-2 pair recorded on {head}")


def _read_second_comment_posted(facts: TaskFacts, task: dict[str, Any]) -> CheckValue:
    c = coord(task)
    head = head_of(task)
    a38 = c.get("a38") if isinstance(c.get("a38"), dict) else {}
    second = a38.get("second_comment")
    if not (head and isinstance(second, dict) and second.get("head") == head):
        return CheckValue("pending")
    if second.get("blocks_merge") is False:
        return _value("ja", f"second review comment on {head}")
    if second.get("blocks_merge") is True:
        return _value("nein", f"second review blocks the merge on {head[:7]}")
    return CheckValue("pending")


def _read_ci_green(facts: TaskFacts, task: dict[str, Any]) -> CheckValue:
    c = coord(task)
    head = head_of(task)
    ev = c.get("evidence") if isinstance(c.get("evidence"), dict) else {}
    if head and ev.get("ci_green") is True and ev.get("ci_head") == head:
        return _value("ja", f"CI green on {head}")
    if head and ev.get("ci_green") is False and ev.get("ci_head") == head:
        return _value("nein", f"CI failed on {head[:7]}")
    return CheckValue("pending")


def _read_readiness_on_head(facts: TaskFacts, task: dict[str, Any]) -> CheckValue:
    head = head_of(task)
    if head and _readiness_keys_closed(facts.store, task, head):
        return _value("ja", f"readiness closed on {head}")
    return CheckValue("pending")


def _read_formal_approved(facts: TaskFacts, task: dict[str, Any]) -> CheckValue:
    c = coord(task)
    head = head_of(task)
    ev = c.get("evidence") if isinstance(c.get("evidence"), dict) else {}
    if head and ev.get("formal_head") == head:
        return _value("ja", f"formal approval on {head}")
    return CheckValue("pending")


def _read_ready_allowed(facts: TaskFacts, task: dict[str, Any]) -> CheckValue:
    result = evaluate_allow(
        "pr-ready",
        session_id=str(task.get("session_id")),
        task_id=str(task["id"]),
        session_tasks=[
            {
                "id": task["id"],
                "session_id": task.get("session_id"),
                "state": task.get("state"),
            }
        ],
    )
    if result.allowed:
        return _value("ja", f"pr-ready allowed in state {task.get('state')}")
    return _value("nein", result.reason)


READERS: dict[str, Callable[[TaskFacts, dict[str, Any]], CheckValue]] = {
    "order_active": _read_order_active,
    "session_registered": _read_session_registered,
    "spec_written": _read_spec_written,
    "draft_open": _read_draft_open,
    "implementer_outcome": _read_implementer_outcome,
    "implementer_done": _read_implementer_done,
    "reviewer_approved": _read_reviewer_approved,
    "tests_green": _read_tests_green,
    "head_published": _read_head_published,
    "report_posted": _read_report_posted,
    "review_1_quality": _read_review_1_quality,
    "review_1_logic": _read_review_1_logic,
    "first_comment_posted": _read_first_comment_posted,
    "review_2_quality": _read_review_2_quality,
    "review_2_logic": _read_review_2_logic,
    "review_2_recorded": _read_review_2_recorded,
    "second_comment_posted": _read_second_comment_posted,
    "ci_green": _read_ci_green,
    "contributing_ok": _read_contributing_ok,
    "deviation_declared": _read_deviation_declared,
    "deviation_granted": _read_deviation_granted,
    "readiness_on_head": _read_readiness_on_head,
    "formal_approved": _read_formal_approved,
    "ready_allowed": _read_ready_allowed,
}


def read_fact(facts: TaskFacts, task: dict[str, Any], check: Check) -> CheckValue:
    if check.fact != "registry":
        return READERS[check.key](facts, task)
    row = facts.checklist().get(check.key)
    if row is None:
        return CheckValue("pending")
    if (
        check.head_bound
        and row.get("head_sha") != head_of(task)
        and row.get("source") != "human"
    ):
        return _value("pending", "recorded on another head")
    return _value(
        str(row["status"]),
        row.get("evidence"),
        row.get("source"),
        row.get("updated_at"),
        row.get("head_sha"),
    )


def action_checks(
    facts: TaskFacts, task: dict[str, Any], action: str
) -> list[tuple[Check, CheckValue]]:
    kind = source_kind(coord(task))
    result = []
    for requirement in ACTIONS[action].requires:
        if kind not in requirement.kinds or not applies(requirement.when, task):
            continue
        check = CHECKS[requirement.check]
        result.append((check, read_fact(facts, task, check)))
    return result


def checks_satisfied(facts: TaskFacts, task: dict[str, Any], action: str) -> bool:
    return all(
        value.status == "ja" or (value.status == "n_a" and check.n_a_ok)
        for check, value in action_checks(facts, task, action)
    )


def record_check(
    store: Store,
    task: dict[str, Any],
    check: Check,
    value: CheckValue,
    *,
    action: str,
    override: dict[str, Any] | None = None,
    skip: dict[str, Any] | None = None,
) -> bool:
    """Write one registry fact through close_allowed. Returns True when written."""
    if check.fact != "registry":
        raise CoordinatorError(f"{check.key} is not a registry fact")
    row_id = registry_row_id(str(task["id"]), check.key)
    existing = store.row("checklist_item", row_id)
    evidence = one_line(value.evidence, CHECK_EVIDENCE_BOUND)
    grant = override or skip or {}
    new = {
        "id": row_id,
        "task_id": task["id"],
        "key": check.key,
        "status": value.status,
        "evidence": evidence or None,
        "source": value.source,
        "deviation_declared": False,
        "deviation_granted": False,
        "granted_by": grant.get("by"),
        "updated_at": utcnow(),
        "registry": True,
        "resolver": check.resolver,
        "head_sha": value.head,
        "override": override,
        "skip": skip,
    }
    compared = ("status", "evidence", "source", "head_sha", "override", "skip")
    if existing is not None and all(existing.get(key) == new.get(key) for key in compared):
        return False
    verdict = close_allowed(
        str(task.get("workflow") or ""),
        check.key,
        checklist={},
        source=value.source,
        evidence=evidence,
        snapshot={
            "registry_action": action,
            "source_kind": source_kind(coord(task)),
            "override": override,
            "skip": skip,
        },
        status=value.status,
    )
    if not verdict.allowed:
        raise CoordinatorError(
            f"close_allowed denied for {check.key}: {verdict.reason}"
        )
    store.write(
        "checklist_item", "insert" if existing is None else "update", row_id, new
    )
    return True


def needs_resolution(check: Check, value: CheckValue) -> bool:
    if check.fact == "registry" and value.status == "ja" and value.source == "human":
        return False
    if check.on_pending != "resolve":
        return False
    if value.status == "pending":
        return True
    if check.recheck:
        return True
    if (
        check.fact == "registry"
        and value.status in ("nein", "unavailable")
        and check.on_no != "question:override"
    ):
        return True
    return False


def evaluate_action(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
    lane_runner: LaneRunner | None,
    *,
    action: str,
) -> list[str] | None:
    kind = source_kind(coord(task))
    facts = TaskFacts(store, task)
    for requirement in ACTIONS[action].requires:
        if kind not in requirement.kinds or not applies(requirement.when, task):
            continue
        check = CHECKS[requirement.check]
        value = read_fact(facts, task, check)
        if needs_resolution(check, value):
            outcome = RESOLVERS[check.resolver](
                CheckContext(store, worker, task, runner, lane_runner, check, action)
            )
            facts.invalidate()
            if isinstance(outcome, Deferred):
                return list(outcome.lines)
            value = outcome
            if check.fact == "registry":
                record_check(store, task, check, value, action=action)
        if value.status == "ja" or (value.status == "n_a" and check.n_a_ok):
            continue
        if value.status == "pending":
            return apply_pending(store, worker, task, runner, check, action=action)
        return apply_outcome(
            store, worker, task, runner, check, value, action=action
        )
    route = coord(task).get("gate_route")
    if isinstance(route, dict) and route.get("action") == action:
        _commit(store, task, lambda changed: coord(changed).pop("gate_route", None))
    return None


def _block(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
    check: Check,
    *,
    text: str,
) -> list[str]:
    phase = current_phase(task)

    def change(changed: dict[str, Any]) -> None:
        c = coord(changed)
        c["phase"] = "blocked"
        c["resume_phase"] = phase
        c["failed_check"] = check.key
        c["blocker"] = text
        c.pop("gate_route", None)

    _commit(store, task, change)
    return publish_blocker(
        store,
        worker,
        task,
        runner,
        text,
        kind=check.block_kind or f"check-{check.key}",
        reply_checkpoint=True,
    )


def _route(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
    check: Check,
    target: str,
    *,
    action: str,
    status: str,
    findings: str = "",
) -> list[str]:
    phase = current_phase(task)
    to = route_phase(target, task)
    prior = coord(task).get("gate_route")
    if (
        isinstance(prior, dict)
        and prior.get("action") == action
        and prior.get("check") == check.key
        and prior.get("phase") == to
    ):
        count = int(prior.get("count") or 0) + 1
    else:
        count = 1
    if count >= ROUTE_LOOP_LIMIT:
        return _block(
            store,
            worker,
            task,
            runner,
            check,
            text=f"Check {check.key} stays pending after its resolver phase {to} ran.",
        )

    def change(changed: dict[str, Any]) -> None:
        c = coord(changed)
        c["gate_route"] = {
            "action": action,
            "check": check.key,
            "phase": to,
            "count": count,
        }
        if to == "publish_draft":
            c["resume_phase"] = phase
        if to == "implement":
            changed["state"] = "implementing"
            if findings and not c.get("findings"):
                c["findings"] = findings
        c["phase"] = to

    _commit(store, task, change)
    if status == "pending":
        return [f"check {check.key} is pending; routing to {to}"]
    return [f"check {check.key}={status}; routing to {to}"]


def apply_pending(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
    check: Check,
    *,
    action: str,
) -> list[str]:
    if check.on_pending.startswith("route:"):
        return _route(
            store,
            worker,
            task,
            runner,
            check,
            check.on_pending.removeprefix("route:"),
            action=action,
            status="pending",
        )
    return [f"check {check.key} is pending"]


def apply_outcome(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
    check: Check,
    value: CheckValue,
    *,
    action: str,
) -> list[str]:
    outcome = check.on_no
    if outcome.startswith("route:"):
        return _route(
            store,
            worker,
            task,
            runner,
            check,
            outcome.removeprefix("route:"),
            action=action,
            status=value.status,
            findings=value.evidence,
        )
    if outcome == "retry":
        phase = current_phase(task)
        lines = lane_retry(
            store, worker, task, phase=phase, reason=value.evidence
        )
        if lines is not None:
            return lines
        return _block(
            store,
            worker,
            task,
            runner,
            check,
            text=f"Model lane interrupted in {phase}; not a code rejection.",
        )
    if outcome in ("question:override", "question:reply"):
        raise_check_question(store, worker, task, runner, check, value)
        return [f"check {check.key}={value.status}; question raised"]
    if outcome == "block":
        return _block(
            store,
            worker,
            task,
            runner,
            check,
            text=f"Check {check.key} failed: {value.evidence}",
        )
    if outcome == "fail":
        blocker = f"Check {check.key} failed: {value.evidence}"

        def change(changed: dict[str, Any]) -> None:
            changed["state"] = "failed"
            c = coord(changed)
            c["phase"] = "blocked"
            clear_reply_recovery(c)
            c["failed_kind"] = check.key
            c["blocker"] = blocker

        _commit(store, task, change)
        return [f"check {check.key}={value.status}; order failed"]
    raise CoordinatorError(
        f"check {check.key} declares an unknown outcome {check.on_no}"
    )


def _round_budget_question(
    worker: WorkerConfig,
    task: dict[str, Any],
    check: Check,
    value: CheckValue,
) -> str:
    del worker, check, value
    c = coord(task)
    if is_order(task):
        source = c.get("source") if isinstance(c.get("source"), dict) else {}
        subject = f'order "{str(source.get("title") or "")[:120]}"'
    else:
        subject = source_label(task)
    pin = c["round_limit"]
    question = c["round_limit_question"]
    origins = {
        "submit": "given at submit",
        "model": f"configured for model {pin['model']}",
        "worker": "configured for the worker",
        "default": "default",
    }
    origin = origins.get(str(pin.get("origin") or ""), "default")
    findings = redact(str(c.get("findings") or "none recorded"), limit=1000)
    return (
        f"Round limit reached for {subject}: {question['limit']} implementation "
        "rounds ended without an approved result.\n"
        f"Limit: {pin['max_rounds']} rounds per grant ({origin}).\n"
        f"Latest findings:\n{findings}\n"
        f"An authorized reply grants {pin['max_rounds']} more rounds; its text "
        "is passed to the implementer."
    )


def _duplicates_checked_question(
    worker: WorkerConfig,
    task: dict[str, Any],
    check: Check,
    value: CheckValue,
) -> str:
    del worker, check
    source = coord(task).get("source")
    source = source if isinstance(source, dict) else {}
    title = str(source.get("title") or "")[:120]
    oid = order_id(task)
    if value.status == "unavailable":
        first = (
            f'Check duplicates_checked could not run for order "{title}": '
            f"{value.evidence}"
        )
    else:
        first = (
            f'Check duplicates_checked failed for order "{title}": '
            f"{value.evidence}"
        )
    lines = [
        first,
        "Continue only with: agent order override "
        f"--id {oid} --check duplicates_checked --reason TEXT",
        f"Otherwise: agent order cancel --id {oid} --reason TEXT",
    ]
    if value.status == "unavailable":
        lines.append(
            f"Run it again with: agent order reply --id {oid} --body-file PATH"
        )
    return "\n".join(lines)


QUESTION_TEXTS: dict[
    str, Callable[[WorkerConfig, dict[str, Any], Check, CheckValue], str]
] = {
    "duplicates_checked": _duplicates_checked_question,
    "round_budget": _round_budget_question,
}


def question_text(
    worker: WorkerConfig,
    task: dict[str, Any],
    check: Check,
    value: CheckValue,
) -> str:
    if check.key in QUESTION_TEXTS:
        return QUESTION_TEXTS[check.key](worker, task, check, value)
    if is_order(task):
        source = coord(task).get("source")
        source = source if isinstance(source, dict) else {}
        title = str(source.get("title") or "")
        subject = f'order "{title[:120]}"'
    else:
        subject = source_label(task)
    return (
        f"Check {check.key} failed for {subject}: {value.evidence}\n"
        "Reply to continue."
    )


def raise_check_question(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
    check: Check,
    value: CheckValue,
) -> None:
    phase = current_phase(task)
    text = question_text(worker, task, check, value)
    target = comment_target(task)
    qid = None
    if target is not None:
        qid = post_issue_comment(
            store,
            worker,
            runner,
            repo=target[0],
            number=target[1],
            body=f"Question:\n{text}",
            kind="question",
            occurrence=f"{task['id']}:check:{check.key}",
            target=target[2],
        )

    def change(changed: dict[str, Any]) -> None:
        c = coord(changed)
        c["phase"] = "ask"
        c["resume_phase"] = phase
        c["failed_check"] = check.key
        c["question_text"] = redact(text, limit=1500)
        c["blocker"] = f"Check {check.key} failed: {value.evidence}"
        changed["state"] = "open"
        if qid is not None:
            if c.get("question_activity_id") != qid:
                c["replies_consumed_through"] = None
            c["question_activity_id"] = qid
        else:
            c.pop("question_activity_id", None)
            c.pop("replies_consumed_through", None)

    _commit(store, task, change)


def resolve_no_live_lane(ctx: CheckContext) -> CheckValue | Deferred:
    state = recover_working_lanes(ctx.store, ctx.worker, ctx.task)
    if state == "waiting":
        return Deferred(["a lane of an earlier tick is still running; waiting"])
    if state == "recovered":
        return CheckValue(
            "nein", "a lane left running by an earlier tick ended without a result"
        )
    return CheckValue("ja", "no working lane record")


def resolve_round_budget(ctx: CheckContext) -> CheckValue:
    store, worker, task = ctx.store, ctx.worker, ctx.task
    pin = round_limit_pin(store, worker, task)
    c = coord(task)
    question = c.get("round_limit_question")
    replies = len(c.get("authorized_replies") or [])
    if isinstance(question, dict) and replies > int(question.get("replies") or 0):
        limit = int(question.get("limit") or pin["limit"]) + pin["max_rounds"]

        def grant(t: dict[str, Any]) -> None:
            coord(t)["round_limit"]["limit"] = limit
            coord(t).pop("round_limit_question", None)

        _commit(store, task, grant)
        pin = coord(task)["round_limit"]
    upcoming = next_round(store, task)
    if upcoming <= pin["limit"]:
        return CheckValue("ja", f"rounds within limit {pin['limit']}")
    if not isinstance(coord(task).get("round_limit_question"), dict):
        asked = {"limit": pin["limit"], "replies": replies}
        _commit(
            store,
            task,
            lambda t: coord(t).__setitem__("round_limit_question", asked),
        )
    return CheckValue(
        "nein",
        f"round {upcoming} exceeds the limit of {pin['limit']} implementation rounds",
    )


def resolve_pr_mergeable(ctx: CheckContext) -> CheckValue | Deferred:
    store, worker, task = ctx.store, ctx.worker, ctx.task
    c = coord(task)
    source = c.get("source") if isinstance(c.get("source"), dict) else {}
    head = head_of(task)
    phase = current_phase(task)
    label = "leave-draft" if phase == "leave_draft" else phase
    number = as_int(c.get("pr_number") or task.get("ref"))
    if number is None:
        raise CoordinatorError(f"{label} requires PR number")
    try:
        view = _fetch_pr_json(
            scoped(store, worker.session_id, ctx.runner),
            target_repo(task),
            number,
            "mergeable",
        )
    except CiObservationTransportError:
        return Deferred(
            [
                f"PR metadata temporarily unavailable on {head[:7]}; "
                f"retrying {label}"
            ]
        )
    value = str(view.get("mergeable") or "").upper() if isinstance(view, dict) else ""
    if value == "MERGEABLE":
        return CheckValue("ja", f"mergeable on {head}", head=head or None)
    if value == "CONFLICTING":
        base = str(source.get("base") or "the base branch")
        return CheckValue(
            "nein",
            f"pull request has merge conflicts with {base}; resolve them on the "
            "branch or cancel, then reply",
            head=head or None,
        )
    return Deferred(
        [f"PR mergeability not yet computed on {head[:7]}; retrying {phase}"]
    )


def resolve_session_registered(context: CheckContext) -> CheckValue:
    evidence = f"session {context.worker.session_id} active"
    try:
        owned_session(context.store, context.worker.session_id)
        set_checklist(
            context.store,
            context.task,
            "session_registered",
            "ja",
            evidence,
            source="script",
        )
    except CoordinatorError as exc:
        return _value("nein", redact(str(exc)))
    return _value("ja", evidence)


def resolve_spec_written(context: CheckContext) -> CheckValue:
    if not is_order(context.task):
        return _value("pending", "written by acceptance")
    c = coord(context.task)
    source = c.get("source") if isinstance(c.get("source"), dict) else {}
    evidence = (
        f"order {order_id(context.task)} submitted by {source.get('submitted_by')} "
        f"at {source.get('submitted_at')}"
    )
    try:
        set_checklist(
            context.store,
            context.task,
            "spec_written",
            "ja",
            evidence,
            source="human",
        )
    except CoordinatorError as exc:
        return _value("nein", redact(str(exc)))
    return _value("ja", evidence, source="human")


def resolve_order_authorized(context: CheckContext) -> CheckValue:
    worker = context.worker
    task = context.task
    c = coord(task)
    source = c.get("source") if isinstance(c.get("source"), dict) else {}
    if not worker.orders:
        return _value("nein", "worker does not accept orders")
    repo = target_repo(task)
    configured = any(name.casefold() == repo.casefold() for name in worker.repositories)
    owner = repo.split("/", 1)[0].casefold()
    if not configured and owner not in worker.repository_owners:
        return _value("nein", f"worker does not serve {repo}")
    if task.get("_origin_device_id") != context.store.device_id():
        return _value("nein", "order row is not owned by this device")
    submitted_by = source.get("submitted_by")
    if (
        source.get("kind") != "order"
        or not isinstance(submitted_by, str)
        or not submitted_by
    ):
        return _value("nein", "order has no recorded submitter")
    return _value(
        "ja",
        f"order submitted by {submitted_by} at {source.get('submitted_at')}",
    )


def resolve_spec_available(context: CheckContext) -> CheckValue:
    c = coord(context.task)
    source = c.get("source") if isinstance(c.get("source"), dict) else {}
    try:
        data = task_text_path(context.worker, str(context.task["id"])).read_bytes()
        text = data.decode("utf-8")
    except (OSError, UnicodeError):
        return _value("nein", "task text file is missing")
    digest = hashlib.sha256(data).hexdigest()
    if digest != source.get("body_sha256"):
        return _value("nein", "task text does not match the submitted text")
    return _value(
        "ja", f"task text sha256 {digest[:12]}, {len(text)} characters"
    )


def resolve_checkout_owned(context: CheckContext) -> CheckValue:
    worktree = str(coord(context.task).get("worktree") or "")
    if not worktree:
        return _value("nein", "checkout is missing")
    try:
        marker = verify_checkout_identity(
            context.store, context.worker, context.runner, worktree
        )
    except CoordinatorError as exc:
        return _value("nein", redact(str(exc)))
    return _value(
        "ja",
        f"checkout {marker['branch']} on base {str(marker['base_sha'])[:7]}",
    )


def resolve_head_signed_clean(context: CheckContext) -> CheckValue:
    try:
        sha = verify_signed_clean_head(
            context.store,
            context.worker,
            context.runner,
            str(coord(context.task).get("worktree") or ""),
        )
    except CoordinatorError as exc:
        return _value(
            "nein", redact(str(exc)), head=head_of(context.task) or None
        )
    return _value("ja", f"signed clean head {sha}", head=sha)


def _resolve_duplicates_checked(ctx: CheckContext) -> CheckValue | Deferred:
    from .coordinator_check_lane import resolve_duplicates_checked

    return resolve_duplicates_checked(ctx)


RESOLVERS: dict[str, Callable[[CheckContext], CheckValue | Deferred]] = {
    "duplicates_checked": _resolve_duplicates_checked,
    "no_live_lane": resolve_no_live_lane,
    "round_budget": resolve_round_budget,
    "pr_mergeable": resolve_pr_mergeable,
    "session_registered": resolve_session_registered,
    "spec_written": resolve_spec_written,
    "order_authorized": resolve_order_authorized,
    "spec_available": resolve_spec_available,
    "checkout_owned": resolve_checkout_owned,
    "head_signed_clean": resolve_head_signed_clean,
}
