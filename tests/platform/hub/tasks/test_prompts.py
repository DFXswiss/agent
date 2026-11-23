"""U-HUB-tasks (follow-up prompts): `POST /v1/tasks/{task}/prompts` - routing to the main session, hints, new main
sessions after `stopped`/`failed`, reopening, idempotency and rights (03-api.md section 4.6.3; R-2.9)."""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from agent_hub.http.context import HubContext
from agent_hub.sessions import prompts
from agent_hub.stream.publish import MemoryPublisher
from agent_hub.tasks import service
from tests.platform.hub.device.support import org_device_scene
from tests.platform.hub.orgs.support import World, new_id
from tests.platform.hub.tasks.support import Desk, hints


@pytest.mark.parametrize(
    "live",
    [
        (),
        ("vm_assigning", "prompt_delivering", "working"),
        ("vm_assigning", "prompt_delivering", "working", "idle"),
        ("vm_assigning", "prompt_delivering", "working", "idle", "suspended"),
    ],
)
def test_a_live_main_session_gets_the_prompt_hint(
    desk: Desk, published: MemoryPublisher, live: tuple[str, ...]
) -> None:
    task, session = desk.started()
    desk.walk(session, *live)
    published.clear()
    event = desk.prompt_event(desk.scene.writer, task)
    response = desk.prompt(desk.scene.writer, task, event)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["session_id"] == session
    stored = desk.rows("SELECT scope_seq FROM hub.event WHERE id = %s", event["header"]["id"])
    assert body == {"scope_seq": stored[0][0], "session_id": session}
    assert hints(published, desk.scene.device, "prompt") == [
        {"type": "prompt", "session_id": session, "scope_id": desk.wg, "scope_seq": body["scope_seq"]}
    ]
    assert desk.rows("SELECT count(*) FROM hub.session WHERE task_id = %s", task) == [(1,)]


def test_a_waiting_main_session_gets_no_hint(desk: Desk, published: MemoryPublisher) -> None:
    desk.offline(desk.scene.device)
    desk.offline(desk.scene.other_device)
    task, session = desk.started()
    response = desk.prompt(desk.scene.writer, task)
    assert response.json()["session_id"] == session
    assert hints(published) == []
    assert desk.task(task)["state"] == "scheduling"


@pytest.mark.parametrize(("end", "reason"), [("stopped", None), ("failed", "agent_exited"), ("stopping", None)])
def test_an_ended_main_session_is_replaced(
    desk: Desk, published: MemoryPublisher, end: str, reason: str | None
) -> None:
    task, session = desk.started()
    desk.walk(session, "prompt_delivering", "working", "idle")
    desk.transition(session, end, reason)
    desk.hub.clock.advance(5)
    published.clear()
    response = desk.prompt(desk.scene.writer, task)
    body = response.json()
    assert body["session_id"] != session
    new = desk.session(body["session_id"])
    assert (new["depth"], new["state"], new["device_id"], new["estimate_ms"]) == (
        0,
        "requested",
        desk.scene.device.id,
        3000,
    )
    assert hints(published, desk.scene.device, "session.start") == [
        {"type": "session.start", "session_id": body["session_id"]}
    ]
    assert hints(published, kind="prompt") == []
    detail = desk.task(task)
    assert (detail["main_session_id"], detail["state"]) == (body["session_id"], "starting")


def test_an_ended_main_session_without_devices_waits(desk: Desk) -> None:
    task, session = desk.started()
    desk.transition(session, "failed", "vm_start_failed")
    assert desk.task(task)["state"] == "failed"
    desk.execute("UPDATE hub.wg_device SET unassigned_seq = 99 WHERE wg_id = %s", desk.wg)
    desk.hub.clock.advance(1)
    body = desk.prompt(desk.scene.writer, task).json()
    assert desk.session(body["session_id"])["device_id"] is None
    detail = desk.task(task)
    assert (detail["state"], detail["state_reason"]) == ("scheduling", "device_offline")


def test_a_prompt_reopens_a_closed_task(desk: Desk, task_signals: list[dict[str, Any]]) -> None:
    task, session = desk.started()
    assert desk.scene.writer.browser.post(f"/v1/tasks/{task}/close", json={}).status_code == 200
    desk.hub.clock.advance(1)
    body = desk.prompt(desk.scene.writer, task).json()
    assert body["session_id"] != session
    detail = desk.task(task)
    assert (detail["state"], detail["closed_at"]) == ("starting", None)
    states = desk.rows("SELECT state FROM hub.task_transition WHERE task_id = %s ORDER BY seq", task)
    assert [row[0] for row in states] == ["scheduling", "starting", "closed", "starting"]
    assert [item["state"] for item in task_signals][-2:] == ["closed", "starting"]


