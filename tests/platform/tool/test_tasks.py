"""`task list|create|prompt|show` (stage c, 11-agent-access.md section 8.4): tasks sealed and opened on the user's
computer, against the hub double. The title rule against `contracts/tasks/title.json`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_platform import tasks
from agent_platform.errors import PlatformError
from agent_runner.crypto.ids import uuid7

from .conftest import Tool
from .hubfake import HubFake, User

TITLES = Path(__file__).resolve().parents[3] / "contracts" / "tasks" / "title.json"


def default_wg(hub: HubFake, user: User) -> str:
    for org in hub.orgs.values():
        if org.log.state.member(user.id) is not None:
            return org.log.state.default_wg
    raise AssertionError("no org")


@pytest.mark.parametrize("case", json.loads(TITLES.read_text())["cases"], ids=lambda case: case["name"])
def test_title_vectors(case: dict[str, str]) -> None:
    assert tasks.derive_title(case["prompt"]) == case["title"]


def test_create_list_prompt_show(tool: Tool, hub: HubFake, account: User) -> None:
    wg = default_wg(hub, account)
    code, created = tool.run("task", "create", "--wg", wg, "--text", "  Fix the login bug\nwith details")
    assert code == 0, created
    task = created["task_id"]
    assert created["title"] == "Fix the login bug"
    assert created["session_id"]
    code, prompted = tool.run("task", "prompt", "--task", task, "--text", "-", stdin="And add a test.")
    assert code == 0, prompted
    assert prompted["scope_seq"] == 3
    code, listed = tool.run("task", "list", "--wg", wg)
    assert listed["tasks"] == [
        {
            "task_id": task,
            # the place of the row, as in the dashboard of `task overview` (E-43)
            "org_id": listed["org_id"],
            "wg_id": wg,
            "state": "scheduling",
            "title": "Fix the login bug",
            "number": 1,
            "agent": "Fix the login bug #1",
            "name_source": "automatic",
            # the ticket fields of ADR-100: an agent task is inProgress with the default urgency
            "status": "inProgress",
            "urgency": "normal",
            "assignee_id": None,
            "topic_id": None,
            "topic": None,
            "created_at": listed["tasks"][0]["created_at"],
            "last_activity_at": listed["tasks"][0]["last_activity_at"],
        }
    ]
    assert tool.run("task", "list", "--wg", wg, "--state", "closed")[1]["tasks"] == []
    assert [row["task_id"] for row in tool.run("task", "list", "--wg", wg, "--state", "open")[1]["tasks"]] == [task]
    code, shown = tool.run("task", "show", "--task", task)
    assert code == 0, shown
    assert shown["title"] == "Fix the login bug"
    assert [event["body"]["text"] for event in shown["events"]] == [
        "  Fix the login bug\nwith details",
        "And add a test.",
    ]
    assert {event["author"] for event in shown["events"]} == {account.id}
    assert shown["unverified"] == 0
    # the hub received ciphertext only
    sent = b"".join(hub.bodies)
    assert b"login bug" not in sent
    assert b"add a test" not in sent


def test_show_leaves_out_what_does_not_verify_and_marks_locked_events(tool: Tool, hub: HubFake, account: User) -> None:
    wg = default_wg(hub, account)
    task = tool.run("task", "create", "--wg", wg, "--text", "Hello")[1]["task_id"]
    other = tool.run("task", "create", "--wg", wg, "--text", "Other")[1]["task_id"]
    stored = hub.events[wg]
    forged: Any = json.loads(json.dumps(stored[1]))
    forged["event"]["ct"] = forged["event"]["ct"][:-4] + "AAAA"
    stored.append({**forged, "scope_seq": len(stored) + 1})
    # a hub that answers events of another task for this one
    foreign = [item for item in stored if item["event"]["header"]["task"] == other]
    hub.hooks[("GET", f"/v1/scopes/{wg}/events")] = lambda body, request: (
        (200, {"events": [*[i for i in stored if i["event"]["header"]["task"] == task], *foreign], "next_after": None}),
    )[0]
    shown = tool.run("task", "show", "--task", task)[1]
    assert [event["body"]["text"] for event in shown["events"]] == ["Hello"]
    assert shown["unverified"] == 3
    del hub.hooks[("GET", f"/v1/scopes/{wg}/events")]
    # without the workgroup key the events are listed as locked
    hub.wraps[:] = [wrap for wrap in hub.wraps if wrap["scope"] != "wg"]
    shown = tool.run("task", "show", "--task", task)[1]
    assert shown["title"] is None
    assert [event.get("locked") for event in shown["events"]] == [True]
    assert tool.run("task", "list", "--wg", wg)[1]["tasks"][0]["title"] is None
    code, document = tool.run("task", "prompt", "--task", task, "--text", "x")
    assert (code, document["error"], document["reason"]) == (1, "hub_refused", "epoch_stale")


def test_no_device_and_bad_input(tool: Tool, hub: HubFake, account: User) -> None:
    wg = default_wg(hub, account)
    hub.no_device.add(wg)
    assert tool.run("task", "create", "--wg", wg, "--text", "Hello")[1]["error"] == "no_device"
    for argv in (
        ("task", "create", "--wg", "not-a-uuid", "--text", "Hello"),
        ("task", "create", "--wg", wg, "--text", "   "),
        ("task", "create", "--wg", wg, "--text", "x" * (tasks.PROMPT_MAX + 1)),
        ("task", "show", "--task", "x"),
    ):
        assert tool.run(*argv)[1]["error"] == "invalid_argument", argv
    assert tool.run("task", "create", "--wg", uuid7(), "--text", "Hello")[1]["error"] == "no_org"
    with pytest.raises(PlatformError):
        tasks.task_list(None, wg, tasks.ListQuery(state="bogus"))  # type: ignore[arg-type]


def test_refusals_and_malformed_answers(tool: Tool, hub: HubFake, account: User) -> None:
    wg = default_wg(hub, account)
    hub.hooks[("POST", "/v1/tasks")] = lambda body, request: (403, {"error": "forbidden"})
    _, document = tool.run("task", "create", "--wg", wg, "--text", "Hello")
    assert (document["error"], document["hub_error"]) == ("hub_refused", "forbidden")
    del hub.hooks[("POST", "/v1/tasks")]
    task = tool.run("task", "create", "--wg", wg, "--text", "Hello")[1]["task_id"]
    hub.hooks[("GET", f"/v1/tasks/{task}")] = lambda body, request: (200, {"task": {"task_id": task}})
    assert tool.run("task", "show", "--task", task)[1]["reason"] == "response_invalid"
    del hub.hooks[("GET", f"/v1/tasks/{task}")]
    hub.hooks[("GET", f"/v1/scopes/{wg}/events")] = lambda body, request: (200, {"events": None})
    assert tool.run("task", "show", "--task", task)[1]["reason"] == "response_invalid"
    del hub.hooks[("GET", f"/v1/scopes/{wg}/events")]
    hub.hooks[("GET", f"/v1/workgroups/{wg}/tasks")] = lambda body, request: (200, {"tasks": None})
    assert tool.run("task", "list", "--wg", wg)[1]["reason"] == "response_invalid"
    hub.hooks[("GET", f"/v1/workgroups/{wg}/tasks")] = lambda body, request: (200, {"tasks": ["x"]})
    assert tool.run("task", "list", "--wg", wg)[1]["tasks"] == []
    del hub.hooks[("GET", f"/v1/workgroups/{wg}/tasks")]
    # paging: a second page after `next_after` (then the one page of the main agent's name, KOR-52)
    pages = iter([(200, {"events": [], "next_after": 5}), *[(200, {"events": [], "next_after": None})] * 2])
    hub.hooks[("GET", f"/v1/scopes/{wg}/events")] = lambda body, request: next(pages)
    assert tool.run("task", "show", "--task", task)[1]["events"] == []


def test_follow_returns_new_events_and_the_next_position(
    tool: Tool, hub: HubFake, account: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tasks, "FOLLOW_POLL_S", 0.01)
    wg = default_wg(hub, account)
    task = tool.run("task", "create", "--wg", wg, "--text", "Hello")[1]["task_id"]
    code, first = tool.run("task", "follow", "--task", task, "--wait", "0")
    assert code == 0, first
    assert [event["body"]["text"] for event in first["events"]] == ["Hello"]
    assert (first["state"], first["unverified"], first["next_after"]) == ("scheduling", 0, 2)
    # nothing new: an empty answer once the wait is over
    empty = tool.run("task", "follow", "--task", task, "--after", "2", "--wait", "0")[1]
    assert (empty["events"], empty["next_after"]) == ([], 2)
    # a prompt arrives while the call waits: the first poll does not see it yet
    tool.run("task", "prompt", "--task", task, "--text", "Next")
    path = f"/v1/scopes/{wg}/events"

    def later(body: object, request: Any) -> tuple[int, Any] | None:
        del hub.hooks[("GET", path)]
        return 200, {"events": [], "next_after": None}

    hub.hooks[("GET", path)] = later
    waited = tool.run("task", "follow", "--task", task, "--after", "2", "--wait", "5")[1]
    assert [event["body"]["text"] for event in waited["events"]] == ["Next"]
    assert waited["next_after"] == 3
    # an item the hub sends for another task, and one without a position, are left out
    other = tool.run("task", "create", "--wg", wg, "--text", "Other")[1]["task_id"]
    foreign = [item for item in hub.events[wg] if item["event"]["header"]["task"] == other]
    hub.hooks[("GET", path)] = lambda body, request: (200, {"events": [*foreign, "x"], "next_after": None})
    odd = tool.run("task", "follow", "--task", task, "--after", "3", "--wait", "0")[1]
    assert (odd["events"], odd["unverified"], odd["next_after"]) == ([], 3, foreign[-1]["scope_seq"])
    for argv in (("--after", "-1"), ("--wait", "601")):
        assert tool.run("task", "follow", "--task", task, *argv)[1]["error"] == "invalid_argument"


def test_open_tasks_are_every_state_but_closed_across_pages(
    tool: Tool, hub: HubFake, account: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--state open` is the tool's word (KOR-67): the hub knows the session states only, so the tool pages through the
    workgroup's tasks and leaves out the closed ones, up to `LIST_LIMIT` tasks or `OPEN_PAGES` pages."""
    wg = default_wg(hub, account)
    made = [str(tool.run("task", "create", "--wg", wg, "--text", f"Task {n}")[1]["task_id"]) for n in range(5)]
    for task in made[1::2]:
        hub.tasks[task]["task"]["state"] = "closed"
    hub.tasks[made[4]]["task"]["state"] = "working"
    monkeypatch.setattr(tasks, "LIST_LIMIT", 2)
    listed = tool.run("task", "list", "--wg", wg, "--state", "open")[1]["tasks"]
    assert [(row["task_id"], row["state"]) for row in listed] == [(made[4], "working"), (made[2], "scheduling")]
    monkeypatch.setattr(tasks, "LIST_LIMIT", 1)
    monkeypatch.setattr(tasks, "OPEN_PAGES", 2)
    assert [row["task_id"] for row in tool.run("task", "list", "--wg", wg, "--state", "open")[1]["tasks"]] == [made[4]]
    hub.tasks[made[4]]["task"]["state"] = "closed"
    assert tool.run("task", "list", "--wg", wg, "--state", "open")[1]["tasks"] == []
    monkeypatch.setattr(tasks, "OPEN_PAGES", 10)
    assert [row["task_id"] for row in tool.run("task", "list", "--wg", wg, "--state", "open")[1]["tasks"]] == [made[2]]


