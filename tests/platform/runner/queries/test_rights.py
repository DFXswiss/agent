"""U-RUN-queries rights: the task owner's rights at call time from the verified log (ADR-024, 03-api.md section 8.1).

An owner who loses the workgroup access keeps the organization level and is `forbidden` on the workgroup level; a
removed owner reads nothing; a device that is no longer assigned to the session's workgroup or cannot act for the org
answers `unavailable`. The change takes effect at the next call, with no cache in between."""

from __future__ import annotations

from typing import Any

from agent_runner.orgs.book import book_for

from ..reconcile.rig import Rig
from .kit import BOB, ODEV, RDEV, TASK, TASK_ONE, WG, WG_ONE, RKit, run, task_row

WORKGROUP_READS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("list_members", {"scope": "workgroup"}),
    ("list_tasks", {}),
    ("get_task", {"task_id": TASK_ONE}),
    ("get_task_messages", {"task_id": TASK_ONE}),
    ("get_whiteboard", {"level": "workgroup"}),
    ("get_whiteboard", {"level": "task", "task_id": TASK_ONE}),
    ("get_context", {}),
    ("list_operations", {}),
    ("get_operation", {"operation_id": "01930007-0000-7000-8000-000000000001"}),
)
ORG_READS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("list_workgroups", {}),
    ("list_members", {"scope": "org"}),
    ("get_whiteboard", {"level": "org"}),
)


def test_an_owner_who_loses_the_workgroup_access_keeps_only_the_org_level(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        k.events.prompt(TASK_ONE, "Read the paper.", wg=WG_ONE, author="carol")
        await k.pull()
        k.hub.tasks = [task_row(TASK_ONE, WG_ONE, owner=BOB)]
        sid = await k.session(1, wg=WG_ONE, owner=BOB, task=TASK_ONE)
        messages = await k.ok(sid, "get_task_messages", {"task_id": TASK_ONE})
        assert [m["text"] for m in messages["messages"]] == ["Read the paper."]
        await k.extend(lambda log: log.add("ungrant", "alice", subject=BOB, wg=WG_ONE))
        for tool, arguments in WORKGROUP_READS:
            assert await k.error(sid, tool, arguments) == "forbidden", tool
        for tool, arguments in ORG_READS:
            assert (await k.read(sid, tool, arguments))["status"] == "ok", tool
        listed = (await k.ok(sid, "list_workgroups"))["workgroups"]
        assert {w["id"]: w["my_access"] for w in listed}[WG_ONE] is None
        # granted again: readable again at the next call
        await k.extend(lambda log: log.add("grant", "alice", subject=BOB, wg=WG_ONE, access="read"))
        assert (await k.read(sid, "get_task_messages", {"task_id": TASK_ONE}))["status"] == "ok"

    run(rig, body)


def test_a_removed_owner_reads_nothing(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        await k.pull()
        sid = await k.session(1, owner=BOB)
        assert (await k.read(sid, "list_workgroups"))["status"] == "ok"
        await k.extend(lambda log: log.add("remove", "alice", subject=BOB))
        for tool, arguments in (*ORG_READS, *WORKGROUP_READS):
            answer = await k.read(sid, tool, arguments)
            assert answer["error"] == {
                "error": "forbidden",
                "message": "The task owner is no longer a member of this organization.",
            }, tool

    run(rig, body)


def test_an_owner_that_is_no_user_reads_nothing(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        sid = await k.session(1, owner=ODEV)
        assert await k.error(sid, "list_workgroups") == "forbidden"

    run(rig, body)


def test_a_device_that_cannot_act_for_the_workgroup_answers_unavailable(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        one = await k.session(1, wg=WG_ONE, owner=BOB, task=TASK_ONE)
        default = await k.session(2)
        await k.extend(lambda log: log.add("device_unassign", "alice", device=RDEV, wg=WG_ONE))
        assert await k.error(one, "list_workgroups") == "unavailable"
        assert (await k.read(default, "list_workgroups"))["status"] == "ok"
        # the org copy stopped (rollback or fork): nothing is read for it
        book_for(k.env.context).stopped[k.events.spec.org] = "log_fork"
        answer = await k.read(default, "list_workgroups")
        assert answer["error"]["error"] == "unavailable"

    run(rig, body)


def test_the_task_owner_not_the_session_decides(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        await k.pull()
        # a session in the default workgroup owned by bob reads the default workgroup (every member writes there)
        sid = await k.session(1, wg=WG, owner=BOB, task=TASK)
        found = await k.ok(sid, "list_members", {"scope": "workgroup"})
        assert BOB in {m["id"] for m in found["members"]}

    run(rig, body)
