"""U-RUN-agent-context: `get_context` names the session (KOR-52, ADR-095 section 3, 03-api.md section 8.1): its kind,
handle and name, its main agent, its parent and its own children, from the task's `number` and the sessions'
`agent_path` of `GET /v1/device/tasks/{task}` and the verified `task.agent_name` / `task.meta` events pulled through
the real inbox. A hub of an older release (no number, no path) gives no `agent` and no `children`; values that break
the contract make the read unavailable (the task) or drop the row (a session). Every answer is checked against
`contracts/mcp/tools/get_context.output.json` by the kit."""

from __future__ import annotations

from typing import Any

from ..reconcile.rig import Rig
from .kit import TASK, RKit, run, session_row, task_row

TITLE = "Fix the login bug and add the tests"
AUTOMATIC = "Fix the login bug and"
LATER = "2026-11-21T12:45:00.000Z"


def numbered(number: object = 42) -> dict[str, Any]:
    row = task_row(TASK)
    return row if number is None else {**row, "number": number}


def agent_row(sid: str, *, depth: int = 0, parent: str | None = None, path: str | None = None) -> dict[str, Any]:
    return {**session_row(sid, TASK, depth=depth, parent=parent), "agent_path": path}


async def tree(k: RKit) -> tuple[str, str, str, str]:
    """A main session with two children and a grandchild; the hub lists the second child first."""
    main = await k.session(1)
    first = await k.session(2, depth=1, parent=main)
    second = await k.session(3, depth=1, parent=main)
    grandchild = await k.session(4, depth=2, parent=first)
    k.hub.tasks = [numbered()]
    k.hub.sessions[TASK] = [
        agent_row(main),
        agent_row(second, depth=1, parent=main, path="2"),
        agent_row(first, depth=1, parent=main, path="1"),
        agent_row(grandchild, depth=2, parent=first, path="1.1"),
    ]
    return main, first, second, grandchild


