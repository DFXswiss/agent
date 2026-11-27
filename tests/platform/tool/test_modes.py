"""Working modes of a task (KOR-75, ADR-098 sections 1 to 3; E-04: what the web offers): `modes list`,
`task create --mode`, `task mode on|off`, the modes of `task show` and the MCP tool `set_task_mode`, against the hub
double with the mode routes of the hub."""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent_platform import connector, errors, modes, relay
from agent_platform.account import Account, Names
from agent_platform.errors import PlatformError
from agent_runner.crypto.ids import uuid7
from agent_runner.sync import codes as sync_codes

from .conftest import Tool
from .hubfake import HubFake, User
from .seeding import default_wg

MODE_ROUTE = r"/v1/tasks/(?P<task>[0-9a-f-]+)/modes/(?P<mode>[a-z0-9_]+)"
TEST_MODE = {
    "name": "test",
    "version": 1,
    "title": {"de": "Testmodus", "en": "Test mode"},
    "needs": {"modes": [], "prerequisites": []},
}
NEEDY = {"name": "needy", "title": {"en": "Needy"}, "needs": {"modes": ["test"], "prerequisites": ["github"]}}
BARE = {"name": "bare"}
ODD = {"name": "odd", "title": {"de": "Nur deutsch"}, "needs": {"modes": None, "prerequisites": "x"}}


def task_mode(name: str, chosen_by: str, way: str = "tool") -> dict[str, Any]:
    return {
        "mode": name,
        "version": 1,
        "chosen_by": chosen_by,
        "way": way,
        "origin_task": None,
        "changed_at": "2026-11-26T10:00:00.000Z",
    }


def mode_routes(hub: HubFake, user: str = "0193002d-0000-7000-8000-000000000001") -> list[tuple[str, str, Any]]:
    """PUT and DELETE of the hub's mode routes; each call is recorded as `(method, path, body)`."""
    seen: list[tuple[str, str, Any]] = []

    def put(request: Any, body: Any, task: str, mode: str) -> tuple[int, Any]:
        seen.append((request.method, request.path, body))
        return 200, {"mode": task_mode(mode, user)}

    def delete(request: Any, body: Any, task: str, mode: str) -> tuple[int, Any]:
        seen.append((request.method, request.path, body))
        return 200, {}

    hub.add_route("PUT", MODE_ROUTE, put)
    hub.add_route("DELETE", MODE_ROUTE, delete)
    return seen


@pytest.fixture
def mode_codes() -> None:
    """The hub codes of ADR-098 are in the signed client's closed list (as `contracts/enums/error-codes.json`)."""
    assert {"mode_needs", "mode_needs_instruction"} <= sync_codes.HUB_ERROR_CODES


# --- the catalogue ---


def test_modes_list(tool: Tool, hub: HubFake, account: User) -> None:
    rows: list[Any] = [TEST_MODE, NEEDY, BARE, ODD, "junk", {"name": 1}]
    hub.add_route("GET", r"/v1/modes", lambda request, body: (200, {"modes": rows}))
    code, listed = tool.run("modes", "list")
    assert code == 0, listed
    assert listed["modes"] == [TEST_MODE, NEEDY, BARE, ODD]
    assert listed["lines"] == [
        "test  Test mode  needs: nothing",
        "needy  Needy  needs: test, github",
        "bare  bare  needs: nothing",
        "odd  odd  needs: nothing",
    ]
    assert account.id


def test_modes_list_refuses_a_malformed_answer(tool: Tool, hub: HubFake, account: User) -> None:
    hub.add_route("GET", r"/v1/modes", lambda request, body: (200, {"modes": None}))
    assert tool.run("modes", "list")[1]["reason"] == "response_invalid"
    assert account.id


def test_names() -> None:
    assert modes.mode_names(["test", "a38", "test"]) == ["test", "a38"]
    for bad in (["Bad"], ["1x"], ["a" * 33], [f"m{index}" for index in range(modes.MAX_MODES + 1)]):
        with pytest.raises(PlatformError) as raised:
            modes.mode_names(bad)
        assert raised.value.fields["reason"] == "mode_invalid"


# --- switching on and off ---


def test_task_mode_on_and_off(tool: Tool, hub: HubFake, account: User) -> None:
    seen = mode_routes(hub, account.id)
    task = uuid7()
    chosen = ("--mode", "test", "--mode", "needy", "--mode", "test")
    code, switched = tool.run("task", "mode", "on", "--task", task, *chosen)
    assert code == 0, switched
    assert switched == {
        "ok": True,
        "task_id": task,
        "modes": [task_mode("test", account.id), task_mode("needy", account.id)],
    }
    assert seen == [
        ("PUT", f"/v1/tasks/{task}/modes/test", {}),
        ("PUT", f"/v1/tasks/{task}/modes/needy", {}),
    ]
    code, off = tool.run("task", "mode", "off", "--task", task, "--mode", "needy")
    assert code == 0, off
    assert off == {"ok": True, "task_id": task, "off": ["needy"]}
    assert seen[-1] == ("DELETE", f"/v1/tasks/{task}/modes/needy", None)