def test_a_repeated_prompt_is_replayed(desk: Desk, published: MemoryPublisher) -> None:
    task, session = desk.started()
    desk.walk(session, "prompt_delivering", "working", "idle")
    event = desk.prompt_event(desk.scene.writer, task)
    first = desk.prompt(desk.scene.writer, task, event).json()
    desk.transition(session, "failed", "oom")
    desk.hub.clock.advance(1)
    published.clear()
    again = desk.prompt(desk.scene.writer, task, event)
    assert again.status_code == 200
    assert again.json() == {**first, "replayed": True}
    assert published.items() == []
    assert desk.rows("SELECT count(*) FROM hub.session WHERE task_id = %s", task) == [(1,)]


def test_prompt_rights_and_form(desk: Desk) -> None:
    task, _ = desk.started()
    reader, stranger, writer = desk.scene.reader, desk.scene.stranger, desk.scene.writer
    response = desk.prompt(reader, task, desk.prompt_event(writer, task))
    assert (response.status_code, response.json()) == (403, {"error": "forbidden"})
    response = desk.prompt(stranger, task, desk.prompt_event(writer, task))
    assert (response.status_code, response.json()) == (403, {"error": "forbidden"})
    unknown = new_id()
    response = desk.prompt(writer, unknown, desk.prompt_event(writer, unknown))
    assert (response.status_code, response.json()) == (404, {"error": "not_found"})
    response = writer.browser.post("/v1/tasks/not-a-task/prompts", json={"event": desk.prompt_event(writer, task)})
    assert response.status_code == 404
    other = desk.prompt_event(writer, new_id())
    response = desk.prompt(writer, task, other)
    assert response.json() == {"error": "request_invalid", "detail": {"pointers": ["/event/header/task"]}}
    meta = desk.scene.wg_event(writer, "task.meta", task=task)
    response = desk.prompt(writer, task, meta, extensions={"contract": False})
    assert response.json() == {"error": "request_invalid", "detail": {"pointers": ["/event/header/kind"]}}
    foreign = desk.prompt_event(desk.scene.admin, task)
    response = desk.prompt(writer, task, foreign)
    assert (response.status_code, response.json()) == (403, {"error": "forbidden"})


def test_two_followups_are_routed_to_their_sessions(world: World, published: MemoryPublisher) -> None:
    """A prompt belongs to the main session that was current when it arrived."""
    desk = Desk(org_device_scene(world))
    desk.online(desk.scene.device)
    task, first = desk.started()
    early = desk.prompt(desk.scene.writer, task).json()
    desk.transition(first, "stopped")
    desk.hub.clock.advance(2)
    late = desk.prompt(desk.scene.writer, task).json()
    assert early["session_id"] == first
    assert late["session_id"] != first


def _waiting_on_a_lock(desk: Desk) -> bool:
    found = desk.rows(
        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock'"
    )
    return bool(found[0][0])


def test_two_concurrent_followups_on_an_ended_session_go_to_the_new_one(desk: Desk) -> None:
    """A-09: the earlier request reads the hub time first, the later one creates the new main session first; both
    prompts belong to the new session and are pending there (none stays with the ended one, never delivered)."""
    task, first = desk.started()
    desk.walk(first, "prompt_delivering", "working", "idle")
    desk.transition(first, "stopped")
    context: HubContext = desk.hub.app.state.hub
    writer = desk.scene.writer
    start: datetime = desk.hub.clock.advance(5)
    later_done = threading.Event()
    answers: dict[str, dict[str, Any]] = {}
    failures: list[BaseException] = []

    def early_clock() -> datetime:
        if "early_read" not in answers:
            answers["early_read"] = {}
            deadline = time.monotonic() + 30
            while not (later_done.is_set() or _waiting_on_a_lock(desk)):
                assert time.monotonic() < deadline, "the later prompt neither finished nor waited for a lock"
                time.sleep(0.01)
        return start

    def send(name: str, clock: Any, done: threading.Event | None) -> None:
        try:
            outcome = service.add_prompt(
                replace(context, clock=clock), UUID(writer.id), UUID(task), desk.prompt_event(writer, task)
            )
            outcome.announce()
            answers[name] = outcome.body
        except BaseException as error:  # reported by the main thread
            failures.append(error)
        finally:
            if done is not None:
                done.set()

    early = threading.Thread(target=send, args=("early", early_clock, None))
    early.start()
    while "early_read" not in answers:
        time.sleep(0.01)
    later = threading.Thread(target=send, args=("later", lambda: start + timedelta(seconds=1), later_done))
    later.start()
    early.join(60)
    later.join(60)
    assert not early.is_alive(), "the early follow-up did not finish"
    assert not later.is_alive(), "the later follow-up did not finish"
    assert failures == []
    assert answers["early"]["session_id"] == answers["later"]["session_id"] != first
    new = answers["early"]["session_id"]
    with context.pool.connection() as connection:
        routed = prompts.routing(connection, [task])
    assert [p.scope_seq for p in routed[new].pending] == sorted(
        [answers["early"]["scope_seq"], answers["later"]["scope_seq"]]
    )
    assert routed[first].pending == ()