def test_get_context_names_the_main_agent_its_subagents_and_their_helpers(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        k.events.title(TASK, TITLE)
        await k.pull()
        main, first, second, grandchild = await tree(k)
        found = await k.ok(main, "get_context")
        assert found["agent"] == {
            "kind": "main",
            "handle": "#42",
            "name": AUTOMATIC,
            "name_source": "automatic",
            "main": {"handle": "#42", "name": AUTOMATIC},
            "parent": None,
        }
        assert found["children"] == [
            {"session_id": first, "handle": "#42.1", "state": "working"},
            {"session_id": second, "handle": "#42.2", "state": "working"},
        ]
        found = await k.ok(first, "get_context")
        assert (found["agent"]["kind"], found["agent"]["handle"]) == ("subagent", "#42.1")
        assert found["agent"]["parent"] == {"session_id": main, "handle": "#42"}
        assert found["agent"]["main"] == {"handle": "#42", "name": AUTOMATIC}
        assert found["children"] == [{"session_id": grandchild, "handle": "#42.1.1", "state": "working"}]
        found = await k.ok(grandchild, "get_context")
        assert found["agent"]["handle"] == "#42.1.1"
        assert found["agent"]["parent"] == {"session_id": first, "handle": "#42.1"}
        assert found["children"] == []

    run(rig, body)


def test_a_given_name_replaces_the_automatic_one_until_it_names_null(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        k.events.title(TASK, TITLE)
        k.events.add("alice", "task.agent_name", {"name": "Login doctor"}, task=TASK)
        await k.pull()
        main, first, _second, _grandchild = await tree(k)
        for sid in (main, first):
            agent = (await k.ok(sid, "get_context"))["agent"]
            assert (agent["name"], agent["name_source"], agent["main"]["name"]) == (
                "Login doctor",
                "given",
                "Login doctor",
            )
        # back to the automatic name: it follows the title again
        k.events.add("alice", "task.agent_name", {"name": None}, task=TASK)
        k.events.add("alice", "task.meta", {"title": "Review"}, task=TASK, ts=LATER)
        await k.pull()
        agent = (await k.ok(main, "get_context"))["agent"]
        assert (agent["name"], agent["name_source"]) == ("Review", "automatic")

    run(rig, body)


def test_without_a_title_the_automatic_name_is_agent(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        main = await k.session(1)
        k.hub.tasks = [numbered(7)]
        found = await k.ok(main, "get_context")
        assert (found["task"]["title"], found["agent"]["name"], found["agent"]["handle"]) == ("", "Agent", "#7")
        assert found["children"] == []

    run(rig, body)


def test_an_older_hub_without_handles_gives_no_agent(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        main = await k.session(1)
        child = await k.session(2, depth=1, parent=main)
        # no number at all: neither agent nor children
        k.hub.tasks = [numbered(None)]
        k.hub.sessions[TASK] = [session_row(main, TASK), session_row(child, TASK, depth=1, parent=main)]
        for sid in (main, child):
            found = await k.ok(sid, "get_context")
            assert "agent" not in found
            assert "children" not in found
        # a number but no paths: the main agent is named, a subagent cannot be, and children without a path are left out
        k.hub.tasks = [numbered()]
        found = await k.ok(main, "get_context")
        assert (found["agent"]["handle"], found["children"]) == ("#42", [])
        assert "agent" not in await k.ok(child, "get_context")

    run(rig, body)


def test_a_guest_before_the_names_gets_neither_agent_nor_children(rig: Rig) -> None:
    """A guest before 0.5.0 refuses an answer with fields its bundled contracts do not allow (HOT-23): the runner leaves
    `agent` and `children` out for it, everything else stays; a VM on a current golden gets them again."""

    async def body(k: RKit) -> None:
        k.events.title(TASK, TITLE)
        await k.pull()
        main, *_ = await tree(k)
        async with k.env.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO runner.golden (version, lima_name, claude_version, guest_version, image_digest, state)"
                " VALUES ('g-old', 'golden-old', '2.1.330', '0.4.0', 'sha256:x', 'ready')"
            )
            cursor = await conn.execute(
                "SELECT s.vm_id, v.golden_version FROM runner.session s JOIN runner.vm v ON v.id = s.vm_id"
                " WHERE s.id = %s",
                (main,),
            )
            row = await cursor.fetchone()
            assert row is not None
            vm_id, golden = row
            await conn.execute("UPDATE runner.vm SET golden_version = 'g-old' WHERE id = %s", (vm_id,))
        found = await k.ok(main, "get_context")
        assert "agent" not in found
        assert "children" not in found
        assert (found["task"]["id"], found["session"]["id"]) == (TASK, main)
        async with k.env.pool.connection() as conn:
            await conn.execute("UPDATE runner.vm SET golden_version = %s WHERE id = %s", (golden, vm_id))
        assert (await k.ok(main, "get_context"))["agent"]["handle"] == "#42"

    run(rig, body)


def test_handles_that_break_the_contract(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        main = await k.session(1)
        child = await k.session(2, depth=1, parent=main)
        path = f"/v1/device/tasks/{TASK}"
        for number in (0, True, "42", 2**53):
            k.hub.raw[path] = (200, {"task": numbered(number), "sessions": [], "operations": []})
            assert await k.error(main, "get_context") == "unavailable", number
        # a path that does not fit the session's depth, a malformed one or one on a main session drops that row
        for wrong in ("1.1", "0", "1.", 3):
            sessions = [agent_row(main, path=None), {**agent_row(child, depth=1, parent=main), "agent_path": wrong}]
            k.hub.raw[path] = (200, {"task": numbered(), "sessions": sessions, "operations": []})
            assert (await k.ok(main, "get_context"))["children"] == [], wrong
            assert "agent" not in await k.ok(child, "get_context")
        sessions = [agent_row(main, path="1"), agent_row(child, depth=1, parent=main, path="1")]
        k.hub.raw[path] = (200, {"task": numbered(), "sessions": sessions, "operations": []})
        assert [s["id"] for s in (await k.ok(main, "get_task", {"task_id": TASK}))["sessions"]] == [child]

    run(rig, body)
