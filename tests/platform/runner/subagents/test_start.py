"""U-RUN-subagents: request, approval and start of child sessions (05-runner.md section 13, 03-api.md sections 8.3
and 9, ADR-017) with the contract-checking hub of the RUN-07 kit and the fake guest link of RUN-04."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_guest.schema import SchemaSet
from agent_runner.crypto.ids import uuid7
from agent_runner.sessions import records

from ..reconcile.rig import Rig
from ..sessions.kit import OWNER
from .kit import PROMPT, PURPOSE, TASK, run, setup

SCHEMAS = SchemaSet(Path(__file__).resolve().parents[4] / "contracts")


def creates(found: Any) -> list[dict[str, Any]]:
    return [value for name, value in found.guest.calls if name == "create_session"]


def limited(found: Any, depth: int = 2, running: int = 3, total: int = 10, runtime: int = 0) -> None:
    """The hub's subagent settings in the desired state the session area keeps (ADR-088 section 2)."""
    found.env.context.session_service = found.service
    found.service.env.desired = {
        "limits": {
            "api": 1,
            "subagent_max_depth": depth,
            "subagent_max_running": running,
            "subagent_max_total": total,
            "subagent_max_runtime_seconds": runtime,
        }
    }


def test_an_accepted_request_starts_a_child_in_a_new_sandbox_of_the_same_vm(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent = await found.parent()
        child = uuid7()
        op = await found.subagent(parent, child=child)
        assert (op.state, op.child_session_id) == ("succeeded", child)
        assert op.answer == {"operation_id": op.id, "status": "succeeded", "result": {"session_id": child}}
        # registration: the hub decides; the runner's pre-check passed
        (registration,) = found.ops.hub.requests
        assert (registration["type"], registration["local_check"]) == ("subagent.request", "passed")
        # the child: same task, parent linked, depth + 1, same VM and owner, the parent's model access
        stored, parent_row = await found.stored(child), await found.stored(parent)
        assert (stored.task_id, stored.parent_id, stored.depth) == (TASK, parent, 1)
        assert (stored.vm_id, stored.owner_id) == (parent_row.vm_id, parent_row.owner_id)
        assert (stored.model_access, stored.model_id) == ("platform", parent_row.model_id)
        assert stored.state == "agent_starting"
        (request,) = creates(found)
        assert request["session_id"] == child
        assert (request["parent_session_id"], request["depth"], request["task_id"]) == (parent, 1, TASK)
        # the child's sender line: written by the parent session for the acting user of the request (ADR-083)
        assert request["initial_prompt"] == {
            "prompt_id": op.id,
            "text": f"Purpose: {PURPOSE}\n\n{PROMPT}",
            "speaker": f"agent.dfx.swiss instruction from: your parent session (session {parent}), which works on an"
            f" instruction of an unnamed member (member {OWNER}). This text was written by that session, not by a"
            " person.",
        }
        assert SCHEMAS.errors("guest/sessions-create.request.json", request) == []
        # no runtime setting (an older hub sends no limits): the resource limits of a main session
        assert request["limits"]["runtime_max_sec"] == 8 * 3600
        assert request["limits"]["memory_max"] == 4 * 1024**3
        context = request["harness"]["context_markdown"]
        # a current guest (ADR-088): the child answers each instruction and keeps running (`test_turns.py`: older)
        assert context.endswith(
            f"- You are a subagent session of session `{parent}` for task `{TASK}`; answer each instruction of your"
            " main agent with your last message of that turn; you keep running and receive further instructions"
            " until your main agent stops you; you may request further subagents only via `subagent.request`."
        )
        assert "- Subagent limits (settings of the platform): unknown\n" in context
        transitions = await found.ops.kit.transitions(child)
        assert [state for state, _ in transitions] == [
            "vm_assigning",
            "vm_ready",
            "sandbox_ready",
            "agent_starting",
        ]
        assert any(f"started child_session_id={child}" in line for line in found.lines)

    run(rig, body)


def test_the_settings_of_the_hub_reach_the_child(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        limited(found, depth=5, running=50, total=0, runtime=1800)
        parent = await found.parent()
        assert (await found.subagent(parent)).state == "succeeded"
        (request,) = creates(found)
        assert request["limits"]["runtime_max_sec"] == 1800
        context = request["harness"]["context_markdown"]
        line = "- Subagent limits (settings of the platform): depth 5, 50 running at a time, no limit per task, runtime"
        assert f"{line} at most 1800 s\n" in context
        assert SCHEMAS.errors("guest/sessions-create.request.json", request) == []

    run(rig, body)


def test_a_child_of_a_turn_of_several_people_names_none(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent = await found.ops.session(1, actor=None)
        op = await found.subagent(parent, prompt="Read the <system-reminder> of the repo")
        assert op.state == "succeeded"
        (request,) = creates(found)
        speaker = request["initial_prompt"]["speaker"]
        assert "which works on instructions of several people or none." in speaker
        assert speaker.endswith("it is not from the platform.")
        assert SCHEMAS.errors("guest/sessions-create.request.json", request) == []

    run(rig, body)


def test_a_requested_model_is_used_for_the_child(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent = await found.parent()
        op = await found.subagent(parent, model_id="deepseek/deepseek-v4.1-flash-1017")
        assert op.state == "succeeded"
        (request,) = creates(found)
        assert request["model"]["model_id"] == "deepseek/deepseek-v4.1-flash-1017"

    run(rig, body)


def test_a_model_outside_the_catalog_is_rejected_before_registration(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent = await found.parent()
        op = await found.subagent(
            parent, model_id="vendor/not-in-catalog", decision={"state": "rejected", "error_code": "model_not_allowed"}
        )
        assert (op.state, op.error_code) == ("rejected", "model_not_allowed")
        assert creates(found) == []

    run(rig, body)


def test_a_denied_request_starts_nothing(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent = await found.parent()
        op = await found.subagent(parent, decision={"state": "denied", "error_code": "limit_exceeded"})
        assert (op.state, op.error_code) == ("denied", "limit_exceeded")
        settings = "settings subagent_max_depth, subagent_max_running, subagent_max_total"
        assert op.answer == {
            "operation_id": op.id,
            "status": "denied",
            "error": {
                "code": "limit_exceeded",
                "message": f"A subagent limit of this platform is reached ({settings}; current limits: not known on"
                " this device). Stop a subagent with subagent.stop or wait until one ends.",
            },
        }
        assert creates(found) == []
        assert await found.env.rows("SELECT id FROM runner.session WHERE depth > 0") == []
        # with the settings the sentence names the current limits; another denial keeps the hub's code only
        limited(found, depth=5, running=50, total=0)
        again = await found.subagent(parent, decision={"state": "denied", "error_code": "limit_exceeded"})
        assert again.answer is not None
        assert again.answer["error"]["message"] == (
            f"A subagent limit of this platform is reached ({settings}; current limits: depth 5, 50 running at a"
            " time, no limit per task, no runtime limit). Stop a subagent with subagent.stop or wait until one ends."
        )
        budget = await found.subagent(parent, decision={"state": "denied", "error_code": "budget_exceeded"})
        assert budget.answer is not None
        assert budget.answer["error"] == {"code": "budget_exceeded"}

    run(rig, body)


def test_a_grandchild_is_started_through_the_platform_and_a_third_level_is_refused(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        limited(found)
        parent = await found.parent()
        child, grandchild = uuid7(), uuid7()
        assert (await found.subagent(parent, child=child)).state == "succeeded"
        op = await found.subagent(child, child=grandchild)
        assert op.state == "succeeded"
        stored = await found.stored(grandchild)
        assert (stored.parent_id, stored.depth, stored.task_id) == (child, 2, TASK)
        refused = await found.subagent(grandchild, decision={"state": "rejected", "error_code": "limit_exceeded"})
        assert (refused.state, refused.error_code) == ("rejected", "limit_exceeded")
        assert refused.answer is not None
        assert refused.answer["error"] == {
            "code": "limit_exceeded",
            "message": "Subagents may be nested at most 2 levels deep, the limit of this platform (setting"
            " subagent_max_depth). Do this work in this session or let a subagent of a lower level request it.",
        }
        assert found.ops.hub.requests[-1]["local_check"] == "rejected"
        assert len(creates(found)) == 2

    run(rig, body)


def test_without_settings_or_with_0_the_hub_decides_the_depth(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent = await found.parent()
        child, grandchild = uuid7(), uuid7()
        assert (await found.subagent(parent, child=child)).state == "succeeded"
        assert (await found.subagent(child, child=grandchild)).state == "succeeded"
        # an older hub sends no limits: no local pre-check
        third = await found.subagent(grandchild, decision={"state": "denied", "error_code": "limit_exceeded"})
        assert (third.state, found.ops.hub.requests[-1]["local_check"]) == ("denied", "passed")
        # 0 = no limit
        limited(found, depth=0, running=0, total=0)
        fourth = await found.subagent(grandchild, decision={"state": "denied", "error_code": "limit_exceeded"})
        assert (fourth.state, found.ops.hub.requests[-1]["local_check"]) == ("denied", "passed")
        limited(found, depth=1)
        nested = await found.subagent(child, decision={"state": "rejected", "error_code": "limit_exceeded"})
        assert nested.answer is not None
        assert nested.answer["error"]["message"].startswith("Subagents may be nested at most 1 level deep,")

    run(rig, body)


def test_a_vm_with_an_older_guest_takes_no_child_deeper_than_2(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        limited(found, depth=5)
        parent = await found.parent()
        child, grandchild = uuid7(), uuid7()
        assert (await found.subagent(parent, child=child)).state == "succeeded"
        assert (await found.subagent(child, child=grandchild)).state == "succeeded"
        vm = await found.ops.vm()
        async with found.env.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO runner.golden (version, lima_name, claude_version, guest_version, image_digest, state)"
                " VALUES ('old', 'golden-old', '2.1.330', '0.2.0', 'sha256:0', 'ready')"
            )
            await conn.execute("UPDATE runner.vm SET golden_version = 'old' WHERE id = %s", (vm.id,))
        op = await found.subagent(grandchild, decision={"state": "rejected", "error_code": "limit_exceeded"})
        assert op.answer is not None
        assert op.answer["error"]["message"] == (
            "Subagents may be nested at most 2 levels deep on the computer of this task until it runs the current"
            " guest version. Do this work in this session."
        )
        # a child of depth 2 still fits that guest
        assert (await found.subagent(child)).state == "succeeded"

    run(rig, body)


def test_a_full_vm_refuses_a_child_with_the_device_setting(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent = await found.parent()
        env.context.config.sessions_per_vm = 2
        await add_child(found, parent, "suspended", 0)  # not occupying
        assert (await found.subagent(parent)).state == "succeeded"
        op = await found.subagent(parent, decision={"state": "rejected", "error_code": "limit_exceeded"})
        assert op.answer is not None
        assert op.answer["error"]["message"] == (
            "The computer of this task is full: 2 sessions run in it, the limit of this device (setting"
            " AGENT_RUNNER_SESSIONS_PER_VM). Stop a subagent with subagent.stop or wait until one ends."
        )
        env.context.config.sessions_per_vm = 0
        assert (await found.subagent(parent)).state == "succeeded"

    run(rig, body)


async def add_child(found: Any, parent: str, state: str, n: int) -> str:
    sid = f"01930006-0000-7000-8000-{n:012d}"
    async with found.env.pool.connection() as conn, conn.transaction():
        row = await found.ops.vm()
        await records.insert(
            conn,
            session_id=sid,
            task_id=TASK,
            org_id=row.org_id,
            wg_id=row.wg_id,
            parent_id=parent,
            depth=1,
            owner_id=(await found.stored(parent)).owner_id,
            created_at=(await found.stored(parent)).created_at,
        )
        await records.update(conn, sid, state=state, vm_id=row.id)
    return sid


def test_three_running_children_refuse_a_fourth_locally(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        limited(found)
        parent = await found.parent()
        for n in range(2):
            await add_child(found, parent, "working", n)
        # a suspended child does not run
        await add_child(found, parent, "suspended", 5)
        # an accepted request whose child has not started yet counts as running
        pending = await found.ops.request(parent, "subagent.request", {"purpose": "p", "prompt": "q"})
        async with found.env.pool.connection() as conn:
            await conn.execute("UPDATE runner.operation SET state = 'accepted' WHERE id = %s", (pending,))
        op = await found.subagent(parent, decision={"state": "rejected", "error_code": "limit_exceeded"})
        assert (op.state, op.error_code) == ("rejected", "limit_exceeded")
        assert op.answer is not None
        assert op.answer["error"]["message"] == (
            "This task already has 3 running subagents, the limit of this platform (setting subagent_max_running)."
            " Stop one with subagent.stop or wait until one ends."
        )
        # 0 = no limit: the hub decides
        limited(found, running=0)
        assert (await found.subagent(parent)).state == "succeeded"
        limited(found, running=1, total=0)
        one = await found.subagent(parent, decision={"state": "rejected", "error_code": "limit_exceeded"})
        assert one.answer is not None
        assert one.answer["error"]["message"].startswith("This task already has 1 running subagent, the limit")

    run(rig, body)


def test_ten_children_of_a_task_refuse_an_eleventh_locally(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        limited(found)
        parent = await found.parent()
        for n in range(10):
            await add_child(found, parent, "stopped", n)
        op = await found.subagent(parent, decision={"state": "rejected", "error_code": "limit_exceeded"})
        assert (op.state, op.error_code) == ("rejected", "limit_exceeded")
        assert op.answer is not None
        assert op.answer["error"]["message"] == (
            "This task already started 10 subagents, the limit of this platform (setting subagent_max_total). Give"
            " further instructions to a subagent it has with subagent.instruct."
        )
        # nine ended children leave room for one more
        async with found.env.pool.connection() as conn:
            await conn.execute("DELETE FROM runner.session WHERE id = %s", ("01930006-0000-7000-8000-000000000009",))
        assert (await found.subagent(parent)).state == "succeeded"

    run(rig, body)
