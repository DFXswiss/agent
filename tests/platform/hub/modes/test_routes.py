"""U-HUB-modes: the catalogue, switching working modes of a task on and off by a person, by a person's agent, by a
session's agent (passing on) and by a sentence through the device, and the driver state (ADR-098 sections 1-4;
`GET /v1/modes`, `PUT/DELETE /v1/tasks/{task}/modes/{mode}`, `PUT /v1/device/tasks/{task}/modes/{mode}[/driver]`)."""

from __future__ import annotations

import secrets
import uuid
from dataclasses import replace
from types import MappingProxyType
from typing import Any

import pytest

from agent_hub.modes import catalog
from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.agents.support import stored_agent
from tests.platform.hub.operations.support import Ops
from tests.platform.hub.orgs.support import new_id
from tests.platform.hub.tasks.support import audit_rows, frames

NEEDY = MappingProxyType({**catalog.CATALOG[0], "name": "needy", "needs": {"modes": ["test"], "prerequisites": []}})


def _put(ops: Ops, task: str, mode: str = "test", client: Any = None, **options: Any) -> Any:
    return (client or ops.scene.writer.browser).put(f"/v1/tasks/{task}/modes/{mode}", json={}, **options)


def _delete(ops: Ops, task: str, mode: str = "test", client: Any = None) -> Any:
    return (client or ops.scene.writer.browser).delete(f"/v1/tasks/{task}/modes/{mode}")


def _reconciles(published: MemoryPublisher) -> list[str]:
    return sorted(str(item.audience.id) for item in published.items() if item.frame == "reconcile")


def _task(ops: Ops, task: str) -> dict[str, Any]:
    response = ops.scene.writer.browser.get(f"/v1/tasks/{task}")
    assert response.status_code == 200, response.text
    return dict(response.json()["task"])


def _session_agent(ops: Ops, session: str) -> Any:
    """An agent of the writer whose pairing belongs to `session` (the main orchestrator on platform hardware)."""
    agent = stored_agent(ops.hub, uuid.UUID(ops.scene.writer.id))
    ops.desk.execute(
        "UPDATE hub.agent SET session_id = %s, device_id = %s, device_sig = %s WHERE id = %s",
        session,
        ops.scene.device.id,
        secrets.token_bytes(64),
        agent.agent_id,
    )
    return agent


def test_no_default_rows(ops: Ops) -> None:
    """A migrated database, a built organization and new tasks have no mode (E-32: never on by default)."""
    writer, task = ops.scene.writer, new_id()
    ops.session()
    body = {"task_id": task, "wg_id": ops.desk.wg, "events": ops.desk.task_events(writer, task)}
    assert writer.browser.post("/v1/tasks", json=body).status_code == 200
    assert ops.desk.rows("SELECT count(*) FROM hub.task_mode") == [(0,)]
    assert audit_rows(ops.desk, "task.mode_on") == []


def test_the_catalogue_lists_the_test_mode_only_where_configured(ops: Ops) -> None:
    response = ops.scene.reader.browser.get("/v1/modes")
    assert response.status_code == 200, response.text
    assert response.json() == {"modes": catalog.offered(True)}
    assert [entry["name"] for entry in response.json()["modes"]] == ["test"]
    state = ops.hub.app.state
    state.hub = replace(state.hub, config=replace(state.hub.config, modes_test=False))
    assert ops.scene.reader.browser.get("/v1/modes").json() == {"modes": []}
    task, _session = ops.session()
    assert _put(ops, task).status_code == 404


def test_a_writer_switches_a_mode_on_and_off(ops: Ops, published: MemoryPublisher) -> None:
    task, _session = ops.session()
    writer = ops.scene.writer
    assert "modes" not in _task(ops, task)
    published.clear()
    response = _put(ops, task)
    assert response.status_code == 200, response.text
    record = response.json()["mode"]
    assert (record["mode"], record["way"], record["chosen_by"], record["origin_task"]) == (
        "test",
        "web",
        writer.id,
        None,
    )
    assert [frame["modes"] for frame in frames(published, "task")] == [[record]]
    assert _reconciles(published) == [ops.scene.device.id]
    [row] = audit_rows(ops.desk, "task.mode_on")
    assert (row[0], str(row[1])) == ("user", writer.id)
    assert row[3] == {
        "mode": "test",
        "version": 1,
        "way": "web",
        "chosen_by": writer.id,
        "operation_id": None,
        "origin_task": None,
    }
    assert _task(ops, task)["modes"] == [record]
    published.clear()
    assert _put(ops, task).json() == {"mode": record}  # unchanged: no audit, no frame
    assert list(published.items()) == []
    assert len(audit_rows(ops.desk, "task.mode_on")) == 1
    assert _delete(ops, task).json() == {}
    [off] = audit_rows(ops.desk, "task.mode_off")
    assert off[3] == {"mode": "test", "reason": "person", "chosen_by": writer.id, "operation_id": None}
    assert "modes" not in _task(ops, task)
    assert _delete(ops, task).status_code == 200  # off already: unchanged
    assert len(audit_rows(ops.desk, "task.mode_off")) == 1
    assert _put(ops, task).json()["mode"]["way"] == "web"  # on again


