"""U-HUB-modes: the section `task_modes` of `GET /v1/device/state` (ADR-098 sections 3 and 4)."""

from __future__ import annotations

from typing import Any

from tests.platform.hub.operations.support import Ops


def _section(ops: Ops, device: Any = None) -> dict[str, Any]:
    response = ops.fleet.client(device or ops.device).get("/v1/device/state")
    assert response.status_code == 200, response.text
    return dict(response.json()["task_modes"])


def test_the_device_learns_the_modes_of_its_tasks(ops: Ops) -> None:
    assert _section(ops) == {"api": 1, "tasks": {}}
    task, _session = ops.session()
    quiet, _other = ops.session()
    assert ops.scene.writer.browser.put(f"/v1/tasks/{task}/modes/test", json={}).status_code == 200
    state = {"step": "woken", "counters": {"runs": 2}, "next_at": None, "at": "2026-11-26T10:00:00.000Z"}
    client = ops.fleet.client(ops.device)
    assert client.put(f"/v1/device/tasks/{task}/modes/test/driver", json={"driver": state}).json() == {"on": True}
    section = _section(ops)
    assert list(section["tasks"]) == [task]
    [entry] = section["tasks"][task]
    assert (entry["mode"], entry["way"], entry["chosen_by"], entry["driver"]) == (
        "test",
        "web",
        ops.scene.writer.id,
        state,
    )
    assert quiet not in section["tasks"]
    assert _section(ops, ops.scene.other_device) == {"api": 1, "tasks": {}}  # no session of the task there
