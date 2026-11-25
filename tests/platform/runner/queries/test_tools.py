"""U-RUN-queries: every read tool against the fixture org (03-api.md section 8.1, ADR-089, 05-runner.md section 2.2):
two readable workgroups of the device and one it does not serve, several members, profiles, prompts, outputs and
whiteboards from verified pulled events, task metadata from the device endpoints. Every answer is checked against
`contracts/mcp/tools/<tool>.output.json` (or `mcp/tool-error.json`) by the kit."""

from __future__ import annotations

from typing import Any

import pytest

from agent_runner.queries import projection

from ..reconcile.rig import Rig, pid
from .kit import (
    ALICE,
    BOB,
    CAROL,
    ODEV,
    ORG,
    RDEV,
    TASK,
    TASK_B,
    TASK_ONE,
    TASK_TWO,
    WG,
    WG_ONE,
    WG_TWO,
    RKit,
    run,
    session_row,
    task_row,
)

E1, E2, E3, E4, E5 = (f"01930006-0000-7000-8000-00000000000{n}" for n in range(1, 6))


# -- list_workgroups -------------------------------------------------------------------------------------------------


def test_list_workgroups_names_every_workgroup_with_the_session_access(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        await k.pull()
        alice = await k.session(1)
        bob = await k.session(2, wg=WG_ONE, owner=BOB, task=TASK_ONE)
        await k.instruct(alice, ALICE)
        # alice writes everywhere; the device does not serve workgroup two, so the session cannot read it
        assert await k.ok(alice, "list_workgroups") == {
            "workgroups": [
                {"id": WG, "name": "General", "is_default": True, "my_access": "write"},
                {"id": WG_ONE, "name": "Research", "is_default": False, "my_access": "read"},
                {"id": WG_TWO, "name": "Secret Lab", "is_default": False, "my_access": None},
            ]
        }
        # bob's session: bob's explicit read on its own workgroup one, read on the default one, none on two
        await k.instruct(bob, BOB)
        found = (await k.ok(bob, "list_workgroups"))["workgroups"]
        assert [(w["id"], w["my_access"]) for w in found] == [(WG, "read"), (WG_ONE, "read"), (WG_TWO, None)]
        # an unknown instructing human: only the own workgroup
        await k.instruct(bob, None)
        found = (await k.ok(bob, "list_workgroups"))["workgroups"]
        assert [(w["id"], w["my_access"]) for w in found] == [(WG, None), (WG_ONE, "read"), (WG_TWO, None)]
        assert await k.error(alice, "list_workgroups", {"x": 1}) == "params_invalid"

    run(rig, body)


def test_list_workgroups_without_profiles_shows_ids_and_leaves_out_archived_ones(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        sid = await k.session(1)
        await k.extend(lambda log: log.add("wg_archive", "alice", wg=WG_TWO))
        found = (await k.ok(sid, "list_workgroups"))["workgroups"]
        assert [(w["id"], w["name"]) for w in found] == [(WG, WG), *sorted([(WG_ONE, WG_ONE)])]

    run(rig, body, profiles=False)


# -- list_members ----------------------------------------------------------------------------------------------------


def test_list_members_of_the_org_and_of_the_workgroup(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        await k.pull()
        alice = await k.session(1)
        bob = await k.session(2, wg=WG_ONE, owner=BOB, task=TASK_ONE)
        org = (await k.ok(alice, "list_members", {"scope": "org"}))["members"]
        assert org == [
            {"id": ALICE, "kind": "user", "display_name": "Alice", "role": "admin"},
            {"id": BOB, "kind": "user", "display_name": "Bob", "role": "member"},
            {"id": CAROL, "kind": "user", "display_name": "Carol", "role": "member"},
            *sorted(
                [
                    {"id": ODEV, "kind": "device", "display_name": ODEV, "role": "device"},
                    {"id": RDEV, "kind": "device", "display_name": "Runner", "role": "device"},
                ],
                key=lambda m: m["display_name"],
            ),
        ]
        # the default workgroup: every user writes; only rdev is assigned
        default = (await k.ok(alice, "list_members", {"scope": "workgroup"}))["members"]
        assert [(m["id"], m["access"]) for m in default] == [
            (ALICE, "write"),
            (BOB, "write"),
            (CAROL, "write"),
            (RDEV, "write"),
        ]
        one = (await k.ok(bob, "list_members", {"scope": "workgroup"}))["members"]
        assert {m["id"]: m["access"] for m in one} == {
            ALICE: "write",
            BOB: "read",
            CAROL: "write",
            ODEV: "write",
            RDEV: "write",
        }
        for wrong in ({}, {"scope": "wg"}, {"scope": "org", "x": 1}):
            assert await k.error(alice, "list_members", wrong) == "params_invalid"

    run(rig, body)


def test_list_members_leaves_out_removed_members(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        await k.pull()
        sid = await k.session(1)
        await k.extend(lambda log: log.add("remove", "alice", subject=CAROL))
        org = (await k.ok(sid, "list_members", {"scope": "org"}))["members"]
        assert CAROL not in {m["id"] for m in org}
        assert {ALICE, BOB} <= {m["id"] for m in org}

    run(rig, body)


def test_list_members_leaves_out_agents_which_act_as_their_users(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        await k.pull()
        sid = await k.session(1)
        await k.extend(lambda log: log.agent_add("alice", "bot", "alice", 1))
        for scope in ("org", "workgroup"):
            members = (await k.ok(sid, "list_members", {"scope": scope}))["members"]
            assert pid("bot") not in {m["id"] for m in members}
            assert ALICE in {m["id"] for m in members}

    run(rig, body)


def test_list_members_of_a_workgroup_leaves_out_users_without_access(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        sid = await k.session(1, wg=WG_ONE, owner=BOB, task=TASK_ONE)
        await k.extend(lambda log: log.add("ungrant", "alice", subject=CAROL, wg=WG_ONE))
        one = (await k.ok(sid, "list_members", {"scope": "workgroup"}))["members"]
        assert {m["id"] for m in one} == {ALICE, BOB, ODEV, RDEV}

    run(rig, body)


# -- list_tasks ------------------------------------------------------------------------------------------------------


def test_list_tasks_of_the_own_workgroup_with_titles_and_pages(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        k.events.title(TASK, "Fix the login")
        k.events.title(TASK_ONE, "Paper draft", wg=WG_ONE, author="carol")
        await k.pull()
        k.hub.tasks = [
            task_row(TASK_B, n=3, state="idle"),
            task_row(TASK_ONE, WG_ONE, owner=CAROL, n=2),
            task_row(TASK, n=1),
        ]
        sid = await k.session(1)
        listed = await k.ok(sid, "list_tasks")
        assert listed == {
            "tasks": [
                {
                    "id": TASK_B,
                    "title": "",
                    "state": "idle",
                    "owner_id": ALICE,
                    "created_at": "2026-11-21T12:03:00.000Z",
                    "last_activity_at": "2026-11-21T12:03:00.000Z",
                },
                {
                    "id": TASK,
                    "title": "Fix the login",
                    "state": "working",
                    "owner_id": ALICE,
                    "created_at": "2026-11-21T12:01:00.000Z",
                    "last_activity_at": "2026-11-21T12:01:00.000Z",
                },
            ],
            "next_before": None,
        }
        page = await k.ok(sid, "list_tasks", {"limit": 1})
        assert [t["id"] for t in page["tasks"]] == [TASK_B]
        assert page["next_before"] == TASK_B.replace("-", "_")
        rest = await k.ok(sid, "list_tasks", {"limit": 1, "before": page["next_before"]})
        assert [t["id"] for t in rest["tasks"]] == [TASK]
        assert [t["id"] for t in (await k.ok(sid, "list_tasks", {"state": "idle"}))["tasks"]] == [TASK_B]
        assert k.hub.queries[-1] == f"/v1/device/workgroups/{WG}/tasks?state=idle&limit=50"
        one = await k.session(2, wg=WG_ONE, owner=CAROL, task=TASK_ONE)
        assert [(t["id"], t["title"]) for t in (await k.ok(one, "list_tasks"))["tasks"]] == [(TASK_ONE, "Paper draft")]
        for wrong in ({"limit": 0}, {"limit": 101}, {"limit": True}, {"state": "open"}, {"before": "a b"}, {"x": 1}):
            assert await k.error(sid, "list_tasks", wrong) == "params_invalid"

    run(rig, body)


def test_list_tasks_drops_rows_of_other_workgroups_and_survives_a_bad_hub(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        sid = await k.session(1)
        path = f"/v1/device/workgroups/{WG}/tasks"
        foreign = task_row(TASK_TWO, WG_TWO)
        broken = {**task_row(TASK_B), "created_at": "yesterday"}
        k.hub.raw[path] = (200, {"tasks": [foreign, broken, task_row(TASK), "x"], "next_before": "a b"})
        listed = await k.ok(sid, "list_tasks")
        assert [t["id"] for t in listed["tasks"]] == [TASK]
        assert listed["next_before"] is None
        k.hub.raw[path] = (200, {"tasks": "none"})
        assert await k.ok(sid, "list_tasks") == {"tasks": [], "next_before": None}
        k.hub.raw[path] = (404, {"error": "not_found"})
        assert await k.ok(sid, "list_tasks") == {"tasks": [], "next_before": None}
        k.hub.raw[path] = (429, {"error": "rate_limited"})
        assert await k.error(sid, "list_tasks") == "unavailable"
        k.hub.unreachable = True
        assert await k.error(sid, "list_tasks") == "unavailable"

    run(rig, body)


# -- get_task --------------------------------------------------------------------------------------------------------


def test_get_task_with_sessions_and_operations_only_in_the_own_workgroup(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        k.events.title(TASK, "Fix the login")
        await k.pull()
        sid = await k.session(1)
        child = "01930005-0000-7000-8000-000000000077"
        op = "01930007-0000-7000-8000-000000000001"
        k.hub.tasks = [task_row(TASK, n=1), task_row(TASK_ONE, WG_ONE, owner=CAROL)]
        k.hub.sessions[TASK] = [session_row(sid, TASK), session_row(child, TASK, depth=1, parent=sid, state="stopped")]
        k.hub.operations[TASK] = [
            {"id": op, "type": "task.set_title", "state": "succeeded", "error_code": None, "result_event_id": None}
        ]
        assert await k.ok(sid, "get_task", {"task_id": TASK}) == {
            "id": TASK,
            "title": "Fix the login",
            "state": "working",
            "owner_id": ALICE,
            "created_at": "2026-11-21T12:01:00.000Z",
            "sessions": [
                {"id": sid, "state": "working", "depth": 0, "parent_session_id": None},
                {"id": child, "state": "stopped", "depth": 1, "parent_session_id": sid},
            ],
            "operations": [{"id": op, "type": "task.set_title", "state": "succeeded"}],
        }
        # a task of the other workgroup this device serves, and an unknown one: not found
        assert await k.error(sid, "get_task", {"task_id": TASK_ONE}) == "not_found"
        assert await k.error(sid, "get_task", {"task_id": TASK_TWO}) == "not_found"
        for wrong in ({}, {"task_id": "x"}, {"task_id": TASK, "x": 1}):
            assert await k.error(sid, "get_task", wrong) == "params_invalid"

    run(rig, body)


def test_get_task_refuses_invalid_hub_answers(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        sid = await k.session(1)
        path = f"/v1/device/tasks/{TASK}"
        bad_session = {**session_row(sid, TASK), "depth": -1}  # any depth >= 0 is valid (ADR-088 section 2)
        bad_op = {"id": "x", "type": "t", "state": "succeeded"}
        k.hub.raw[path] = (200, {"task": task_row(TASK), "sessions": [bad_session, "x"], "operations": [bad_op, 1]})
        found = await k.ok(sid, "get_task", {"task_id": TASK})
        assert (found["sessions"], found["operations"]) == ([], [])
        k.hub.raw[path] = (200, {"task": task_row(TASK), "sessions": None, "operations": None})
        assert (await k.ok(sid, "get_task", {"task_id": TASK}))["sessions"] == []
        for answer in ({"task": task_row(TASK_B)}, {"task": {**task_row(TASK), "state": "x"}}, {"task": None}):
            k.hub.raw[path] = (200, answer)
            assert await k.error(sid, "get_task", {"task_id": TASK}) == "unavailable"
        k.hub.raw[path] = (200, {"task": {**task_row(TASK), "org_id": WG}})
        assert await k.error(sid, "get_task", {"task_id": TASK}) == "unavailable"
        k.hub.raw[path] = (403, {"error": "forbidden"})
        assert await k.error(sid, "get_task", {"task_id": TASK}) == "not_found"
        k.hub.unreachable = True
        assert await k.error(sid, "get_task", {"task_id": TASK}) == "unavailable"

    run(rig, body)


# -- get_task_messages -----------------------------------------------------------------------------------------------


def test_get_task_messages_orders_prompts_and_outputs_and_pages(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        k.hub.tasks = [task_row(TASK)]
        sid = await k.session(1)
        k.events.title(TASK, "Fix the login")  # seq 1, not a chat item
        k.events.prompt(TASK, "Please fix the login.")  # 2
        k.events.output(TASK, "Looking at it.", session=sid)  # 3
        k.events.prompt(TASK_B, "Another task.")  # 4
        k.events.prompt(TASK, "Thanks, ünïcode ok?", author="carol")  # 5
        k.events.output(TASK, "Done.", session=sid)  # 6
        k.events.prompt(TASK_ONE, "Other workgroup.", wg=WG_ONE)
        await k.pull()
        result = await k.ok(sid, "get_task_messages", {"task_id": TASK})
        assert result == {
            "messages": [
                {
                    "seq": 2,
                    "kind": "prompt",
                    "author_id": ALICE,
                    "ts": "2026-11-21T12:30:00.000Z",
                    "text": "Please fix the login.",
                },
                {
                    "seq": 3,
                    "kind": "output",
                    "author_id": RDEV,
                    "ts": "2026-11-21T12:30:00.000Z",
                    "text": "Looking at it.",
                },
                {
                    "seq": 5,
                    "kind": "prompt",
                    "author_id": CAROL,
                    "ts": "2026-11-21T12:30:00.000Z",
                    "text": "Thanks, ünïcode ok?",
                },
                {"seq": 6, "kind": "output", "author_id": RDEV, "ts": "2026-11-21T12:30:00.000Z", "text": "Done."},
            ],
            "next_after": None,
        }
        page = await k.ok(sid, "get_task_messages", {"task_id": TASK, "limit": 2})
        assert ([m["seq"] for m in page["messages"]], page["next_after"]) == ([2, 3], 3)
        rest = await k.ok(sid, "get_task_messages", {"task_id": TASK, "after": 3, "limit": 2})
        assert ([m["seq"] for m in rest["messages"]], rest["next_after"]) == ([5, 6], None)
        # a task of another workgroup: its chat is not readable (unknown here, the hub has it in WG_ONE)
        k.hub.tasks = [task_row(TASK), task_row(TASK_ONE, WG_ONE)]
        assert await k.error(sid, "get_task_messages", {"task_id": TASK_ONE}) == "not_found"
        for wrong in ({}, {"task_id": TASK, "after": -1}, {"task_id": TASK, "limit": 201}, {"task_id": TASK, "y": 1}):
            assert await k.error(sid, "get_task_messages", wrong) == "params_invalid"

    run(rig, body)


def test_get_task_messages_leaves_out_prompts_this_device_withdrew(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        k.hub.tasks = [task_row(TASK)]
        sid = await k.session(1)
        k.events.prompt(TASK, "First.")  # 1
        withdrawn = k.events.prompt(TASK, "Never mind.")  # 2
        k.events.prompt(TASK, "Second.")  # 3
        await k.pull()
        async with k.env.pool.connection() as conn:
            await conn.execute("INSERT INTO runner.prompt_withdrawal (event_id) VALUES (%s)", (withdrawn,))
        result = await k.ok(sid, "get_task_messages", {"task_id": TASK})
        assert [m["text"] for m in result["messages"]] == ["First.", "Second."]
        page = await k.ok(sid, "get_task_messages", {"task_id": TASK, "limit": 1})
        assert ([m["seq"] for m in page["messages"]], page["next_after"]) == ([1], 1)
        rest = await k.ok(sid, "get_task_messages", {"task_id": TASK, "after": 1, "limit": 1})
        assert ([m["seq"] for m in rest["messages"]], rest["next_after"]) == ([3], None)

    run(rig, body)


def test_get_task_messages_of_a_task_known_only_to_the_hub_is_empty(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        sid = await k.session(1)
        k.hub.tasks = [task_row(TASK_B)]
        assert await k.ok(sid, "get_task_messages", {"task_id": TASK_B}) == {"messages": [], "next_after": None}
        assert await k.error(sid, "get_task_messages", {"task_id": TASK_TWO}) == "not_found"

    run(rig, body)


def test_get_task_messages_stays_within_the_answer_budget(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        k.hub.tasks = [task_row(TASK)]
        sid = await k.session(1)
        big = "\U0001f600" * 60_000  # 720 kB as escaped JSON: two do not fit into one answer
        k.events.prompt(TASK, "short")
        k.events.output(TASK, big, session=sid)
        k.events.output(TASK, big, session=sid)
        k.events.output(TASK, "after", session=sid)
        await k.pull()
        first = await k.ok(sid, "get_task_messages", {"task_id": TASK})
        assert ([m["seq"] for m in first["messages"]], first["next_after"]) == ([1, 2], 2)
        assert first["messages"][1]["text"] == big
        rest = await k.ok(sid, "get_task_messages", {"task_id": TASK, "after": 2})
        assert ([m["seq"] for m in rest["messages"]], rest["next_after"]) == ([3, 4], None)

    run(rig, body)


# -- get_whiteboard --------------------------------------------------------------------------------------------------


def _texts(result: Any) -> list[tuple[str, str]]:
    return [(e["entry_id"], e["text"]) for e in result["entries"]]


def test_get_whiteboard_on_every_level(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        k.hub.tasks = [task_row(TASK)]
        sid = await k.session(1)
        ev = k.events
        ev.board("whiteboard.entry", E1, {"level": "org", "text": "Org rules"}, level="org")
        ev.board("whiteboard.entry", E2, {"level": "org", "text": "Holiday plan"}, level="org")
        ev.board("whiteboard.edit", E1, {"text": "Org rules v2", "base_event_id": E1}, level="org")
        ev.board("whiteboard.delete", E2, {"base_event_id": E2}, level="org")
        ev.board("whiteboard.edit", E2, {"text": "too late", "base_event_id": E2}, level="org")
        ev.board("whiteboard.entry", E1, {"level": "org", "text": "second entry with the same ID"}, level="org")
        ev.board("whiteboard.entry", E3, {"level": "workgroup", "text": "WG notes"}, level="workgroup", author="bob")
        ev.board("whiteboard.entry", E4, {"level": "task", "text": "Task notes"}, level="task", task=TASK)
        # an edit of a task entry on the workgroup level does not apply
        ev.board("whiteboard.edit", E4, {"text": "hijack", "base_event_id": E4}, level="workgroup")
        ev.board(
            "whiteboard.edit",
            "01930006-0000-7000-8000-000000000099",
            {"text": "x", "base_event_id": E4},
            level="workgroup",
        )
        ev.board(
            "whiteboard.entry",
            E5,
            {"level": "workgroup", "text": "Research only"},
            level="workgroup",
            wg=WG_ONE,
            author="carol",
        )
        # an entry ID already used on the org level is not projected on another level (the hub refuses it)
        ev.board("whiteboard.entry", E1, {"level": "workgroup", "text": "reused ID"}, level="workgroup")
        await k.pull()
        org = await k.ok(sid, "get_whiteboard", {"level": "org"})
        assert _texts(org) == [(E1, "Org rules v2")]
        entry = org["entries"][0]
        assert (entry["author_id"], entry["created_at"], entry["updated_at"]) == (
            ALICE,
            "2026-11-21T12:31:00.000Z",
            "2026-11-21T12:31:00.000Z",
        )
        assert _texts(await k.ok(sid, "get_whiteboard", {"level": "workgroup"})) == [(E3, "WG notes")]
        assert _texts(await k.ok(sid, "get_whiteboard", {"level": "task", "task_id": TASK})) == [(E4, "Task notes")]
        one = await k.session(2, wg=WG_ONE, owner=CAROL, task=TASK_ONE)
        assert _texts(await k.ok(one, "get_whiteboard", {"level": "workgroup"})) == [(E5, "Research only")]
        for wrong in (
            {"level": "task"},
            {"level": "org", "task_id": TASK},
            {"level": "workgroup", "task_id": TASK},
            {"level": "board"},
            {"level": "task", "task_id": "x"},
            {},
        ):
            assert await k.error(sid, "get_whiteboard", wrong) == "params_invalid"

    run(rig, body)


def test_get_whiteboard_of_a_foreign_task_is_not_found(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        sid = await k.session(1)
        k.events.board(
            "whiteboard.entry", E4, {"level": "task", "text": "Research task"}, level="task", wg=WG_ONE, task=TASK_ONE
        )
        await k.pull()
        k.hub.tasks = [task_row(TASK_ONE, WG_ONE), task_row(TASK_B)]
        assert await k.error(sid, "get_whiteboard", {"level": "task", "task_id": TASK_ONE}) == "not_found"
        assert await k.ok(sid, "get_whiteboard", {"level": "task", "task_id": TASK_B}) == {"entries": []}

    run(rig, body)


def test_get_whiteboard_newest_first_within_the_budget(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        sid = await k.session(1)
        ids = [f"01930006-0000-7000-8000-0000000001{n:02d}" for n in range(16)]
        for n, entry in enumerate(ids):
            k.events.board(
                "whiteboard.entry", entry, {"level": "workgroup", "text": f"{n}:" + "x" * 60_000}, level="workgroup"
            )
        await k.pull()
        found = (await k.ok(sid, "get_whiteboard", {"level": "workgroup"}))["entries"]
        assert [e["entry_id"] for e in found] == ids[::-1][: len(found)]
        assert 10 <= len(found) < 16

    run(rig, body)


# -- get_context -----------------------------------------------------------------------------------------------------


def test_get_context_of_a_session(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        k.events.title(TASK, "Fix the login")
        await k.pull()
        sid = await k.session(1)
        child = await k.session(2, depth=1, parent=sid)
        k.hub.tasks = [task_row(TASK, state="idle")]
        assert await k.ok(child, "get_context") == {
            "org": {"id": ORG, "name": "Acme"},
            "workgroup": {"id": WG, "name": "General", "is_default": True},
            "task": {
                "id": TASK,
                "title": "Fix the login",
                "state": "idle",
                "owner": {"id": ALICE, "display_name": "Alice"},
            },
            "session": {"id": child, "parent_session_id": sid, "depth": 1, "model_id": "deepseek/x-1"},
            "rights": {"workgroup": "write"},
            # an older hub sends no limits: unknown
            "limits": {
                "subagent_max_depth": None,
                "subagent_max_parallel": None,
                "subagent_max_total": None,
                "subagent_max_runtime_seconds": None,
            },
            "api_version": 1,
        }
        assert await k.error(sid, "get_context", {"x": 1}) == "params_invalid"
        k.hub.tasks = []
        assert await k.error(sid, "get_context") == "unavailable"

    run(rig, body)


def test_get_context_shows_the_settings_and_clamps_them_for_an_older_guest(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        sid = await k.session(1)
        k.hub.tasks = [task_row(TASK, state="idle")]
        k.env.context.session_service = k.skit.service
        settings = {"subagent_max_depth": 5, "subagent_max_running": 50, "subagent_max_total": 0}
        k.skit.service.env.desired = {"limits": {"api": 1, **settings, "subagent_max_runtime_seconds": 0}}
        assert (await k.ok(sid, "get_context"))["limits"] == {
            "subagent_max_depth": 5,
            "subagent_max_parallel": 50,
            "subagent_max_total": 0,
            "subagent_max_runtime_seconds": 0,
        }
        # a guest before ADR-088 validates against its former contract: within its range, 0 is its maximum
        vm = (await k.skit.stored(sid)).vm_id
        async with k.env.pool.connection() as conn:
            await conn.execute("UPDATE runner.golden SET guest_version = '0.2.0'")
        assert vm is not None
        assert (await k.ok(sid, "get_context"))["limits"] == {
            "subagent_max_depth": 2,
            "subagent_max_parallel": 3,
            "subagent_max_total": 10,
        }
        k.skit.service.env.desired = {
            "limits": {"api": 1, **settings, "subagent_max_depth": 1, "subagent_max_runtime_seconds": 60}
        }
        assert (await k.ok(sid, "get_context"))["limits"] == {
            "subagent_max_depth": 1,
            "subagent_max_parallel": 3,
            "subagent_max_total": 10,
        }

    run(rig, body)


def test_get_context_without_profiles_shows_ids(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        sid = await k.session(1, wg=WG_ONE, owner=BOB, task=TASK_ONE)
        k.hub.tasks = [task_row(TASK_ONE, WG_ONE, owner=BOB)]
        found = await k.ok(sid, "get_context")
        assert (found["org"]["name"], found["workgroup"]["name"], found["task"]["title"]) == (ORG, WG_ONE, "")
        assert found["task"]["owner"] == {"id": BOB, "display_name": BOB}
        assert (found["workgroup"]["is_default"], found["rights"]) == (False, {"workgroup": "read"})

    run(rig, body, profiles=False)


# -- limits of the contract and of the answer ------------------------------------------------------------------------


def test_texts_and_lists_are_cut_to_the_contract_limits(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    async def body(k: RKit) -> None:
        sid = await k.session(1)
        k.events.board("whiteboard.entry", E1, {"level": "workgroup", "text": "y" * 70_000}, level="workgroup")
        await k.pull()
        entry = (await k.ok(sid, "get_whiteboard", {"level": "workgroup"}))["entries"][0]
        assert entry["text"] == "y" * projection.MAX_ENTRY_TEXT
        # a hub that lists more sessions and operations than its contract allows
        path = f"/v1/device/tasks/{TASK}"
        sessions = [session_row(f"01930005-0000-7000-8000-{n:012d}", TASK) for n in range(101)]
        ops = [
            {"id": f"01930007-0000-7000-8000-{n:012d}", "type": "task.set_title", "state": "succeeded"}
            for n in range(1001)
        ]
        k.hub.raw[path] = (200, {"task": task_row(TASK), "sessions": sessions, "operations": ops})
        found = await k.ok(sid, "get_task", {"task_id": TASK})
        assert (len(found["sessions"]), len(found["operations"])) == (100, 1000)
        # more task rows than asked for: cut, and no cursor into the unreachable rest
        k.hub.raw[f"/v1/device/workgroups/{WG}/tasks"] = (
            200,
            {"tasks": [task_row(TASK), task_row(TASK_B)], "next_before": "abc"},
        )
        assert await k.ok(sid, "list_tasks", {"limit": 1}) == {
            "tasks": [
                {
                    "id": TASK,
                    "title": "",
                    "state": "working",
                    "owner_id": ALICE,
                    "created_at": "2026-11-21T12:00:00.000Z",
                    "last_activity_at": "2026-11-21T12:00:00.000Z",
                }
            ],
            "next_before": None,
        }
        # members beyond the answer budget are left out
        monkeypatch.setattr(projection, "BUDGET", 250)
        members = (await k.ok(sid, "list_members", {"scope": "org"}))["members"]
        assert 0 < len(members) < 5
        assert members[0]["id"] == ALICE

    run(rig, body)


def test_arguments_of_the_wrong_type_are_params_invalid(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        sid = await k.session(1)
        assert await k.error(sid, "list_members", {"scope": ["org"]}) == "params_invalid"
        assert await k.error(sid, "get_whiteboard", {"level": {"org": 1}}) == "params_invalid"
        assert await k.error(sid, "list_tasks", {"state": ["idle"]}) == "params_invalid"

    run(rig, body)


def test_a_read_only_owner_reads_workgroup_and_task_whiteboards(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        sid = await k.session(1, wg=WG_ONE, owner=BOB, task=TASK_ONE)
        k.hub.tasks = [task_row(TASK_ONE, WG_ONE, owner=BOB)]
        ev = k.events
        ev.board("whiteboard.entry", E1, {"level": "workgroup", "text": "R"}, level="workgroup", wg=WG_ONE)
        ev.board("whiteboard.entry", E2, {"level": "task", "text": "T"}, level="task", wg=WG_ONE, task=TASK_ONE)
        await k.pull()
        assert _texts(await k.ok(sid, "get_whiteboard", {"level": "workgroup"})) == [(E1, "R")]
        assert _texts(await k.ok(sid, "get_whiteboard", {"level": "task", "task_id": TASK_ONE})) == [(E2, "T")]

    run(rig, body)