def test_refusals_of_the_person_routes(ops: Ops) -> None:
    task, _session = ops.session()
    assert _put(ops, task, client=ops.scene.reader.browser).status_code == 403
    assert _delete(ops, task, client=ops.scene.reader.browser).status_code == 403
    assert _put(ops, new_id()).status_code == 404
    assert _put(ops, task, "Bad-Name").status_code == 404
    assert _put(ops, task, "nothing").status_code == 404
    assert _delete(ops, task, "Bad-Name").status_code == 404
    assert audit_rows(ops.desk, "task.mode_on") == []


def test_a_mode_whose_needs_are_off_is_refused(ops: Ops, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog, "CATALOG", (catalog.CATALOG[0], NEEDY))
    task, _session = ops.session()
    refused = _put(ops, task, "needy")
    assert refused.status_code == 409
    assert (refused.json()["error"], refused.json()["detail"]) == ("mode_needs", {"modes": ["test"]})
    assert _put(ops, task).status_code == 200
    assert _put(ops, task, "needy").status_code == 200


def test_a_person_chooses_modes_when_creating_a_task(ops: Ops, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog, "CATALOG", (catalog.CATALOG[0], NEEDY))
    writer, task = ops.scene.writer, new_id()
    body = {
        "task_id": task,
        "wg_id": ops.desk.wg,
        "events": ops.desk.task_events(writer, task),
        "modes": ["needy", "test"],
    }
    response = writer.browser.post("/v1/tasks", json=body)
    assert response.status_code == 200, response.text
    assert [item["mode"] for item in response.json()["task"]["modes"]] == ["needy", "test"]
    assert {row[3]["way"] for row in audit_rows(ops.desk, "task.mode_on")} == {"web"}
    plain = new_id()
    body = {"task_id": plain, "wg_id": ops.desk.wg, "events": ops.desk.task_events(writer, plain)}
    assert "modes" not in writer.browser.post("/v1/tasks", json=body).json()["task"]


def test_a_persons_agent_chooses_with_the_tool(ops: Ops) -> None:
    task, _session = ops.session()
    agent = stored_agent(ops.hub, uuid.UUID(ops.scene.writer.id)).client(ops.hub)
    response = _put(ops, task, client=agent)
    assert response.status_code == 200, response.text
    assert (response.json()["mode"]["way"], response.json()["mode"]["chosen_by"]) == ("tool", ops.scene.writer.id)
    assert _delete(ops, task, client=agent).status_code == 200
    assert audit_rows(ops.desk, "task.mode_off")[0][3]["reason"] == "person"


def test_a_sessions_agent_needs_an_instruction(ops: Ops) -> None:
    task, session = ops.session()
    agent = _session_agent(ops, session).client(ops.hub)
    refused = _put(ops, task, client=agent)
    assert (refused.status_code, refused.json()["error"]) == (403, "mode_needs_instruction")
    other, _other_session = ops.session()
    assert _put(ops, other).status_code == 200
    assert _put(ops, other, client=agent).status_code == 403  # on elsewhere, but not on its own task
    assert _put(ops, task).status_code == 200
    assert _put(ops, other, client=agent).status_code == 403  # its own task has it, but it did not create `other`
    assert _delete(ops, task, client=agent).status_code == 200  # off is always possible
    assert audit_rows(ops.desk, "task.mode_off")[0][3]["reason"] == "session"


def _register(ops: Ops, session: str, triggered_by: str | None, op_type: str = "task.mode") -> str:
    body = ops.request(session, op_type)
    if triggered_by is not None:
        body["triggered_by"] = triggered_by
    response = ops.register(body)
    assert response.status_code == 200, response.text
    return str(body["operation_id"])


