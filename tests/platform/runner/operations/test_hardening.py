"""U-RUN-operations: the pipeline never trusts the hub or the guest beyond the specification (00-overview.md section 3
B2/B4; 05-runner.md section 12; 03-api.md sections 6.1, 9, 11, 12) - a hub that accepts a locally rejected operation,
duplicates naming originals of other workgroups or types, guest input the store cannot keep, accepted operations the
hub has expired, store failures after an executor succeeded, executor codes outside the closed list, and the
`operation.finished` notifier."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import psycopg
import pytest

from agent_runner.crypto.ids import uuid7
from agent_runner.guestlink.errors import GuestUnreachable
from agent_runner.operations import records, register
from agent_runner.operations.extensions import OPERATION_ERROR_CODES, OperationContext, OperationFailed, known_code
from agent_runner.operations.pipeline import clean_error

from ..reconcile.rig import Rig, seal
from ..sessions.kit import ORG, TASK, WG, WG_OTHER
from .kit import OKit, ok, run, setup

CONTRACTS = Path(__file__).resolve().parents[4] / "contracts"
REPO = "example-org/example-repo"
RESULT = {"comment_id": 11, "url": "https://github.com/example-org/example-repo/pull/7#c11"}
AT = "2026-11-21T12:00:01.000Z"
PARAMS = {"repo": REPO, "number": 7, "body": "Looks good."}


def nested(depth: int) -> object:
    value: object = "x"
    for _ in range(depth):
        value = [value]
    return value


async def counting(env: Any) -> tuple[OKit, list[str]]:
    runs: list[str] = []

    async def record(ctx: OperationContext) -> dict[str, Any]:
        runs.append(ctx.operation.id)
        return {} if ctx.operation.type == "task.set_title" else RESULT

    found = await setup(env, executors={"task.set_title": record, "github.comment": record})
    await found.settings([REPO])
    await found.connect()
    return found, runs


def test_the_closed_code_list_equals_the_contract() -> None:
    enum = json.loads((CONTRACTS / "enums" / "operation-error-codes.json").read_text(encoding="utf-8"))
    assert set(enum["codes"]) == OPERATION_ERROR_CODES
    assert known_code("timeout") == "timeout"
    assert known_code("rate_limited") == "upstream_error"
    assert known_code(None, "params_invalid") == "params_invalid"


# -- H1: the hub accepts what the runner rejected --------------------------------------------------------------


def test_a_locally_rejected_operation_is_never_executed_even_if_the_hub_accepts_it(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, runs = await counting(env)
        sid = await kit_.session()
        kit_.hub.decisions.append({"state": "accepted"})
        op = await kit_.run(sid, "task.set_title", {"title": "two\nlines"})
        assert runs == []
        assert kit_.hub.requests[0]["local_check"] == "rejected"
        assert (op.state, op.error_code, op.retryable) == ("failed", "params_invalid", False)
        assert op.answer is not None
        assert (op.answer["status"], op.answer["error"]["code"]) == ("rejected", "params_invalid")
        assert kit_.answers() == [op.answer]
        # the event and the failed transition release the slot the hub took
        items = await kit_.outbox()
        assert [kind for kind, _ in items] == ["event", "operation_transition"]
        assert items[1][1]["state"] == "failed"
        assert items[1][1]["error_code"] == "params_invalid"
        assert items[1][1]["result_event_id"] == items[0][1]["header"]["id"]
        assert any("hub_contradiction" in line for line in kit_.lines)
        await kit_.kit.pushed_events()

    run(rig, body)


def test_a_guest_rejected_operation_accepted_by_the_hub_is_not_executed_or_answered_again(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, runs = await counting(env)
        sid = await kit_.session()
        kit_.hub.decisions.append({"state": "accepted"})
        op = await kit_.run(sid, "task.set_title", {"title": "x"}, rejected={"code": "params_invalid"})
        assert runs == []
        assert op.state == "failed"
        assert kit_.answers() == []
        assert op.answered_at is not None

    run(rig, body)


def test_a_duplicate_for_a_locally_rejected_operation_returns_no_original_result(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, _ = await counting(env)
        sid = await kit_.session()
        first = await kit_.run(sid, "github.comment", PARAMS)
        decision = {"state": "duplicate", "duplicate_of": first.id, "original": {"state": "succeeded"}}
        kit_.hub.decisions.append(decision)
        op = await kit_.run(sid, "github.comment", {**PARAMS, "repo": "other/repo"})
        assert op.state == "rejected"
        assert op.answer == {
            "operation_id": op.id,
            "status": "rejected",
            "error": {"code": "repo_not_allowed", "message": "The repository is not allowed for this workgroup."},
        }

    run(rig, body)


# -- H2: duplicates only within the workgroup and type -----------------------------------------------------------


def test_a_duplicate_of_another_workgroup_or_type_returns_nothing(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, _ = await counting(env)
        sid = await kit_.session()
        other_type = await kit_.run(sid, "task.set_title", {"title": "secret plans"})
        other_wg = await kit_.run(sid, "github.comment", PARAMS)
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.operation SET wg_id = %s WHERE id = %s", (WG_OTHER, other_wg.id))
        for original in (other_type.id, other_wg.id):
            decision = {"state": "duplicate", "duplicate_of": original, "original": {"state": "succeeded"}}
            kit_.hub.decisions.append(decision)
            op_id = await kit_.request(sid, "github.comment", PARAMS)
            await kit_.age(op_id, 25)
            await kit_.pipeline.process(op_id)
            op = await kit_.op(op_id)
            assert op.answer == {"operation_id": op_id, "status": "pending", "duplicate_of": original}

    run(rig, body)


def test_a_result_event_of_another_workgroup_is_not_returned(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, _ = await counting(env)
        sid = await kit_.session()
        original = uuid7()
        org = env.world.orgs[ORG]
        sealed = seal(
            org,
            "rdev",
            "operation.result",
            label="r",
            body={"operation_id": original, "status": "succeeded", "result": {"leak": True}},
            ref=original,
        )
        env.world.add_event(WG, 2, sealed)
        from .kit import pull  # noqa: PLC0415

        await pull(env)
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.operation_result SET wg_id = %s", (WG_OTHER,))
        kit_.hub.decisions.append({"state": "duplicate", "duplicate_of": original, "original": {"state": "succeeded"}})
        op_id = await kit_.request(sid, "github.comment", PARAMS)
        await kit_.age(op_id, 25)
        await kit_.pipeline.process(op_id)
        op = await kit_.op(op_id)
        assert op.answer == {"operation_id": op_id, "status": "pending", "duplicate_of": original}

    run(rig, body)


# -- M1: guest input the store cannot keep ---------------------------------------------------------------------


def test_requests_the_store_cannot_keep_are_skipped_without_stalling_the_journal(rig: Rig) -> None:
    async def body(env: Any) -> None:
        from agent_runner.guestlink.client import JournalRecord  # noqa: PLC0415

        kit_ = await setup(env)
        sid = await kit_.session()
        bodies = [
            {"title": "a\x00b"},
            {"t\x00": "x"},
            {"title": "a\ud800"},
            {"deep": nested(70)},
            {"title": "fine"},
        ]
        page = []
        good = uuid7()
        for n, params in enumerate(bodies, start=1):
            op_id = good if params == {"title": "fine"} else uuid7()
            body_ = {
                "pending_id": uuid7(),
                "operation_id": op_id,
                "type": "task.set_title",
                "params": params,
                "requested_at": "2026-11-21T12:00:01.000Z",
            }
            page.append(
                JournalRecord(
                    seq=n, ts="2026-11-21T12:00:01.000Z", session_id=sid, kind="operation.request", body=body_
                )
            )
        await kit_.reader(await kit_.vm()).handle(page)
        rows = await env.rows("SELECT id::text FROM runner.operation")
        assert rows == [(good,)]
        assert sum("record_skipped" in line for line in kit_.lines) == 4

    run(rig, body)


# -- M3: accepted operations the hub has expired ------------------------------------------------------------------


def test_an_operation_accepted_too_long_ago_is_expired_and_not_executed(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, runs = await counting(env)
        sid = await kit_.session()
        op_id = await kit_.request(sid, "task.set_title", {"title": "x"})
        found = await kit_.pipeline._checks(await kit_.op(op_id))
        assert found is not None
        op, error, checked = found
        prepared = await kit_.pipeline._prepare(op, env.clock() + 600, error, checked)
        assert prepared is not None
        async with env.pool.connection() as conn:
            await records.update(conn, op_id, state="accepted")
        await kit_.age(op_id, 1800)
        await kit_.pipeline.process(op_id)
        op = await kit_.op(op_id)
        assert runs == []
        assert (op.state, op.error_code, op.retryable) == ("expired", "timeout", True)
        assert op.answer is not None
        assert op.answer["error"] == {"code": "timeout", "message": "The operation was not started.", "retryable": True}
        transitions = [b for k, b in await kit_.outbox() if k == "operation_transition"]
        assert [(t["state"], t["error_code"]) for t in transitions] == [("expired", "timeout")]
        await kit_.kit.pushed_events()

    run(rig, body)


def test_an_expiry_before_any_registration_writes_nothing_for_the_hub(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, _ = await counting(env)
        sid = await kit_.session()
        op_id = await kit_.request(sid, "task.set_title", {"title": "x"})
        await kit_.age(op_id, 700)

        class NoKey:
            async def seal(self, *_args: Any, **_kw: Any) -> None:
                return None

        kit_.pipeline.sealer = NoKey()
        await kit_.pipeline.process(op_id)
        op = await kit_.op(op_id)
        assert (op.state, op.result_event_id) == ("expired", None)
        assert await kit_.outbox() == []

    run(rig, body)


# -- M4: store failures after the executor succeeded ---------------------------------------------------------


def test_a_store_failure_while_finishing_is_retried_and_the_outcome_kept(rig: Rig, monkeypatch: Any) -> None:
    async def body(env: Any) -> None:
        kit_, runs = await counting(env)
        sid = await kit_.session()
        original = records.update
        failures = [1]

        async def flaky(conn: Any, operation_id: str, **fields: object) -> None:
            if "result_event_id" in fields and failures:
                failures.pop()
                raise psycopg.OperationalError("connection lost")
            await original(conn, operation_id, **fields)

        monkeypatch.setattr(records, "update", flaky)
        op = await kit_.run(sid, "github.comment", PARAMS)
        assert op.state == "succeeded"
        assert runs == [op.id]
        assert any("store_failed attempt=1 error=OperationalError" in line for line in kit_.lines)
        # the rolled-back attempt left nothing behind: one event, one transition after `executing`
        kinds = [k for k, _ in await kit_.outbox()]
        assert (kinds.count("operation_transition"), kinds.count("event")) == (2, 1)

        async def broken(conn: Any, operation_id: str, **fields: object) -> None:
            if "result_event_id" in fields:
                raise psycopg.OperationalError("down")
            await original(conn, operation_id, **fields)

        monkeypatch.setattr(records, "update", broken)
        op_id = await kit_.request(sid, "github.comment", PARAMS)
        with pytest.raises(psycopg.OperationalError):
            await kit_.pipeline.process(op_id)
        assert (await kit_.op(op_id)).state == "executing"

    run(rig, body)


# -- M5: executor codes and messages ---------------------------------------------------------------------------


def test_executor_codes_and_messages_outside_the_contract_are_replaced(rig: Rig) -> None:
    async def body(env: Any) -> None:
        async def odd(ctx: OperationContext) -> dict[str, Any]:
            raise OperationFailed("rate_limited", message="Zu viele Anfragen \u2013 sp\u00e4ter")

        kit_ = await setup(env, executors={"task.set_title": odd})
        sid = await kit_.session()
        op = await kit_.run(sid, "task.set_title", {"title": "x"})
        assert (op.state, op.error_code) == ("failed", "upstream_error")
        assert op.answer == {
            "operation_id": op.id,
            "status": "failed",
            "error": {"code": "upstream_error", "retryable": False},
        }
        await kit_.kit.pushed_events()  # contract-valid result event and transition

    run(rig, body)


def test_clean_error_keeps_only_contract_members() -> None:
    error = {"code": "nope", "message": "x" * 2000, "pointers": "/a", "retryable": "yes", "extra": 1}
    assert clean_error(error) == {"code": "upstream_error"}
    assert clean_error({"code": "timeout", "message": "M.", "pointers": ["/a"], "retryable": True}) == {
        "code": "timeout",
        "message": "M.",
        "pointers": ["/a"],
        "retryable": True,
    }


# -- M6, M7: operation.finished ----------------------------------------------------------------------------------


def test_the_notifier_runs_once_and_only_after_the_answer_was_delivered(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_ = await setup(env, executors={"task.set_title": ok})
        sid = await kit_.session()
        op_id = await kit_.request(sid, "task.set_title", {"title": "x"})
        await kit_.age(op_id, 25)
        kit_.guest.answer_errors[:] = [GuestUnreachable("pending")] * 3
        await kit_.pipeline.process(op_id)
        assert kit_.notified == []  # not delivered: the next scan tries again
        await kit_.pipeline.process(op_id)
        assert kit_.notified == [op_id]
        op = await kit_.op(op_id)
        await kit_.pipeline._deliver(replace(op, answered_at=None))
        assert kit_.notified == [op_id]

    run(rig, body)


def test_a_request_older_than_its_row_counts_from_the_request(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_ = await setup(env, executors={"task.set_title": ok})
        sid = await kit_.session()
        # the runner caught up with the journal a minute late: the MCP call had returned `pending` already
        op_id = await kit_.request(sid, "task.set_title", {"title": "x"}, requested_at="2026-11-21T11:59:00.000Z")
        await kit_.pipeline.process(op_id)
        assert kit_.notified == [op_id]
        # a request time that is not a contract time counts nothing
        op_id = await kit_.request(sid, "task.set_title", {"title": "y"}, requested_at="2026-11-21T11:59:00Z")
        await kit_.pipeline.process(op_id)
        assert kit_.notified == [kit_.notified[0]]

    run(rig, body)


# -- L1, L2 ------------------------------------------------------------------------------------------------------


def test_hub_codes_outside_the_closed_list_are_not_a_decision() -> None:
    with pytest.raises(ValueError, match="decision"):
        register.parse_decision({"state": "denied", "error_code": "made_up"})


def test_parameters_that_cannot_be_encoded_are_params_invalid(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_ = await setup(env)
        sid = await kit_.session()
        op_id = await kit_.request(sid, "task.set_title", {"title": "x"})
        op = replace(await kit_.op(op_id), params={"title": "a\ud800"})
        async with env.pool.connection() as conn:
            built = await register.build(
                conn,
                sealer=env.sync.pusher.sealer,
                storage_key=env.sync.storage_key,
                op=op,
                at="2026-11-21T12:00:02.000Z",
                error_code=None,
                params=op.params,
                extra={},
            )
        assert built.size_exceeded is False
        assert (built.body["local_check"], built.body["error_code"]) == ("rejected", "params_invalid")
        assert TASK
        assert WG

    run(rig, body)