def test_task_mode_bad_input(tool: Tool, hub: HubFake, account: User) -> None:
    seen = mode_routes(hub, account.id)
    task = uuid7()
    for argv in (
        ("task", "mode", "on", "--task", "x", "--mode", "test"),
        ("task", "mode", "off", "--task", "x", "--mode", "test"),
        ("task", "mode", "on", "--task", task),
    ):
        assert tool.run(*argv)[1]["error"] == "invalid_argument", argv
    for direction in ("on", "off"):
        document = tool.run("task", "mode", direction, "--task", task, "--mode", "Bad")[1]
        assert (document["error"], document["reason"]) == ("invalid_argument", "mode_invalid")
    assert seen == []


def test_task_mode_refuses_a_malformed_answer(tool: Tool, hub: HubFake, account: User) -> None:
    task = uuid7()
    for answer in ({"mode": None}, {"mode": task_mode("other", account.id)}):

        def answered(body: Any, request: Any, found: dict[str, Any] = answer) -> tuple[int, dict[str, Any]]:
            return 200, found

        hub.hooks[("PUT", f"/v1/tasks/{task}/modes/test")] = answered
        assert tool.run("task", "mode", "on", "--task", task, "--mode", "test")[1]["reason"] == "response_invalid"


@pytest.mark.usefixtures("mode_codes")
def test_task_mode_refusals(tool: Tool, hub: HubFake, account: User) -> None:
    mode_routes(hub, account.id)
    task = uuid7()
    path = f"/v1/tasks/{task}/modes/needy"
    # an agent inside a platform session
    hub.hooks[("PUT", path)] = lambda body, request: (403, {"error": "mode_needs_instruction"})
    document = tool.run("task", "mode", "on", "--task", task, "--mode", "needy")[1]
    assert (document["error"], document["hub_error"], document["reason"]) == (
        "hub_refused",
        "mode_needs_instruction",
        "mode_needs_instruction",
    )
    assert document["next"] == errors.NEXT_FOR_REASON[("hub_refused", "mode_needs_instruction")]
    assert "task.mode" in document["next"]
    # a mode that needs another that is off: the modes the detail names, and the ones already switched on
    needs = {"error": "mode_needs", "detail": {"modes": ["test"]}}
    hub.hooks[("PUT", path)] = lambda body, request: (409, needs)
    document = tool.run("task", "mode", "on", "--task", task, "--mode", "other", "--mode", "needy")[1]
    assert (document["error"], document["reason"], document["missing"]) == ("hub_refused", "mode_needs", ["test"])
    assert document["switched_on"] == ["other"]
    assert document["next"] == errors.NEXT_FOR_REASON[("hub_refused", "mode_needs")]
    hub.hooks[("PUT", path)] = lambda body, request: (409, {"error": "mode_needs"})
    document = tool.run("task", "mode", "on", "--task", task, "--mode", "needy")[1]
    assert (document["missing"], "switched_on" in document) == ([], False)
    # an unknown task or mode
    hub.hooks[("PUT", path)] = lambda body, request: (404, {"error": "not_found"})
    document = tool.run("task", "mode", "on", "--task", task, "--mode", "needy")[1]
    assert (document["error"], document["reason"]) == ("not_found", "mode_or_task")
    assert document["next"] == errors.NEXT_FOR_REASON[("not_found", "mode_or_task")]
    hub.hooks[("DELETE", path)] = lambda body, request: (404, {"error": "not_found"})
    document = tool.run("task", "mode", "off", "--task", task, "--mode", "needy")[1]
    assert (document["error"], document["reason"]) == ("not_found", "mode_or_task")
    # any other refusal stays as the hub sent it
    hub.hooks[("DELETE", path)] = lambda body, request: (403, {"error": "forbidden"})
    document = tool.run("task", "mode", "off", "--task", task, "--mode", "needy")[1]
    assert (document["error"], document["hub_error"], "reason" in document) == ("hub_refused", "forbidden", False)


def test_refusal_reads_the_missing_modes_from_the_detail() -> None:
    def needs(detail: dict[str, object]) -> object:
        error = PlatformError("hub_refused", hub_error="mode_needs", status=409)
        error.detail = detail
        return modes.refusal(error).fields["missing"]

    assert needs({"modes": ["a38", "orchestrator"]}) == ["a38", "orchestrator"]
    assert needs({"modes": ["Bad"]}) == []
    assert needs({"modes": [1]}) == []
    assert needs({"modes": "test"}) == []
    assert needs({}) == []
    other = PlatformError("hub_refused", hub_error="not_found", status=404)
    assert modes.refusal(other) is other
    assert modes.refusal(other, unknown=True).code == "not_found"


# --- task create --mode and task show ---


def created_bodies(hub: HubFake) -> list[dict[str, Any]]:
    """The bodies of every `POST /v1/tasks` the hub received."""
    wanted = ("POST", "/v1/tasks")
    return [json.loads(request.raw) for request in hub.requests if (request.method, request.path) == wanted]


