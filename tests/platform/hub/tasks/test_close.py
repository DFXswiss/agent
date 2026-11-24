"""U-HUB-tasks (close and control): `POST /v1/tasks/{task}/close` and `POST /v1/sessions/{session}/control`
(03-api.md section 4.6, 6.2 `session.control`)."""

from __future__ import annotations

from uuid import UUID

import pytest

from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.orgs.support import new_id
from tests.platform.hub.tasks.support import Desk, audit_rows, frames, hints


def test_close_stops_every_live_session(desk: Desk, published: MemoryPublisher) -> None:
    task, main = desk.started()
    desk.walk(main, "prompt_delivering", "working")
    # a child session on the device and an ended one, as HUB-12 stores them
    child, ended = new_id(), new_id()
    for session, state in ((child, "working"), (ended, "stopped")):
        desk.execute(
            "INSERT INTO hub.session (id, task_id, org_id, wg_id, owner_id, device_id, parent_session_id, depth, state,"
            " created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, 1, %s, %s)",
            session,
            task,
            desk.org,
            desk.wg,
            desk.scene.writer.id,
            desk.scene.device.id,
            main,
            state,
            desk.hub.clock(),
        )
    published.clear()
    response = desk.scene.writer.browser.post(f"/v1/tasks/{task}/close", json={})
    assert response.status_code == 200, response.text
    closed = response.json()["task"]
    assert (closed["state"], closed["closed_at"]) == ("closed", "2026-11-21T12:00:00.123Z")
    states = dict(desk.rows("SELECT id::text, state FROM hub.session WHERE task_id = %s", task))
    assert states == {main: "stopping", child: "stopping", ended: "stopped"}
    assert sorted(item["session_id"] for item in hints(published, desk.scene.device, "session.control")) == sorted(
        [main, child]
    )
    assert all(item["action"] == "stop" for item in hints(published, kind="session.control"))
    assert frames(published, "task")[-1]["state"] == "closed"
    assert [row[3] for row in audit_rows(desk, "task.close")] == [{"by": "user"}]
    assert {row[0] for row in audit_rows(desk, "session.stopping")} == {"user"}
    # the runner reports the end; the task stays closed
    desk.transition(main, "stopped")
    assert desk.task(task)["state"] == "closed"
    published.clear()
    again = desk.scene.writer.browser.post(f"/v1/tasks/{task}/close", json={})
    assert again.json()["task"] == desk.task(task)
    assert published.items() == []
    assert len(audit_rows(desk, "task.close")) == 1


def test_close_ends_a_waiting_session_at_once(desk: Desk, published: MemoryPublisher) -> None:
    desk.offline(desk.scene.device)
    desk.offline(desk.scene.other_device)
    task, session = desk.started()
    desk.scene.writer.browser.post(f"/v1/tasks/{task}/close", json={})
    assert desk.session(session)["state"] == "stopped"
    assert hints(published) == []


def test_close_rights(desk: Desk) -> None:
    task, _ = desk.started()
    for actor in (desk.scene.reader, desk.scene.stranger):
        response = actor.browser.post(f"/v1/tasks/{task}/close", json={})
        assert (response.status_code, response.json()) == (403, {"error": "forbidden"})
    response = desk.scene.writer.browser.post(f"/v1/tasks/{new_id()}/close", json={})
    assert response.status_code == 404
    response = desk.scene.writer.browser.post(f"/v1/tasks/{task}/close", json={"x": 1}, extensions={"contract": False})
    assert response.status_code == 400


@pytest.mark.parametrize("action", ["interrupt", "stop"])
def test_control_sends_the_hint(desk: Desk, published: MemoryPublisher, action: str) -> None:
    _, session = desk.started()
    desk.walk(session, "prompt_delivering", "working")
    published.clear()
    response = desk.scene.writer.browser.post(f"/v1/sessions/{session}/control", json={"action": action})
    assert (response.status_code, response.json()) == (200, {})
    assert hints(published) == [{"type": "session.control", "session_id": session, "action": action}]
    assert desk.session(session)["state"] == "working"
    rows = audit_rows(desk, "session.control")
    assert [(row[0], row[1], row[2], row[3]) for row in rows] == [
        ("user", UUID(desk.scene.writer.id), session, {"action": action})
    ]


def test_control_of_a_waiting_session(desk: Desk, published: MemoryPublisher) -> None:
    desk.offline(desk.scene.device)
    desk.offline(desk.scene.other_device)
    task, session = desk.started()
    client = desk.scene.writer.browser
    assert client.post(f"/v1/sessions/{session}/control", json={"action": "interrupt"}).status_code == 200
    assert desk.session(session)["state"] == "requested"
    assert client.post(f"/v1/sessions/{session}/control", json={"action": "stop"}).status_code == 200
    assert desk.session(session)["state"] == "stopped"
    assert desk.task(task)["state"] == "idle"
    assert client.post(f"/v1/sessions/{session}/control", json={"action": "stop"}).status_code == 200
    assert hints(published) == []
    assert len(audit_rows(desk, "session.control")) == 3


def test_control_rights_and_form(desk: Desk) -> None:
    _, session = desk.started()
    for actor in (desk.scene.reader, desk.scene.stranger):
        response = actor.browser.post(f"/v1/sessions/{session}/control", json={"action": "stop"})
        assert (response.status_code, response.json()) == (403, {"error": "forbidden"})
    response = desk.scene.writer.browser.post(f"/v1/sessions/{new_id()}/control", json={"action": "stop"})
    assert response.status_code == 404
    response = desk.scene.writer.browser.post(
        f"/v1/sessions/{session}/control", json={"action": "suspend"}, extensions={"contract": False}
    )
    assert response.status_code == 400


def test_close_leaves_a_stopping_session_alone(desk: Desk, published: MemoryPublisher) -> None:
    task, session = desk.started()
    desk.walk(session, "prompt_delivering", "working", "stopping")
    published.clear()
    desk.scene.writer.browser.post(f"/v1/tasks/{task}/close", json={})
    assert hints(published) == []
    assert audit_rows(desk, "session.stopping")[-1][0] == "device"
    assert desk.task(task)["state"] == "closed"
