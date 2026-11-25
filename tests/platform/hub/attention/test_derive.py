"""U-HUB-attention (derivation): every rule of ADR-076 section 2 as a table, the mappings against the contract
`contracts/enums/attention.json` (KOR-39)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from agent_hub.attention import derive
from agent_hub.attention.derive import Attention, Inputs, Operation
from agent_hub.sessions.transitions import SESSION_FAILURE_REASONS
from tests.platform.contracts.harness import CONTRACTS

MAIN = uuid.UUID(int=1)
OP = uuid.UUID(int=2)
OLDER = uuid.UUID(int=3)


def _inputs(task_state: str, **fields: Any) -> Inputs:
    fields.setdefault("main_id", MAIN)
    return Inputs(task_state=task_state, task_reason=fields.pop("task_reason", None), **fields)


def _op(
    op_type: str, state: str, code: str | None = None, *, superseded: bool = False, op_id: uuid.UUID = OP
) -> Operation:
    return Operation(op_id, op_type, state, code, superseded)


def test_the_categories_equal_the_contract() -> None:
    contract = json.loads((CONTRACTS / "enums" / "attention.json").read_text(encoding="utf-8"))
    found = {row["category"]: (row["order"], tuple(row["reasons"])) for row in contract["categories"]}
    assert found == dict(derive.CATEGORIES)
    assert [row["category"] for row in contract["categories"]] == list(derive.CATEGORIES)


def test_every_outcome_of_every_table_is_a_listed_reason_of_its_category() -> None:
    tables = (
        derive.FAILED_REASONS,
        derive.QUESTION_TYPES,
        derive.BLOCKING_CODES,
        derive.SCHEDULING_REASONS,
        derive.IDLE_REASONS,
    )
    outcomes = {outcome for table in tables for outcome in table.values()} | {derive.FAILED_DEFAULT}
    for category, reason in outcomes:
        assert reason in derive.CATEGORIES[category][1], (category, reason)
    assert set(derive.FAILED_REASONS) <= SESSION_FAILURE_REASONS
    codes = json.loads((CONTRACTS / "enums" / "operation-error-codes.json").read_text(encoding="utf-8"))["codes"]
    assert set(derive.BLOCKING_CODES) <= set(codes)
    assert {reason for _order, reasons in derive.CATEGORIES.values() for reason in reasons} >= derive.WG_WIDE


@pytest.mark.parametrize(
    ("inputs", "expected"),
    [
        # 1. closed wins over everything
        (_inputs("closed", main_state="failed", main_reason="oom"), Attention("done", "closed")),
        # 2. failed by the main session's reason
        (
            _inputs("failed", main_reason="budget_exceeded"),
            Attention("needs_decision", "credit_empty", "session", MAIN),
        ),
        (
            _inputs("failed", main_reason="credit_exhausted"),
            Attention("needs_decision", "credit_empty", "session", MAIN),
        ),
        (_inputs("failed", main_reason="unassigned"), Attention("needs_decision", "device_missing", "session", MAIN)),
        (
            _inputs("failed", main_reason="model_access_unavailable"),
            Attention("blocked", "model_access_missing", "session", MAIN),
        ),
        (_inputs("failed", main_reason="device_offline"), Attention("blocked", "device_unreachable", "session", MAIN)),
        (_inputs("failed", main_reason="device_shutdown"), Attention("blocked", "device_unreachable", "session", MAIN)),
        (_inputs("failed", main_reason="vm_start_failed"), Attention("blocked", "guest_error", "session", MAIN)),
        (_inputs("failed", main_reason="guest_unreachable"), Attention("blocked", "guest_error", "session", MAIN)),
        (_inputs("failed", main_reason="sandbox_failed"), Attention("blocked", "guest_error", "session", MAIN)),
        (_inputs("failed", main_reason="capacity_exhausted"), Attention("blocked", "no_capacity", "session", MAIN)),
        (_inputs("failed", main_reason="agent_exited"), Attention("blocked", "session_failed", "session", MAIN)),
        (_inputs("failed", main_reason="oom"), Attention("blocked", "session_failed", "session", MAIN)),
        # the task's reason stands in for a missing main reason; no main session: no ref
        (_inputs("failed", task_reason="unassigned"), Attention("needs_decision", "device_missing", "session", MAIN)),
        (_inputs("failed", main_id=None), Attention("blocked", "session_failed")),
        # 3a. a question or an approval of the last turn
        (
            _inputs("idle", main_state="idle", last_turn=[_op("human.question", "succeeded")]),
            Attention("needs_decision", "question", "operation", OP),
        ),
        (
            _inputs("idle", main_state="idle", last_turn=[_op("human.approval", "succeeded")]),
            Attention("needs_decision", "approval", "operation", OP),
        ),
        # ... also while subagents keep the task working, and for a stopped main session
        (
            _inputs(
                "working", main_state="idle", subagent_working=True, last_turn=[_op("human.question", "succeeded")]
            ),
            Attention("needs_decision", "question", "operation", OP),
        ),
        (
            _inputs("idle", main_state="stopped", last_turn=[_op("human.approval", "succeeded")]),
            Attention("needs_decision", "approval", "operation", OP),
        ),
        # ... never while the main session works
        (
            _inputs("working", main_state="working", main_working=True, last_turn=[_op("human.question", "succeeded")]),
            Attention("working", "working"),
        ),
        # a refused question is a refused operation; another successful type does not matter
        (
            _inputs("idle", main_state="idle", last_turn=[_op("human.question", "denied", "rights_denied")]),
            Attention("blocked", "operation_refused", "operation", OP),
        ),
        (_inputs("idle", main_state="idle", last_turn=[_op("github.push", "succeeded")]), Attention("done", "idle")),
        # 3b. a refused operation with a blocking code
        (
            _inputs("idle", main_state="idle", last_turn=[_op("github.push", "rejected", "github_not_connected")]),
            Attention("needs_decision", "connection_missing", "operation", OP),
        ),
        (
            _inputs("idle", main_state="idle", last_turn=[_op("subagent.request", "denied", "budget_exceeded")]),
            Attention("needs_decision", "credit_empty", "operation", OP),
        ),
        (
            _inputs("idle", main_state="idle", last_turn=[_op("github.commit", "failed", "credit_exhausted")]),
            Attention("needs_decision", "credit_empty", "operation", OP),
        ),
        *[
            (
                _inputs("idle", main_state="idle", last_turn=[_op("github.push", state, code)]),
                Attention("blocked", "operation_refused", "operation", OP),
            )
            for state, code in (
                ("denied", "rights_denied"),
                ("failed", "repo_not_accessible"),
                ("rejected", "repo_not_allowed"),
                ("failed", "branch_protected"),
                ("rejected", "secret_detected"),
                ("expired", "model_not_allowed"),
            )
        ],
        # a subagent limit of the hub's settings (ADR-088 section 2), refused by the runner or the hub
        *[
            (
                _inputs("idle", main_state="idle", last_turn=[_op("subagent.request", state, "limit_exceeded")]),
                Attention("blocked", "limit_reached", "operation", OP),
            )
            for state in ("rejected", "denied")
        ],
        # other codes, a missing code, a non-final state and a superseded refusal do not matter
        (
            _inputs("idle", main_state="idle", last_turn=[_op("github.push", "failed", "non_fast_forward")]),
            Attention("done", "idle"),
        ),
        (
            _inputs("idle", main_state="idle", last_turn=[_op("github.push", "expired", "timeout")]),
            Attention("done", "idle"),
        ),
        (_inputs("idle", main_state="idle", last_turn=[_op("github.push", "failed")]), Attention("done", "idle")),
        (_inputs("idle", main_state="idle", last_turn=[_op("github.push", "accepted")]), Attention("done", "idle")),
        (
            _inputs(
                "idle",
                main_state="idle",
                last_turn=[_op("github.push", "rejected", "github_not_connected", superseded=True)],
            ),
            Attention("done", "idle"),
        ),
        # the newest operation that matters decides
        (
            _inputs(
                "idle",
                main_state="idle",
                last_turn=[
                    _op("github.push", "succeeded"),
                    _op("github.commit", "rejected", "secret_detected"),
                    _op("human.question", "succeeded", op_id=OLDER),
                ],
            ),
            Attention("blocked", "operation_refused", "operation", OP),
        ),
        # 4. scheduling: the waiting reasons of KOR-41 first
        *[
            (_inputs("scheduling", main_state="requested", task_reason=reason, device_assigned=False), expected)
            for reason, expected in (
                ("no_device", Attention("needs_decision", "device_missing")),
                ("platform_access_required", Attention("needs_decision", "device_missing")),
                ("placement_offline", Attention("blocked", "device_unreachable")),
                ("credit_exhausted", Attention("needs_decision", "credit_empty")),
                ("wg_vm_limit", Attention("working", "queued")),
                ("placement_full", Attention("working", "queued")),
                ("keys_pending", Attention("working", "queued")),
            )
        ],
        (
            _inputs("scheduling", main_state="requested", device_assigned=False),
            Attention("needs_decision", "device_missing"),
        ),
        (
            _inputs("scheduling", main_state="requested", task_reason="device_offline"),
            Attention("blocked", "device_unreachable"),
        ),
        (
            _inputs("scheduling", main_state="requested", task_reason="capacity_exhausted"),
            Attention("working", "queued"),
        ),
        (_inputs("scheduling", main_id=None), Attention("working", "queued")),
        # 5. starting and working
        (_inputs("starting", main_state="vm_assigning"), Attention("working", "starting")),
        (_inputs("working", main_state="working", main_working=True), Attention("working", "working")),
        (
            _inputs("working", main_state="working", main_working=True, operation_running=True),
            Attention("working", "operation"),
        ),
        (_inputs("working", main_state="idle", subagent_working=True), Attention("working", "subagents")),
        (
            _inputs("working", main_state="idle", subagent_working=True, operation_running=True),
            Attention("working", "operation"),
        ),
        (
            _inputs("working", main_state="working", main_working=True, subagent_working=True),
            Attention("working", "working"),
        ),
        # 6. idle
        (_inputs("idle", main_state="idle"), Attention("done", "idle")),
        (
            _inputs("idle", main_state="idle", main_reason="budget_exceeded"),
            Attention("needs_decision", "credit_empty", "session", MAIN),
        ),
        (
            _inputs("idle", main_state="suspended", main_reason="credit_exhausted"),
            Attention("needs_decision", "credit_empty", "session", MAIN),
        ),
        (_inputs("idle", main_state="stopped", main_reason="agent_exited"), Attention("done", "idle")),
    ],
)
def test_derivation(inputs: Inputs, expected: Attention) -> None:
    assert derive.derive(inputs) == expected


def test_a_main_session_waiting_for_model_quota() -> None:
    """ADR-081 section 5 (KOR-50): the choice of DFX access needs a person unless the task has the consent or the
    org's credit is exhausted; otherwise the task waits by itself. Both records carry the wait."""
    until = datetime(2026, 11, 21, 14, 5, tzinfo=UTC)
    waiting = {"main_state": "suspended", "main_reason": "model_quota_exhausted", "main_wait_until": until}
    choice = Attention("needs_decision", "quota_choice", "session", MAIN, until)
    wait = Attention("working", "quota_wait", wait_until=until)
    assert derive.derive(_inputs("idle", main_dfx_choice=True, **waiting)) == choice
    assert derive.derive(_inputs("idle", main_dfx_choice=True, model_consent=True, **waiting)) == wait
    assert derive.derive(_inputs("idle", main_dfx_choice=True, credit_exhausted=True, **waiting)) == wait
    assert derive.derive(_inputs("idle", **waiting)) == wait
    # another pause of the main session is no quota wait
    assert derive.derive(_inputs("idle", main_state="suspended", main_reason="credit_exhausted")) == Attention(
        "needs_decision", "credit_empty", "session", MAIN
    )
    assert derive.derive(_inputs("idle", main_state="idle", main_reason="model_quota_exhausted")) == Attention(
        "done", "idle"
    )
    # a wait time holds any live main session (KOR-50: the item session_wait), an ended one never
    assert derive.derive(_inputs("working", main_state="working", main_wait_until=until)) == wait
    assert derive.derive(_inputs("starting", main_state="agent_starting", main_wait_until=until)) == wait
    assert derive.derive(_inputs("idle", main_state="stopped", main_wait_until=until)) == Attention("done", "idle")
    assert derive.derive(_inputs("scheduling", main_id=None, task_reason="no_device")) == Attention(
        "needs_decision", "device_missing"
    )
    for category, reason in (derive.QUOTA_CHOICE, derive.QUOTA_WAIT):
        assert reason in derive.CATEGORIES[category][1]