@pytest.mark.usefixtures("mode_codes")
def test_create_with_modes_and_show_them(
    tool: Tool, hub: HubFake, account: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    wg = default_wg(hub, account)
    code, created = tool.run("task", "create", "--wg", wg, "--text", "Hello", "--mode", "test", "--mode", "test")
    assert code == 0, created
    assert created["modes"] == ["test"]
    assert created_bodies(hub)[-1]["modes"] == ["test"]
    task = created["task_id"]
    # without a mode the request and the answer carry none, and `task show` says so
    plain = tool.run("task", "create", "--wg", wg, "--text", "Plain")[1]
    assert "modes" not in plain
    assert "modes" not in created_bodies(hub)[-1]
    shown = tool.run("task", "show", "--task", plain["task_id"])[1]
    assert (shown["modes"], shown["modes_line"]) == ([], "Modes: none")
    # the hub lists the task's modes; the chooser by name where the org names them, else by ID
    other = "0193002d-0000-7000-8000-000000000002"
    hub.tasks[task]["task"]["modes"] = [task_mode("test", account.id), task_mode("needy", other, "web"), "junk"]
    with monkeypatch.context() as patch:
        patch.setattr(Account, "names", lambda self, org: Names(members={account.id: "Ada"}))
        code, shown = tool.run("task", "show", "--task", task)
    assert code == 0, shown
    assert shown["modes"] == [
        {**task_mode("test", account.id), "chosen_by_name": "Ada"},
        {**task_mode("needy", other, "web"), "chosen_by_name": None},
    ]
    assert shown["modes_line"] == f"Modes: test (tool, chosen by Ada), needy (web, chosen by {other})"
    # refusals of the creation
    hub.hooks[("POST", "/v1/tasks")] = lambda body, request: (403, {"error": "mode_needs_instruction"})
    document = tool.run("task", "create", "--wg", wg, "--text", "Hello", "--mode", "test")[1]
    assert (document["error"], document["reason"]) == ("hub_refused", "mode_needs_instruction")
    needs = {"error": "mode_needs", "detail": {"modes": ["a"]}}
    hub.hooks[("POST", "/v1/tasks")] = lambda body, request: (409, needs)
    document = tool.run("task", "create", "--wg", wg, "--text", "Hello", "--mode", "needy")[1]
    assert (document["reason"], document["missing"]) == ("mode_needs", ["a"])
    hub.hooks[("POST", "/v1/tasks")] = lambda body, request: (404, {"error": "not_found"})
    document = tool.run("task", "create", "--wg", wg, "--text", "Hello", "--mode", "needy")[1]
    assert (document["error"], document["hub_error"]) == ("hub_refused", "not_found")
    assert tool.run("task", "create", "--wg", wg, "--text", "Hello", "--mode", "Bad")[1]["reason"] == "mode_invalid"


# --- the MCP tool and the session ---


def test_set_task_mode_through_the_mcp_server(tool: Tool, hub: HubFake, account: User) -> None:
    seen = mode_routes(hub, account.id)
    task = uuid7()
    assert [item["name"] for item in connector.TOOLS] == ["search", "run", "set_task_mode"]
    switched = connector.tool_set_task_mode({"task_id": task, "mode": "test", "on": True}, tool.env)
    assert switched == {"ok": True, "task_id": task, "modes": [task_mode("test", account.id)]}
    off = connector.tool_set_task_mode({"task_id": task, "mode": "test", "on": False}, tool.env)
    assert off == {"ok": True, "task_id": task, "off": ["test"]}
    assert [(method, path) for method, path, _body in seen] == [
        ("PUT", f"/v1/tasks/{task}/modes/test"),
        ("DELETE", f"/v1/tasks/{task}/modes/test"),
    ]
    # through the protocol, with the result of the command line
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "set_task_mode", "arguments": {"task_id": task, "mode": "test", "on": False}},
    }
    reply = connector.answer(message, tool.env)
    assert reply is not None
    assert reply["result"]["structuredContent"] == off
    assert reply["result"]["isError"] is False
    # a task ID that looks like an option stays the value of --task
    assert connector.tool_set_task_mode({"task_id": "-x", "mode": "test", "on": True}, tool.env)["error"] == (
        "invalid_argument"
    )


def test_set_task_mode_refuses_arguments_outside_its_schema() -> None:
    bad = PlatformError("invalid_argument").to_json()
    task = "0193002d-0000-7000-8000-000000000001"
    for arguments in (
        {},
        {"task_id": task, "mode": "test"},
        {"task_id": 1, "mode": "test", "on": True},
        {"task_id": task, "mode": None, "on": True},
        {"task_id": task, "mode": "test", "on": "yes"},
        {"task_id": task, "mode": "test", "on": True, "quote": "x"},
    ):
        assert connector.tool_set_task_mode(arguments, {}) == bad, arguments


def test_a_session_may_list_the_modes() -> None:
    assert not relay.refused_in_session(["modes", "list"])
    assert not relay.refused_in_session(["task", "mode", "on", "--task", "x", "--mode", "test"])
