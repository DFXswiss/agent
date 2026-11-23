"""U-HUB-operations: the outbox extensions - `operation.finished` and `subagent.finished` signals (inputs of HUB-15)
and the dedupe slot release after `failed` reported through the outbox."""

from __future__ import annotations

from typing import Any

from agent_hub.operations import outbox as operations_outbox
from agent_hub.outbox import chains
from tests.platform.hub.operations.support import Ops
from tests.platform.hub.outbox.support import item

SignalLog = list[tuple[str, dict[str, Any]]]


def test_handlers_extend_the_base_chains() -> None:
    found = chains.chains()
    assert found["operation_transition"][-1] is operations_outbox.operation_finished
    assert found["session_transition"][-1] is operations_outbox.subagent_finished


def test_operation_finished_once_per_final_state(ops: Ops, op_signals: SignalLog) -> None:
    task, session = ops.session()
    operation, _answer = ops.decide(session, dedupe="f")
    ops.step(operation, "executing")
    assert op_signals == []
    ops.finish(operation, "failed")
    expected = {
        "operation_id": operation,
        "session_id": session,
        "task_id": task,
        "org_id": ops.desk.org,
        "state": "failed",
    }
    assert op_signals == [("operation.finished", expected)]
    assert ops.slot_holder("f") is None
    seq = ops.outbox.position() + 1
    again: dict[str, Any] = {
        "operation_id": operation,
        "state": "failed",
        "at": ops.outbox.now(),
        "error_code": "upstream_error",
    }
    again.update(
        {
            "retryable": True,
            "result_event_id": ops.desk.rows(
                "SELECT result_event_id::text FROM hub.operation WHERE id = %s", operation
            )[0][0],
        }
    )
    assert ops.outbox.statuses(item(seq, "operation_transition", again)) == [(seq, "ok", None)]
    assert len(op_signals) == 1


def test_refused_step_signals_nothing(ops: Ops, op_signals: SignalLog) -> None:
    _task, session = ops.session()
    operation, _answer = ops.decide(session, local_check="rejected", error_code="params_invalid")
    assert ops.step(operation, "expired", error_code="timeout")[0][1] == "rejected"
    assert op_signals == []


def test_subagent_finished_when_a_child_ends(ops: Ops, op_signals: SignalLog) -> None:
    task, session = ops.session()
    child = ops.decide(session, "subagent.request")[1]["child_session_id"]
    ops.scene.sessions[child] = (task, ops.desk.wg)
    seq = ops.outbox.position() + 1
    states = ["vm_assigning", "vm_ready", "sandbox_ready", "agent_starting", "prompt_delivering", "working"]
    items = [ops.outbox.transition(seq + index, child, state) for index, state in enumerate(states)]
    ops.outbox.ok(*items)
    assert op_signals == []
    seq = ops.outbox.position() + 1
    ops.outbox.ok(ops.outbox.transition(seq, child, "stopping"), ops.outbox.transition(seq + 1, child, "stopped"))
    assert op_signals == [
        (
            "subagent.finished",
            {"session_id": child, "parent_session_id": session, "task_id": task, "org_id": ops.desk.org},
        )
    ]
    ops.outbox.ok(ops.outbox.transition(ops.outbox.position() + 1, child, "stopped"))
    assert len(op_signals) == 1


def test_main_session_end_is_no_subagent_signal(ops: Ops, op_signals: SignalLog) -> None:
    _task, session = ops.session()
    ops.outbox.ok(ops.outbox.transition(1, session, "failed", "agent_exited"))
    assert op_signals == []
