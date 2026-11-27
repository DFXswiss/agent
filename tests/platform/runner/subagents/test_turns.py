"""U-RUN-subagents: full children of a current guest (ADR-088 section 1; 05-runner.md section 13, 03-api.md sections 8.1
and 9) - turns with a `subagent.finished` ping each, follow-up instructions one per turn in the order of creation,
the acting user of a child's turn, suspension of an idle child and resume by the next instruction, `subagent.stop`,
the end with the parent, and `get_subagent_result` about the newest instruction. Driven by guest journal records
through the real handlers, the real pipeline and the contract-checking hub of the RUN-07 kit."""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_guest.schema import SchemaSet
from agent_runner.credentials import acting
from agent_runner.crypto.ids import uuid7
from agent_runner.guestlink.errors import GuestRefused, GuestUnreachable
from agent_runner.operations.extensions import OperationContext, OperationFailed, prechecks
from agent_runner.prompts.resume import RETRY
from agent_runner.subagents import instructions, session_state, store, target_checks, turns
from agent_runner.subagents.controller import GRACE_S, SETTLE_S, Subagents, ping_id
from agent_runner.subagents.instructions import Instruction
from agent_runner.vm import tasks as vm_tasks
from agent_runner.vm.room import Room

from ..operations.kit import OpGuest
from ..reconcile.rig import Rig
from ..sessions.kit import OWNER
from ..settle import until
from .kit import TASK, SKit, run
from .kit import setup as kit_setup

SCHEMAS = SchemaSet(Path(__file__).resolve().parents[4] / "contracts")
OTHER = "0193000b-0000-7000-8000-00000000000b"
"""Another member who instructs the parent's turn."""


@dataclasses.dataclass
class TurnGuest(OpGuest):
    prompt_errors: list[BaseException] = dataclasses.field(default_factory=list)
    hold: asyncio.Event | None = None
    """When set, a registration waits for it (the guest typed and journaled before the runner sees its answer)."""
    entered: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)

    async def register_prompt(self, sid: str, prompt_id: str, text: str, speaker: str | None = None) -> dict[str, Any]:
        self._call("register_prompt", (sid, prompt_id, text, speaker))
        if self.hold is not None:
            self.entered.set()
            await self.hold.wait()
        if self.prompt_errors:
            raise self.prompt_errors.pop(0)
        return {"state": "registered"}

    def registered(self) -> list[tuple[str, str, str, str | None]]:
        return [value for name, value in self.calls if name == "register_prompt"]


async def setup(env: Any, **options: Any) -> SKit:
    return await kit_setup(env, guest=TurnGuest(), **options)


def guest(found: SKit) -> TurnGuest:
    assert isinstance(found.guest, TurnGuest)
    return found.guest


def controls(found: SKit) -> list[tuple[str, str]]:
    return [value for name, value in found.guest.calls if name == "control"]


async def full_child(found: SKit, parent: str | None = None) -> tuple[str, str, str]:
    """A child that accepted its first instruction (the request): `(parent, child, request operation ID)`."""
    parent = parent or await found.parent()
    child = uuid7()
    op = await found.subagent(parent, child=child)
    assert op.state == "succeeded"
    await found.state(child, "prompt_delivering")
    await found.state(child, "prompt_accepted", prompt_id=op.id)
    return parent, child, op.id


async def turn_ends(found: SKit, child: str, event: str = "Stop") -> None:
    await found.hook(child, event)
    await found.state(child, "idle")


async def settle_turn(found: SKit) -> None:
    """Grace, settle and ping of a turn's end: the controller's ticks with the clock moved in between."""
    await found.tick()
    found.env.clock.now += GRACE_S
    await found.tick()
    found.env.clock.now += SETTLE_S
    await found.tick()


async def result(found: SKit, parent: str, child: str, **arguments: Any) -> dict[str, Any]:
    answer = await found.read(parent, {"session_id": child, **arguments})
    assert answer["status"] == "ok", answer
    assert SCHEMAS.errors("mcp/tools/get_subagent_result.output.json", answer["result"]) == []
    return dict(answer["result"])


