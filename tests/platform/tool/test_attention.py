"""The situation view of the agent tool (KOR-39, ADR-076 sections 7 and 8): `status --attention`, `task list --org
--state ...` and `task prompt --approve|--decline`, against the hub double. Questions are device-signed
`operation.params` events opened and verified on this computer."""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent_platform import attention, tasks
from agent_platform.errors import PlatformError
from agent_runner.crypto.encoding import body_json
from agent_runner.crypto.event import new_header, seal_event
from agent_runner.crypto.ids import uuid7
from agent_runner.crypto.log.state import WG

from .conftest import Tool
from .hubfake import HubFake, User, now_ms_text
from .test_tasks import default_wg

QUESTION = "Which database should the service use?\n- PostgreSQL\n- SQLite"


def org_of(hub: HubFake, wg: str) -> str:
    found = hub.org_of_wg(wg)
    assert found is not None
    return found


def record(task: str, category: str, reason: str, since: str, **extra: Any) -> dict[str, Any]:
    return {
        "task_id": task,
        "category": category,
        "reason": reason,
        "ref_kind": extra.pop("ref_kind", None),
        "ref_id": extra.pop("ref_id", None),
        "since": since,
        "seq": 1,
        "subagents_total": 0,
        "subagents_active": 0,
        "updated_at": since,
        **extra,
    }


class World:
    """An org with a device assigned to its default workgroup and tasks created through the tool."""

    def __init__(self, tool: Tool, hub: HubFake, account: User) -> None:
        self.tool, self.hub, self.account = tool, hub, account
        self.wg = default_wg(hub, account)
        self.org = org_of(hub, self.wg)
        self.device, seq = hub.certify_device()
        hub.candidates[self.wg] = [{"device_id": self.device, "platform_seq": seq}]
        assert tool.run("device", "assign", "--wg", self.wg)[0] == 0

    def task(self, text: str) -> str:
        code, created = self.tool.run("task", "create", "--wg", self.wg, "--text", text)
        assert code == 0, created
        return str(created["task_id"])

    def params_event(self, task: str, operation: str, body: dict[str, Any], *, wg: str | None = None) -> None:
        """A device-signed `operation.params` event of the workgroup (as the runner writes it)."""
        log = self.hub.orgs[self.org].log
        header = new_header(
            id=uuid7(),
            org=self.org,
            scope=WG,
            scope_id=self.wg,
            epoch=log.state.epoch(WG, self.wg),
            author=self.device,
            kind="operation.params",
            ts=now_ms_text(),
            lh=log.head.seq,
            wg=self.wg,
            task=task,
            ref=operation,
        )
        key = self.hub.org_key(self.org, self.account, WG, self.wg)
        event = seal_event(key, header, body_json(body), self.hub.platform_devices[self.device].sig)
        self.hub.store_event(wg or self.wg, event)

    def ask(self, task: str, op_type: str = "human.question", question: str = QUESTION) -> str:
        operation = uuid7()
        self.params_event(
            task, operation, {"operation_id": operation, "type": op_type, "params": {"question": question}}
        )
        return operation


@pytest.fixture
def world(tool: Tool, hub: HubFake, account: User) -> World:
    return World(tool, hub, account)


def test_status_shows_counters_decisions_with_questions_and_stuck_tasks(world: World, hub: HubFake) -> None:
    asked = world.task("Set up the service")
    approval = world.task("Clean up branches")
    stuck = world.task("Deploy")
    running = world.task("Write docs")
    question_op = world.ask(asked)
    approval_op = world.ask(approval, "human.approval", "May I delete the branch old-release?")
    hub.attention[asked] = record(asked, "needs_decision", "question", "2026-11-24T10:00:00.000Z", ref_id=question_op)
    hub.attention[approval] = record(
        approval, "needs_decision", "approval", "2026-11-24T11:00:00.000Z", ref_kind="operation", ref_id=approval_op
    )
    hub.attention[stuck] = record(stuck, "blocked", "session_failed", "2026-11-24T09:00:00.000Z", ref_kind="session")
    hub.attention[running] = record(running, "working", "working", "2026-11-24T08:00:00.000Z", subagents_active=2)
    code, document = world.tool.run("status", "--attention")
    assert code == 0, document
    (org,) = document["attention"]
    assert org["org_id"] == world.org
    assert org["summary"] == {
        "needs_decision": 2,
        "needs_decision_mine": 2,
        "blocked": 1,
        "working": 1,
        "done_since": 0,
    }
    first, second = org["needs_decision"]
    assert (first["task_id"], first["title"], first["question"]) == (asked, "Set up the service", QUESTION)
    assert (first["workgroup"], first["owner_id"], first["owner"]) == ("Default", world.account.id, "Ada")
    assert (first["reason"], first["ref_id"], first["since"]) == ("question", question_op, "2026-11-24T10:00:00.000Z")
    assert (second["task_id"], second["question"]) == (approval, "May I delete the branch old-release?")
    (blocked,) = org["blocked"]
    assert (blocked["task_id"], blocked["reason"], blocked["title"]) == (stuck, "session_failed", "Deploy")
    assert "question" not in blocked
    assert org["unverified"] == 0
    # one org by name, the same content
    assert world.tool.run("status", "--attention", "--org", world.org)[1]["attention"] == document["attention"]
    # the questions reached the hub sealed only
    assert b"PostgreSQL" not in b"".join(hub.bodies)