def test_a_followup_is_never_received_before_the_newest_main_session(desk: Desk) -> None:
    """A hub clock behind the newest main session (another hub process, a clock step) does not route a prompt to an
    ended session: the receipt time is the session's creation time at the earliest."""
    task, first = desk.started()
    created = desk.rows("SELECT created_at FROM hub.session WHERE id = %s", first)[0][0]
    desk.walk(first, "prompt_delivering", "working", "idle")
    desk.transition(first, "stopped")
    desk.hub.clock.advance(-60)
    body = desk.prompt(desk.scene.writer, task).json()
    assert body["session_id"] != first
    new = desk.rows("SELECT created_at FROM hub.session WHERE id = %s", body["session_id"])
    assert new == [(created + timedelta(microseconds=1),)]
    with desk.hub.pool.connection() as connection:
        routed = prompts.routing(connection, [task])
    assert [p.scope_seq for p in routed[body["session_id"]].pending] == [body["scope_seq"]]


def _pending(desk: Desk, task: str) -> dict[str, list[str]]:
    with desk.hub.pool.connection() as connection:
        routed = prompts.routing(connection, [task])
    return {session: [p.event_id for p in item.pending] for session, item in routed.items()}


@pytest.mark.parametrize("ending", [("stopped",), ("stopping", "stopped")])
def test_a_followup_to_a_session_the_device_ends_goes_to_the_next_session(
    desk: Desk, published: MemoryPublisher, ending: tuple[str, ...]
) -> None:
    """NA-04: the hub routed a follow-up to a live session that the device ends in the same instant (the prompt is
    not accepted): the hub starts the next main session at once and the prompt is pending there."""
    task, first = desk.started()
    desk.walk(first, "prompt_delivering", "working", "idle")
    event = desk.prompt_event(desk.scene.writer, task)
    assert desk.prompt(desk.scene.writer, task, event).json()["session_id"] == first
    published.clear()
    desk.hub.clock.advance(1)
    desk.walk(first, *ending)
    mains = desk.rows("SELECT id FROM hub.session WHERE task_id = %s AND depth = 0 ORDER BY created_at", task)
    assert len(mains) == 2
    second = str(mains[1][0])
    assert _pending(desk, task)[second] == [event["header"]["id"]]
    assert hints(published, desk.scene.device, "session.start") == [{"type": "session.start", "session_id": second}]
    detail = desk.task(task)
    assert (detail["main_session_id"], detail["state"]) == (second, "starting")
    following = desk.prompt_event(desk.scene.writer, task)
    later = desk.prompt(desk.scene.writer, task, following).json()
    assert later["session_id"] == second
    assert _pending(desk, task)[second] == [event["header"]["id"], following["header"]["id"]]


def test_a_carried_prompt_counts_as_delivered_by_the_next_session(desk: Desk) -> None:
    task, first = desk.started()
    desk.walk(first, "prompt_delivering", "working", "idle")
    event = desk.prompt_event(desk.scene.writer, task)
    desk.prompt(desk.scene.writer, task, event)
    desk.hub.clock.advance(1)
    desk.transition(first, "stopped")
    second = desk.task(task)["main_session_id"]
    desk.walk(second, "prompt_delivering", "working")
    with desk.hub.pool.connection() as connection:
        routed = prompts.routing(connection, [task])
    assert [p.event_id for p in routed[second].delivered] == [event["header"]["id"]]
    assert routed[second].pending == ()


