"""`task close|operations|overview` and `session show|stop|interrupt|terminal` against the hub double: rows checked
like the web, events opened and verified (session kinds with the session's device as author)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_platform import sessions
from agent_runner.crypto.ids import uuid7

from .conftest import Tool
from .fakes.sessions import add_operation, add_session, device_event, state_of
from .hubfake import HubFake, User
from .seeding import assign_device, default_wg, new_task, private_org, reader


def test_task_close(tool: Tool, hub: HubFake, account: User) -> None:
    wg = default_wg(hub, account)
    task = new_task(tool, wg)
    plain = add_session(hub, task)
    remote = add_session(hub, task, device=uuid7())
    code, closed = tool.run("task", "close", "--task", task)
    assert code == 0, closed
    assert closed["task"]["state"] == "closed"
    rows = state_of(hub).rows
    assert (rows[plain]["state"], rows[remote]["state"]) == ("stopped", "stopping")
    assert tool.run("task", "close", "--task", task)[1]["task"]["state"] == "closed"
    assert tool.run("task", "close", "--task", "x")[1]["error"] == "invalid_argument"
    assert tool.run("task", "close", "--task", uuid7())[1]["hub_error"] == "not_found"
    hub.hooks[("POST", f"/v1/tasks/{task}/close")] = lambda body, request: (200, {"task": {"task_id": uuid7()}})
    assert tool.run("task", "close", "--task", task)[1]["reason"] == "response_invalid"


def test_session_show_and_control(tool: Tool, hub: HubFake, account: User) -> None:
    task = new_task(tool, default_wg(hub, account))
    session = add_session(hub, task)
    child = add_session(hub, task)
    state_of(hub).rows[child]["parent_session_id"] = session
    code, shown = tool.run("session", "show", "--session", session)
    assert code == 0, shown
    assert (shown["session"]["session_id"], shown["children"]) == (session, [child])
    assert set(shown) == {"ok", "session", "transitions", "usage", "vm", "children"}
    assert tool.run("session", "interrupt", "--session", session)[1]["action"] == "interrupt"
    assert state_of(hub).rows[session]["state"] == "working"
    assert tool.run("session", "stop", "--session", session)[1] == {"ok": True, "session_id": session, "action": "stop"}
    assert state_of(hub).controls == [(session, "interrupt"), (session, "stop")]
    assert state_of(hub).rows[session]["state"] == "stopped"
    assert tool.run("session", "stop", "--session", uuid7())[1]["hub_error"] == "not_found"
    assert tool.run("session", "show", "--session", "nope")[1]["error"] == "invalid_argument"
    hub.hooks[("GET", f"/v1/sessions/{session}")] = lambda body, request: (200, {"session": {"session_id": session}})
    assert tool.run("session", "show", "--session", session)[1]["reason"] == "response_invalid"


def test_operations_with_their_verified_events(tool: Tool, hub: HubFake, account: User) -> None:
    _org, wg, device = assign_device(tool, hub, account)
    task = new_task(tool, wg)
    session = add_session(hub, task, device=device)
    first = add_operation(hub, task, session)
    second = add_operation(hub, task, session, "github.list_repositories")
    device_event(hub, account, device, "operation.params", {"repo": "acme/app"}, wg=wg, ref=first)
    device_event(hub, account, device, "operation.result", {"ok": True}, wg=wg, ref=first)
    forged: Any = json.loads(json.dumps(hub.events[wg][-1]))
    forged["event"]["ct"] = forged["event"]["ct"][:-4] + "AAAA"
    hub.events[wg].append({**forged, "scope_seq": len(hub.events[wg]) + 1})
    code, listed = tool.run("task", "operations", "--task", task)
    assert code == 0, listed
    assert [row["operation_id"] for row in listed["operations"]] == [second, first]
    assert [event["body"] for event in listed["operations"][1]["events"]] == [{"repo": "acme/app"}, {"ok": True}]
    assert (listed["operations"][0]["events"], listed["unverified"], listed["next_before"]) == ([], 1, None)
    code, one = tool.run("task", "operations", "--task", task, "--operation", first)
    assert code == 0, one
    assert [event["kind"] for event in one["operation"]["events"]] == ["operation.params", "operation.result"]
    assert one["transitions"][0]["state"] == "succeeded"
    # a hub that hands over every event of the scope for one operation: the others are left out
    device_event(hub, account, device, "operation.params", {"x": 1}, wg=wg, ref=second)
    everything = list(hub.events[wg])
    hub.hooks[("GET", f"/v1/scopes/{wg}/events")] = lambda body, request: (
        200,
        {"events": everything, "next_after": None},
    )
    slipped = tool.run("task", "operations", "--task", task, "--operation", first)[1]
    assert (len(slipped["operation"]["events"]), slipped["unverified"]) == (2, 4)
    del hub.hooks[("GET", f"/v1/scopes/{wg}/events")]
    # paging, and another task's row slipped into the list is left out
    path = f"/v1/tasks/{task}/operations"
    foreign = {**state_of(hub).operations[first]["operation"], "task_id": uuid7()}
    nameless = {**foreign, "task_id": task, "operation_id": 5}
    hub.hooks[("GET", path)] = lambda body, request: (
        200,
        {"operations": [foreign, nameless, "x"], "next_before": "50"},
    )
    paged = tool.run("task", "operations", "--task", task, "--before", "0")[1]
    assert (paged["operations"], paged["next_before"]) == ([], "50")
    assert [request.query for request in hub.requests if request.path == path][-1]["before"] == "0"
    hub.hooks[("GET", path)] = lambda body, request: (200, {"operations": None})
    assert tool.run("task", "operations", "--task", task)[1]["reason"] == "response_invalid"
    # an operation of another task, a wrong ID, an unknown one
    other = add_operation(hub, new_task(tool, wg, "Other"), session)
    assert tool.run("task", "operations", "--task", task, "--operation", other)[1]["reason"] == "response_invalid"
    assert tool.run("task", "operations", "--task", task, "--operation", "x")[1]["error"] == "invalid_argument"
    assert tool.run("task", "operations", "--task", task, "--operation", uuid7())[1]["hub_error"] == "not_found"
    hub.hooks[("GET", f"/v1/tasks/{task}")] = lambda body, request: (200, {"task": {"task_id": task}})
    assert tool.run("task", "operations", "--task", task)[1]["reason"] == "response_invalid"


def test_terminal_output_as_text(tool: Tool, hub: HubFake, account: User) -> None:
    org, wg, device = assign_device(tool, hub, account)
    task = new_task(tool, wg)
    session = add_session(hub, task, device=device)
    place = {"wg": wg, "task": task, "session": session}
    chunk = {
        "offset_ms_start": 0,
        "frames": [[5, "o", "x"], [0, "o", "\x1b[1mhi\x1b[0m\r\n"], [7, "m", "restart"]],
        "cols": 80,
        "rows": 24,
    }
    device_event(hub, account, device, "session.terminal", chunk, **place)
    device_event(hub, account, device, "session.terminal", {**chunk, "frames": []}, **place)
    device_event(hub, account, device, "session.terminal", {**chunk, "frames": [[-1, "o", "y"]]}, **place)
    device_event(hub, account, device, "session.terminal", {**chunk, "frames": [[1, "o", 7]]}, **place)
    code, shown = tool.run("session", "terminal", "--session", session)
    assert code == 0, shown
    assert (shown["output"], shown["text"]) == ("\x1b[1mhi\x1b[0m\r\nx", "hi\nx")
    assert (shown["chunks"], shown["unverified"], shown["locked"]) == (1, 3, 0)
    last = shown["next_after"]
    device_event(hub, account, device, "session.terminal", {**chunk, "frames": [[9, "o", "\x1b]0;t\x07z\r"]]}, **place)
    more = tool.run("session", "terminal", "--session", session, "--after", str(last))[1]
    assert (more["output"], more["text"], more["next_after"]) == ("\x1b]0;t\x07z\r", "z\n", last + 1)
    # a chunk of another session that the hub hands over, and a chunk the session's device did not write
    other = add_session(hub, task, device=device)
    foreign = device_event(hub, account, device, "session.terminal", chunk, wg=wg, task=task, session=other)
    hub.hooks[("GET", f"/v1/scopes/{wg}/events")] = lambda body, request: (
        200,
        {"events": [foreign, "x"], "next_after": None},
    )
    assert tool.run("session", "terminal", "--session", session)[1]["unverified"] == 2
    pages = iter([(200, {"events": [], "next_after": 50}), (200, {"events": [], "next_after": None})])
    hub.hooks[("GET", f"/v1/scopes/{wg}/events")] = lambda body, request: next(pages)
    assert tool.run("session", "terminal", "--session", session)[1]["next_after"] == 0
    hub.hooks[("GET", f"/v1/scopes/{wg}/events")] = lambda body, request: (200, {"events": None})
    assert tool.run("session", "terminal", "--session", session)[1]["reason"] == "response_invalid"
    del hub.hooks[("GET", f"/v1/scopes/{wg}/events")]
    state_of(hub).rows[session]["device_id"] = uuid7()
    assert tool.run("session", "terminal", "--session", session)[1]["unverified"] == 5
    state_of(hub).rows[session]["device_id"] = None
    # without the workgroup key every chunk is locked
    hub.wraps[:] = [wrap for wrap in hub.wraps if wrap["scope"] != "wg" or wrap["recipient_id"] != account.id]
    locked = tool.run("session", "terminal", "--session", session)[1]
    assert (locked["locked"], locked["chunks"], locked["unverified"]) == (5, 0, 0)
    assert tool.run("session", "terminal", "--session", session, "--after", "-1")[1]["error"] == "invalid_argument"
    assert org


def test_task_overview(tool: Tool, hub: HubFake, account: User) -> None:
    org, wg, device = assign_device(tool, hub, account)
    second = tool.run("wg", "create", "--org", org, "--name", "Ops")[1]["wg_id"]
    first_task = new_task(tool, wg, "Fix the login bug")
    other_task = new_task(tool, second, "Deploy the app")
    device_event(hub, account, device, "task.meta", {"title": "Login bug, renamed"}, wg=wg, task=first_task)
    # a task the hub lists without any title event, and one titled by the device only
    bare = uuid7()
    hub.tasks[bare] = {"task": {**hub.tasks[first_task]["task"], "task_id": bare, "last_activity_at": "1"}}
    titled = uuid7()
    hub.tasks[titled] = {"task": {**hub.tasks[first_task]["task"], "task_id": titled, "last_activity_at": "0"}}
    device_event(hub, account, device, "task.meta", {"title": "Device"}, wg=wg, task=titled)
    device_event(hub, account, device, "task.meta", {"title": 5}, wg=wg, task=titled)
    code, overview = tool.run("task", "overview", "--org", org)
    assert code == 0, overview
    rows = overview["tasks"]
    assert [row["task_id"] for row in rows] == [other_task, first_task, bare, titled]
    assert [row["title"] for row in rows] == ["Deploy the app", "Login bug, renamed", None, "Device"]
    assert [row["owner_id"] for row in rows] == [account.id, account.id, None, None]
    assert rows[1]["workgroup"] == "Default"
    mine = tool.run("task", "overview", "--org", org, "--mine", "--search", "LOGIN")[1]["tasks"]
    assert [row["task_id"] for row in mine] == [first_task]
    assert tool.run("task", "overview", "--org", org, "--state", "closed")[1]["tasks"] == []
    # paging of a workgroup's list; rows of another workgroup are left out
    path = f"/v1/workgroups/{second}/tasks"
    hub.hooks[("GET", path)] = lambda body, request: (
        200,
        {"tasks": [hub.tasks[first_task]["task"]], "next_before": "c"},
    )
    paged = tool.run("task", "overview", "--org", org)[1]["tasks"]
    assert other_task not in [row["task_id"] for row in paged]
    assert len([request for request in hub.requests if request.path == path]) == 3 + sessions.OVERVIEW_PAGES
    assert tool.run("task", "overview", "--org", "x")[1]["error"] == "invalid_argument"
    assert private_org(hub, account) == org


def test_screen_text() -> None:
    assert sessions.screen_text("a\x1b[2Kb\x1b(Bc\x07\r\nd\re") == "abc\nd\ne"
    assert sessions.search_text("  ÄBC ") == "äbc"


def test_read_only_members_cannot_close_or_control(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    other, _bob, _org, wg = reader(tool, hub, tmp_path)
    task = new_task(tool, wg)
    session = add_session(hub, task)
    assert other.run("task", "close", "--task", task)[1]["hub_error"] == "forbidden"
    assert other.run("session", "stop", "--session", session)[1]["hub_error"] == "forbidden"
    assert other.run("session", "show", "--session", session)[1]["session"]["state"] == "working"
