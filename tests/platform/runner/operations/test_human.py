"""U-RUN-operations: `human.question` and `human.approval` (KOR-39, ADR-076 section 3; 03-api.md section 9) through the
real pipeline with the contract-checking hub: checked, registered without a dedupe slot (idempotency key only), the
question sealed in the `operation.params` event, `succeeded` with `{}` at once; a subagent's request is denied by the
hub, and one a hub accepts anyway is never answered `succeeded`."""

from __future__ import annotations

from typing import Any

import pytest

from agent_runner.operations import executors
from agent_runner.operations.executors import EXECUTORS

from ..reconcile.rig import Rig
from ..sessions.kit import TASK
from .kit import run, setup

TYPES = ("human.question", "human.approval")
QUESTION = "Which database should the service use?\n- PostgreSQL\n- SQLite"


@pytest.mark.parametrize("op_type", TYPES)
def test_a_question_succeeds_at_once_with_an_empty_result(rig: Rig, op_type: str) -> None:
    async def body(env: Any) -> None:
        kit_ = await setup(env, executors=EXECUTORS)
        sid = await kit_.session()
        op = await kit_.run(sid, op_type, {"question": QUESTION})
        assert (op.state, op.error_code, op.result) == ("succeeded", None, {})
        assert op.answer == {"operation_id": op.id, "status": "succeeded", "result": {}}
        assert kit_.answers() == [op.answer]
        (registration,) = kit_.hub.requests
        assert (registration["type"], registration["local_check"], registration["dedupe"]) == (op_type, "passed", None)
        header, params = kit_.hub.params_event()
        assert (header["kind"], header["ref"], header["task"], header["session"]) == (
            "operation.params",
            op.id,
            TASK,
            sid,
        )
        assert params == {"operation_id": op.id, "type": op_type, "params": {"question": QUESTION}}
        # nothing but the transitions and the result: no event of its own
        items = await kit_.outbox()
        kinds = [(kind, b.get("state") or b.get("header", {}).get("kind")) for kind, b in items]
        assert kinds == [
            ("operation_transition", "executing"),
            ("event", "operation.result"),
            ("operation_transition", "succeeded"),
        ]
        # the question never reaches the hub in plaintext
        assert "PostgreSQL" not in str(kit_.hub.requests)

    run(rig, body)


def test_only_an_idempotency_key_gives_a_slot(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_ = await setup(env, executors=EXECUTORS)
        sid = await kit_.session()
        await kit_.run(sid, "human.question", {"question": "A?"}, key="ask-1")
        await kit_.run(sid, "human.question", {"question": "A?"})
        first, second = kit_.hub.requests
        assert first["dedupe"] is not None
        assert len(first["dedupe"]["tag"]) == 43
        assert second["dedupe"] is None

    run(rig, body)


@pytest.mark.parametrize(("question", "pointer"), [("", "/params/question"), ("q" * 4001, "/params/question")])
def test_a_bad_question_is_rejected_params_invalid(rig: Rig, question: str, pointer: str) -> None:
    async def body(env: Any) -> None:
        kit_ = await setup(env, executors=EXECUTORS)
        sid = await kit_.session()
        kit_.hub.decisions.append({"state": "rejected", "error_code": "params_invalid"})
        op = await kit_.run(sid, "human.approval", {"question": question})
        assert (op.state, op.error_code) == ("rejected", "params_invalid")
        assert op.answer is not None
        assert op.answer["error"]["pointers"] == [pointer]
        (registration,) = kit_.hub.requests
        assert (registration["local_check"], registration["error_code"]) == ("rejected", "params_invalid")

    run(rig, body)


def test_a_subagent_is_denied_by_the_hub(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_ = await setup(env, executors=EXECUTORS)
        sid = await kit_.session(depth=1)
        kit_.hub.decisions.append({"state": "denied", "error_code": "rights_denied"})
        op = await kit_.run(sid, "human.question", {"question": "May I?"})
        assert (op.state, op.error_code) == ("denied", "rights_denied")
        assert op.answer == {"operation_id": op.id, "status": "denied", "error": {"code": "rights_denied"}}

    run(rig, body)


def test_a_subagent_question_a_hub_accepts_fails_rights_denied(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_ = await setup(env, executors=EXECUTORS)
        sid = await kit_.session(depth=1)
        op = await kit_.run(sid, "human.approval", {"question": "May I?"})
        assert (op.state, op.error_code, op.retryable) == ("failed", "rights_denied", False)
        assert op.answer is not None
        assert op.answer["status"] == "failed"
        assert op.answer["error"]["code"] == "rights_denied"

    run(rig, body)


def test_the_types_are_wired() -> None:
    assert EXECUTORS["human.question"] is EXECUTORS["human.approval"] is executors.ask_person