def test_a_followup_to_a_failing_session_is_not_carried(desk: Desk, published: MemoryPublisher) -> None:
    """A failed main session shows its reason and "Retry" (06-web.md section 4.4), which sends the prompt again: its
    undelivered prompts stay with it, and no session is started for them."""
    task, first = desk.started()
    desk.walk(first, "prompt_delivering", "working", "idle")
    desk.prompt(desk.scene.writer, task)
    desk.hub.clock.advance(1)
    published.clear()
    desk.transition(first, "failed", "agent_exited")
    assert desk.rows("SELECT count(*) FROM hub.session WHERE task_id = %s", task) == [(1,)]
    assert hints(published, kind="session.start") == []
    assert (desk.task(task)["state"], desk.task(task)["state_reason"]) == ("failed", "agent_exited")
    desk.hub.clock.advance(1)
    again = desk.prompt_event(desk.scene.writer, task)
    retry = desk.prompt(desk.scene.writer, task, again).json()
    assert _pending(desk, task)[retry["session_id"]] == [again["header"]["id"]]


@pytest.mark.parametrize("how", ["stop", "close"])
def test_a_session_the_user_ends_starts_no_session_but_hands_its_prompts_on(
    desk: Desk, published: MemoryPublisher, how: str
) -> None:
    """The user stopped the session or closed the task: no new session is started for its queued prompt, but the
    prompt is not lost either - the next main session (the next follow-up) delivers it first."""
    task, first = desk.started()
    desk.walk(first, "prompt_delivering", "working")
    queued = desk.prompt_event(desk.scene.writer, task)
    desk.prompt(desk.scene.writer, task, queued)
    browser = desk.scene.writer.browser
    if how == "stop":
        response = browser.post(f"/v1/sessions/{first}/control", json={"action": "stop"})
    else:
        response = browser.post(f"/v1/tasks/{task}/close", json={})
    assert response.status_code == 200, response.text
    desk.hub.clock.advance(1)
    published.clear()
    desk.transition(first, "stopped")
    assert desk.rows("SELECT count(*) FROM hub.session WHERE task_id = %s", task) == [(1,)]
    assert hints(published, kind="session.start") == []
    desk.hub.clock.advance(1)
    following = desk.prompt_event(desk.scene.writer, task)
    body = desk.prompt(desk.scene.writer, task, following).json()
    assert _pending(desk, task)[body["session_id"]] == [queued["header"]["id"], following["header"]["id"]]


def test_an_ended_session_without_pending_prompts_starts_nothing(desk: Desk, published: MemoryPublisher) -> None:
    task, first = desk.started()
    desk.walk(first, "prompt_delivering", "working", "idle")
    published.clear()
    desk.transition(first, "stopped")
    assert desk.rows("SELECT count(*) FROM hub.session WHERE task_id = %s", task) == [(1,)]
    assert hints(published, kind="session.start") == []
    assert desk.task(task)["state"] == "idle"


def test_a_session_the_device_ends_during_its_start_does_not_restart_itself(
    desk: Desk, published: MemoryPublisher
) -> None:
    """A session that never delivered a prompt (here ended after an unassignment during its start) starts no next
    session, so a device that keeps refusing the workgroup cannot cause a chain of sessions; its prompt goes to the
    next follow-up's session first."""
    task, first = desk.started()
    desk.walk(first, "vm_assigning")
    published.clear()
    desk.transition(first, "stopping", "unassigned")
    desk.transition(first, "stopped")
    assert desk.rows("SELECT count(*) FROM hub.session WHERE task_id = %s", task) == [(1,)]
    assert hints(published, kind="session.start") == []
    initial = desk.rows("SELECT id FROM hub.event WHERE task_id = %s AND kind = 'prompt'", task)[0][0]
    desk.hub.clock.advance(1)
    following = desk.prompt_event(desk.scene.writer, task)
    body = desk.prompt(desk.scene.writer, task, following).json()
    assert _pending(desk, task)[body["session_id"]] == [str(initial), following["header"]["id"]]


def test_a_followup_while_the_device_stops_the_session_carries_its_prompt(desk: Desk) -> None:
    """The device reported `stopping` with a prompt pending; a follow-up then creates the next main session at once
    (the session is ending), which delivers the earlier prompt first; the final `stopped` starts nothing more."""
    task, first = desk.started()
    desk.walk(first, "prompt_delivering", "working", "idle")
    early = desk.prompt_event(desk.scene.writer, task)
    desk.prompt(desk.scene.writer, task, early)
    desk.transition(first, "stopping")
    desk.hub.clock.advance(1)
    late = desk.prompt_event(desk.scene.writer, task)
    second = desk.prompt(desk.scene.writer, task, late).json()["session_id"]
    assert second != first
    assert _pending(desk, task)[second] == [early["header"]["id"], late["header"]["id"]]
    desk.transition(first, "stopped")
    assert desk.rows("SELECT count(*) FROM hub.session WHERE task_id = %s", task) == [(2,)]
    assert _pending(desk, task)[second] == [early["header"]["id"], late["header"]["id"]]
