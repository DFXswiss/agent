"""`task wait` against the hub double: it follows the task's chat until the task no longer works or the time is up and
answers once with the state, the last answer, the pull requests and the position for `task follow`."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace

import pytest

from agent_platform import tasks
from agent_runner.crypto.ids import uuid7

from .conftest import Tool
from .fakes.sessions import add_session, device_event
from .hubfake import HubFake, User
from .seeding import assign_device, new_task

PR_7 = "https://github.com/acme/app/pull/7"
PR_9 = "https://github.com/acme/lib.js/pull/9"


class Clock:
    """A clock for `tasks.time`: each sleep moves it on and runs the next scripted step, if any."""

    def __init__(self, steps: list[Callable[[], None]]) -> None:
        self.now = 0.0
        self.steps = steps
        self.sleeps = 0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.now += seconds
        if self.steps:
            self.steps.pop(0)()


def clocked(monkeypatch: pytest.MonkeyPatch, steps: list[Callable[[], None]]) -> Clock:
    clock = Clock(steps)
    monkeypatch.setattr(tasks, "time", SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep, time=lambda: 0.0))
    return clock


def working_task(
    tool: Tool, hub: HubFake, account: User, text: str = "Fix the login bug"
) -> tuple[str, Callable[..., int]]:
    """A working task with a session and a writer of the session's events."""
    _org, wg, device = assign_device(tool, hub, account)
    task = new_task(tool, wg, text)
    session = add_session(hub, task, device=device)
    hub.tasks[task]["task"]["state"] = "working"

    def write(kind: str, body: object, **extra: str) -> int:
        stored = device_event(hub, account, device, kind, body, wg=wg, task=task, session=session, **extra)
        return int(stored["scope_seq"])

    return task, write


def test_wait_returns_once_the_task_is_done(
    tool: Tool, hub: HubFake, account: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    task, write = working_task(tool, hub, account, f"Review {PR_9} please")
    last: list[int] = []

    def progress() -> None:
        write("session.output", {"message_id": "m1", "role": "assistant", "text": "Working on it", "turn": 1})

    def done() -> None:
        result = {"operation_id": uuid7(), "status": "succeeded", "result": {"url": PR_7}}
        write("operation.result", result, ref=uuid7())
        write("session.tool", {"tool_use_id": "t1", "name": "x", "input": f"see {PR_7}/files and {PR_9}"})
        text = f"Done: {PR_7}\n" + "x" * 5000
        last.append(write("session.output", {"message_id": "m2", "role": "assistant", "text": text, "turn": 2}))
        hub.tasks[task]["task"]["state"] = "idle"

    clock = clocked(monkeypatch, [progress, lambda: None, done])
    code, waited = tool.run("task", "wait", "--task", task)
    assert code == 0, waited
    assert (waited["ok"], waited["state"], waited["timed_out"], clock.sleeps) == (True, "idle", False, 3)
    assert waited["answer"] == (f"Done: {PR_7}\n" + "x" * 5000)[: tasks.ANSWER_MAX]
    assert len(waited["answer"]) == tasks.ANSWER_MAX
    # the prompt's address does not count; each address once, in the order the task named them
    assert waited["pull_requests"] == [PR_7, PR_9]
    assert waited["next_after"] == last[0]
    assert "reason" not in waited
    # `task follow` goes on from there
    assert tool.run("task", "follow", "--task", task, "--after", str(last[0]), "--wait", "0")[1]["events"] == []


def test_wait_ends_when_the_time_is_up(
    tool: Tool, hub: HubFake, account: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    task, _write = working_task(tool, hub, account)
    clock = clocked(monkeypatch, [])
    code, waited = tool.run("task", "wait", "--task", task, "--timeout", "7")
    assert code == 0, waited
    assert (waited["state"], waited["timed_out"], waited["answer"], waited["pull_requests"]) == (
        "working",
        True,
        None,
        [],
    )
    assert clock.now == 7
    assert clock.sleeps == 4  # 2 + 2 + 2 + 1 seconds
    assert waited["next_after"] == 2
    # 0 looks once
    assert tool.run("task", "wait", "--task", task, "--timeout", "0")[1]["timed_out"] is True
    assert clock.sleeps == 4
    for value in ("-1", str(tasks.WAIT_MAX_S + 1)):
        assert tool.run("task", "wait", "--task", task, "--timeout", value)[1]["error"] == "invalid_argument"
    assert tool.run("task", "wait", "--task", "x")[1]["error"] == "invalid_argument"


def test_wait_reports_a_failed_task_with_its_reason(
    tool: Tool, hub: HubFake, account: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    task, write = working_task(tool, hub, account)

    def failed() -> None:
        write("session.error", {"code": "gateway_route"})
        hub.tasks[task]["task"] |= {"state": "failed", "state_reason": "model_access"}

    clocked(monkeypatch, [failed])
    code, waited = tool.run("task", "wait", "--task", task, "--timeout", "60")
    assert code == 0, waited
    assert (waited["state"], waited["reason"], waited["timed_out"], waited["answer"]) == (
        "failed",
        "model_access",
        False,
        None,
    )


@pytest.mark.parametrize("state", ["closed", "waiting"])
def test_wait_returns_at_once_for_a_task_that_no_longer_works(
    tool: Tool, hub: HubFake, account: User, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    task, _write = working_task(tool, hub, account)
    hub.tasks[task]["task"]["state"] = state
    clock = clocked(monkeypatch, [])
    waited = tool.run("task", "wait", "--task", task)[1]
    assert (waited["state"], waited["timed_out"], clock.sleeps) == (state, False, 0)


def test_pull_request_addresses() -> None:
    text = f"{PR_7}, {PR_7}#discussion and https://github.com/acme/app/issues/3 or http://github.com/a/b/pull/1"
    assert tasks.PULL_URL.findall(text) == [PR_7, PR_7]
    assert tasks._strings({"a": [1, "x", {"b": "y"}], "c": None}) == ["x", "y"]
