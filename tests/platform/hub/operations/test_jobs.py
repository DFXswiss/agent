"""U-HUB-operations: the expiry job `operations.expire` with the fake clock (03-api.md section 12)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from agent_hub.jobs import Job
from agent_hub.operations import jobs
from agent_hub.registry import JOBS, concat_sequences
from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.conftest import contracts_registry
from tests.platform.hub.operations.support import NO_IDENTITY, Ops


def _run(ops: Ops) -> int:
    return jobs.expire(ops.hub.app.state.hub)


def test_job_is_discovered() -> None:
    found = [job for job in concat_sequences(JOBS) if job.name == "operations.expire"]
    assert len(found) == 1
    assert isinstance(found[0], Job)
    assert found[0].run is jobs.expire


def test_accepted_without_executing_expires_after_30_minutes(
    ops: Ops, published: MemoryPublisher, op_signals: list[tuple[str, dict[str, Any]]]
) -> None:
    task, session = ops.session()
    waiting, _answer = ops.decide(session, dedupe="slow")
    running, _answer = ops.decide(session)
    ops.step(running, "executing")
    ops.hub.clock.advance(30 * 60 - 1)
    assert _run(ops) == 0
    ops.hub.clock.advance(1)
    published.clear()
    assert _run(ops) == 1
    now = ops.hub.clock()
    assert ops.operation(waiting)[:2] == ("expired", "timeout")
    assert ops.desk.rows("SELECT finished_at FROM hub.operation WHERE id = %s", waiting) == [(now,)]
    assert ops.transitions(waiting)[-1] == (3, "expired", "timeout", None)
    assert ops.slot_holder("slow") is None
    audit = ops.audit(waiting)[-1]
    assert audit[:4] == ("operation.expired", "failed", "hub", None)
    assert audit[4] == {
        "type": "github.comment",
        "error_code": "timeout",
        "duplicate_of": None,
        "local_check": None,
        **NO_IDENTITY,
    }
    contracts_registry().validate("audit/operation.expired.json", audit[4])
    sent = [dict(entry.data) for entry in published.items() if entry.frame == "operation"]
    assert [(frame["operation_id"], frame["state"]) for frame in sent] == [(waiting, "expired")]
    assert op_signals == [
        (
            "operation.finished",
            {
                "operation_id": waiting,
                "session_id": session,
                "task_id": task,
                "org_id": ops.desk.org,
                "state": "expired",
            },
        )
    ]
    assert ops.state(running) == "executing"
    assert ops.decide(session, dedupe="slow")[1] == {"state": "accepted"}
    assert ops.step(waiting, "executing") == [(ops.outbox.position(), "rejected", "request_invalid")]
    assert ops.step(waiting, "expired", error_code="timeout")[0][1] == "ok"


def test_received_expires_after_10_minutes(ops: Ops) -> None:
    _task, session = ops.session()
    operation, _answer = ops.decide(session)
    ops.desk.execute(
        "UPDATE hub.operation SET state = 'received', decided_at = NULL, created_at = %s WHERE id = %s",
        ops.hub.clock() - timedelta(minutes=10) + timedelta(seconds=1),
        operation,
    )
    assert _run(ops) == 0
    ops.hub.clock.advance(1)
    assert _run(ops) == 1
    assert ops.operation(operation)[:2] == ("expired", "hub_unreachable")


def test_final_and_decided_states_never_expire(ops: Ops) -> None:
    _task, session = ops.session()
    done, _answer = ops.decide(session)
    ops.finish(done)
    ops.decide(session, local_check="rejected", error_code="params_invalid")
    ops.hub.clock.advance(7 * 86400)
    assert _run(ops) == 0


def test_expiry_runs_in_batches(ops: Ops, monkeypatch: pytest.MonkeyPatch) -> None:
    _task, session = ops.session()
    for _ in range(3):
        ops.decide(session)
    monkeypatch.setattr(jobs, "BATCH", 2)
    ops.hub.clock.advance(3600)
    assert _run(ops) == 3
    assert ops.desk.rows("SELECT state, count(*) FROM hub.operation GROUP BY state") == [("expired", 3)]
