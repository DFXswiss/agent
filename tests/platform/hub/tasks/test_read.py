"""U-HUB-tasks (reads): `GET /v1/workgroups/{wg}/tasks`, `GET /v1/tasks/{task}`, `GET /v1/sessions/{session}` - list
order and paging, the task tree, rights without revealing foreign objects, and no project level (R-2.10)."""

from __future__ import annotations

import json

import pytest

from agent_hub.app import create_app
from agent_hub.http.routing import route_spec
from tests.platform.hub.conftest import HubHarness
from tests.platform.hub.orgs.support import new_id
from tests.platform.hub.tasks.support import Desk


def test_tasks_are_listed_newest_first_with_a_cursor(desk: Desk) -> None:
    created = []
    for index in range(5):
        created.append(desk.started()[0])
        if index != 2:
            desk.hub.clock.advance(0.001)
    client = desk.scene.reader.browser
    first = client.get(f"/v1/workgroups/{desk.wg}/tasks", params={"limit": "2"}).json()
    # two tasks share a creation time: the ID breaks the tie (created_at DESC, id DESC)
    expected = sorted(created, key=lambda task: (desk.task(task)["created_at"], task), reverse=True)
    assert [item["task_id"] for item in first["tasks"]] == expected[:2]
    second = client.get(f"/v1/workgroups/{desk.wg}/tasks", params={"limit": "2", "before": first["next_before"]})
    third = client.get(f"/v1/workgroups/{desk.wg}/tasks", params={"before": second.json()["next_before"]}).json()
    assert [item["task_id"] for item in second.json()["tasks"] + third["tasks"]] == expected[2:]
    assert third["next_before"] is None
    whole = client.get(f"/v1/workgroups/{desk.wg}/tasks").json()
    assert [item["task_id"] for item in whole["tasks"]] == expected
    assert whole["next_before"] is None


def test_tasks_are_filtered_by_state(desk: Desk) -> None:
    open_task, _ = desk.started()
    closed_task, _ = desk.started()
    desk.scene.writer.browser.post(f"/v1/tasks/{closed_task}/close", json={})
    client = desk.scene.reader.browser
    closed = client.get(f"/v1/workgroups/{desk.wg}/tasks", params={"state": "closed"}).json()
    assert [item["task_id"] for item in closed["tasks"]] == [closed_task]
    starting = client.get(f"/v1/workgroups/{desk.wg}/tasks", params={"state": "starting"}).json()
    assert [item["task_id"] for item in starting["tasks"]] == [open_task]


@pytest.mark.parametrize(
    ("query", "pointer"),
    [
        ({"state": "running"}, "/state"),
        ({"before": "AAAA"}, "/before"),
        ({"limit": "0"}, None),
        ({"limit": "201"}, None),
    ],
)
def test_list_query_is_checked(desk: Desk, query: dict[str, str], pointer: str | None) -> None:
    response = desk.scene.reader.browser.get(
        f"/v1/workgroups/{desk.wg}/tasks", params=query, extensions={"contract": False}
    )
    assert response.status_code == 400
    assert response.json() == {"error": "request_invalid", **({"detail": {"pointers": [pointer]}} if pointer else {})}


def test_reads_do_not_reveal_foreign_objects(desk: Desk) -> None:
    task, session = desk.started()
    stranger = desk.scene.stranger.browser
    for path in (f"/v1/workgroups/{desk.wg}/tasks", f"/v1/tasks/{task}", f"/v1/sessions/{session}"):
        response = stranger.get(path)
        assert (response.status_code, response.json()) == (404, {"error": "not_found"})
    reader = desk.scene.reader.browser
    for path in (
        f"/v1/workgroups/{new_id()}/tasks",
        f"/v1/tasks/{new_id()}",
        f"/v1/sessions/{new_id()}",
        "/v1/tasks/x",
        "/v1/sessions/x",
    ):
        assert reader.get(path).status_code == 404
    assert reader.get(f"/v1/tasks/{task}").status_code == 200
    assert reader.get(f"/v1/sessions/{session}").status_code == 200


def test_task_detail_has_the_session_tree_usage_and_transitions(desk: Desk) -> None:
    task, main = desk.started()
    desk.walk(main, "vm_assigning", "vm_ready", "prompt_delivering", "working")
    child = new_id()
    desk.execute(
        "INSERT INTO hub.session (id, task_id, org_id, wg_id, owner_id, device_id, parent_session_id, depth, state,"
        " created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, 1, 'requested', %s)",
        child,
        task,
        desk.org,
        desk.wg,
        desk.scene.writer.id,
        desk.scene.device.id,
        main,
        desk.hub.clock(),
    )
    desk.execute(
        "INSERT INTO hub.usage_counter (session_id, requests, input_tokens, output_tokens, cache_read_tokens,"
        " cache_write_tokens, thinking_tokens, cost_micro_usd, cost_basis, context_used_tokens,"
        " context_window_tokens, last_provider, updated_at) VALUES (%s, 3, 100, 50, 0, 0, 0, 1234, 'provider', 900,"
        " 200000, 'deepseek', %s)",
        main,
        desk.hub.clock(),
    )
    body = desk.scene.reader.browser.get(f"/v1/tasks/{task}").json()
    assert body["task"]["task_id"] == task
    assert [item["session"]["session_id"] for item in body["sessions"]] == [main, child]
    assert body["sessions"][0]["usage"]["cost_micro_usd"] == 1234
    assert body["sessions"][1]["usage"] is None
    assert [item["state"] for item in body["sessions"][0]["transitions"]] == [
        "requested",
        "vm_assigning",
        "vm_ready",
        "prompt_delivering",
        "working",
    ]
    assert [item["state"] for item in body["transitions"]] == ["scheduling", "starting", "working"]
    detail = desk.scene.reader.browser.get(f"/v1/sessions/{main}").json()
    assert detail["children"] == [child]
    assert detail["usage"]["requests"] == 3
    assert detail["vm"] is None
    assert [item["seq"] for item in detail["transitions"]] == [1, 2, 3, 4, 5]


def test_session_detail_has_its_vm(desk: Desk) -> None:
    _, session = desk.started()
    vm = desk.vm(desk.scene.device, "running")
    desk.execute("UPDATE hub.session SET vm_id = %s WHERE id = %s", vm, session)
    detail = desk.scene.reader.browser.get(f"/v1/sessions/{session}").json()
    assert (detail["vm"]["vm_id"], detail["vm"]["state"], detail["vm"]["device_id"]) == (
        vm,
        "running",
        desk.scene.device.id,
    )
    desk.execute("UPDATE hub.session SET vm_id = 'vm-0000abcd' WHERE id = %s", session)
    assert desk.scene.reader.browser.get(f"/v1/sessions/{session}").json()["vm"] is None


def test_there_is_no_project_level(hub: HubHarness, desk: Desk) -> None:
    """R-2.10: organization → workgroup → task; no route, field or value names a project."""
    app = create_app(hub.config, pool=hub.pool, clock=hub.clock, run_jobs=False)
    paths = [getattr(route, "path", "") for route in app.routes if route_spec(route) is not None]
    assert paths
    assert not [path for path in paths if "project" in path.lower()]
    task, session = desk.started()
    client = desk.scene.reader.browser
    for path in (f"/v1/workgroups/{desk.wg}", f"/v1/workgroups/{desk.wg}/tasks", f"/v1/tasks/{task}"):
        assert "project" not in json.dumps(client.get(path).json()).lower()
    assert "project" not in json.dumps(client.get(f"/v1/sessions/{session}").json()).lower()
