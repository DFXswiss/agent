"""U-HUB-attention (derivation): every rule of ADR-064 section 2 as a table, the mappings against the contract
`contracts/enums/attention.json` (KOR-39)."""

from __future__ import annotations

import json
import uuid
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
                ("denied", "limit_exceeded"),
                ("expired", "model_not_allowed"),
            )
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
        # 4. scheduling
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
        (_inputs("idle", main_state="stopped", main_reason="agent_exited"), Attention("done", "idle")),
    ],
)
def test_derivation(inputs: Inputs, expected: Attention) -> None:
    assert derive.derive(inputs) == expected
