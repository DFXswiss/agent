"""The attention record of a task as a pure function of its metadata (ADR-076 section 2, normative).

`derive(inputs)` applies the rules in order; the first that applies decides (rule 2a: KOR-50, ADR-081 section 5):

1. task `closed` → `done/closed`;
2. task `failed` → by the main session's reason (`FAILED_REASONS`, default `blocked/session_failed`; ref: the session);
2a. the main session is live (not `stopped`/`failed`) and waits for model quota - a wait time set (a `suspended`
   transition with `model_quota_exhausted` or the item `session_wait`, any live state including the start chain) or
   `suspended` with that reason → `needs_decision/quota_choice` (ref: the session) when DFX access would be available
   (`dfx_choice`), the task has no consent yet and the org's credit is not exhausted, else `working/quota_wait`; both
   carry the session's `wait_until`. A cleared wait gives the other rules back;
3. the main session is not `working` and the newest operation of its last turn that matters (`Inputs.last_turn`) is
   a. a `human.question` / `human.approval` that `succeeded` (`QUESTION_TYPES`) → `needs_decision/question` /
      `needs_decision/approval`, or
   b. an operation that ended `rejected`, `denied`, `failed` or `expired` (`REFUSED_STATES`) with a blocking error code
      (`BLOCKING_CODES`) and no later operation of the same type of the task `succeeded` (`Operation.superseded`)
   (ref: the operation);
4. task `scheduling` → a waiting reason of `SCHEDULING_REASONS` (KOR-41) → its record, except `device_offline`
   (`WG_DEVICE_REASONS`), which yields to a missing assignment; else no active device assigned to the workgroup →
   `needs_decision/device_missing`; otherwise the reason's record or `working/queued`;
5. task `starting` → `working/starting`; task `working` → an operation of the task `accepted` or `executing` →
   `working/operation`, no session of depth 0 `working` but a subagent `working` → `working/subagents`, else
   `working/working`;
6. task `idle` → a main session reason of `IDLE_REASONS` → its record (ref: the session); otherwise `done/idle`.

The mappings are tables: a later package adds a reason by a row here and in `contracts/enums/attention.json` (the
contract test compares `CATEGORIES` with the enum). Only `needs_decision` and `blocked` records carry a ref; a record
of `working` or `done` names no object (a phase change inside `working` is no new occasion).

The overview of the open tasks (`OVERVIEW`, `overview_of`) reads only these records: `working` is "In progress",
`needs_decision` "Waiting for a person", `blocked` and `done/idle` "No agent"; `done/closed` is not open.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from uuid import UUID

NEEDS_DECISION, BLOCKED, WORKING, DONE = "needs_decision", "blocked", "working", "done"
ASCENDING, DESCENDING = "since_asc", "since_desc"
CATEGORIES: Mapping[str, tuple[str, tuple[str, ...]]] = MappingProxyType(
    {
        NEEDS_DECISION: (
            ASCENDING,
            ("question", "approval", "connection_missing", "credit_empty", "device_missing", "quota_choice"),
        ),
        BLOCKED: (
            DESCENDING,
            (
                "session_failed",
                "guest_error",
                "device_unreachable",
                "model_access_missing",
                "no_capacity",
                "operation_refused",
                "device_failed",
                "limit_reached",
            ),
        ),
        WORKING: (DESCENDING, ("queued", "starting", "working", "operation", "subagents", "quota_wait")),
        DONE: (DESCENDING, ("idle", "closed")),
    }
)
"""Category → (list order, reasons): a copy of `contracts/enums/attention.json` (a test compares them)."""
OCCASIONS = frozenset({NEEDS_DECISION, BLOCKED})
"""Categories in which a change of reason or ref is a new occasion (`seq`, notifications)."""

Outcome = tuple[str, str]
FAILED_REASONS: Mapping[str, Outcome] = MappingProxyType(
    {
        "budget_exceeded": (NEEDS_DECISION, "credit_empty"),
        "credit_exhausted": (NEEDS_DECISION, "credit_empty"),
        "unassigned": (NEEDS_DECISION, "device_missing"),
        "model_access_unavailable": (BLOCKED, "model_access_missing"),
        "device_offline": (BLOCKED, "device_unreachable"),
        "device_shutdown": (BLOCKED, "device_unreachable"),
        "vm_start_failed": (BLOCKED, "guest_error"),
        "guest_unreachable": (BLOCKED, "guest_error"),
        "sandbox_failed": (BLOCKED, "guest_error"),
        "capacity_exhausted": (BLOCKED, "no_capacity"),
    }
)
FAILED_DEFAULT: Outcome = (BLOCKED, "session_failed")
QUESTION_TYPES: Mapping[str, Outcome] = MappingProxyType(
    {"human.question": (NEEDS_DECISION, "question"), "human.approval": (NEEDS_DECISION, "approval")}
)
SUCCEEDED = "succeeded"
REFUSED_STATES = frozenset({"rejected", "denied", "failed", "expired"})
BLOCKING_CODES: Mapping[str, Outcome] = MappingProxyType(
    {
        "github_not_connected": (NEEDS_DECISION, "connection_missing"),
        "budget_exceeded": (NEEDS_DECISION, "credit_empty"),
        "credit_exhausted": (NEEDS_DECISION, "credit_empty"),
        **dict.fromkeys(
            (
                "rights_denied",
                "repo_not_accessible",
                "repo_not_allowed",
                "branch_protected",
                "secret_detected",
                "model_not_allowed",
            ),
            (BLOCKED, "operation_refused"),
        ),
        # a subagent limit of the hub's settings (ADR-088 section 2): the operator can raise it
        "limit_exceeded": (BLOCKED, "limit_reached"),
    }
)
SCHEDULING_REASONS: Mapping[str, Outcome] = MappingProxyType(
    {
        # waiting for a device (03-api.md section 4.6.2, KOR-41): someone must provide or reach one
        "no_device": (NEEDS_DECISION, "device_missing"),
        "platform_access_required": (NEEDS_DECISION, "device_missing"),
        "device_offline": (BLOCKED, "device_unreachable"),
        "placement_offline": (BLOCKED, "device_unreachable"),
        "credit_exhausted": (NEEDS_DECISION, "credit_empty"),
        # the main session's device failed (ADR-087): it continues there or on another device by itself
        "device_failed": (BLOCKED, "device_failed"),
        # waiting for capacity or for the key of an organization device: quiet, it goes on by itself
        "capacity_exhausted": (WORKING, "queued"),
        "wg_vm_limit": (WORKING, "queued"),
        "placement_full": (WORKING, "queued"),
        "keys_pending": (WORKING, "queued"),
    }
)
WG_DEVICE_REASONS = frozenset({"device_offline"})
"""Waiting reasons about the devices assigned to the workgroup itself (checked after the assignment)."""
IDLE_REASONS: Mapping[str, Outcome] = MappingProxyType(
    {"budget_exceeded": (NEEDS_DECISION, "credit_empty"), "credit_exhausted": (NEEDS_DECISION, "credit_empty")}
)
MODEL_QUOTA = "model_quota_exhausted"
SUSPENDED = "suspended"
ENDED = frozenset({"stopped", "failed"})
QUOTA_CHOICE: Outcome = (NEEDS_DECISION, "quota_choice")
QUOTA_WAIT: Outcome = (WORKING, "quota_wait")
RUNNING_OPERATION_STATES = ("accepted", "executing")
WG_WIDE = frozenset({"device_missing", "device_unreachable", "no_capacity", "device_failed"})
"""Reasons that concern a whole workgroup (one notification per owner, workgroup and reason, ADR-076 section 5)."""
CLOSED = "closed"
OVERVIEW_WORKING, OVERVIEW_WAITING, OVERVIEW_NO_AGENT = "working", "waiting", "no_agent"
OVERVIEW: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        # an agent works: a session runs, starts, waits in the queue or for model quota and goes on by itself
        OVERVIEW_WORKING: (WORKING,),
        # a person must answer, approve or decide (a question, an approval, a connection, credit, a device, quota)
        OVERVIEW_WAITING: (NEEDS_DECISION,),
        # no agent works and nobody is asked: the session failed or ended, the task lies idle and is not closed
        OVERVIEW_NO_AGENT: (BLOCKED, DONE),
    }
)
"""The overview of the open tasks (the start page's three numbers): overview → the categories it counts. Every record
falls into exactly one overview except `done/closed` (a closed task is not open); see `overview_of`."""


def overview_of(category: str, reason: str) -> str | None:
    """The overview of a record (`OVERVIEW`), None for a closed task (and for a category this release does not know)."""
    if category == DONE and reason == CLOSED:
        return None
    for name, categories in OVERVIEW.items():
        if category in categories:
            return name
    return None


@dataclass(frozen=True)
class Operation:
    """An operation of the main session's last turn (rule 3)."""

    id: UUID
    type: str
    state: str
    error_code: str | None
    superseded: bool = False
    """A later operation of the same type of the task succeeded."""


@dataclass(frozen=True)
class Inputs:
    """The metadata the derivation reads (ADR-076 section 2)."""

    task_state: str
    task_reason: str | None
    main_id: UUID | None = None
    main_state: str | None = None
    main_reason: str | None = None
    last_turn: Sequence[Operation] = ()
    """Operations of the main session's last turn after the last human input, newest first."""
    device_assigned: bool = True
    operation_running: bool = False
    main_working: bool = False
    subagent_working: bool = False
    main_wait_until: datetime | None = None
    """The main session's wait (ADR-081 section 4)."""
    main_dfx_choice: bool = False
    model_consent: bool = False
    """The task's consent to DFX model access (ADR-081 section 5)."""
    credit_exhausted: bool = False
    """The org's credit is exhausted (read only for a main session waiting for model quota)."""


@dataclass(frozen=True)
class Attention:
    """A derived record: category, reason, the object the next action refers to and, for a quota wait, when the
    task continues by itself."""

    category: str
    reason: str
    ref_kind: str | None = None
    ref_id: UUID | None = None
    wait_until: datetime | None = None


def _session(outcome: Outcome, main_id: UUID | None) -> Attention:
    return Attention(*outcome, "session" if main_id is not None else None, main_id)


def _matters(operation: Operation) -> Outcome | None:
    """Rule 3: the record an operation of the last turn calls for, None when it does not matter."""
    if operation.state == SUCCEEDED:
        return QUESTION_TYPES.get(operation.type)
    if operation.state in REFUSED_STATES and not operation.superseded and operation.error_code is not None:
        return BLOCKING_CODES.get(operation.error_code)
    return None


def _last_turn(inputs: Inputs) -> Attention | None:
    if inputs.main_state == WORKING:
        return None
    for operation in inputs.last_turn:
        outcome = _matters(operation)
        if outcome is not None:
            return Attention(*outcome, "operation", operation.id)
    return None


def _working(inputs: Inputs) -> Attention:
    if inputs.operation_running:
        return Attention(WORKING, "operation")
    if not inputs.main_working and inputs.subagent_working:
        return Attention(WORKING, "subagents")
    return Attention(WORKING, "working")


def _scheduling(inputs: Inputs) -> Attention:
    known = SCHEDULING_REASONS.get(inputs.task_reason or "")
    # a reason of the workgroup's own devices yields to a missing assignment (the reason may be older than it);
    # the other reasons also cover devices given to the whole organization (KOR-41) and stand as they are
    if known is not None and inputs.task_reason not in WG_DEVICE_REASONS:
        return Attention(*known)
    if not inputs.device_assigned:
        return Attention(NEEDS_DECISION, "device_missing")
    return Attention(*(known or (WORKING, "queued")))


def waits(main_state: str | None, main_reason: str | None, wait_until: datetime | None) -> bool:
    """Rule 2a: the main session is live and waits for model quota (module docstring)."""
    if main_state is None or main_state in ENDED:
        return False
    return wait_until is not None or (main_state == SUSPENDED and main_reason == MODEL_QUOTA)


def _quota(inputs: Inputs) -> Attention:
    """Rule 2a (module docstring)."""
    if inputs.main_dfx_choice and not inputs.model_consent and not inputs.credit_exhausted:
        return Attention(*QUOTA_CHOICE, "session", inputs.main_id, inputs.main_wait_until)
    return Attention(*QUOTA_WAIT, wait_until=inputs.main_wait_until)


def _idle(inputs: Inputs) -> Attention:
    credit = IDLE_REASONS.get(inputs.main_reason or "")
    return Attention(DONE, "idle") if credit is None else _session(credit, inputs.main_id)


def _by_state(inputs: Inputs) -> Attention:
    """Rules 4-6."""
    if inputs.task_state == "scheduling":
        return _scheduling(inputs)
    if inputs.task_state == "starting":
        return Attention(WORKING, "starting")
    if inputs.task_state == WORKING:
        return _working(inputs)
    return _idle(inputs)


def derive(inputs: Inputs) -> Attention:
    """The attention record of a task (module docstring)."""
    if inputs.task_state == "closed":
        return Attention(DONE, CLOSED)
    if inputs.task_state == "failed":
        reason = inputs.main_reason or inputs.task_reason or ""
        return _session(FAILED_REASONS.get(reason, FAILED_DEFAULT), inputs.main_id)
    if waits(inputs.main_state, inputs.main_reason, inputs.main_wait_until):
        return _quota(inputs)
    asked = _last_turn(inputs)
    return _by_state(inputs) if asked is None else asked
