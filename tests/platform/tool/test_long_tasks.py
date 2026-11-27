"""HOT-29: `task wait` and `task show` of a task whose events together are larger than one answer of the hub may be
(`agent_runner.sync.http.MAX_RESPONSE_BYTES`). They read the chat page by page and from its end; against a hub that
bounds its pages by their count only, a page too large is asked for again with half the count. Every run prints
exactly one JSON object, without a warning of `asyncio` before it."""

from __future__ import annotations

import gc
import json
import logging
from collections.abc import Callable
from typing import Any

import pytest

from agent_platform import optimize, reader, tasks
from agent_platform.errors import PlatformError
from agent_runner.sync.http import MAX_RESPONSE_BYTES

from .conftest import Tool
from .hubfake import HubFake, User
from .test_task_wait import PR_7, PR_9, clocked, working_task

BULK = 180_000
"""Characters of one large tool event (its `ct` is below the hub's 256 KiB)."""
LARGE = 45
"""Large events of the long task: together well above `MAX_RESPONSE_BYTES`."""


def long_task(tool: Tool, hub: HubFake, account: User) -> tuple[str, str, list[int]]:
    """An idle task with an early answer naming PR 7, `LARGE` large tool events and a last answer naming PR 9; its
    workgroup and the positions of the two answers."""
    task, write = working_task(tool, hub, account)
    first = write("session.output", {"message_id": "m1", "role": "assistant", "text": f"Opened {PR_7}", "turn": 1})
    for number in range(LARGE):
        write("session.tool", {"tool_use_id": f"t{number}", "name": "Bash", "input": "x" * BULK})
    last = write("session.output", {"message_id": "m2", "role": "assistant", "text": f"Done: {PR_9}", "turn": 2})
    hub.tasks[task]["task"]["state"] = "idle"
    wg = str(hub.tasks[task]["task"]["wg_id"])
    assert sum(len(json.dumps(item)) for item in hub.events[wg]) > MAX_RESPONSE_BYTES
    return task, wg, [first, last]


def event_limits(hub: HubFake, wg: str) -> list[int]:
    return [int(r.query["limit"]) for r in hub.requests if r.path == f"/v1/scopes/{wg}/events"]


@pytest.fixture
def quiet(caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]) -> Callable[[], None]:
    """A check that nothing of `asyncio` was logged or written to stderr (also once the garbage is collected)."""

    def check() -> None:
        gc.collect()
        assert not [r for r in caplog.records if r.name == "asyncio" and r.levelno >= logging.WARNING]
        err = capfd.readouterr().err
        assert "Task was destroyed" not in err
        assert "never awaited" not in err

    return check


