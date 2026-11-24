"""`task queue`, `task prompt --cancel` and the person who triggered an operation or a session (ADR-083, KOR-46)
against the hub double: the queue in delivery order with its withdrawals, the withdrawal of a waiting prompt with the
hub's refusals, and `triggered_by` passed through by `task operations` and `session show`."""

from __future__ import annotations

from typing import Any

from agent_runner.crypto.ids import uuid7

from .conftest import Tool
from .fakes.sessions import add_operation, add_session, state_of
from .hubfake import HubFake, User
from .seeding import default_wg, new_task

QUEUED = {
    "author_id": None,
    "via_agent": False,
    "scope_seq": 3,
    "received_at": "2026-11-24T10:00:00.000Z",
    "session_id": None,
    "device_id": None,
    "state": "waiting",
}


def test_task_queue(tool: Tool, hub: HubFake, account: User) -> None:
    task = new_task(tool, default_wg(hub, account))
    session = add_session(hub, task)
    first, second, gone = uuid7(), uuid7(), uuid7()
    answer: dict[str, Any] = {
        "task_id": task,
        "prompts": [
            {**QUEUED, "event_id": first, "author_id": account.id, "session_id": session},
            "x",
            {
                **QUEUED,
                "event_id": second,
                "author_id": account.id,
                "via_agent": True,
                "scope_seq": 5,
                "session_id": session,
                "state": "waiting_for_device",
            },
        ],
        "withdrawals": [{"event_id": gone, "state": "too_late", "requested_at": "2026-11-24T09:00:00.000Z"}, 5],
    }
    path = ("GET", f"/v1/tasks/{task}/queue")
    hub.hooks[path] = lambda body, request: (200, answer)
    code, shown = tool.run("task", "queue", "--task", task)
    assert code == 0, shown
    assert [(row["position"], row["event_id"], row["via_agent"], row["state"]) for row in shown["prompts"]] == [
        (1, first, False, "waiting"),
        (2, second, True, "waiting_for_device"),
    ]
    assert shown["prompts"][0] == {
        "position": 1,
        **QUEUED,
        "event_id": first,
        "author_id": account.id,
        "session_id": session,
    }
    assert shown["withdrawals"] == [{"event_id": gone, "state": "too_late", "requested_at": "2026-11-24T09:00:00.000Z"}]
    # an answer without withdrawals has none; one without a prompt list is refused
    hub.hooks[path] = lambda body, request: (200, {"task_id": task, "prompts": []})
    assert tool.run("task", "queue", "--task", task)[1]["withdrawals"] == []
    hub.hooks[path] = lambda body, request: (200, {"task_id": task})
    assert tool.run("task", "queue", "--task", task)[1]["reason"] == "response_invalid"
    del hub.hooks[path]
    assert tool.run("task", "queue", "--task", task)[1]["hub_error"] == "not_found"
    assert tool.run("task", "queue", "--task", "x")[1]["error"] == "invalid_argument"


def test_withdraw_a_waiting_prompt(tool: Tool, hub: HubFake, account: User) -> None:
    task = new_task(tool, default_wg(hub, account))
    event = uuid7()
    path = ("POST", f"/v1/tasks/{task}/prompts/{event}/withdraw")
    bodies: list[Any] = []

    def withdraw(body: Any, request: Any) -> tuple[int, Any]:
        bodies.append(body)
        return 200, {"state": "withdrawing"}

    hub.hooks[path] = withdraw
    code, withdrawn = tool.run("task", "prompt", "--task", task, "--cancel", event)
    assert code == 0, withdrawn
    assert withdrawn == {"ok": True, "task_id": task, "event_id": event, "state": "withdrawing"}
    assert bodies == [{}]
    hub.hooks[path] = lambda body, request: (200, {"state": "withdrawn"})
    assert tool.run("task", "prompt", "--task", task, "--cancel", event)[1]["state"] == "withdrawn"
    hub.hooks[path] = lambda body, request: (409, {"error": "peer_outdated"})
    assert tool.run("task", "prompt", "--task", task, "--cancel", event)[1]["hub_error"] == "peer_outdated"
    hub.hooks[path] = lambda body, request: (200, {"state": "too_late"})
    assert tool.run("task", "prompt", "--task", task, "--cancel", event)[1]["reason"] == "response_invalid"
    # exactly one of --text and --cancel; IDs are checked before anything is sent
    for argv in (("--cancel", event, "--text", "x"), (), ("--cancel", "x")):
        assert tool.run("task", "prompt", "--task", task, *argv)[1]["error"] == "invalid_argument", argv
    # without --cancel the prompt is sent as before
    code, prompted = tool.run("task", "prompt", "--task", task, "--text", "Next")
    assert code == 0, prompted
    assert prompted["session_id"]


def test_triggered_by_is_shown(tool: Tool, hub: HubFake, account: User) -> None:
    task = new_task(tool, default_wg(hub, account))
    session = add_session(hub, task)
    operation = add_operation(hub, task, session)
    state_of(hub).rows[session]["triggered_by"] = account.id
    state_of(hub).operations[operation]["operation"]["triggered_by"] = None
    code, listed = tool.run("task", "operations", "--task", task)
    assert code == 0, listed
    assert listed["operations"][0]["triggered_by"] is None
    code, shown = tool.run("session", "show", "--session", session)
    assert code == 0, shown
    assert shown["session"]["triggered_by"] == account.id