async def instruct(
    found: SKit, parent: str, child: str, prompt: str = "Check the tests too.", *, rejected: str | None = None
) -> Any:
    """Request and process a `subagent.instruct`; `rejected`: the runner refuses it locally (the hub then answers
    `rejected` with that code)."""
    if rejected is not None:
        found.ops.hub.decisions.append({"state": "rejected", "error_code": rejected})
    return await found.ops.run(parent, "subagent.instruct", {"session_id": child, "prompt": prompt})


async def stop(found: SKit, parent: str, child: str, *, rejected: str | None = None) -> Any:
    if rejected is not None:
        found.ops.hub.decisions.append({"state": "rejected", "error_code": rejected})
    return await found.ops.run(parent, "subagent.stop", {"session_id": child})


async def stamp(found: SKit, child: str) -> str | None:
    async with found.env.pool.connection() as conn:
        return await acting.stamp(conn, await found.stored(child))


async def instructed_by(found: SKit, parent: str, actor: str) -> None:
    async with found.env.pool.connection() as conn:
        await conn.execute("UPDATE runner.session SET turn_actor = %s WHERE id = %s", (actor, parent))


async def rows(found: SKit, child: str) -> list[Instruction]:
    async with found.env.pool.connection() as conn:
        cursor = await conn.execute(
            "SELECT operation_id::text FROM runner.subagent_instruction WHERE child_session_id = %s"
            " ORDER BY created_at, operation_id",
            (child,),
        )
        found_rows = []
        for (operation_id,) in await cursor.fetchall():
            row = await instructions.get(conn, operation_id)
            assert row is not None
            found_rows.append(row)
        return found_rows


def limited(found: SKit, running: int) -> None:
    found.env.context.session_service = found.service
    found.service.env.desired = {
        "limits": {
            "api": 1,
            "subagent_max_depth": 5,
            "subagent_max_running": running,
            "subagent_max_total": 0,
            "subagent_max_runtime_seconds": 0,
        }
    }


def vm_full(found: SKit) -> None:
    """The task's VM measured full (`vm.room`, through the daemon's VM manager): RAM and CPU used up (HOT-32)."""

    async def session_room(_row: Any) -> Room:
        return Room(False, 512, 2048, 1.1, 1.2)

    vars(found.env.context)[vm_tasks.MANAGER_KEY] = SimpleNamespace(
        env=SimpleNamespace(db=found.env.pool, session_room=session_room)
    )


# -- turns -------------------------------------------------------------------------------------------------------------