@pytest.mark.parametrize("page_bytes", [None, 1024 * 1024], ids=["count_only_hub", "byte_bounded_hub"])
def test_wait_and_show_answer_for_a_task_larger_than_one_answer(
    tool: Tool, hub: HubFake, account: User, quiet: Callable[[], None], page_bytes: int | None
) -> None:
    hub.event_page_bytes = page_bytes
    task, wg, (first, last) = long_task(tool, hub, account)
    code, waited = tool.run("task", "wait", "--task", task, "--timeout", "0")
    assert code == 0, waited
    assert (waited["state"], waited["timed_out"], waited["answer"]) == ("idle", False, f"Done: {PR_9}")
    assert (waited["pull_requests"], waited["next_after"]) == ([PR_7, PR_9], last)
    code, shown = tool.run("task", "show", "--task", task)
    assert code == 0, shown
    texts = [event["body"].get("text") for event in shown["events"] if event["kind"] == "session.output"]
    assert texts == [f"Opened {PR_7}", f"Done: {PR_9}"]
    assert (len(shown["events"]), shown["next_before"], shown["unverified"]) == (LARGE + 3, None, 0)
    # the newest events only, and the older ones behind `next_before`
    code, newest = tool.run("task", "show", "--task", task, "--limit", "2")
    assert code == 0, newest
    assert [event["kind"] for event in newest["events"]] == ["session.tool", "session.output"]
    assert newest["next_before"] == last - 1
    older = tool.run("task", "show", "--task", task, "--limit", "60", "--before", str(newest["next_before"]))[1]
    assert (older["events"][0]["kind"], older["events"][-1]["seq"], older["next_before"]) == ("prompt", last - 2, None)
    assert len(older["events"]) == LARGE + 1
    assert first in [event["seq"] for event in older["events"]]
    # against a hub that bounds pages by count only, the pages too large were asked for again with half the count
    assert (reader.PAGE // 2 in event_limits(hub, wg)) == (page_bytes is None)
    quiet()


def test_an_answer_too_large_is_one_json_object_without_asyncio_warnings(
    tool: Tool, hub: HubFake, account: User, quiet: Callable[[], None]
) -> None:
    """An answer the signed client refuses as too large ends the command with one JSON object; the byte stream of
    the refused answer is closed at once, so no task of the event loop is left behind."""
    task, _write = working_task(tool, hub, account)
    huge = {"task": {"task_id": task, "pad": "x" * (MAX_RESPONSE_BYTES + 1)}}
    hub.hooks[("GET", f"/v1/tasks/{task}")] = lambda body, request: (200, huge)
    code, document = tool.run("task", "show", "--task", task)
    assert (code, document["error"], document["reason"]) == (1, "hub_refused", "response_too_large")
    quiet()


class Pages:
    """An account whose `call` answers event pages from a script (`(status_or_error, answer)` per request)."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.targets: list[str] = []

    def call(self, method: str, target: str, body: object | None = None) -> dict[str, Any]:
        self.targets.append(target)
        answer = self.answers.pop(0)
        if isinstance(answer, PlatformError):
            raise answer
        return dict(answer)


def item(seq: int) -> dict[str, Any]:
    return {"scope_seq": seq, "event": {}}


TOO_LARGE = PlatformError("hub_refused", reason="response_too_large")


def test_a_page_too_large_is_halved_down_to_one_event() -> None:
    account = Pages(TOO_LARGE, TOO_LARGE, {"events": [item(1)], "next_after": 1}, {"events": [], "next_after": None})
    assert reader.scope_events(account, "s", {"kinds": "prompt"}) == [item(1)]  # type: ignore[arg-type]
    assert [t.split("limit=")[1].split("&")[0] for t in account.targets] == ["1000", "500", "250", "250"]
    # one event still too large, or another refusal, ends the read
    halving = [TOO_LARGE] * 10
    with pytest.raises(PlatformError) as caught:
        reader.scope_events(Pages(*halving, TOO_LARGE), "s", {})  # type: ignore[arg-type]
    assert caught.value.fields["reason"] == "response_too_large"
    refused = PlatformError("hub_refused", hub_error="forbidden", status=403)
    with pytest.raises(PlatformError) as caught:
        reader.scope_events(Pages(refused), "s", {})  # type: ignore[arg-type]
    assert caught.value.fields["hub_error"] == "forbidden"
    with pytest.raises(PlatformError) as caught:
        reader.newest_events(Pages(refused), "s", {}, 3)  # type: ignore[arg-type]
    assert caught.value.fields["hub_error"] == "forbidden"


def test_newest_events_page_back_from_the_end() -> None:
    account = Pages(
        {"events": [item(9), item(8)], "next_before": 8},
        {"events": [item(7)], "next_before": 7},
    )
    tail = reader.newest_events(account, "s", {"kinds": "prompt"}, 3)  # type: ignore[arg-type]
    assert (tail.items, tail.next_before) == ([item(7), item(8), item(9)], 7)
    assert [t.split("&")[-1] for t in account.targets] == [f"before={reader.NEWEST}", "before=8"]
    assert "limit=3" in account.targets[0]
    assert "limit=1" in account.targets[1]
    # the end of the chat, and a hub whose cursor does not go back
    done = reader.newest_events(Pages({"events": [item(2)], "next_before": None}), "s", {}, 5, 3)  # type: ignore[arg-type]
    assert (done.items, done.next_before) == ([item(2)], None)
    stuck = reader.newest_events(Pages({"events": [item(2)], "next_before": 4}), "s", {}, 5, 3)  # type: ignore[arg-type]
    assert (stuck.items, stuck.next_before) == ([item(2)], None)


def test_newest_events_of_a_hub_without_before() -> None:
    """A hub that refuses `before`, or answers it ascending, is read ascending; the newest `count` below `before`."""
    ascending = [{"events": [item(1), item(2), item(3)], "next_after": 3}, {"events": [item(4)], "next_after": None}]
    refusing = Pages(PlatformError("hub_refused", hub_error="request_invalid", status=400), *ascending)
    tail = reader.newest_events(refusing, "s", {}, 2, 4)  # type: ignore[arg-type]
    assert (tail.items, tail.next_before) == ([item(2), item(3)], 2)
    ignoring = Pages(ascending[0], *ascending)
    every = reader.newest_events(ignoring, "s", {}, 5)  # type: ignore[arg-type]
    assert (every.items, every.next_before) == ([item(1), item(2), item(3), item(4)], None)
    assert reader.scope_seq("x") == 0
    assert reader.scope_seq({"scope_seq": True}) == 0


def test_the_answer_is_the_newest_output_with_text(
    tool: Tool, hub: HubFake, account: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`task wait` reads the outputs from the end, `OUTPUT_PAGE` at a time, past outputs without text."""
    monkeypatch.setattr(tasks, "OUTPUT_PAGE", 1)
    task, write = working_task(tool, hub, account)
    write("session.output", {"message_id": "m1", "role": "assistant", "text": "The answer", "turn": 1})
    write("session.output", {"message_id": "m2", "role": "assistant", "turn": 1})
    hub.tasks[task]["task"]["state"] = "idle"
    clocked(monkeypatch, [])
    waited = tool.run("task", "wait", "--task", task, "--timeout", "0")[1]
    assert waited["answer"] == "The answer"
    for argv in (("--limit", "0"), ("--before", "-1")):
        assert tool.run("task", "show", "--task", task, *argv)[1]["error"] == "invalid_argument", argv


def test_adopt_reads_the_newest_output_from_the_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """`optimize adopt` takes the newest output with text (`tasks.task_outputs`); none is `no_output`."""
    outputs = [{"id": "e2", "body": {"text": " "}}, {"id": "e1", "body": {"text": "Report"}}]
    monkeypatch.setattr(optimize, "task_outputs", lambda account, place: iter(outputs))
    assert optimize._newest_output(None, ("o", "w", "t"))["id"] == "e1"  # type: ignore[arg-type]
    monkeypatch.setattr(optimize, "task_outputs", lambda account, place: iter(outputs[:1]))
    with pytest.raises(PlatformError) as caught:
        optimize._newest_output(None, ("o", "w", "t"))  # type: ignore[arg-type]
    assert caught.value.fields["reason"] == "no_output"
