"""U-RUN-queries across workgroups (ADR-089, key model section 8, 03-api.md section 8.1 "Read scope").

A session reads another workgroup W when its task owner and the instructing human of the read's turn both may read W
(explicit access or an admin's inherited read; archived W included) and this device is assigned to W. The instructing
human is the one captured when the read arrived; a later prompt of another person does not lend its rights to it.

- `rights.decide` is a pure function: its table of cases (owner, instructing human, admin inheritance, a removed or
  downgraded human, an unknown human, a device not assigned, an archived or unknown workgroup) runs without a store;
- the rig suites read through the real path (journal record -> handler -> tool -> audit) in the fixture org: the
  runner device serves the default workgroup and workgroup one, not workgroup two (`kit.py`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from agent_runner.crypto.ids import uuid7
from agent_runner.crypto.log.state import ADMIN, DEVICE, MEMBER, USER, OrgState, Principal, Workgroup
from agent_runner.queries.rights import (
    NO_ACCESS,
    NO_WORKGROUP,
    UNKNOWN_HUMAN,
    NotServed,
    ReadRefused,
    decide,
)
from agent_runner.subagents import instructions
from agent_runner.subagents.extensions import QueryCall, QueryOutcome

from ..reconcile.rig import Rig, pid
from .kit import ALICE, BOB, CAROL, TASK, TASK_ONE, TASK_TWO, WG, WG_ONE, WG_TWO, RKit, run, task_row

# -- the decision table ----------------------------------------------------------------------------------------------

OWNER, HUMAN, ADMIN_USER, OUTSIDER, GONE = (pid(f"decide/{n}") for n in ("owner", "human", "admin", "out", "gone"))
DEV = pid("decide/device")
OWN, OTHER, UNSERVED, ARCHIVED, DEFAULT = (pid(f"decide/wg/{n}") for n in ("own", "w", "unserved", "arch", "def"))
NOWHERE = pid("decide/wg/none")


def _principal(principal: str, kind: str, role: str, *, removed: int | None = None) -> Principal:
    return Principal(id=principal, kind=kind, role=role, kem="k", sig="s", joined_seq=0, removed_seq=removed)


def decision_state() -> OrgState:
    """The org of the table: the owner reads every workgroup but the archived one explicitly, the human two of them;
    the admin has no explicit access beyond its own workgroup; the device serves all but `UNSERVED`."""
    principals = {
        OWNER: _principal(OWNER, USER, MEMBER),
        HUMAN: _principal(HUMAN, USER, MEMBER),
        ADMIN_USER: _principal(ADMIN_USER, USER, ADMIN),
        OUTSIDER: _principal(OUTSIDER, USER, MEMBER),
        GONE: _principal(GONE, USER, MEMBER, removed=9),
        DEV: _principal(DEV, DEVICE, DEVICE),
    }
    workgroups = {
        wg: Workgroup(id=wg, default=wg == DEFAULT, created_seq=1, archived_seq=5 if wg == ARCHIVED else None)
        for wg in (OWN, OTHER, UNSERVED, ARCHIVED, DEFAULT)
    }
    access = {
        (OWNER, OWN): "write",
        (OWNER, OTHER): "read",
        (OWNER, UNSERVED): "read",
        (OWNER, ARCHIVED): "read",
        (HUMAN, OWN): "write",
        (HUMAN, OTHER): "write",
        (HUMAN, UNSERVED): "read",
        (HUMAN, ARCHIVED): "read",
        (OUTSIDER, OWN): "write",
        (ADMIN_USER, OWN): "write",
        (GONE, OTHER): "write",
    }
    assignments = {(DEV, wg): 1 for wg in (OWN, OTHER, ARCHIVED, DEFAULT)}
    return OrgState(
        org=pid("decide/org"),
        founder="f",
        default_wg=DEFAULT,
        private=True,
        seq=10,
        head="h",
        principals=principals,
        invites={},
        workgroups=workgroups,
        access=access,
        assignments=assignments,
        epochs={},
        epoch_authors={},
    )


# (case, owner, instructing human, workgroup, own workgroup, expected access or refusal)
DECISIONS: tuple[tuple[str, str, str | None, str, str, str | ReadRefused], ...] = (
    ("own workgroup: the owner's explicit access", OWNER, HUMAN, OWN, OWN, "write"),
    ("own workgroup with an unknown human", OWNER, None, OWN, OWN, "write"),
    ("own workgroup read by an admin by inheritance only", ADMIN_USER, HUMAN, OTHER, OTHER, NO_ACCESS),
    ("owner and human both read", OWNER, HUMAN, OTHER, OWN, "read"),
    ("the human is the owner", OWNER, OWNER, OTHER, OWN, "read"),
    ("the default workgroup: every user writes", OWNER, OUTSIDER, DEFAULT, OWN, "read"),
    ("the human may not read: B through A's session", OWNER, OUTSIDER, OTHER, OWN, NO_WORKGROUP),
    ("the owner may not read", OUTSIDER, HUMAN, OTHER, OWN, NO_WORKGROUP),
    ("an admin human reads by inheritance", OWNER, ADMIN_USER, OTHER, OWN, "read"),
    ("an admin owner reads by inheritance", ADMIN_USER, HUMAN, OTHER, OWN, "read"),
    ("a removed human", OWNER, GONE, OTHER, OWN, NO_WORKGROUP),
    ("an unknown human", OWNER, None, OTHER, OWN, UNKNOWN_HUMAN),
    ("an unknown human and no such workgroup: the same answer", OWNER, None, NOWHERE, OWN, UNKNOWN_HUMAN),
    ("no such workgroup", OWNER, HUMAN, NOWHERE, OWN, NO_WORKGROUP),
    ("no such workgroup for an admin", ADMIN_USER, ADMIN_USER, NOWHERE, OWN, NO_WORKGROUP),
    ("readable but not served by the device", OWNER, HUMAN, UNSERVED, OWN, NotServed(UNSERVED)),
    ("an archived workgroup stays readable", OWNER, HUMAN, ARCHIVED, OWN, "read"),
)


@pytest.mark.parametrize(
    ("owner", "human", "wg", "own", "expected"), [case[1:] for case in DECISIONS], ids=[case[0] for case in DECISIONS]
)
def test_the_decision_table(owner: str, human: str | None, wg: str, own: str, expected: str | ReadRefused) -> None:
    state = decision_state()
    if isinstance(expected, str):
        assert decide(state, device_id=DEV, own_wg=own, owner_id=owner, actor_id=human, wg_id=wg) == expected
        return
    with pytest.raises(ReadRefused) as caught:
        decide(state, device_id=DEV, own_wg=own, owner_id=owner, actor_id=human, wg_id=wg)
    refused = caught.value
    assert (type(refused), refused.error, refused.message, refused.wg_id) == (
        type(expected),
        expected.error,
        expected.message,
        wg,
    )


def test_a_workgroup_the_device_does_not_serve_is_named_with_the_remedy() -> None:
    refused = NotServed(UNSERVED, "Secret Lab")
    assert (refused.error, refused.wg_id) == ("unavailable", UNSERVED)
    assert refused.message == (
        "This execution device does not serve workgroup Secret Lab; an admin of the organization can assign the device"
        " to that workgroup or to the whole organization."
    )
    assert NotServed(UNSERVED).message.startswith(f"This execution device does not serve workgroup {UNSERVED};")


# -- reads through the session ---------------------------------------------------------------------------------------

E1, E2 = "01930006-0000-7000-8000-000000000101", "01930006-0000-7000-8000-000000000102"
HUMAN_GONE_TEXT = "The person whose instruction this session works on is no longer a member of this workgroup."
UNKNOWN_TEXT = (
    "This turn has instructions of several people (or none), so only this workgroup can be read. Ask one person to"
    " give the instruction."
)


def research(k: RKit) -> None:
    """Content of workgroup one (title, chat, whiteboards) and of the default workgroup."""
    k.events.title(TASK_ONE, "Read the paper", wg=WG_ONE, author="carol")
    k.events.prompt(TASK_ONE, "Summarise the paper.", wg=WG_ONE, author="carol")
    k.events.board("whiteboard.entry", E1, {"level": "workgroup", "text": "Lab notes"}, level="workgroup", wg=WG_ONE)
    k.events.board(
        "whiteboard.entry", E2, {"level": "task", "text": "Paper notes"}, level="task", wg=WG_ONE, task=TASK_ONE
    )
    k.events.prompt(TASK, "Fix the login.")
    k.hub.tasks = [task_row(TASK_ONE, WG_ONE, owner=CAROL, n=2), task_row(TASK, n=1)]


async def audit_of(k: RKit, index: int = -1) -> tuple[str, Mapping[str, Any]]:
    audit = (await k.read_audits())[index]
    return audit["result"], audit["detail"]


def test_a_session_reads_a_workgroup_its_owner_and_its_human_may_read(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        research(k)
        await k.pull()
        sid = await k.session(1)
        await k.instruct(sid, BOB)
        listed = await k.ok(sid, "list_tasks", {"workgroup_id": WG_ONE})
        assert [(t["id"], t["title"], t["owner_id"]) for t in listed["tasks"]] == [(TASK_ONE, "Read the paper", CAROL)]
        assert await audit_of(k) == ("ok", {"result_count": 1, "read_wg_id": WG_ONE, "triggered_by": BOB})
        assert (await k.ok(sid, "list_tasks", {"workgroup_id": WG}))["tasks"][0]["id"] == TASK
        task = await k.ok(sid, "get_task", {"task_id": TASK_ONE})
        assert (task["id"], task["title"]) == (TASK_ONE, "Read the paper")
        assert await audit_of(k) == ("ok", {"result_count": 1, "read_wg_id": WG_ONE, "triggered_by": BOB})
        messages = await k.ok(sid, "get_task_messages", {"task_id": TASK_ONE})
        assert [m["text"] for m in messages["messages"]] == ["Summarise the paper."]
        board = await k.ok(sid, "get_whiteboard", {"level": "workgroup", "workgroup_id": WG_ONE})
        assert [e["text"] for e in board["entries"]] == ["Lab notes"]
        assert await audit_of(k) == ("ok", {"result_count": 1, "read_wg_id": WG_ONE, "triggered_by": BOB})
        task_board = await k.ok(sid, "get_whiteboard", {"level": "task", "task_id": TASK_ONE})
        assert [e["text"] for e in task_board["entries"]] == ["Paper notes"]
        own_board = await k.ok(sid, "get_whiteboard", {"level": "workgroup"})
        assert own_board == {"entries": []}
        assert await audit_of(k) == ("ok", {"result_count": 0, "read_wg_id": WG, "triggered_by": BOB})
        members = await k.ok(sid, "list_members", {"scope": "workgroup", "workgroup_id": WG_ONE})
        users = {m["id"]: m["access"] for m in members["members"] if m["kind"] == "user"}
        assert users == {ALICE: "write", BOB: "read", CAROL: "write"}
        assert {m["kind"] for m in members["members"]} == {"user", "device"}
        assert await audit_of(k) == (
            "ok",
            {"result_count": len(members["members"]), "read_wg_id": WG_ONE, "triggered_by": BOB},
        )
        org = await k.ok(sid, "list_members", {"scope": "org"})
        assert await audit_of(k) == (
            "ok",
            {"result_count": len(org["members"]), "read_wg_id": None, "triggered_by": BOB},
        )

    run(rig, body)


def test_member_b_cannot_read_through_a_session_of_a_what_only_a_reads(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        research(k)
        await k.pull()
        # bob loses workgroup one; alice (the owner) still reads it; bob instructs alice's session
        await k.extend(lambda log: log.add("ungrant", "alice", subject=BOB, wg=WG_ONE))
        sid = await k.session(1)
        await k.instruct(sid, BOB)
        for tool, arguments in (
            ("list_tasks", {"workgroup_id": WG_ONE}),
            ("list_members", {"scope": "workgroup", "workgroup_id": WG_ONE}),
            ("get_whiteboard", {"level": "workgroup", "workgroup_id": WG_ONE}),
        ):
            answer = await k.read(sid, tool, arguments)
            assert answer["error"] == {"error": "not_found", "message": "There is no such workgroup you can read."}
            assert await audit_of(k) == ("denied", {"result_count": 0, "read_wg_id": WG_ONE, "triggered_by": BOB})
        for tool, arguments in (
            ("get_task", {"task_id": TASK_ONE}),
            ("get_task_messages", {"task_id": TASK_ONE}),
            ("get_whiteboard", {"level": "task", "task_id": TASK_ONE}),
        ):
            answer = await k.read(sid, tool, arguments)
            assert answer["error"] == {"error": "not_found", "message": "There is no such task you can read."}
            assert await audit_of(k) == ("denied", {"result_count": 0, "read_wg_id": WG_ONE, "triggered_by": BOB})
        listed = (await k.ok(sid, "list_workgroups"))["workgroups"]
        assert {w["id"]: w["my_access"] for w in listed} == {WG: "write", WG_ONE: None, WG_TWO: None}
        # alice herself reads it
        await k.instruct(sid, ALICE)
        assert (await k.ok(sid, "list_tasks", {"workgroup_id": WG_ONE}))["tasks"][0]["id"] == TASK_ONE

    run(rig, body)


def test_an_unknown_instructing_human_reads_only_the_own_workgroup(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        research(k)
        await k.pull()
        sid = await k.session(1)
        for wg in (WG_ONE, WG_TWO, "01930006-0000-7000-8000-0000000000ff"):
            answer = await k.read(sid, "list_tasks", {"workgroup_id": wg})
            assert answer["error"] == {"error": "forbidden", "message": UNKNOWN_TEXT}
            assert await audit_of(k) == ("denied", {"result_count": 0, "read_wg_id": wg, "triggered_by": None})
        assert await k.error(sid, "get_task", {"task_id": TASK_ONE}) == "not_found"
        assert (await k.ok(sid, "list_tasks", {"workgroup_id": WG}))["tasks"][0]["id"] == TASK
        assert (await k.ok(sid, "list_tasks"))["tasks"][0]["id"] == TASK

    run(rig, body)


def test_a_workgroup_this_device_does_not_serve_is_unavailable_with_its_name(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        await k.pull()
        sid = await k.session(1)
        await k.instruct(sid, ALICE)
        answer = await k.read(sid, "list_tasks", {"workgroup_id": WG_TWO})
        assert answer["error"] == {
            "error": "unavailable",
            "message": "This execution device does not serve workgroup Secret Lab; an admin of the organization can"
            " assign the device to that workgroup or to the whole organization.",
        }
        assert await audit_of(k) == ("failed", {"result_count": 0, "read_wg_id": WG_TWO, "triggered_by": ALICE})
        # a task the hub names in that workgroup is no task the session can read
        k.hub.raw[f"/v1/device/tasks/{TASK_TWO}"] = (
            200,
            {"task": task_row(TASK_TWO, WG_TWO), "sessions": [], "operations": []},
        )
        assert await k.error(sid, "get_task", {"task_id": TASK_TWO}) == "not_found"
        assert await audit_of(k) == ("denied", {"result_count": 0, "read_wg_id": WG_TWO, "triggered_by": ALICE})

    run(rig, body, profiles=True)


def test_a_workgroup_without_a_profile_is_named_by_its_id(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        sid = await k.session(1)
        await k.instruct(sid, ALICE)
        answer = await k.read(sid, "get_whiteboard", {"level": "workgroup", "workgroup_id": WG_TWO})
        assert answer["error"]["message"].startswith(f"This execution device does not serve workgroup {WG_TWO};")

    run(rig, body, profiles=False)


def test_an_admin_reads_another_workgroup_by_inheritance(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        research(k)
        await k.pull()
        # alice keeps only the inherited read of an admin on workgroup one
        await k.extend(
            lambda log: log.add("role", "alice", subject=CAROL, role="admin"),
            lambda log: log.add("ungrant", "carol", subject=ALICE, wg=WG_ONE),
        )
        bobs = await k.session(1, owner=BOB)
        await k.instruct(bobs, ALICE)
        assert (await k.ok(bobs, "list_tasks", {"workgroup_id": WG_ONE}))["tasks"][0]["id"] == TASK_ONE
        alices = await k.session(2)
        await k.instruct(alices, BOB)
        assert (await k.ok(alices, "get_task", {"task_id": TASK_ONE}))["id"] == TASK_ONE

    run(rig, body)


def test_an_instructing_human_who_left_makes_every_read_forbidden(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        await k.pull()
        sid = await k.session(1)
        await k.instruct(sid, BOB)
        assert (await k.read(sid, "list_workgroups"))["status"] == "ok"
        await k.extend(lambda log: log.add("remove", "alice", subject=BOB))
        for tool, arguments in (("list_workgroups", {}), ("list_tasks", {}), ("get_whiteboard", {"level": "org"})):
            answer = await k.read(sid, tool, arguments)
            assert answer["error"] == {"error": "forbidden", "message": HUMAN_GONE_TEXT}, tool
            assert await audit_of(k) == ("denied", {"result_count": 0, "read_wg_id": None, "triggered_by": BOB})

    run(rig, body)


def test_an_instructing_human_who_lost_another_workgroup_reads_it_no_more(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        research(k)
        await k.pull()
        sid = await k.session(1)
        await k.instruct(sid, CAROL)
        assert (await k.read(sid, "list_tasks", {"workgroup_id": WG_ONE}))["status"] == "ok"
        await k.extend(lambda log: log.add("ungrant", "alice", subject=CAROL, wg=WG_ONE))
        assert await k.error(sid, "list_tasks", {"workgroup_id": WG_ONE}) == "not_found"

    run(rig, body)


def test_an_archived_workgroup_is_readable_by_its_id(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        research(k)
        await k.pull()
        await k.extend(lambda log: log.add("wg_archive", "alice", wg=WG_ONE))
        sid = await k.session(1)
        await k.instruct(sid, BOB)
        assert (await k.ok(sid, "list_tasks", {"workgroup_id": WG_ONE}))["tasks"][0]["id"] == TASK_ONE
        listed = (await k.ok(sid, "list_workgroups"))["workgroups"]
        assert [w["id"] for w in listed] == [WG, WG_TWO]

    run(rig, body)


def test_a_hub_naming_a_wrong_workgroup_yields_no_content_of_another(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        research(k)
        await k.pull()
        sid = await k.session(1, wg=WG_ONE, owner=CAROL, task=TASK_ONE)
        await k.instruct(sid, CAROL)
        # the hub presents the default workgroup's task as one of workgroup one: its chat is sealed in the default
        # workgroup's scope, so nothing of it is read under workgroup one
        k.hub.raw[f"/v1/device/tasks/{TASK}"] = (
            200,
            {"task": task_row(TASK, WG_ONE), "sessions": [], "operations": []},
        )
        assert await k.ok(sid, "get_task_messages", {"task_id": TASK}) == {"messages": [], "next_after": None}
        assert await k.ok(sid, "get_whiteboard", {"level": "task", "task_id": TASK}) == {"entries": []}
        # a workgroup the log does not know, and a row without a workgroup
        stranger = "01930006-0000-7000-8000-0000000000fe"
        k.hub.raw[f"/v1/device/tasks/{TASK}"] = (
            200,
            {"task": task_row(TASK, stranger), "sessions": [], "operations": []},
        )
        assert await k.error(sid, "get_task", {"task_id": TASK}) == "not_found"
        assert await audit_of(k) == ("denied", {"result_count": 0, "read_wg_id": None, "triggered_by": CAROL})
        k.hub.raw[f"/v1/device/tasks/{TASK}"] = (200, {"task": {**task_row(TASK), "wg_id": 7}, "sessions": []})
        assert await k.error(sid, "get_task", {"task_id": TASK}) == "unavailable"

    run(rig, body)


def test_workgroup_arguments_are_checked(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        sid = await k.session(1)
        await k.instruct(sid, ALICE)
        for tool, arguments in (
            ("list_tasks", {"workgroup_id": "x"}),
            ("list_tasks", {"workgroup_id": 1}),
            ("list_members", {"scope": "org", "workgroup_id": WG_ONE}),
            ("get_whiteboard", {"level": "org", "workgroup_id": WG_ONE}),
            ("get_whiteboard", {"level": "task", "task_id": TASK_ONE, "workgroup_id": WG_ONE}),
            ("list_operations", {"task_id": TASK_ONE}),
        ):
            assert await k.error(sid, tool, arguments) == "params_invalid", (tool, arguments)
            assert await audit_of(k) == ("failed", {"result_count": 0, "read_wg_id": None, "triggered_by": ALICE})

    run(rig, body)


def test_the_human_captured_when_the_read_arrived_is_used(rig: Rig) -> None:
    """The race of ADR-089 section 1: a prompt of another person accepted while a read is answered (here: the turn's
    acting user changes between the journal record and the tool) does not lend its rights to the earlier read."""

    async def body(k: RKit) -> None:
        research(k)
        await k.pull()
        sid = await k.session(1)
        controller = k.skit.controller
        real = controller.tools["list_tasks"]
        gate = asyncio.Event()
        seen: list[str | None] = []

        async def gated(call: QueryCall) -> QueryOutcome:
            await gate.wait()
            seen.append(call.actor_id)
            return await real(call)

        controller.tools["list_tasks"] = gated
        # bob instructs, the read arrives; then a prompt of nobody known (several people) becomes the turn's
        await k.instruct(sid, BOB)
        pending = await k.journal_read(sid, "list_tasks", {"workgroup_id": WG_ONE})
        await k.instruct(sid, None)
        gate.set()
        await k.skit.settle_queries()
        assert k.answer(pending, "list_tasks")["status"] == "ok"
        assert await audit_of(k) == ("ok", {"result_count": 1, "read_wg_id": WG_ONE, "triggered_by": BOB})
        # and the other way: arrived without a known human, answered after bob became the acting user
        gate.clear()
        pending = await k.journal_read(sid, "list_tasks", {"workgroup_id": WG_ONE})
        await k.instruct(sid, BOB)
        gate.set()
        await k.skit.settle_queries()
        assert k.answer(pending, "list_tasks")["error"]["error"] == "forbidden"
        assert await audit_of(k) == ("denied", {"result_count": 0, "read_wg_id": WG_ONE, "triggered_by": None})
        assert seen == [BOB, None]

    run(rig, body)


def test_reads_keep_the_org_of_the_session(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        research(k)
        await k.pull()
        sid = await k.session(1)
        await k.instruct(sid, BOB)
        path = f"/v1/device/workgroups/{WG_ONE}/tasks"
        k.hub.raw[path] = (200, {"tasks": [{**task_row(TASK_ONE, WG_ONE), "org_id": WG}, task_row(TASK, WG)]})
        assert await k.ok(sid, "list_tasks", {"workgroup_id": WG_ONE}) == {"tasks": [], "next_before": None}

    run(rig, body)


def test_a_child_reads_with_the_human_of_the_instruction_it_works_on(rig: Rig) -> None:
    """ADR-088 section 1: a child session's reads use the acting user of the instruction its turn works on - an
    instruction of bob lends the child only what bob may read, and a replayed acceptance of an older instruction
    brings back no rights."""

    async def body(k: RKit) -> None:
        research(k)
        await k.pull()
        await k.extend(lambda log: log.add("ungrant", "alice", subject=BOB, wg=WG_ONE))
        parent = await k.session(1)
        child = await k.session(2, depth=1, parent=parent)
        start = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
        ids = {actor: uuid7() for actor in (ALICE, BOB)}
        async with k.env.pool.connection() as conn:
            for n, actor in enumerate((ALICE, BOB)):
                at = start + timedelta(seconds=n)
                await instructions.add(
                    conn,
                    operation_id=ids[actor],
                    child_id=child,
                    parent_id=parent,
                    actor_id=actor,
                    created_at=at,
                    delivered_at=at,
                )
        await k.skit.state(child, "prompt_accepted", prompt_id=ids[ALICE])
        assert (await k.ok(child, "list_tasks", {"workgroup_id": WG_ONE}))["tasks"][0]["id"] == TASK_ONE
        assert await audit_of(k) == ("ok", {"result_count": 1, "read_wg_id": WG_ONE, "triggered_by": ALICE})
        await k.skit.state(child, "idle")
        await k.skit.state(child, "prompt_accepted", prompt_id=ids[BOB])
        answer = await k.read(child, "list_tasks", {"workgroup_id": WG_ONE})
        assert answer["error"] == {"error": "not_found", "message": "There is no such workgroup you can read."}
        assert await audit_of(k) == ("denied", {"result_count": 0, "read_wg_id": WG_ONE, "triggered_by": BOB})
        # alice's older instruction accepted again (a replay): still bob's turn
        await k.skit.state(child, "prompt_accepted", prompt_id=ids[ALICE])
        assert await k.error(child, "list_tasks", {"workgroup_id": WG_ONE}) == "not_found"

    run(rig, body)