def test_a_turn_keeps_the_child_running_and_pings_its_parent_once_per_instruction(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent, child, first = await full_child(found)
        assert await result(found, parent, child) == {"session_id": child, "state": "working", "instruction_id": first}
        await found.output(child, "The change looks good.", "msg_a")
        await turn_ends(found, child)
        # within the settle time the turn's answer is not final yet
        await found.tick()
        found.env.clock.now += GRACE_S
        await found.tick()
        assert await result(found, parent, child) == {"session_id": child, "state": "working", "instruction_id": first}
        found.env.clock.now += SETTLE_S
        await found.tick()
        assert controls(found) == []
        assert (await found.stored(child)).state == "idle"
        assert [(p.parent_session_id, p.child_session_id, p.ping_id) for p in found.pinged] == [
            (parent, child, turns.ping_id(child, first))
        ]
        assert turns.ping_id(child, first) != ping_id(child)
        answer = await result(found, parent, child)
        assert answer == {
            "session_id": child,
            "state": "idle",
            "final_text": "The change looks good.",
            "finished_at": answer["finished_at"],
            "instruction_id": first,
        }
        assert await result(found, parent, child) == answer  # the cached text
        # one ping per turn, also over further ticks
        await found.tick()
        assert len(found.pinged) == 1
        assert any(f"turn_ended child_session_id={child} instruction_id={first}" in line for line in found.lines)

    run(rig, body)


def test_a_follow_up_is_handed_over_with_the_childs_sender_line_and_answered_in_its_own_turn(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent, child, first = await full_child(found)
        await found.output(child, "First answer.", "msg_a")
        await turn_ends(found, child)
        await settle_turn(found)
        await instructed_by(found, parent, OTHER)
        op = await instruct(found, parent, child)
        assert (op.state, op.result) == ("succeeded", {"session_id": child, "instruction_id": op.id})
        assert op.actor_id == OTHER
        ((sid, prompt_id, text, line),) = guest(found).registered()
        assert (sid, prompt_id, text) == (child, op.id, "Check the tests too.")
        assert line is not None
        assert line.startswith(f"agent.dfx.swiss instruction from: your parent session (session {parent})")
        assert f"(member {OTHER})" in line
        # registered, not accepted: the newest instruction is working, the turn's actor is still the request's
        assert await result(found, parent, child) == {"session_id": child, "state": "working", "instruction_id": op.id}
        assert await stamp(found, child) == OWNER
        await found.state(child, "prompt_accepted", prompt_id=op.id)
        assert await stamp(found, child) == OTHER
        await found.output(child, "Second answer.", "msg_b")
        await turn_ends(found, child)
        await settle_turn(found)
        assert [p.ping_id for p in found.pinged] == [turns.ping_id(child, first), turns.ping_id(child, op.id)]
        answer = await result(found, parent, child)
        assert (answer["state"], answer["final_text"], answer["instruction_id"]) == ("idle", "Second answer.", op.id)
        # after the turn the child keeps that turn's actor
        assert await stamp(found, child) == OTHER
        # a replayed acceptance of the older instruction (the owner's request) brings back no rights
        await found.state(child, "prompt_accepted", prompt_id=first)
        assert await stamp(found, child) == OTHER
        # a repeated execution of a delivered instruction answers the same result
        ctx = OperationContext(env.context, op, await found.stored(parent), op.params, {}, 2, found.ops.kit.session_env)
        assert await found.controller.turns.instruct(ctx) == {"session_id": child, "instruction_id": op.id}
        assert len(guest(found).registered()) == 1

    run(rig, body)


def test_instructions_are_handed_over_one_per_turn_and_a_queued_one_never_changes_the_running_turns_actor(
    rig: Rig,
) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent, child, first = await full_child(found)
        a = asyncio.ensure_future(instruct(found, parent, child, "A"))
        await until(lambda: _count(found, child, 2), "instruction A")
        await instructed_by(found, parent, OTHER)
        b = asyncio.ensure_future(instruct(found, parent, child, "B"))
        await until(lambda: _count(found, child, 3), "instruction B")
        _request, row_a, row_b = await rows(found, child)
        assert (row_a.actor_id, row_b.actor_id) == (OWNER, OTHER)
        # the child works on the request: nothing is handed over, the turn's actor stays
        await asyncio.sleep(0.05)
        assert guest(found).registered() == []
        assert await stamp(found, child) == OWNER
        await turn_ends(found, child)
        await until(a.done, "instruction A handed over")
        assert (await a).result == {"session_id": child, "instruction_id": row_a.operation_id}
        assert [prompt_id for _sid, prompt_id, _text, _line in guest(found).registered()] == [row_a.operation_id]
        # A is registered but not accepted: B waits
        await asyncio.sleep(0.05)
        assert not b.done()
        await found.state(child, "prompt_accepted", prompt_id=row_a.operation_id)
        assert await stamp(found, child) == OWNER
        await asyncio.sleep(0.05)
        assert not b.done()
        assert await result(found, parent, child) == {
            "session_id": child,
            "state": "working",
            "instruction_id": row_b.operation_id,
        }
        await turn_ends(found, child)
        await until(b.done, "instruction B handed over")
        assert (await b).state == "succeeded"
        await found.state(child, "prompt_accepted", prompt_id=row_b.operation_id)
        assert await stamp(found, child) == OTHER
        assert first != row_a.operation_id

    run(rig, body)


async def _count(found: SKit, child: str, count: int) -> bool:
    return len(await rows(found, child)) >= count


def test_a_stop_failure_fails_the_child_and_pings_with_the_turns_id(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent, child, first = await full_child(found)
        await turn_ends(found, child, "StopFailure")
        await settle_turn(found)
        assert ("failed", "agent_exited") in await found.ops.kit.transitions(child)
        assert controls(found) == [(child, "stop")]
        await found.tick()
        assert [p.ping_id for p in found.pinged] == [turns.ping_id(child, first)]
        answer = await result(found, parent, child)
        assert answer == {"session_id": child, "state": "failed", "finished_at": answer["finished_at"]}
        # a turn end the guest already failed changes nothing
        row = await found.stored(child)
        found.controller.turns.seen[child] = found.env.clock() - 100
        await found.controller.turns._turn_ended(row, None, child)
        assert controls(found) == [(child, "stop")]

    run(rig, body)


def test_a_wait_ends_with_the_turn(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent, child, first = await full_child(found)
        await found.output(child, "Done.", "msg_a")
        controller = found.controller
        original = controller.sleep
        waited: list[float] = []

        async def sleep(seconds: float) -> None:
            waited.append(seconds)
            await original(seconds)
            if len(waited) == 1:
                await turn_ends(found, child)
                await settle_turn(found)

        controller.sleep = sleep
        answer = await result(found, parent, child, wait_seconds=30)
        assert (answer["state"], answer["final_text"], answer["instruction_id"]) == ("idle", "Done.", first)

    run(rig, body)


# -- suspend and resume ------------------------------------------------------------------------------------------------


def test_an_idle_child_is_suspended_and_resumed_by_its_next_instruction(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        found.controller.idle_seconds = 60
        parent, child, first = await full_child(found)
        await found.output(child, "Answer.", "msg_a")
        await turn_ends(found, child)
        await settle_turn(found)
        assert controls(found) == []
        found.env.clock.now += 60
        await found.tick()
        assert controls(found) == [(child, "suspend")]
        assert (await found.stored(child)).state == "suspended"
        answer = await result(found, parent, child)
        assert (answer["state"], answer["final_text"], answer["instruction_id"]) == ("suspended", "Answer.", first)
        # resuming makes it run again: no number of running children (an older hub's `subagent_max_running` is read
        # past, ADR-075 addendum 2) and no refusal although the task's VM is measured full (HOT-32: running work)
        _other_parent, _busy, _ = await full_child(found, await found.parent(2))
        limited(found, running=1)
        vm_full(found)
        creates_before = len([1 for name, _ in found.guest.calls if name == "create_session"])
        op = await instruct(found, parent, child)
        assert op.state == "succeeded"
        resumes = [value for name, value in found.guest.calls if name == "create_session"][creates_before:]
        (request,) = resumes
        assert (request["session_id"], request["parent_session_id"], request["depth"]) == (child, parent, 1)
        assert request["harness"]["context_markdown"].endswith(
            "you keep running and receive further instructions until your main agent stops you; you may request"
            " further subagents only via `subagent.request`."
        )
        assert request["limits"]["runtime_max_sec"] == 8 * 3600
        assert SCHEMAS.errors("guest/sessions-create.request.json", request) == []
        assert [prompt_id for _s, prompt_id, _t, _l in guest(found).registered()] == [op.id]

    run(rig, body)


def test_a_suspension_waits_for_a_pending_instruction_and_a_refusing_or_unreachable_guest(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        found.controller.idle_seconds = 60
        parent, child, _first = await full_child(found)
        await turn_ends(found, child)
        await settle_turn(found)
        turns_of = found.controller.turns
        found.env.clock.now += 60
        # the guest refuses (a turn started meanwhile): counted again from now
        found.guest.fail["control"] = GuestRefused("control", 409, "state_conflict")
        await found.tick()
        assert found.controller.idle_since[child] == found.env.clock()
        assert any(f"suspend_refused child_session_id={child}" in line for line in found.lines)
        # unreachable: tried again later
        found.guest.fail["control"] = GuestUnreachable("x")
        found.env.clock.now += 60
        await found.tick()
        assert any(f"suspend_retry child_session_id={child}" in line for line in found.lines)
        del found.guest.fail["control"]
        # an instruction was handed over meanwhile: no suspension
        op = await instruct(found, parent, child)
        assert op.state == "succeeded"
        row = await found.stored(child)
        await turns_of._suspend(row)
        assert (await found.stored(child)).state == "idle"
        assert not turns_of.due(row, (await rows(found, child))[-1], 10**6)
        # a child whose VM does not run is not suspended
        await found.state(child, "prompt_accepted", prompt_id=op.id)
        await turn_ends(found, child)
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.vm SET state = 'stopped' WHERE id = %s", (row.vm_id,))
        await turns_of._suspend(await found.stored(child))
        assert (await found.stored(child)).state == "idle"

    run(rig, body)


# -- end -------------------------------------------------------------------------------------------------------------


def test_the_parent_stops_its_child_and_a_stop_of_another_session_is_refused(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent, child, first = await full_child(found)
        other = await found.parent(2)
        # only the parent: another session, an unknown session and the parent itself are not its children
        for session, target in ((other, child), (parent, uuid7()), (child, parent)):
            refused = await stop(found, session, target, rejected="not_found")
            assert (refused.state, refused.error_code) == ("rejected", "not_found")
            assert refused.answer is not None
            assert refused.answer["error"] == {"code": "not_found", "message": turns.NOT_CHILD}
            refused = await instruct(found, session, target, rejected="not_found")
            assert (refused.state, refused.error_code) == ("rejected", "not_found")
        # the executor checks the target again (it may have changed since the pre-check)
        ctx = OperationContext(
            env.context, await found.ops.op(first), await found.stored(other), {"session_id": child}, {}, 1, None
        )
        with pytest.raises(OperationFailed) as caught:
            await found.controller.turns.stop(ctx)
        assert (caught.value.code, caught.value.message) == ("not_found", turns.NOT_CHILD)
        op = await stop(found, parent, child)
        assert (op.state, op.result) == ("succeeded", {"session_id": child, "state": "stopped"})
        assert controls(found) == [(child, "stop")]
        assert await result(found, parent, child) == {"session_id": child, "state": "stopping"}  # not settled yet
        ended = OperationContext(
            env.context, op, await found.stored(parent), {"session_id": child, "prompt": "p"}, {}, 1, None
        )
        with pytest.raises(OperationFailed) as caught:
            await found.controller.turns.instruct(ended)
        assert caught.value.code == "not_found"
        # an ended child: refused before the registration, a repeated execution answers its state
        again = await stop(found, parent, child, rejected="not_found")
        assert (again.state, again.error_code) == ("rejected", "not_found")
        late = await instruct(found, parent, child, rejected="not_found")
        assert (late.state, late.error_code) == ("rejected", "not_found")
        ctx = OperationContext(env.context, op, await found.stored(parent), op.params, {}, 2, found.ops.kit.session_env)
        assert await found.controller.turns.stop(ctx) == {"session_id": child, "state": "stopped"}
        # the end pings the parent with the newest instruction's ID
        await found.tick()
        found.env.clock.now += SETTLE_S
        await found.tick()
        assert found.pinged[-1].ping_id == turns.ping_id(child, first)
        answer = await result(found, parent, child)
        assert (answer["state"], answer["instruction_id"]) == ("stopped", first)

    run(rig, body)


def test_a_stop_while_the_guest_cannot_be_reached_answers_stopping(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent, child, _first = await full_child(found)
        found.guest.fail["control"] = GuestUnreachable("x")
        op = await stop(found, parent, child)
        assert (op.state, op.result) == ("succeeded", {"session_id": child, "state": "stopping"})
        assert await result(found, parent, child) == {"session_id": child, "state": "stopping"}

    run(rig, body)


def test_children_end_with_their_parent(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent, child, _first = await full_child(found)
        _other_parent, other, _ = await full_child(found, await found.parent(2))
        await found.tick()
        assert controls(found) == []
        await found.state(parent, "failed", reason="oom")
        await found.tick()
        assert controls(found) == [(child, "stop")]
        assert (await found.stored(child)).state == "stopped"
        assert (await found.stored(other)).state == "working"
        assert any(f"parent_ended child_session_id={child} stopped=1" in line for line in found.lines)
        # the parent ended: no ping
        found.env.clock.now += SETTLE_S
        await found.tick()
        assert found.pinged == []

    run(rig, body)


# -- the executor's failures -----------------------------------------------------------------------------------------


def test_an_instruction_for_a_child_that_ended_while_it_waited_fails_not_found(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent, child, first = await full_child(found)
        waiting = asyncio.ensure_future(instruct(found, parent, child))
        await until(lambda: _count(found, child, 2), "the instruction")
        await found.state(child, "failed", reason="oom")
        op = await waiting
        assert (op.state, op.error_code) == ("failed", "not_found")
        assert op.answer is not None
        assert op.answer["error"]["message"] == turns.ENDED
        # a failed instruction is no instruction: the newest is still the request
        assert (await result(found, parent, child))["state"] == "failed"
        async with env.pool.connection() as conn:
            assert (await instructions.newest(conn, [child]))[child].operation_id == first

    run(rig, body)


def test_the_guest_refusing_or_missing_an_instruction(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent, child, _first = await full_child(found)
        await turn_ends(found, child)
        # registered before (a repeated delivery): success
        guest(found).prompt_errors.append(GuestRefused("prompts", 409, "state_conflict"))
        assert (await instruct(found, parent, child)).state == "succeeded"
        await found.state(child, "prompt_accepted", prompt_id=guest(found).registered()[-1][1])
        await turn_ends(found, child)
        # any other refusal fails the instruction
        guest(found).prompt_errors.extend([GuestRefused("prompts", 400, "request_invalid")] * 2)
        refused = await instruct(found, parent, child)
        assert (refused.state, refused.error_code) == ("failed", "upstream_error")
        assert refused.answer is not None
        assert refused.answer["error"]["message"] == turns.REFUSED
        # unreachable: retried by the pipeline, then failed retryable
        guest(found).prompt_errors.extend([GuestUnreachable("x")] * 4)
        unreachable = await instruct(found, parent, child)
        assert (unreachable.state, unreachable.error_code, unreachable.retryable) == ("failed", "upstream_error", True)
        # neither counts as handed over (the write-ahead mark is taken back), so the next one is not blocked
        marks = {row.operation_id: row.delivered_at for row in await rows(found, child)}
        assert (marks[refused.id], marks[unreachable.id]) == (None, None)
        nxt = await instruct(found, parent, child)
        assert nxt.state == "succeeded"
        await found.state(child, "prompt_accepted", prompt_id=nxt.id)
        await turn_ends(found, child)
        # a VM that does not run (the store no longer knows the child's VM)
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.session SET vm_id = 'vm-gone' WHERE id = %s", (child,))
        stopped = await instruct(found, parent, child)
        assert (stopped.state, stopped.error_code) == ("failed", "upstream_error")

    run(rig, body)


def test_a_resume_that_must_be_retried_or_that_ended_the_child(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent, child, _first = await full_child(found)
        await turn_ends(found, child)
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.session SET state = 'suspended' WHERE id = %s", (child,))
        turns_of = found.controller.turns
        outcomes = [RETRY, "vm_start_failed"]

        async def resume(_session: Any, _amend: Any = None) -> str:
            return outcomes.pop(0)

        turns_of.resumer.resume = resume  # type: ignore[method-assign,assignment]
        outcomes[:] = [RETRY] * 4
        retried = await instruct(found, parent, child)
        assert (retried.state, retried.error_code, retried.retryable) == ("failed", "upstream_error", True)
        outcomes[:] = ["vm_start_failed"]
        ended = await instruct(found, parent, child)
        assert (ended.state, ended.error_code) == ("failed", "not_found")

    run(rig, body)


def test_a_child_is_never_moved_to_another_vm_on_resume(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent, child, _first = await full_child(found)
        await turn_ends(found, child)
        row = await found.stored(child)
        moved = dataclasses.replace(row, vm_id="vm-elsewhere", state="suspended")
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.session SET state = 'suspended' WHERE id = %s", (child,))
        outcome = await found.controller.turns.resumer.resume(moved, found.controller.resume_request)
        assert outcome == "vm_start_failed"
        assert (await found.stored(child)).state == "failed"
        assert parent != child

    run(rig, body)


def test_the_executors_need_a_controller(rig: Rig) -> None:
    async def body(env: Any) -> None:
        from agent_runner.subagents import executors  # noqa: PLC0415

        found = await setup(env)
        parent, child, first = await full_child(found)
        op = await found.ops.op(first)
        bare = SimpleNamespace(identity=None)  # a device that is not enrolled
        ctx = OperationContext(bare, op, await found.stored(parent), {"session_id": child}, {}, 1, None)
        assert executors.EXECUTORS["subagent.instruct"] is executors.instruct_subagent
        assert executors.EXECUTORS["subagent.stop"] is executors.stop_subagent
        assert prechecks()["subagent.instruct"] is target_checks.instruct_check
        assert prechecks()["subagent.stop"] is target_checks.stop_check
        for executor in (executors.instruct_subagent, executors.stop_subagent):
            with pytest.raises(OperationFailed) as caught:
                await executor(ctx)
            assert (caught.value.code, caught.value.message) == ("upstream_error", executors.UNAVAILABLE)

    run(rig, body)


def test_an_acceptance_journaled_before_the_guests_answer_arrives_counts(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent, child, first = await full_child(found)
        await turn_ends(found, child)
        await settle_turn(found)
        await instructed_by(found, parent, OTHER)
        hold = guest(found).hold = asyncio.Event()
        pending = asyncio.ensure_future(instruct(found, parent, child, "Go on."))
        await guest(found).entered.wait()
        # the guest typed the prompt and journaled its acceptance; the runner has not seen the answer yet
        op_id = guest(found).registered()[-1][1]
        await found.state(child, "prompt_accepted", prompt_id=op_id)
        assert await stamp(found, child) == OTHER
        hold.set()
        op = await pending
        assert (op.state, op.id) == ("succeeded", op_id)
        (row,) = [row for row in await rows(found, child) if row.operation_id == op_id]
        assert row.delivered_at is not None
        assert row.accepted_at is not None
        await found.output(child, "Went on.", "msg_b")
        await turn_ends(found, child)
        await settle_turn(found)
        assert [p.ping_id for p in found.pinged] == [turns.ping_id(child, first), turns.ping_id(child, op_id)]
        answer = await result(found, parent, child)
        assert (answer["state"], answer["final_text"], answer["instruction_id"]) == ("idle", "Went on.", op_id)

    run(rig, body)


def test_a_child_suspended_while_its_instruction_waited_is_resumed_also_on_a_full_vm(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent, child, first = await full_child(found)
        await full_child(found, await found.parent(2))  # another running child of the task

        async def suspended_meanwhile() -> Any:
            # the instruction passes the pre-check (the child works) and waits; then the turn ends and the child is
            # suspended in one step, so the executor finds it suspended at the hand-over
            waiting = asyncio.ensure_future(instruct(found, parent, child))
            await until(lambda: _count(found, child, len(await_rows) + 1), "the instruction")
            async with env.pool.connection() as conn, conn.transaction():
                await conn.execute(
                    "UPDATE runner.subagent_instruction SET ended_at = now() WHERE operation_id = %s", (first,)
                )
                await conn.execute("UPDATE runner.session SET state = 'suspended' WHERE id = %s", (child,))
            found.controller.wake()
            return await waiting

        # no number of running children (ADR-075 addendum 2) and no refusal on a full VM (HOT-32: running work)
        limited(found, running=1)
        vm_full(found)
        creates_before = len([1 for name, _ in found.guest.calls if name == "create_session"])
        await_rows = await rows(found, child)
        resumed = await suspended_meanwhile()
        assert resumed.state == "succeeded"
        resumes = [value for name, value in found.guest.calls if name == "create_session"][creates_before:]
        assert [request["session_id"] for request in resumes] == [child]

    run(rig, body)


def test_a_turn_followed_at_once_by_the_next_instruction_is_still_pinged_once(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent, child, first = await full_child(found)
        await found.output(child, "First answer.", "msg_a")
        await turn_ends(found, child)
        # the parent instructs again within the settle time; the child already works on it
        op = await instruct(found, parent, child)
        assert op.state == "succeeded"
        await found.state(child, "prompt_accepted", prompt_id=op.id)
        await found.tick()
        assert [p.ping_id for p in found.pinged] == [turns.ping_id(child, first)]
        assert any(f"turn_ended child_session_id={child} instruction_id={first}" in line for line in found.lines)
        # once per daemon
        await found.tick()
        assert len(found.pinged) == 1
        # the newest turn is pinged as usual when it ends
        await turn_ends(found, child)
        await settle_turn(found)
        assert [p.ping_id for p in found.pinged] == [turns.ping_id(child, first), turns.ping_id(child, op.id)]
        # without live children nothing is asked
        turns_of = found.controller.turns
        await turns_of.ping_earlier([])
        # a turn that ended longer ago than the watch window is not pinged again (a restarted daemon)
        turns_of.turned.clear()
        async with env.pool.connection() as conn:
            await conn.execute(
                "UPDATE runner.subagent_instruction SET ended_at = ended_at - make_interval(secs => %s)"
                " WHERE operation_id = %s",
                (store.RECENT_S + 60, first),
            )
        await turns_of.ping_earlier([await found.stored(child)])
        assert len(found.pinged) == 2

    run(rig, body)


# -- older guests and children without instructions ------------------------------------------------------------------


def test_a_current_child_without_instructions_answers_and_pings_as_before(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent, child, _first = await full_child(found)
        async with env.pool.connection() as conn:
            await conn.execute("DELETE FROM runner.subagent_instruction WHERE child_session_id = %s", (child,))
        # a child older than the instructions: its acting user is the request's
        assert await stamp(found, child) == OWNER
        assert await result(found, parent, child) == {"session_id": child, "state": "working"}
        await turn_ends(found, child)
        await settle_turn(found)
        assert [p.ping_id for p in found.pinged] == [ping_id(child)]
        assert (await found.stored(child)).state == "idle"
        await found.state(child, "failed", reason="oom")
        await found.tick()
        assert [p.ping_id for p in found.pinged] == [ping_id(child)] * 2
        assert any(f"turn_ended child_session_id={child} instruction_id=-" in line for line in found.lines)

    run(rig, body)


def test_the_acting_user_of_a_child_turn(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent, child, first = await full_child(found)
        assert await stamp(found, child) == OWNER
        # a prompt ID that names no instruction of this child: unknown, never anybody's
        await found.state(child, "prompt_accepted", prompt_id=uuid7())
        assert await stamp(found, child) is None
        await found.state(child, "prompt_accepted", prompt_id=first)
        assert await stamp(found, child) == OWNER
        # a prompt ID that is no UUID (the guest sends UUIDs) changes no instruction and names nobody
        journal = SimpleNamespace(env=SimpleNamespace(wall=lambda: 0.0, context=SimpleNamespace()))
        async with found.env.pool.connection() as conn:
            body = {"state": "prompt_accepted", "prompt_id": "not-a-uuid"}
            await session_state.observe(journal, conn, await found.stored(child), body)
            await acting.instruction_accepted(conn, child, "not-a-uuid")
        assert await stamp(found, child) is None
        await found.state(child, "prompt_accepted", prompt_id=first)
        assert await stamp(found, child) == OWNER
        await turn_ends(found, child)
        assert await stamp(found, child) == OWNER
        # the instruction table rows of the child
        (request,) = await rows(found, child)
        assert (request.parent_session_id, request.actor_id) == (parent, OWNER)
        assert None not in (request.delivered_at, request.accepted_at, request.ended_at)
        assert parent != TASK

    run(rig, body)


def test_the_controller_forgets_children_it_no_longer_watches(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        turns_of = found.controller.turns
        turns_of.current_of["gone"] = True
        turns_of.lock("gone")
        turns_of.turned.update({"gone:x", "kept:x"})
        turns_of.seen["gone:x"] = 0.0
        turns_of.texts["kept:x"] = "t"
        turns_of.prune({"kept"})
        assert (turns_of.current_of, turns_of.locks, turns_of.turned) == ({}, {}, {"kept:x"})
        assert (turns_of.seen, turns_of.texts) == ({}, {"kept:x": "t"})
        # a new controller takes the device's idle time
        assert Subagents(found.ops.kit.session_env, found.service, idle_seconds=5).idle_seconds == 5

    run(rig, body)
