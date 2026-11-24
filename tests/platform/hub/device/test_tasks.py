"""U-HUB-device-api (tasks): `GET /v1/device/workgroups/{wg}/tasks` and `GET /v1/device/tasks/{task}`, read from
the tables of the tasks area (03-api.md section 6.1, 02-data-model.md sections 2.4, 2.5)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest

from agent_hub.crypto.encoding import b64u_encode
from agent_hub.device import tasks
from agent_hub.errors import HubError
from tests.platform.hub.device.support import Fleet, org_device_scene
from tests.platform.hub.events.support import Scene
from tests.platform.hub.orgs.support import World, new_id

NOT_CONTRACT = {"contract": False}


def error(response: Any) -> tuple[int, str]:
    return response.status_code, response.json()["error"]


def add_task(scene: Scene, *, state: str = "working", offset_us: int = 0, wg: str | None = None) -> str:
    task = new_id()
    at = scene.world.hub.clock() + timedelta(microseconds=offset_us)
    with scene.world.hub.pool.connection() as connection, connection.transaction():
        connection.execute(
            "INSERT INTO hub.task (id, org_id, wg_id, owner_id, state, created_at, state_at, last_activity_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (task, scene.org, wg or scene.wg, scene.writer.id, state, at, at, at),
        )
    return task


def test_tasks_of_an_assigned_workgroup(world: World, fleet: Fleet) -> None:
    scene = org_device_scene(world)
    first = add_task(scene, state="idle", offset_us=1)
    second = add_task(scene, offset_us=2)
    third = add_task(scene, offset_us=2)
    add_task(scene, wg=scene.draft.default_wg)
    client = fleet.client(scene.device)
    answer = client.get(f"/v1/device/workgroups/{scene.wg}/tasks").json()
    newest = sorted([second, third], reverse=True)
    assert [row["task_id"] for row in answer["tasks"]] == [*newest, first]
    assert answer["next_before"] is None
    row = answer["tasks"][-1]
    assert row["state"] == "idle"
    assert row["created_at"] == "2026-11-21T12:00:00.123Z"
    assert (row["main_session_id"], row["title_event_id"], row["closed_at"]) == (None, None, None)
    page = client.get(f"/v1/device/workgroups/{scene.wg}/tasks", params={"limit": "2"}).json()
    assert [row["task_id"] for row in page["tasks"]] == newest
    rest = client.get(f"/v1/device/workgroups/{scene.wg}/tasks", params={"before": page["next_before"]}).json()
    assert [row["task_id"] for row in rest["tasks"]] == [first]
    idle = client.get(f"/v1/device/workgroups/{scene.wg}/tasks", params={"state": "idle"}).json()
    assert [row["task_id"] for row in idle["tasks"]] == [first]


def test_task_list_refusals(world: World, fleet: Fleet) -> None:
    scene = org_device_scene(world)
    client = fleet.client(scene.device)
    for wg in (scene.draft.default_wg, new_id(), "not-an-id"):
        assert error(client.get(f"/v1/device/workgroups/{wg}/tasks", extensions=NOT_CONTRACT)) == (404, "not_found")
    for params, pointer in (
        ({"state": "running"}, "/state"),
        ({"limit": "201"}, "/limit"),
        ({"before": "a+b"}, "/before"),
        ({"before": "abc"}, "/before"),
    ):
        response = client.get(f"/v1/device/workgroups/{scene.wg}/tasks", params=params, extensions=NOT_CONTRACT)
        assert response.json() == {"error": "request_invalid", "detail": {"pointers": [pointer]}}, params


def test_cursor_round_trip_and_forgeries() -> None:
    task = new_id()
    moment = tasks.EPOCH + timedelta(days=20000, microseconds=123457)
    cursor = tasks.encode_cursor(moment, UUID(task))
    assert tasks.decode_cursor(cursor) == (moment, UUID(task))

    for forged in (
        b64u_encode(b'{"at":1.5,"id":"' + task.encode() + b'"}'),
        b64u_encode(b'{"at":true,"id":"' + task.encode() + b'"}'),
        b64u_encode(b'{"at":"x","id":"' + task.encode() + b'"}'),
        b64u_encode(b'{"at":1,"id":"' + task.upper().encode() + b'"}'),
        b64u_encode(b'{"at":1}'),
        b64u_encode(b"[1]"),
        b64u_encode(b'{"at":99999999999999999999999,"id":"' + task.encode() + b'"}'),
        b64u_encode(b"\xff"),
        "a",
    ):
        with pytest.raises(HubError):
            tasks.decode_cursor(forged)


def test_task_with_sessions_and_operations(world: World, fleet: Fleet) -> None:
    scene = org_device_scene(world)
    task, session = scene.start_session()
    child = new_id()
    now = fleet.hub.clock()
    operation = new_id()
    with fleet.hub.pool.connection() as connection, connection.transaction():
        connection.execute(
            "INSERT INTO hub.session (id, task_id, org_id, wg_id, owner_id, device_id, parent_session_id, depth,"
            " state, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, 1, 'requested', %s)",
            (child, task, scene.org, scene.wg, scene.writer.id, scene.device.id, session, now),
        )
        connection.execute(
            "INSERT INTO hub.operation (id, org_id, wg_id, task_id, session_id, device_id, actor_id, type, state,"
            " error_code, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, 'github.push', 'failed', 'timeout', %s)",
            (operation, scene.org, scene.wg, task, session, scene.device.id, scene.writer.id, now),
        )
    title = scene.wg_event(scene.writer, "task.meta", task=task)
    assert scene.post(scene.writer, title).status_code == 200
    newer = scene.wg_event(scene.writer, "task.meta", task=task)
    assert scene.post(scene.writer, newer).status_code == 200
    answer = fleet.client(scene.device).get(f"/v1/device/tasks/{task}").json()
    assert answer["task"]["task_id"] == task
    assert answer["task"]["main_session_id"] == session
    assert answer["task"]["title_event_id"] == newer["header"]["id"]
    assert [(row["session_id"], row["depth"], row["parent_session_id"]) for row in answer["sessions"]] == [
        (session, 0, None),
        (child, 1, session),
    ]
    assert answer["sessions"][0]["device_id"] == scene.device.id
    assert answer["operations"] == [
        {"id": operation, "type": "github.push", "state": "failed", "error_code": "timeout", "result_event_id": None}
    ]


def test_task_refusals(world: World, fleet: Fleet) -> None:
    scene = org_device_scene(world)
    client = fleet.client(scene.device)
    foreign = add_task(scene, wg=scene.draft.default_wg)
    for task in (foreign, new_id(), "x"):
        assert error(client.get(f"/v1/device/tasks/{task}", extensions=NOT_CONTRACT)) == (404, "not_found")


def test_main_session_is_the_newest_one(world: World, fleet: Fleet) -> None:
    """A-15: after a follow-up replaced an ended main session, the device API names the current (newest) main session,
    like the browser API - by creation time, not by ID."""
    scene = org_device_scene(world)
    task, first = scene.start_session()
    newer = "00000000-0000-7000-8000-000000000001"  # sorts before `first`
    at = fleet.hub.clock() + timedelta(seconds=1)
    with fleet.hub.pool.connection() as connection, connection.transaction():
        connection.execute("UPDATE hub.session SET state = 'stopped' WHERE id = %s", (first,))
        connection.execute(
            "INSERT INTO hub.session (id, task_id, org_id, wg_id, owner_id, device_id, depth, state, created_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, 0, 'requested', %s)",
            (newer, task, scene.org, scene.wg, scene.writer.id, scene.device.id, at),
        )
    client = fleet.client(scene.device)
    assert client.get(f"/v1/device/tasks/{task}").json()["task"]["main_session_id"] == newer
    rows = client.get(f"/v1/device/workgroups/{scene.wg}/tasks").json()["tasks"]
    assert [row["main_session_id"] for row in rows if row["task_id"] == task] == [newer]
    browser = scene.writer.browser.get(f"/v1/workgroups/{scene.wg}/tasks").json()["tasks"]
    assert [row["main_session_id"] for row in browser if row["task_id"] == task] == [newer]
