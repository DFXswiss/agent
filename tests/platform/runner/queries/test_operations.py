"""U-RUN-queries operations: `list_operations` and `get_operation` of the own task (03-api.md sections 8.1 and 9)
through the real operation pipeline of RUN-07 (`Pipeline.wait_answer`)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_runner.crypto.ids import uuid7
from agent_runner.operations import records as op_records
from agent_runner.queries.tools import get_operation as get_operation_module
from agent_runner.queries.tools.list_operations import ERROR_CODES

from ..reconcile.rig import Rig
from .kit import ORG, TASK, TASK_B, WG, RKit, run

ROOT = Path(__file__).resolve().parents[4]


def test_the_error_codes_are_the_contract_ones() -> None:
    found = json.loads((ROOT / "contracts" / "enums" / "operation-error-codes.json").read_text())
    assert set(found["codes"]) == ERROR_CODES


async def _received(k: RKit, sid: str, op_type: str, *, error_code: str | None = None) -> str:
    op_id = uuid7()
    vm = await k.skit.ops.vm()
    async with k.env.pool.connection() as conn, conn.transaction():
        await op_records.insert(
            conn,
            operation_id=op_id,
            session_id=sid,
            task_id=TASK,
            wg_id=WG,
            org_id=ORG,
            vm_id=vm.id,
            pending_id=uuid7(),
            op_type=op_type,
            params={},
            requested_at="2026-11-21T12:00:01.000Z",
            idempotency_key=None,
            local_error=None,
            actor_id=None,
        )
        if error_code is not None:
            await op_records.update(conn, op_id, state="failed", error_code=error_code)
    return op_id


def test_operations_of_the_own_task(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        sid = await k.session(1)
        other = await k.session(2, task=TASK_B)
        op = await k.skit.ops.run(sid, "task.set_title", {"title": "Renamed"})
        assert op.state == "succeeded"
        found = await k.ok(sid, "get_operation", {"operation_id": op.id})
        assert found == {"operation_id": op.id, "status": "succeeded", "result": {}}
        listed = (await k.ok(sid, "list_operations"))["operations"]
        assert [(o["operation_id"], o["type"], o["state"], o["error_code"]) for o in listed] == [
            (op.id, "task.set_title", "succeeded", None)
        ]
        # another task of the workgroup, and an unknown operation: not found
        assert await k.error(other, "get_operation", {"operation_id": op.id}) == "not_found"
        assert await k.error(sid, "get_operation", {"operation_id": uuid7()}) == "not_found"
        assert (await k.ok(other, "list_operations"))["operations"] == []
        for wrong in ({}, {"operation_id": "x"}, {"operation_id": op.id, "wait_seconds": 31}):
            assert await k.error(sid, "get_operation", wrong) == "params_invalid"
        assert await k.error(sid, "list_operations", {"limit": 101}) == "params_invalid"

    run(rig, body)


def test_a_running_operation_is_pending_after_the_wait(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        sid = await k.session(1)
        op_id = await _received(k, sid, "task.set_title")
        before = k.env.clock.now
        assert await k.ok(sid, "get_operation", {"operation_id": op_id, "wait_seconds": 3}) == {
            "operation_id": op_id,
            "status": "pending",
        }
        assert k.env.clock.now - before >= 3

    run(rig, body)


def test_list_operations_newest_first_with_contract_values_only(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        sid = await k.session(1)
        first = await _received(k, sid, "task.set_title", error_code="weird_code")
        await _received(k, sid, "Not A Type")
        second = await _received(k, sid, "github.comment", error_code="timeout")
        listed = (await k.ok(sid, "list_operations", {"limit": 10}))["operations"]
        assert [(o["operation_id"], o["error_code"]) for o in listed] == [(second, "timeout"), (first, None)]
        assert (await k.ok(sid, "list_operations", {"limit": 1}))["operations"][0]["operation_id"] == second

    run(rig, body)


def test_get_operation_without_a_pipeline_is_unavailable(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_operation_module, "pipeline_for", lambda _context: None)

    async def body(k: RKit) -> None:
        sid = await k.session(1)
        answer = await k.read(sid, "get_operation", {"operation_id": uuid7()})
        assert answer["error"] == {
            "error": "unavailable",
            "message": "Operations are not available on this device right now.",
        }

    run(rig, body)