def test_open_task_list_says_when_it_is_truncated(
    tool: Tool, hub: HubFake, account: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`truncated` is true as soon as open tasks or pages were cut off, and false when every page was read."""
    wg = default_wg(hub, account)
    made = [str(tool.run("task", "create", "--wg", wg, "--text", f"Task {n}")[1]["task_id"]) for n in range(3)]

    def listed() -> dict[str, Any]:
        return tool.run("task", "list", "--wg", wg, "--state", "open")[1]

    assert listed()["truncated"] is False
    monkeypatch.setattr(tasks, "LIST_LIMIT", 2)
    cut = listed()
    assert ([row["task_id"] for row in cut["tasks"]], cut["truncated"]) == ([made[2], made[1]], True)
    monkeypatch.setattr(tasks, "LIST_LIMIT", 1)
    hub.tasks[made[2]]["task"]["state"] = "closed"
    hub.tasks[made[1]]["task"]["state"] = "closed"
    monkeypatch.setattr(tasks, "OPEN_PAGES", 2)
    assert (listed()["tasks"], listed()["truncated"]) == ([], True)
    monkeypatch.setattr(tasks, "OPEN_PAGES", 3)
    whole = listed()
    assert ([row["task_id"] for row in whole["tasks"]], whole["truncated"]) == ([made[0]], False)
