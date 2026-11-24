"""U-RUN-operations: registration at the hub - retries with backoff while the hub is unreachable, 10 min expiry,
final refusals, decisions outside the contract, the stored body and the wait for a workgroup key (03-api.md section
8.3, 05-runner.md section 12 step 5, 03-api.md section 12)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from agent_runner.operations import register
from agent_runner.operations.register import Decision, parse_decision, plain_type

from ..reconcile.rig import Rig
from .kit import ok, run, setup

OP = "01930009-0000-7000-8000-000000000001"


async def ready(env: Any) -> Any:
    found = await setup(env, executors={"task.set_title": ok})
    return found, await found.session()


def test_an_unreachable_hub_is_retried_with_backoff_and_the_same_body(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, sid = await ready(env)
        kit_.hub.unreachable = 2
        kit_.hub.refusals[:] = [(503, {"error": "unavailable"}), (429, {"error": "rate_limited"})]
        start = len(env.clock.sleeps)
        op = await kit_.run(sid, "task.set_title", {"title": "x"})
        assert op.state == "succeeded"
        assert env.clock.sleeps[start : start + 4] == [1.0, 2.0, 4.0, 8.0]
        # the hub saw three attempts (two refusals, one decision) with byte-identical bodies
        assert len(kit_.hub.requests) == 3
        assert kit_.hub.requests[0] == kit_.hub.requests[1] == kit_.hub.requests[2] == op.registration
        assert sum("register_retry" in line for line in kit_.lines) == 4

    run(rig, body)


def test_a_replayed_decision_is_accepted(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, sid = await ready(env)
        kit_.hub.decisions.append({"state": "accepted", "replayed": True})
        assert (await kit_.run(sid, "task.set_title", {"title": "x"})).state == "succeeded"

    run(rig, body)


def test_decisions_outside_the_contract_are_retried(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, sid = await ready(env)
        kit_.hub.raw[:] = [{"state": "maybe"}, {"state": "accepted", "child_session_id": "x"}]
        op = await kit_.run(sid, "task.set_title", {"title": "x"})
        assert op.state == "succeeded"
        assert sum("reason=decision_invalid" in line for line in kit_.lines) == 2

    run(rig, body)


def test_ten_minutes_without_a_decision_expire_the_operation(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, sid = await ready(env)
        op_id = await kit_.request(sid, "task.set_title", {"title": "x"})
        await kit_.age(op_id, 590)
        kit_.hub.unreachable = 1000
        await kit_.pipeline.process(op_id)
        op = await kit_.op(op_id)
        assert (op.state, op.error_code, op.retryable) == ("expired", "hub_unreachable", True)
        error = {"code": "hub_unreachable", "message": "The hub could not be reached.", "retryable": True}
        assert op.answer == {"operation_id": op_id, "status": "failed", "error": error}
        assert kit_.answers() == [op.answer]
        assert kit_.notified == [op_id]  # the MCP call had returned `pending` long ago
        # the registration was sent (the hub may know it): the error event and the `expired` transition
        items = await kit_.outbox()
        assert [kind for kind, _ in items] == ["event", "operation_transition"]
        assert items[0][1]["body"] == {"operation_id": op_id, "status": "expired", "error": error}
        assert items[1][1]["state"] == "expired"
        assert items[1][1]["error_code"] == "hub_unreachable"
        assert items[1][1]["result_event_id"] == items[0][1]["header"]["id"]
        assert kit_.hub.requests == []
        assert 9.0 <= sum(env.clock.sleeps) <= 11.0

    run(rig, body)


def test_an_operation_older_than_the_expiry_gets_one_attempt(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, sid = await ready(env)
        op_id = await kit_.request(sid, "task.set_title", {"title": "x"})
        await kit_.age(op_id, 700)
        kit_.hub.unreachable = 1
        await kit_.pipeline.process(op_id)
        assert (await kit_.op(op_id)).state == "expired"
        assert env.clock.sleeps == []

    run(rig, body)


@pytest.mark.parametrize(
    ("status", "error", "code"), [(403, "forbidden", "rights_denied"), (400, "request_invalid", "upstream_error")]
)
def test_a_final_refusal_fails_the_operation(rig: Rig, status: int, error: str, code: str) -> None:
    async def body(env: Any) -> None:
        kit_, sid = await ready(env)
        kit_.hub.refusals.append((status, {"error": error}))
        op = await kit_.run(sid, "task.set_title", {"title": "x"})
        assert (op.state, op.error_code, op.retryable) == ("failed", code, False)
        assert op.answer == {
            "operation_id": op.id,
            "status": "failed",
            "error": {"code": code, "message": "The hub refused the registration."},
        }
        assert await kit_.outbox() == []

    run(rig, body)


class LateKey:
    """A sealer without a workgroup key for the first `misses` attempts."""

    def __init__(self, sealer: Any, misses: int) -> None:
        self.sealer = sealer
        self.misses = misses

    async def seal(self, conn: Any, draft: Mapping[str, Any], *, min_epoch: int = 1) -> dict[str, object] | None:
        if self.misses:
            self.misses -= 1
            return None
        found: dict[str, object] | None = await self.sealer.seal(conn, draft, min_epoch=min_epoch)
        return found


def test_registration_waits_for_the_workgroup_key(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, sid = await ready(env)
        kit_.pipeline.sealer = LateKey(env.sync.pusher.sealer, 2)
        start = len(env.clock.sleeps)
        op = await kit_.run(sid, "task.set_title", {"title": "x"})
        assert op.state == "succeeded"
        assert env.clock.sleeps[start : start + 2] == [2.0, 2.0]
        assert sum("waiting_for_key" in line for line in kit_.lines) == 2

    run(rig, body)


def test_without_a_workgroup_key_the_operation_expires(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, sid = await ready(env)
        kit_.pipeline.sealer = LateKey(env.sync.pusher.sealer, 1000)
        op_id = await kit_.request(sid, "task.set_title", {"title": "x"})
        await kit_.age(op_id, 597)
        await kit_.pipeline.process(op_id)
        op = await kit_.op(op_id)
        assert (op.state, op.registration) == ("expired", None)
        assert kit_.hub.requests == []

    run(rig, body)


def test_an_unknown_type_with_a_well_formed_name_is_sent_as_is() -> None:
    assert plain_type("github.delete_repo") == "github.delete_repo"
    assert plain_type("Bad Type") == "invalid.type"
    assert plain_type("a." + "b" * 70) == "invalid.type"
    assert plain_type("nodot") == "invalid.type"


UUID = "01930005-0000-7000-8000-000000000001"


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"state": "accepted"}, Decision("accepted")),
        (
            {"state": "accepted", "child_session_id": UUID, "replayed": False},
            Decision("accepted", child_session_id=UUID),
        ),
        (
            {"state": "duplicate", "duplicate_of": UUID, "original": {"state": "executing"}},
            Decision("duplicate", duplicate_of=UUID, original_state="executing"),
        ),
        (
            {"state": "duplicate", "duplicate_of": UUID, "original": {"state": "succeeded", "result_event_id": UUID}},
            Decision("duplicate", duplicate_of=UUID, original_state="succeeded", original_result_event_id=UUID),
        ),
        ({"state": "denied", "error_code": "limit_exceeded"}, Decision("denied", error_code="limit_exceeded")),
        ({"state": "rejected", "error_code": "params_invalid"}, Decision("rejected", error_code="params_invalid")),
    ],
)
def test_decisions_are_parsed(data: dict[str, Any], expected: Decision) -> None:
    assert parse_decision(data) == expected


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"state": "maybe"},
        {"state": "accepted", "replayed": "yes"},
        {"state": "accepted", "extra": 1},
        {"state": "accepted", "error_code": "x"},
        {"state": "accepted", "child_session_id": "nope"},
        {"state": "duplicate", "duplicate_of": UUID},
        {"state": "duplicate", "duplicate_of": UUID, "original": {"state": "executing"}, "error_code": "x"},
        {"state": "duplicate", "duplicate_of": UUID, "original": {"state": "gone"}},
        {"state": "duplicate", "duplicate_of": UUID, "original": {"state": "executing", "x": 1}},
        {"state": "duplicate", "duplicate_of": 7, "original": {"state": "executing"}},
        {"state": "duplicate", "duplicate_of": UUID, "original": {"state": "succeeded", "result_event_id": 1}},
        {"state": "denied"},
        {"state": "denied", "error_code": "Bad"},
        {"state": "rejected", "error_code": "x", "duplicate_of": UUID},
    ],
)
def test_decisions_outside_the_contract_are_refused(data: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=r"uuid|decision"):
        parse_decision(data)


def test_refused_is_its_own_decision_state() -> None:
    assert register.REFUSED not in register.STATES


def test_a_stored_registration_is_sent_again_after_a_restart(rig: Rig) -> None:
    async def body(env: Any) -> None:
        kit_, sid = await ready(env)
        op_id = await kit_.request(sid, "task.set_title", {"title": "x"})
        found = await kit_.pipeline._checks(await kit_.op(op_id))
        assert found is not None
        op, error, checked = found
        prepared = await kit_.pipeline._prepare(op, env.clock() + 600, error, checked)
        assert prepared is not None
        assert prepared.registration is not None
        assert prepared.state == "received"
        await kit_.pipeline.process(op_id)  # the daemon restarted between storing and sending
        (sent,) = kit_.hub.requests
        assert sent == prepared.registration
        assert (await kit_.op(op_id)).state == "succeeded"

    run(rig, body)