def test_questions_that_do_not_verify_or_do_not_match_are_not_shown(world: World, hub: HubFake) -> None:
    task = world.task("Ask")
    other = world.task("Other")
    operation = uuid7()
    # wrong operation ID, wrong type, no params, another task's event, a broken event, then nothing that matches
    world.params_event(
        task, operation, {"operation_id": uuid7(), "type": "human.question", "params": {"question": "x"}}
    )
    world.params_event(
        task, operation, {"operation_id": operation, "type": "human.approval", "params": {"question": "y"}}
    )
    world.params_event(task, operation, {"operation_id": operation, "type": "human.question", "params": "z"})
    world.params_event(task, operation, {"operation_id": operation, "type": "human.question"})
    world.params_event(
        other, operation, {"operation_id": operation, "type": "human.question", "params": {"question": "o"}}
    )
    stored = hub.events[world.wg]
    broken: Any = json.loads(json.dumps(stored[-1]))
    broken["event"]["header"]["task"] = task
    stored.append({**broken, "scope_seq": len(stored) + 1})
    hub.attention[task] = record(task, "needs_decision", "question", "2026-11-24T10:00:00.000Z", ref_id=operation)
    hub.attention[other] = record(other, "needs_decision", "question", "2026-11-24T10:00:01.000Z", ref_id="not-an-id")
    rows = world.tool.run("task", "list", "--org", world.org, "--state", "needs-decision")[1]["tasks"]
    assert [(row["task_id"], row["question"]) for row in rows] == [(task, None), (other, None)]
    # an unknown asking reason without a question is listed without one
    hub.attention[task]["reason"] = "credit_empty"
    rows = world.tool.run("task", "list", "--org", world.org, "--state", "needs-decision")[1]["tasks"]
    assert "question" not in rows[0]


def test_without_the_workgroup_key_the_question_stays_locked(world: World, hub: HubFake) -> None:
    task = world.task("Ask")
    operation = world.ask(task)
    hub.attention[task] = record(task, "needs_decision", "question", "2026-11-24T10:00:00.000Z", ref_id=operation)
    hub.wraps[:] = [wrap for wrap in hub.wraps if wrap["scope"] != "wg"]
    (row,) = world.tool.run("task", "list", "--org", world.org, "--state", "needs-decision")[1]["tasks"]
    assert (row["title"], row["question"]) == (None, None)


def test_task_list_by_category_pages_mine_and_limit(world: World, hub: HubFake, tool: Tool) -> None:
    made = [world.task(f"Task {n}") for n in range(5)]
    for n, task in enumerate(made):
        hub.attention[task] = record(task, "done", "idle", f"2026-11-24T10:00:0{n}.000Z")
    foreign = made[0]
    hub.tasks[foreign]["task"]["owner_id"] = uuid7()
    code, listed = tool.run("task", "list", "--org", world.org, "--state", "done")
    assert code == 0, listed
    assert listed["category"] == "done"
    assert [row["task_id"] for row in listed["tasks"]] == list(reversed(made))
    assert listed["tasks"][0]["title"] == "Task 4"
    mine = tool.run("task", "list", "--org", world.org, "--state", "done", "--mine")[1]["tasks"]
    assert foreign not in [row["task_id"] for row in mine]
    limited = tool.run("task", "list", "--org", world.org, "--state", "done", "--limit", "2")[1]["tasks"]
    assert [row["task_id"] for row in limited] == list(reversed(made))[:2]
    # paging: pages of two (the hub's page size here) are followed with next_before
    original = hub.r_attention

    def small_pages(request: Any, body: Any, org: str) -> tuple[int, Any]:
        request.query["limit"] = "2"
        return original(request, body, org)

    hub.override("r_attention", small_pages)
    paged = tool.run("task", "list", "--org", world.org, "--state", "done")[1]["tasks"]
    assert [row["task_id"] for row in paged] == list(reversed(made))
    hub.restore("r_attention")
    assert tool.run("task", "list", "--org", world.org, "--state", "working")[1]["tasks"] == []


