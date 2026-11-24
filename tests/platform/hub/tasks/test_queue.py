"""U-HUB-tasks (KOR-46, ADR-083 section 3): the queue of a task's prompts `GET /v1/tasks/{task}/queue`, the
withdrawal of a waiting prompt `POST /v1/tasks/{task}/prompts/{event}/withdraw` - at once without a device, else
requested from the device (`prompt_queue` of `GET /v1/device/state`) and confirmed by its outbox audit
`session.prompt_withdrawn` - and the routing without withdrawn prompts."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx
import pytest

from agent_hub.crypto.encoding import format_time
from agent_hub.notifications import signals as notification_signals
from agent_hub.sessions import prompts
from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.agents.support import agent_client
from tests.platform.hub.conftest import SignedClient
from tests.platform.hub.device.support import Fleet
from tests.platform.hub.orgs.support import Actor, new_id
from tests.platform.hub.outbox.support import Outbox, item
from tests.platform.hub.tasks.support import Desk, audit_rows, frames, hints

Json = dict[str, Any]


def first_prompt(desk: Desk, task: str) -> str:
    return str(
        desk.rows("SELECT id FROM hub.event WHERE task_id = %s AND kind = 'prompt' ORDER BY scope_seq", task)[0][0]
    )


def follow(desk: Desk, task: str, client: SignedClient | None = None, author: Actor | None = None) -> str:
    """A follow-up prompt of `author` (default `writer`) sent by `client` (default the author's browser); its ID."""
    person = author or desk.scene.writer
    event = desk.prompt_event(person, task)
    response = (client or person.browser).post(f"/v1/tasks/{task}/prompts", json={"event": event})
    assert response.status_code == 200, response.text
    return str(event["header"]["id"])


def queue(desk: Desk, task: str, actor: Actor | None = None) -> Json:
    response = (actor or desk.scene.writer).browser.get(f"/v1/tasks/{task}/queue")
    assert response.status_code == 200, response.text
    return dict(response.json())


def states(desk: Desk, task: str) -> list[tuple[str, str]]:
    return [(entry["event_id"], entry["state"]) for entry in queue(desk, task)["prompts"]]


def withdrawals(desk: Desk, task: str) -> list[tuple[str, str]]:
    return [(entry["event_id"], entry["state"]) for entry in queue(desk, task)["withdrawals"]]


def withdraw(client: SignedClient, task: str, event: str) -> httpx.Response:
    return client.post(f"/v1/tasks/{task}/prompts/{event}/withdraw", json={})


def announce(desk: Desk, *devices: Actor) -> None:
    """The devices announced `prompt.withdraw` on their live channel (`agent_hub.device.features`)."""
    for device in devices:
        desk.execute("UPDATE hub.device SET features = %s WHERE id = %s", ["prompt.withdraw"], device.id)


def reconciles(published: MemoryPublisher) -> list[tuple[str, Json]]:
    return [
        (str(entry.audience.id), dict(entry.data))
        for entry in published.items()
        if entry.audience.kind == "device" and entry.frame == "reconcile"
    ]


def grant_write(desk: Desk, user: Actor) -> None:
    """`user` may write the workgroup (a grant in the org's log)."""
    scene = desk.scene
    scene.draft.add("grant", scene.admin, subject=user.id, wg=scene.wg, access="write")
    response = scene.draft.post(scene.admin)
    assert response.status_code == 200, response.text


def device_state(fleet: Fleet, device: Actor) -> Json:
    response = fleet.client(device).get("/v1/device/state")
    assert response.status_code == 200, response.text
    return dict(response.json())


def withdrawn_report(task: str, event: str, session: str | None = None, **changes: Any) -> Json:
    body: Json = {
        "action": "session.prompt_withdrawn",
        "result": "ok",
        "task_id": task,
        "target_kind": "event",
        "target_id": event,
        "detail": {},
    }
    if session is not None:
        body["session_id"] = session
    body.update(changes)
    return body


# --- queue ----------------------------------------------------------------------------------------------------------


def test_the_queue_lists_the_waiting_prompts_in_event_order(desk: Desk, published: MemoryPublisher) -> None:
    task, session = desk.started()
    device, writer = desk.scene.device, desk.scene.writer
    first = first_prompt(desk, task)
    agent = agent_client(desk.hub, UUID(writer.id))
    published.clear()
    second = follow(desk, task, agent)
    sent = hints(published, device, "prompt")
    assert [hint.get("via_agent") for hint in sent] == [True]
    found = desk.rows(
        "SELECT scope_seq, received_at FROM hub.event WHERE task_id = %s AND kind = 'prompt' ORDER BY scope_seq", task
    )
    expected = [
        {
            "event_id": event,
            "author_id": writer.id,
            "via_agent": event == second,
            "scope_seq": row[0],
            "received_at": format_time(row[1]),
            "session_id": session,
            "device_id": device.id,
            "state": "waiting",
        }
        for event, row in zip((first, second), found, strict=True)
    ]
    assert queue(desk, task, desk.scene.reader) == {"task_id": task, "prompts": expected, "withdrawals": []}
    desk.offline(device)
    assert states(desk, task) == [(first, "waiting_for_device"), (second, "waiting_for_device")]
    desk.online(device)
    desk.walk(session, "prompt_delivering", "working")
    assert states(desk, task) == [(second, "waiting")]
    for client, path in (
        (desk.scene.stranger.browser, f"/v1/tasks/{task}/queue"),
        (writer.browser, f"/v1/tasks/{new_id()}/queue"),
    ):
        response = client.get(path)
        assert (response.status_code, response.json()["error"]) == (404, "not_found")


def test_a_carried_prompt_is_held_by_the_next_session(desk: Desk) -> None:
    task, first = desk.started()
    desk.walk(first, "prompt_delivering", "working", "idle")
    carried = follow(desk, task)
    desk.hub.clock.advance(1)
    desk.transition(first, "stopped")
    second = desk.task(task)["main_session_id"]
    assert [(entry["event_id"], entry["session_id"]) for entry in queue(desk, task)["prompts"]] == [(carried, second)]


# --- withdrawal through the device ------------------------------------------------------------------------------------


def test_a_requested_withdrawal_is_confirmed_by_the_device(
    desk: Desk, fleet: Fleet, published: MemoryPublisher
) -> None:
    outbox = Outbox(desk, fleet)
    device, other, writer = desk.scene.device, desk.scene.other_device, desk.scene.writer
    task, session = desk.started()
    first = first_prompt(desk, task)
    second = follow(desk, task, agent_client(desk.hub, UUID(writer.id)))
    third = follow(desk, task)
    announce(desk, device)
    published.clear()
    requested_at = desk.hub.clock()
    response = withdraw(writer.browser, task, second)
    assert (response.status_code, response.json()) == (200, {"state": "withdrawing"})
    assert reconciles(published) == [(device.id, {})]
    assert frames(published, "session")[-1]["session"]["session_id"] == session
    assert audit_rows(desk, "prompt.withdraw") == [("user", UUID(writer.id), second, {"state": "withdrawing"})]
    assert desk.rows("SELECT target_kind, session_id::text FROM hub.audit WHERE action = 'prompt.withdraw'") == [
        ("event", session)
    ]
    assert states(desk, task) == [(first, "waiting"), (second, "withdrawing"), (third, "waiting")]
    assert queue(desk, task)["withdrawals"] == [
        {"event_id": second, "state": "withdrawing", "requested_at": format_time(requested_at)}
    ]
    assert device_state(fleet, device)["prompt_queue"] == {"via_agent": [second], "withdrawn": [second]}
    # a repeat answers the current state and changes nothing
    assert withdraw(writer.browser, task, second).json() == {"state": "withdrawing"}
    assert len(audit_rows(desk, "prompt.withdraw")) == 1
    # a report of another device, or about something else than an event, confirms nothing
    seq = outbox.position(other) + 1
    assert outbox.statuses(item(seq, "audit", withdrawn_report(task, second)), device=other) == [(seq, "ok", None)]
    seq = outbox.position() + 1
    wrong = [
        item(seq, "audit", withdrawn_report(task, second, session, target_kind="session")),
        item(seq + 1, "audit", withdrawn_report(task, "not-an-id", session)),
    ]
    assert [status[1:] for status in outbox.statuses(*wrong)] == [
        ("rejected", "request_invalid"),
        ("rejected", "request_invalid"),
    ]
    assert withdrawals(desk, task) == [(second, "withdrawing")]
    # the device withdrew it: the hub confirms it, the browsers are told
    published.clear()
    seq = outbox.position() + 1
    assert outbox.statuses(item(seq, "audit", withdrawn_report(task, second, session))) == [(seq, "ok", None)]
    assert desk.rows("SELECT withdrawn_at FROM hub.prompt_withdrawal WHERE event_id = %s", second) == [
        (desk.hub.clock(),)
    ]
    assert [frame["session"]["session_id"] for frame in frames(published, "session")] == [session]
    assert states(desk, task) == [(first, "waiting"), (third, "waiting")]
    assert withdrawals(desk, task) == [(second, "withdrawn")]
    answer = device_state(fleet, device)
    assert [entry["event_id"] for entry in answer["pending_prompts"]] == [first, third]
    assert answer["prompt_queue"] == {"via_agent": [], "withdrawn": []}
    assert withdraw(writer.browser, task, second).json() == {"state": "withdrawn"}
    # a repeated report changes nothing
    seq = outbox.position() + 1
    assert outbox.statuses(item(seq, "audit", withdrawn_report(task, second, session))) == [(seq, "ok", None)]
    # the others keep their order: the first is delivered, then the third
    desk.walk(session, "prompt_delivering", "working", "idle", "working")
    with desk.hub.pool.connection() as connection:
        routed = prompts.routing(connection, [task])[session]
    assert [prompt.event_id for prompt in routed.delivered] == [first, third]


def test_a_device_without_the_feature_is_outdated(desk: Desk, published: MemoryPublisher) -> None:
    task, _session = desk.started()
    first = first_prompt(desk, task)
    published.clear()
    response = withdraw(desk.scene.writer.browser, task, first)
    assert response.status_code == 409, response.text
    assert response.json() == {"error": "peer_outdated", "detail": {"devices": [desk.scene.device.id]}}
    assert desk.rows("SELECT count(*) FROM hub.prompt_withdrawal") == [(0,)]
    assert reconciles(published) == []
    assert states(desk, task) == [(first, "waiting")]


def test_who_may_withdraw_which_prompt(desk: Desk) -> None:
    scene = desk.scene
    task, _session = desk.started()
    announce(desk, scene.device, scene.other_device)
    prompt = follow(desk, task)
    meta = str(desk.rows("SELECT id FROM hub.event WHERE task_id = %s AND kind = 'task.meta'", task)[0][0])
    other_task, _other = desk.started()
    for event in (new_id(), meta, first_prompt(desk, other_task)):
        response = withdraw(scene.writer.browser, task, event)
        assert (response.status_code, response.json()["error"]) == (404, "not_found"), event
    response = withdraw(scene.writer.browser, new_id(), prompt)
    assert (response.status_code, response.json()["error"]) == (404, "not_found")
    response = withdraw(scene.reader.browser, task, prompt)
    assert (response.status_code, response.json()["error"]) == (403, "forbidden")
    grant_write(desk, scene.reader)
    response = withdraw(scene.reader.browser, task, prompt)
    assert (response.status_code, response.json()["error"]) == (403, "forbidden")
    # the author's agent acts as the author
    response = withdraw(agent_client(desk.hub, UUID(scene.writer.id)), task, prompt)
    assert (response.status_code, response.json()) == (200, {"state": "withdrawing"})


def test_only_a_prompt_in_the_queue_can_be_withdrawn(desk: Desk) -> None:
    writer = desk.scene.writer.browser
    task, session = desk.started()
    announce(desk, desk.scene.device, desk.scene.other_device)
    first = first_prompt(desk, task)
    desk.walk(session, "prompt_delivering", "working")
    response = withdraw(writer, task, first)
    assert (response.status_code, response.json()["error"]) == (409, "version_conflict")
    second, third = follow(desk, task), follow(desk, task)
    assert withdraw(writer, task, second).json() == {"state": "withdrawing"}
    # the device had handed it to the guest already: it is delivered
    desk.walk(session, "idle", "working")
    assert states(desk, task) == [(third, "waiting")]
    assert withdrawals(desk, task) == [(second, "too_late")]
    response = withdraw(writer, task, second)
    assert (response.status_code, response.json()["error"]) == (409, "version_conflict")
    # the prompts of a failed session wait for "Retry", they are not in the queue
    desk.transition(session, "failed", "agent_exited")
    assert states(desk, task) == []
    response = withdraw(writer, task, third)
    assert (response.status_code, response.json()["error"]) == (409, "version_conflict")


# --- withdrawal without a device -------------------------------------------------------------------------------------


def test_without_a_device_a_prompt_is_withdrawn_at_once(desk: Desk, published: MemoryPublisher) -> None:
    desk.offline(desk.scene.device)
    desk.offline(desk.scene.other_device)
    writer = desk.scene.writer.browser
    task, session = desk.started()
    first = first_prompt(desk, task)
    second = follow(desk, task)
    published.clear()
    assert withdraw(writer, task, second).json() == {"state": "withdrawn"}
    assert desk.session(session)["state"] == "requested"
    assert states(desk, task) == [(first, "waiting_for_device")]
    assert withdrawals(desk, task) == [(second, "withdrawn")]
    # the last prompt of a session that never delivered one: the session ends as well
    assert withdraw(writer, task, first).json() == {"state": "withdrawn"}
    assert desk.session(session)["state"] == "stopped"
    assert states(desk, task) == []
    assert [row[3] for row in audit_rows(desk, "prompt.withdraw")] == [{"state": "withdrawn"}] * 2
    assert reconciles(published) == []
    assert withdraw(writer, task, first).json() == {"state": "withdrawn"}


# --- the session learns who triggered it -----------------------------------------------------------------------------


def test_a_main_session_is_triggered_by_the_author_of_its_first_prompt(desk: Desk) -> None:
    scene = desk.scene
    task, first = desk.started()
    assert desk.session(first)["triggered_by"] == scene.writer.id
    detail = scene.writer.browser.get(f"/v1/tasks/{task}").json()
    assert [entry["session"]["triggered_by"] for entry in detail["sessions"]] == [scene.writer.id]
    grant_write(desk, scene.reader)
    # a follow-up of another member after the device ended the session: a new session of that member
    desk.walk(first, "prompt_delivering", "working", "idle")
    desk.transition(first, "stopped")
    desk.hub.clock.advance(1)
    follow(desk, task, author=scene.reader)
    second = desk.task(task)["main_session_id"]
    assert (desk.session(second)["triggered_by"], desk.session(second)["owner_id"]) == (
        scene.reader.id,
        scene.writer.id,
    )
    # carried over: the author of the first prompt it takes over
    desk.walk(second, "prompt_delivering", "working", "idle")
    follow(desk, task, author=scene.reader)
    follow(desk, task)
    desk.hub.clock.advance(1)
    desk.transition(second, "stopped")
    third = desk.task(task)["main_session_id"]
    assert third != second
    assert desk.session(third)["triggered_by"] == scene.reader.id


def test_the_next_attempt_after_a_refusal_is_triggered_by_the_first_author(desk: Desk) -> None:
    scene = desk.scene
    grant_write(desk, scene.reader)
    task, first = desk.started(scene.reader)
    follow(desk, task)
    desk.hub.clock.advance(1)
    desk.walk(first, "vm_assigning")
    desk.transition(first, "failed", "capacity_exhausted")
    attempt = desk.task(task)["main_session_id"]
    assert attempt != first
    assert desk.session(attempt)["triggered_by"] == scene.reader.id


# --- notifications ---------------------------------------------------------------------------------------------------


def _waiting(desk: Desk) -> list[tuple[str, str, str]]:
    return [
        (str(row[0]), str(row[1]), str(row[2]))
        for row in desk.rows(
            "SELECT recipient_id, task_id, ref FROM hub.notification WHERE kind = 'prompt.waiting_for_device'"
            " ORDER BY created_at, ref"
        )
    ]


@pytest.mark.parametrize("devices", ["offline", "online"])
def test_a_prompt_for_a_session_without_an_online_device_notifies_its_author(
    desk: Desk, published: MemoryPublisher, devices: str
) -> None:
    writer = desk.scene.writer
    if devices == "offline":
        desk.offline(desk.scene.device)
        desk.offline(desk.scene.other_device)
    published.clear()
    task, session = desk.started()
    first = first_prompt(desk, task)
    second = follow(desk, task)
    if devices == "online":
        assert _waiting(desk) == []
        desk.offline(desk.scene.device)
        third = follow(desk, task)
        assert _waiting(desk) == [(writer.id, task, third)]
        return
    assert _waiting(desk) == [(writer.id, task, first), (writer.id, task, second)]
    sent = [entry for entry in published.items() if entry.audience.kind == "user" and entry.frame == "notification"]
    assert [(str(entry.audience.id), entry.data["ref"]) for entry in sent] == [(writer.id, first), (writer.id, second)]
    assert sent[0].data["kind"] == "prompt.waiting_for_device"
    # a session that ended: the follow-up's new session finds no device either
    desk.execute("UPDATE hub.session SET state = 'stopped' WHERE id = %s", session)
    third = follow(desk, task)
    assert _waiting(desk)[-1] == (writer.id, task, third)


def test_a_device_going_offline_notifies_the_authors_of_its_waiting_prompts(desk: Desk) -> None:
    device, writer = desk.scene.device, desk.scene.writer
    notification_signals.bind(desk.hub.app.state.hub)
    try:
        task, session = desk.started()
        first = first_prompt(desk, task)
        other_task, other_session = desk.started()
        assert desk.session(other_session)["device_id"] == device.id
        desk.walk(other_session, "prompt_delivering", "working", "idle")
        follow(desk, other_task)
        desk.transition(other_session, "stopping")  # its prompt is no longer pending at the device
        went = {"device_id": UUID(device.id), "online": 0}
        notification_signals.on_presence("device.presence", {**went, "online": 1})
        notification_signals.on_presence("device.presence", went)  # still online when the work runs
        notification_signals.wait()
        assert _waiting(desk) == []
        desk.offline(device)
        desk.execute("UPDATE hub.wg_access SET revoked_seq = 999 WHERE wg_id = %s AND user_id = %s", desk.wg, writer.id)
        notification_signals.on_presence("device.presence", went)  # the author reads the workgroup no more
        notification_signals.wait()
        assert _waiting(desk) == []
        desk.execute(
            "UPDATE hub.wg_access SET revoked_seq = NULL WHERE wg_id = %s AND user_id = %s", desk.wg, writer.id
        )
        notification_signals.on_presence("device.presence", went)
        notification_signals.on_presence("device.presence", went)  # once per prompt
        notification_signals.wait()
        assert _waiting(desk) == [(writer.id, task, first)]
        assert desk.session(session)["device_id"] == device.id
    finally:
        notification_signals.wait()
        notification_signals.bind(None)
