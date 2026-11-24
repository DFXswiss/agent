"""Held operations in the task's chat (KOR-25, ADR-066) and the further chat and runtime views of KOR-40 against the
hub double: the waiting events of a session folded into one card per reason with its state (`waiting.ts`), the guest's
errors (`session.error`), the runtime of a task and of a session and the reachability of a session's device."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_platform import sessions, waiting
from agent_runner.crypto.encoding import format_time
from agent_runner.crypto.ids import uuid7
from agent_runner.crypto.log.state import DEVICE, USER

from .conftest import Tool
from .fakes.membership import extra
from .fakes.sessions import add_session, device_event, state_of
from .hubfake import HubFake, User, now_ms_text
from .seeding import assign_device, new_task, reader
from .teams import grant_platform_access
from .test_devicereq import GITHUB_BODY, device_record


def held(
    operation: str, *, kind: str = "github.push", repo: str | None = "acme/app", minutes: int = 10
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "state": "waiting",
        "waiting_for": "github_connection",
        "operation_id": operation,
        "operation_type": kind,
        "until": now_ms_text(timedelta(minutes=minutes)),
    }
    return body if repo is None else {**body, "repo": repo}


def register(hub: HubFake, task: str, operation: str, state: str) -> None:
    """The operation as the hub lists it once the runner registered it."""
    row = hub.tasks[task]["task"]
    state_of(hub).operations[operation] = {
        "operation": {
            "operation_id": operation,
            "org_id": row["org_id"],
            "wg_id": row["wg_id"],
            "task_id": task,
            "session_id": None,
            "type": "github.push",
            "state": state,
            "error_code": None,
            "created_at": now_ms_text(),
        },
        "transitions": [],
    }


def cards(document: dict[str, Any]) -> list[dict[str, Any]]:
    return [event["waiting"] for event in document["events"] if "waiting" in event]


def test_waiting_cards_of_the_owner(tool: Tool, hub: HubFake, account: User) -> None:
    _org, wg, device = assign_device(tool, hub, account)
    task = new_task(tool, wg)
    session = add_session(hub, task, device=device)
    place = {"wg": wg, "task": task, "session": session}
    first, second = uuid7(), uuid7()
    device_event(hub, account, device, "session.status", held(first), **place)
    device_event(hub, account, device, "session.status", {**held(uuid7()), "text": "click here"}, **place)
    device_event(hub, account, device, "session.error", {"code": "gateway_route"}, **place)
    device_event(hub, account, device, "session.status", held(second, kind="github.create_issue", repo=None), **place)
    device_event(hub, account, device, "session.status", held(second, minutes=5), **place)
    code, shown = tool.run("task", "show", "--task", task)
    assert code == 0, shown
    kinds = [event["kind"] for event in shown["events"]]
    assert kinds == ["prompt", "session.status", "session.error", "session.status"]
    # the invalid waiting body stays a plain event; the valid ones are one card at the place of the newest
    assert "waiting" not in shown["events"][1]
    (card,) = cards(shown)
    assert (card["reason"], card["operations"], card["repo"]) == ("github_connection", [first, second], "acme/app")
    assert (card["operation_type"], card["status"], card["owner_id"], card["is_owner"]) == (
        "github.push",
        "open",
        account.id,
        True,
    )
    assert (card["next"], card["reconnect"]) == ("github connect", False)
    assert card["until_ms"] > card["since_ms"]
    assert shown["events"][2]["body"] == {"code": "gateway_route"}
    # a connection older than the newest waiting item is the one the runner could not use
    device_record(hub, account, device, "github", {**GITHUB_BODY, "created_at": "2020-01-01T00:00:00.000Z"})
    (stale,) = cards(tool.run("task", "show", "--task", task)[1])
    assert (stale["status"], stale["reconnect"]) == ("open", True)
    # a record that does not open is not usable either
    device_record(hub, account, device, "github", {**GITHUB_BODY, "login": "bad login"})
    assert cards(tool.run("task", "show", "--task", task)[1])[0]["reconnect"] is True
    # connected after the newest waiting item: done
    device_record(hub, account, device, "github", {**GITHUB_BODY, "created_at": now_ms_text(timedelta(seconds=5))})
    (done,) = cards(tool.run("task", "show", "--task", task)[1])
    assert (done["status"], "next" in done) == ("done", False)
    # follow folds the new events of its answer the same way (the owner read from the task's `task.meta`)
    followed = tool.run("task", "follow", "--task", task, "--wait", "0")[1]
    assert [card["status"] for card in cards(followed)] == ["done"]


def test_waiting_cards_end_with_the_operations_or_the_time(tool: Tool, hub: HubFake, account: User) -> None:
    _org, wg, device = assign_device(tool, hub, account)
    task = new_task(tool, wg)
    session = add_session(hub, task, device=device)
    place = {"wg": wg, "task": task, "session": session}
    first, second = uuid7(), uuid7()
    device_event(hub, account, device, "session.status", held(first), **place)
    device_event(hub, account, device, "session.status", held(second), **place)
    register(hub, task, first, "succeeded")
    assert cards(tool.run("task", "show", "--task", task)[1])[0]["status"] == "open"
    register(hub, task, second, "denied")
    assert cards(tool.run("task", "show", "--task", task)[1])[0]["status"] == "done"
    register(hub, task, first, "rejected")
    assert cards(tool.run("task", "show", "--task", task)[1])[0]["status"] == "expired"
    # after `until` an unregistered operation expires
    later = new_task(tool, wg, "Another one")
    device_event(hub, account, device, "session.status", held(uuid7(), minutes=-1), wg=wg, task=later, session=session)
    (expired,) = cards(tool.run("task", "show", "--task", later)[1])
    assert (expired["status"], "next" in expired) == ("expired", False)


def test_waiting_card_of_another_member(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    other, bob, org, wg = reader(tool, hub, tmp_path)
    grant_platform_access(hub, org)
    device, seq = hub.certify_device()
    hub.candidates[wg] = [{"device_id": device, "platform_seq": seq}]
    assert tool.run("device", "assign", "--wg", wg)[1]["ok"] is True
    task = new_task(tool, wg)
    session = add_session(hub, task, device=device)
    device_event(hub, account, device, "session.status", held(uuid7(), repo=None), wg=wg, task=task, session=session)
    calls = len(hub.requests)
    (card,) = cards(other.run("task", "show", "--task", task)[1])
    assert (card["status"], card["owner_id"], card["is_owner"], "next" in card) == ("open", account.id, False, False)
    assert card["repo"] is None
    # the connection of somebody else is never read
    assert not any(request.path.startswith("/v1/me/credentials") for request in hub.requests[calls:])
    assert bob.id != account.id


def test_parse_waiting_follows_the_contract() -> None:
    good = held(uuid7())
    assert waiting.parse_waiting(good) is not None
    assert waiting.parse_waiting({k: v for k, v in good.items() if k != "repo"}) is not None
    for broken in (
        "x",
        {**good, "state": "failed"},
        {**good, "text": "x"},
        {k: v for k, v in good.items() if k != "until"},
        {**good, "waiting_for": "approval"},
        {**good, "waiting_for": ["github_connection"]},
        {**good, "operation_id": "x"},
        {**good, "operation_type": "push"},
        {**good, "operation_type": 5},
        {**good, "operation_type": "github." + "a" * 60},
        {**good, "repo": "not a repo"},
        {**good, "repo": 5},
        {**good, "until": "soon"},
    ):
        assert waiting.parse_waiting(broken) is None, broken


def test_the_owner_of_a_task() -> None:
    principals = {"u": SimpleNamespace(kind=USER), "d": SimpleNamespace(kind=DEVICE)}
    account: Any = SimpleNamespace(org_log=lambda org: SimpleNamespace(state=SimpleNamespace(principals=principals)))

    def owner(*authors: str) -> str | None:
        metas = [{"seq": index, "author": author} for index, author in enumerate(authors)]
        return waiting.task_owner(account, "o", "w", "t", metas)

    assert owner("d", "u") == "u"
    assert owner("x", "u") is None
    assert owner("d") is None
    assert owner() is None


def test_runtime_of_tasks_and_sessions(tool: Tool, hub: HubFake, account: User) -> None:
    _org, wg, device = assign_device(tool, hub, account)
    task = new_task(tool, wg)
    # every stamp is relative to one moment: a closed runtime is exact however long the tool calls take
    base = datetime.now(UTC)

    def ago(seconds: int) -> str:
        return format_time(base - timedelta(seconds=seconds))

    def since(seconds: int) -> int:
        """`seconds` plus the time that has passed since `base`: the largest runtime a running object can show now."""
        return seconds + math.ceil((datetime.now(UTC) - base).total_seconds())

    row = hub.tasks[task]["task"]
    row["created_at"] = ago(90)
    shown = tool.run("task", "show", "--task", task)[1]
    assert 90 <= shown["runtime_s"] <= since(90)
    assert shown["task"]["closed_at"] is None
    row["closed_at"] = ago(30)
    assert tool.run("task", "show", "--task", task)[1]["runtime_s"] == 60
    session = add_session(hub, task, device=device)
    stored = state_of(hub).rows[session]
    stored |= {
        "created_at": ago(50),
        "first_working_at": ago(20),
    }
    shown = tool.run("session", "show", "--session", session)[1]
    assert 20 <= shown["runtime_s"] <= since(20)
    assert shown["device_reachable"] is False
    extra(hub).online[device] = True
    assert tool.run("session", "show", "--session", session)[1]["device_reachable"] is True
    # an ended session runs to its end; without one its runtime is unknown
    stored |= {"state": "stopped", "first_working_at": None, "ended_at": ago(10)}
    assert tool.run("session", "show", "--session", session)[1]["runtime_s"] == 40
    stored["ended_at"] = None
    assert tool.run("session", "show", "--session", session)[1]["runtime_s"] is None
    # without a device, or when the hub's device list does not name it or cannot be read, reachability is unknown
    stored["device_id"] = uuid7()
    assert tool.run("session", "show", "--session", session)[1]["device_reachable"] is None
    stored["device_id"] = device
    hub.hooks[("GET", f"/v1/orgs/{row['org_id']}/devices")] = lambda body, request: (500, {"error": "unavailable"})
    assert tool.run("session", "show", "--session", session)[1]["device_reachable"] is None
    stored["device_id"] = None
    assert tool.run("session", "show", "--session", session)[1]["device_reachable"] is None


@pytest.mark.parametrize(
    ("start", "end", "running", "expected"),
    [
        ("2026-11-24T08:00:00.000Z", "2026-11-24T08:01:01.500Z", False, 61),
        ("2026-11-24T08:00:00.000Z", None, False, None),
        ("soon", None, True, None),
        ("2026-11-24T08:01:00.000Z", "2026-11-24T08:00:00.000Z", False, 0),
    ],
)
def test_runtime_seconds(start: str, end: str | None, running: bool, expected: int | None) -> None:
    assert sessions.runtime_seconds(start, end, running, 0.0) == expected


def access(operation: str, step: dict[str, str], *, for_user: str | None = None) -> dict[str, Any]:
    """A `github_access` waiting body (KOR-65) with its next step."""
    body = {**held(operation, repo="third-org/app"), "waiting_for": "github_access", "next": step}
    return body if for_user is None else {**body, "for_user": for_user}


APPROVE = {"action": "approve_app", "owner": "third-org", "url": "https://github.com/settings/applications"}


def test_access_cards_name_the_one_next_step(tool: Tool, hub: HubFake, account: User) -> None:
    _org, wg, device = assign_device(tool, hub, account)
    task = new_task(tool, wg)
    session = add_session(hub, task, device=device)
    place = {"wg": wg, "task": task, "session": session}
    first = uuid7()
    device_event(hub, account, device, "session.status", access(first, APPROVE, for_user=account.id), **place)
    (card,) = cards(tool.run("task", "show", "--task", task)[1])
    assert (card["reason"], card["for_user"], card["owner_id"], card["is_owner"]) == (
        "github_access",
        account.id,
        account.id,
        True,
    )
    assert (card["action"], card["url"], card["next"]) == ("approve_app", APPROVE["url"], APPROVE["url"])
    # a new connection does not end an access card: the runner checks the repository again
    device_record(hub, account, device, "github", {**GITHUB_BODY, "created_at": now_ms_text(timedelta(seconds=5))})
    assert cards(tool.run("task", "show", "--task", task)[1])[0]["status"] == "open"
    # the step of the newest item counts; "connect" is the command, a token step names the link and the command
    connect = {"action": "connect", "owner": "third-org"}
    device_event(hub, account, device, "session.status", access(uuid7(), connect, for_user=account.id), **place)
    (card,) = cards(tool.run("task", "show", "--task", task)[1])
    assert (card["action"], card["next"], "url" in card) == ("connect", "github connect --app oauth_app", False)
    token = {"action": "new_token", "owner": "third-org", "url": "https://github.com/settings/tokens/new"}
    device_event(hub, account, device, "session.status", access(uuid7(), token, for_user=account.id), **place)
    (card,) = cards(tool.run("task", "show", "--task", task)[1])
    assert card["next"].startswith("create a token at https://github.com/settings/tokens/new, then: printf")
    # a step for another owner is a card of its own
    other = {"action": "connect", "owner": "other-org"}
    device_event(hub, account, device, "session.status", access(uuid7(), other, for_user=account.id), **place)
    assert [card["action"] for card in cards(tool.run("task", "show", "--task", task)[1])] == ["new_token", "connect"]


def test_an_access_card_for_another_person_shows_no_step(tool: Tool, hub: HubFake, account: User) -> None:
    _org, wg, device = assign_device(tool, hub, account)
    task = new_task(tool, wg)
    session = add_session(hub, task, device=device)
    someone = uuid7()
    body = access(uuid7(), APPROVE, for_user=someone)
    device_event(hub, account, device, "session.status", body, wg=wg, task=task, session=session)
    (card,) = cards(tool.run("task", "show", "--task", task)[1])
    assert (card["owner_id"], card["is_owner"], "next" in card, "action" in card) == (someone, False, False, False)


def test_parse_waiting_checks_the_next_step() -> None:
    good = access(uuid7(), APPROVE, for_user=uuid7())
    assert waiting.parse_waiting(good) is not None
    assert waiting.parse_waiting({**good, "next": {"action": "connect", "owner": "o"}}) is not None
    for broken in (
        {**good, "for_user": "x"},
        {**good, "next": "connect"},
        {**good, "next": {"action": "connect"}},
        {**good, "next": {**APPROVE, "extra": 1}},
        {**good, "next": {**APPROVE, "action": "ask"}},
        {**good, "next": {**APPROVE, "owner": "bad owner"}},
        {**good, "next": {**APPROVE, "owner": 5}},
        {**good, "next": {**APPROVE, "url": "javascript:alert(1)"}},
        {**good, "next": {**APPROVE, "url": 5}},
        {**good, "next": {**APPROVE, "url": "https://github.com.evil.example/login"}},
        {**good, "next": {**APPROVE, "url": "https://evil.example/settings/applications"}},
    ):
        assert waiting.parse_waiting(broken) is None, broken
