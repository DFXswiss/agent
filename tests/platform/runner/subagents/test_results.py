"""U-RUN-subagents: `get_subagent_result` and the `platform.query` handler (03-api.md sections 7.2, 8.1 and 10,
05-runner.md section 13, ADR-024, ADR-038): only children of the calling session, waits, `platform.read` events and
`read.session.<tool>` audits, answers to the guest."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_runner.crypto.ids import uuid7
from agent_runner.guestlink.client import JournalRecord
from agent_runner.guestlink.errors import GuestRefused, GuestUnreachable
from agent_runner.sessions.journal import JournalRecordInvalid
from agent_runner.subagents import controller as controller_module
from agent_runner.subagents.extensions import QueryCall, QueryOutcome, failure
from agent_runner.subagents.journal import READ_TOOLS, parse

from ..reconcile.rig import Rig
from .kit import TASK, run, setup
from .test_completion import first_turn_ends, settle, working_child

ROOT = Path(__file__).resolve().parents[4]
NOT_CHILD = {
    "status": "error",
    "error": {"error": "not_found", "message": "Only child sessions of this session can be read."},
}


def test_the_read_tools_are_the_audited_ones() -> None:
    actions = json.loads((ROOT / "contracts" / "enums" / "outbox-audit-actions.json").read_text())["actions"]
    assert {action.removeprefix("read.session.") for action in actions if action.startswith("read.session.")} == (
        READ_TOOLS
    )


def test_only_the_parent_reads_its_child_and_every_read_is_recorded(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent, child = await working_child(found)
        other = await found.parent(2)
        running = await found.read(parent, {"session_id": child})
        assert running == {"status": "ok", "result": {"session_id": child, "state": "working"}}
        # another session of the task, the child itself and an unknown ID: not found
        assert await found.read(other, {"session_id": child}) == NOT_CHILD
        assert await found.read(child, {"session_id": parent}) == NOT_CHILD
        assert await found.read(parent, {"session_id": uuid7()}) == NOT_CHILD
        # events and audits of the four reads
        events = [(h, b) for h, b in await found.ops.kit.pushed_events() if h["kind"] == "platform.read"]
        assert [(h["session"], h["task"], b["result_count"]) for h, b in events] == [
            (parent, TASK, 1),
            (other, TASK, 0),
            (child, TASK, 0),
            (parent, TASK, 0),
        ]
        # `ref` names the read target (04-crypto.md section 4.3: the hub refuses a platform.read with an empty ref)
        audits_by_order = [a for a in await found.env.audits() if a["action"] == "read.session.get_subagent_result"]
        assert [h["ref"] for h, _ in events] == [a["target_id"] for a in audits_by_order]
        assert events[0][0]["ref"] == child
        assert events[0][1] == {
            "tool": "get_subagent_result",
            "arguments": {"session_id": child},
            "result_count": 1,
        }
        audits = [a for a in await found.env.audits() if a["action"] == "read.session.get_subagent_result"]
        assert [(a["session_id"], a["result"], a["target_id"]) for a in audits] == [
            (parent, "ok", child),
            (other, "denied", child),
            (child, "denied", parent),
            (parent, "denied", audits[3]["target_id"]),
        ]
        assert audits[0]["detail"] == {"result_count": 1}
        assert audits[0]["target_kind"] == "session"

    run(rig, body)


def test_a_read_waits_for_the_end_of_the_child(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent, child = await working_child(found)
        await found.output(child, "Done.")
        await first_turn_ends(found, child)
        controller = found.controller
        original = controller.sleep
        sleeps: list[float] = []

        async def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            await original(seconds)
            if len(sleeps) == 1:
                await settle(found)

        controller.sleep = sleep
        answer = await found.read(parent, {"session_id": child, "wait_seconds": 30})
        assert answer["result"]["state"] == "stopped"
        assert answer["result"]["final_text"] == "Done."
        assert sleeps[0] == 0.5
        # a wait that runs out answers the current state
        _p, busy = await working_child(found, parent)
        sleeps.clear()
        controller.sleep = original
        answer = await found.read(parent, {"session_id": busy, "wait_seconds": 1})
        assert answer == {"status": "ok", "result": {"session_id": busy, "state": "working"}}

    run(rig, body)


def test_an_accepted_child_that_has_not_started_yet_is_requested(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent = await found.parent()
        child = uuid7()
        op_id = await found.ops.request(parent, "subagent.request", {"purpose": "p", "prompt": "q"})
        async with found.env.pool.connection() as conn:
            await conn.execute(
                "UPDATE runner.operation SET state = 'accepted', child_session_id = %s WHERE id = %s", (child, op_id)
            )
        assert await found.read(parent, {"session_id": child}) == {
            "status": "ok",
            "result": {"session_id": child, "state": "requested"},
        }

    run(rig, body)


@pytest.mark.parametrize(
    "arguments",
    [
        {"session_id": "not-a-uuid"},
        {"session_id": None},
        {"session_id": "x", "wait_seconds": True},
        # an unknown key (the input schema has additionalProperties false; KOR-03, AUD-01 A-18)
        {"session_id": "01930007-0000-7000-8000-000000000001", "x": "text of the VM"},
    ],
)
def test_invalid_arguments_are_refused(rig: Rig, arguments: dict[str, Any]) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent = await found.parent()
        call = QueryCall(env.context, await found.stored(parent), "get_subagent_result", arguments)
        outcome = await found.controller.result(call)
        assert outcome.answer() == {
            "status": "error",
            "error": {"error": "params_invalid", "message": "The arguments do not match the tool."},
        }

    run(rig, body)


def test_a_wait_above_the_limit_is_refused(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent = await found.parent()
        call = QueryCall(
            env.context, await found.stored(parent), "get_subagent_result", {"session_id": uuid7(), "wait_seconds": 31}
        )
        assert (await found.controller.result(call)).error == "params_invalid"

    run(rig, body)


def test_a_tool_without_provider_is_left_to_the_guest_timeout(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent = await found.parent()
        before = found.guest_answers()
        await found.emit(parent, "platform.query", {"pending_id": uuid7(), "tool": "get_operation", "arguments": {}})
        await found.settle_queries()
        assert found.guest_answers() == before
        assert [a for a in await found.env.audits() if a["action"].startswith("read.")] == []
        assert f"runner.subagents query_unrouted tool=get_operation session_id={parent}" in found.lines

    run(rig, body)


def test_a_pending_call_is_answered_once_and_parallel_reads_are_bounded(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent, child = await working_child(found)
        controller = found.controller
        row = await found.ops.vm()
        session = await found.stored(parent)
        args = {"session_id": child}
        first = controller.spawn_query(row, session, "p-1", "get_subagent_result", args)
        assert first is not None
        assert controller.spawn_query(row, session, "p-1", "get_subagent_result", args) is None
        await found.settle_queries()
        # more than four reads of one session at a time: the fifth is refused without a record
        gate = asyncio.Event()

        async def slow(call: QueryCall) -> QueryOutcome:
            await gate.wait()
            return QueryOutcome(result={}, result_count=0)

        controller.tools["list_tasks"] = slow
        before = len(found.guest_answers())
        for n in range(5):
            controller.spawn_query(row, session, f"q-{n}", "list_tasks", {})
        for _ in range(5):
            await asyncio.sleep(0)
        assert f"runner.subagents query_refused tool=list_tasks session_id={parent}" in found.lines
        gate.set()
        await found.settle_queries()
        answers = found.guest_answers()[before:]
        assert sum(1 for _name, (_p, a) in answers if a["status"] == "error") == 1
        assert controller.reading == {}
        # the memory of answered calls is bounded
        controller_module.PENDING_MEMORY, saved = 2, controller_module.PENDING_MEMORY
        try:
            for n in range(3):
                controller.spawn_query(row, session, f"r-{n}", "list_tasks", {})
            assert list(controller.answered)[-2:] == ["r-1", "r-2"]
            assert "r-0" not in controller.answered
        finally:
            controller_module.PENDING_MEMORY = saved
        await found.settle_queries()

    run(rig, body)


def test_a_failing_tool_is_answered_unavailable_and_recorded(rig: Rig) -> None:
    async def body(env: Any) -> None:
        async def broken(_call: QueryCall) -> QueryOutcome:
            raise RuntimeError("x")

        found = await setup(env, tools={"list_tasks": broken})
        parent = await found.parent()
        answer = await found.read(parent, {}, tool="list_tasks")
        assert answer["error"]["error"] == "unavailable"
        (audit,) = [a for a in await found.env.audits() if a["action"] == "read.session.list_tasks"]
        assert (audit["result"], audit["target_id"]) == ("failed", parent)
        assert "runner.subagents query_failed tool=list_tasks error=RuntimeError" in found.lines
        # nothing was read and no tool checked the arguments: no platform.read event (KOR-03, AUD-01 A-18)
        assert not [h for h, _b in await found.ops.kit.pushed_events() if h["kind"] == "platform.read"]

    run(rig, body)


def test_answers_are_retried_and_a_refusal_ends_the_attempts(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent, child = await working_child(found)
        before = len(found.guest_answers())
        found.guest.answer_errors.extend([GuestUnreachable("x"), GuestRefused("pending", 409, "state_conflict")])
        pending = uuid7()
        await found.emit(
            parent,
            "platform.query",
            {"pending_id": pending, "tool": "get_subagent_result", "arguments": {"session_id": child}},
        )
        await found.settle_queries()
        assert len(found.guest_answers()) == before + 2
        assert any("answer_failed" in line and "attempt=1" in line for line in found.lines)
        assert any("answer_refused" in line and "error=state_conflict" in line for line in found.lines)
        # three failures: given up
        found.guest.answer_errors.extend([GuestUnreachable("x")] * 3)
        await found.emit(
            parent,
            "platform.query",
            {"pending_id": uuid7(), "tool": "get_subagent_result", "arguments": {"session_id": child}},
        )
        await found.settle_queries()
        assert len(found.guest_answers()) == before + 5
        assert any("attempt=3" in line for line in found.lines)

    run(rig, body)


def test_a_read_that_cannot_be_recorded_is_not_answered(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent, child = await working_child(found)

        async def broken(*_args: Any) -> None:
            raise RuntimeError("store down")

        found.controller._record = broken  # type: ignore[method-assign,assignment]
        before = found.guest_answers()
        await found.emit(
            parent,
            "platform.query",
            {"pending_id": uuid7(), "tool": "get_subagent_result", "arguments": {"session_id": child}},
        )
        await found.settle_queries()
        assert found.guest_answers() == before
        assert "runner.subagents query_unanswered tool=get_subagent_result error=RuntimeError" in found.lines

    run(rig, body)


def test_reads_of_unknown_sessions_and_without_controller_are_dropped(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent = await found.parent()
        body_ = {"pending_id": uuid7(), "tool": "get_subagent_result", "arguments": {"session_id": parent}}
        await found.emit(uuid7(), "platform.query", body_)
        await found.settle_queries()
        assert found.guest_answers() == []
        # without controller (a device that is not enrolled has none)
        found.ops.kit.session_env.context = SimpleNamespace(identity=None)
        await found.emit(parent, "platform.query", body_)
        assert found.guest_answers() == []
        assert f"runner.subagents query_unrouted tool=get_subagent_result session_id={parent}" in found.lines

    run(rig, body)


@pytest.mark.parametrize(
    "body",
    [
        {"pending_id": "x", "tool": "get_task", "arguments": {}},
        {"pending_id": "01930000-0000-7000-8000-000000000001", "tool": "shell", "arguments": {}},
        {"pending_id": "01930000-0000-7000-8000-000000000001", "tool": "get_task", "arguments": []},
        {"pending_id": "01930000-0000-7000-8000-000000000001", "tool": "get_task", "arguments": {"a": "\x00"}},
        {"pending_id": "01930000-0000-7000-8000-000000000001", "tool": "get_task", "arguments": {}, "x": 1},
        {"pending_id": 1, "tool": "get_task", "arguments": {}},
    ],
)
def test_malformed_queries_are_refused(body: dict[str, Any]) -> None:
    with pytest.raises(JournalRecordInvalid):
        parse(body)


def test_a_malformed_query_is_skipped_by_the_journal(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent = await found.parent()
        record = JournalRecord(seq=1, ts="t", session_id=parent, kind="platform.query", body={"tool": "x"})
        reader = found.reader(await found.ops.vm())
        await reader.handle([record])
        assert reader.skipped == 1

    run(rig, body)


def test_outcomes() -> None:
    assert QueryOutcome(result={"a": 1}, result_count=1).answer() == {"status": "ok", "result": {"a": 1}}
    assert QueryOutcome(error="timeout").answer() == {
        "status": "error",
        "error": {"error": "timeout", "message": "The read failed."},
    }
    assert QueryOutcome(error="timeout").audit_result == "failed"
    assert failure("forbidden", "No.").audit_result == "denied"
    with pytest.raises(ValueError, match="shell"):
        failure("shell", "x")


def test_the_tool_without_controller_is_unavailable(rig: Rig) -> None:
    async def body(env: Any) -> None:
        from agent_runner.subagents.query_tools import get_subagent_result  # noqa: PLC0415

        found = await setup(env)
        parent = await found.stored(await found.parent())
        outcome = await get_subagent_result(
            QueryCall(SimpleNamespace(identity=None), parent, "get_subagent_result", {})
        )
        assert outcome.error == "unavailable"

    run(rig, body)