def test_malformed_attention_answers(world: World, hub: HubFake, tool: Tool) -> None:
    task = world.task("Task")
    good = {"task": hub.tasks[task]["task"], "attention": record(task, "blocked", "guest_error", "2026-11-24T10:00Z")}
    bad_items: list[Any] = [
        "x",
        {"task": "x", "attention": {}},
        {"task": {"task_id": "nope"}, "attention": {"task_id": "nope", "category": "blocked"}},
        {"task": {**good["task"]}, "attention": {**good["attention"], "task_id": uuid7()}},
        {"task": {**good["task"]}, "attention": {**good["attention"], "category": "done"}},
        {"task": {**good["task"], "wg_id": "x"}, "attention": good["attention"]},
        {"task": {**good["task"], "org_id": uuid7()}, "attention": good["attention"]},
    ]
    pages = iter(
        [
            (200, {"items": [*bad_items, good], "next_before": "c1"}),
            (200, {"items": [good], "next_before": "c1"}),  # the same cursor again ends the paging
        ]
    )
    route = ("GET", f"/v1/orgs/{world.org}/attention")
    hub.hooks[route] = lambda body, request: next(pages)
    listed = tool.run("task", "list", "--org", world.org, "--state", "blocked")[1]
    assert ([row["task_id"] for row in listed["tasks"]], listed["unverified"]) == ([task, task], len(bad_items))
    hub.hooks[route] = lambda body, request: (200, {"items": [], "next_before": "c2"})
    assert tool.run("task", "list", "--org", world.org, "--state", "blocked")[1]["tasks"] == []
    hub.hooks[route] = lambda body, request: (200, {"items": None})
    assert tool.run("task", "list", "--org", world.org, "--state", "blocked")[1]["reason"] == "response_invalid"
    del hub.hooks[route]
    summary = ("GET", f"/v1/orgs/{world.org}/attention/summary")
    hub.hooks[summary] = lambda body, request: (200, {"needs_decision": 1})
    assert tool.run("status", "--attention")[1]["reason"] == "response_invalid"
    hub.hooks[summary] = lambda body, request: (
        200,
        {"needs_decision": True, "needs_decision_mine": 0, "blocked": 0, "working": 0, "done_since": 0},
    )
    assert tool.run("status", "--attention")[1]["reason"] == "response_invalid"


def test_bad_attention_arguments(world: World, tool: Tool) -> None:
    # without an attention category, `--org` lists the task dashboard of that organization (ADR-101)
    assert tool.run("task", "list", "--org", world.org)[0] == 0
    assert tool.run("task", "list", "--org", world.org, "--state", "open")[0] == 0
    for argv in (
        ("task", "list", "--org", world.org, "--state", "done", "--limit", "0"),
        ("task", "list", "--org", world.org, "--state", "done", "--limit", "1001"),
        ("task", "list", "--org", "not-a-uuid", "--state", "done"),
        ("task", "list", "--wg", world.wg, "--state", "blocked"),
        ("task", "list", "--wg", world.wg, "--mine"),
        ("task", "list", "--wg", world.wg, "--org", world.org),
        ("task", "list"),
        ("status", "--org", world.org),
        ("status", "--attention", "--org", "x"),
    ):
        assert tool.run(*argv)[1]["error"] == "invalid_argument", argv
    assert tool.run("task", "list", "--org", uuid7(), "--state", "done")[1]["error"] == "no_org"
    assert tool.run("status", "--attention", "--org", uuid7())[1]["error"] == "no_org"
    # the workgroup listing keeps working
    assert tool.run("task", "list", "--wg", world.wg, "--state", "open")[0] == 0
    with pytest.raises(PlatformError):
        attention.task_list(None, world.org, "bogus", mine=False, limit=1)  # type: ignore[arg-type]


def test_approve_decline_and_answer_send_the_web_client_text(world: World, hub: HubFake, tool: Tool) -> None:
    task = world.task("Clean up")
    for argv, expected in (
        (("--approve",), "Approved."),
        (("--decline", "--text", "Keep it for now."), "Declined.\n\nKeep it for now."),
        (("--approve", "--text", "  "), "Approved."),
        (("--text", "Use PostgreSQL."), "Use PostgreSQL."),
    ):
        code, document = tool.run("task", "prompt", "--task", task, *argv)
        assert code == 0, document
        shown = tool.run("task", "show", "--task", task)[1]
        assert shown["events"][-1]["body"]["text"] == expected
    code, document = tool.run("task", "prompt", "--task", task, "--approve", "--text", "-", stdin="From stdin.")
    assert code == 0, document
    assert tool.run("task", "show", "--task", task)[1]["events"][-1]["body"]["text"] == "Approved.\n\nFrom stdin."
    refused: list[tuple[str, ...]] = [("--approve", "--decline"), ()]
    for bad in refused:
        assert tool.run("task", "prompt", "--task", task, *bad)[1]["error"] == "invalid_argument", bad
    assert tasks.answer_text("decline", None) == "Declined."