def _device_put(ops: Ops, task: str, operation: str, *, on: bool = True, device: Any = None, mode: str = "test") -> Any:
    client = ops.fleet.client(device or ops.device)
    return client.put(f"/v1/device/tasks/{task}/modes/{mode}", json={"on": on, "operation_id": operation})


def test_a_sentence_switches_a_mode_through_the_device(ops: Ops, published: MemoryPublisher) -> None:
    task, session = ops.session()
    writer = ops.scene.writer
    operation = _register(ops, session, writer.id)
    published.clear()
    response = _device_put(ops, task, operation)
    assert response.status_code == 200, response.text
    record = response.json()["mode"]
    assert (record["way"], record["chosen_by"]) == ("sentence", writer.id)
    [row] = audit_rows(ops.desk, "task.mode_on")
    assert (row[0], str(row[1])) == ("device", ops.device.id)
    assert row[3]["operation_id"] == operation
    assert _reconciles(published) == [ops.scene.device.id]
    assert _device_put(ops, task, operation).json() == {"mode": record}  # a retry of the executor
    off = _register(ops, session, None)
    assert _device_put(ops, task, off, on=False).json() == {"mode": None}
    assert audit_rows(ops.desk, "task.mode_off")[0][3] == {
        "mode": "test",
        "reason": "session",
        "chosen_by": writer.id,
        "operation_id": off,
    }


def test_the_device_route_accepts_only_its_own_open_task_mode_operation(ops: Ops) -> None:
    task, session = ops.session()
    other_task, _other = ops.session()
    reader_turn = _register(ops, session, ops.scene.reader.id)
    assert _device_put(ops, task, reader_turn).status_code == 403  # the instruction's author may only read
    nobody = _register(ops, session, None)
    assert _device_put(ops, task, nobody).status_code == 403
    writer_turn = _register(ops, session, ops.scene.writer.id)
    assert _device_put(ops, other_task, writer_turn).status_code == 404  # another task
    assert _device_put(ops, task, writer_turn, device=ops.scene.other_device).status_code == 404
    assert _device_put(ops, task, writer_turn, mode="nothing").status_code == 404
    assert _device_put(ops, new_id(), writer_turn).status_code == 404
    assert _device_put(ops, task, new_id()).status_code == 404
    comment = _register(ops, session, ops.scene.writer.id, "github.comment")
    assert _device_put(ops, task, comment).status_code == 404
    ops.finish(writer_turn)
    assert _device_put(ops, task, writer_turn).status_code == 404  # no longer open
    assert audit_rows(ops.desk, "task.mode_on") == []


def _driver(ops: Ops, task: str, at: str, step: str = "woken", device: Any = None) -> Any:
    state = {"step": step, "counters": {"runs": 1}, "next_at": None, "at": at}
    client = ops.fleet.client(device or ops.device)
    return client.put(f"/v1/device/tasks/{task}/modes/test/driver", json={"driver": state})


def test_the_driver_state_is_kept_while_the_mode_is_on(ops: Ops) -> None:
    task, _session = ops.session()
    assert _driver(ops, task, "2026-11-26T10:00:00.000Z").json() == {"on": False}
    assert _put(ops, task).status_code == 200
    assert _driver(ops, task, "2026-11-26T10:00:01.000Z").json() == {"on": True}
    assert _driver(ops, task, "2026-11-26T10:00:00.500Z", step="older").json() == {"on": True}
    stored = ops.desk.rows("SELECT driver_state FROM hub.task_mode WHERE task_id = %s", task)[0][0]
    assert (stored["step"], stored["at"]) == ("woken", "2026-11-26T10:00:01.000Z")
    assert _driver(ops, task, "2026-11-26T10:00:01.500Z").json() == {"on": True}  # same step: stored, not audited
    [row] = audit_rows(ops.desk, "task.mode_driver")
    assert (row[0], str(row[1]), row[3]) == ("device", ops.device.id, {"mode": "test", "step": "woken"})
    assert _driver(ops, new_id(), "2026-11-26T10:00:02.000Z").status_code == 404
    assert _delete(ops, task).status_code == 200
    assert _driver(ops, task, "2026-11-26T10:00:03.000Z").json() == {"on": False}
    assert _put(ops, task).status_code == 200  # on again: the driver starts afresh
    assert ops.desk.rows("SELECT driver_state FROM hub.task_mode WHERE task_id = %s", task)[0][0] is None
