"""U-HUB-tasks (create): `POST /v1/tasks` - one transaction, the scheduler's device and estimate, waiting tasks,
`no_device`, idempotency, rights and the request form (03-api.md sections 4.6, 4.6.1; R-0.2, R-0.3, R-2.6, R-4.8)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from agent_hub.scheduler import load
from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.device.support import Fleet
from tests.platform.hub.orgs.support import World, new_id
from tests.platform.hub.tasks.support import Desk, audit_rows, frames, hints


def test_first_task_is_placed_with_an_estimate(
    desk: Desk, published: MemoryPublisher, task_signals: list[dict[str, Any]]
) -> None:
    writer, device = desk.scene.writer, desk.scene.device
    task_id = new_id()
    events = desk.task_events(writer, task_id)
    response = desk.create(writer, task_id, events=events)
    assert response.status_code == 200, response.text
    body = response.json()
    session_id = body["session"]["session_id"]
    assert body["session"] == {
        "session_id": session_id,
        "state": "requested",
        "device_id": device.id,
        "estimate_ms": 3000,
    }
    assert "replayed" not in body
    task = body["task"]
    assert (task["task_id"], task["wg_id"], task["org_id"], task["owner_id"]) == (
        task_id,
        desk.wg,
        desk.org,
        writer.id,
    )
    assert (task["state"], task["state_reason"], task["closed_at"]) == ("starting", None, None)
    assert task["main_session_id"] == session_id
    assert task["title_event_id"] == events[0]["header"]["id"]
    assert task["created_at"] == task["state_at"] == task["last_activity_at"] == "2026-11-21T12:00:00.123Z"
    assert [item["id"] for item in body["events"]] == [event["header"]["id"] for event in events]
    assert [item["replayed"] for item in body["events"]] == [False, False]
    # rows: metadata only, agent claude-code (R-4.8), transitions of task and session
    session = desk.session(session_id)
    assert (session["depth"], session["parent_session_id"], session["agent_kind"]) == (0, None, "claude-code")
    transitions = desk.rows(
        "SELECT state, reason, by_id FROM hub.task_transition WHERE task_id = %s ORDER BY seq", task_id
    )
    assert transitions == [("scheduling", None, UUID(writer.id)), ("starting", None, UUID(writer.id))]
    assert desk.rows("SELECT state, reported_by FROM hub.session_transition WHERE session_id = %s", session_id) == [
        ("requested", None)
    ]
    # after the commit: hint, frames, signal
    assert hints(published, kind="session.start") == [{"type": "session.start", "session_id": session_id}]
    assert hints(published, device, "session.start")
    assert frames(published, "session")[-1]["session"]["device_id"] == device.id
    assert frames(published, "task")[-1]["state"] == "starting"
    assert task_signals == [{"task_id": UUID(task_id), "state": "starting"}]
    assert [item.frame for item in published.items() if item.audience.kind == "org"].count("event") == 2
    # audit
    assert [(row[0], row[2], row[3]) for row in audit_rows(desk, "task.create")] == [
        ("user", task_id, {"origin": "browser"})
    ]
    assert [row[3] for row in audit_rows(desk, "session.requested")] == [
        {"reason": None, "model_access": None, "model_id": None}
    ]


@pytest.mark.parametrize(
    ("vm_state", "estimate"), [("running", 2500), ("stopped", 6500), ("assigning", 3000), ("deleted", 3000)]
)
def test_estimate_follows_the_vm_of_the_workgroup(desk: Desk, vm_state: str, estimate: int) -> None:
    desk.vm(desk.scene.other_device, vm_state)
    body = desk.create(desk.scene.writer).json()
    expected = desk.scene.device if vm_state in ("assigning", "deleted") else desk.scene.other_device
    assert (body["session"]["device_id"], body["session"]["estimate_ms"]) == (expected.id, estimate)


def test_without_pool_the_estimate_is_a_clone(desk: Desk) -> None:
    desk.set_capacity(desk.scene.device, {**_capacity(free=7), "pool_ready": 0})
    assert desk.create(desk.scene.writer).json()["session"]["estimate_ms"] == 8000


def _capacity(free: int) -> dict[str, int]:
    return {
        "vm_memory_budget_mib": 65536,
        "vm_memory_used_mib": 0,
        "vm_slots_total": 8,
        "vm_slots_free": free,
        "pool_ready": 1 if free else 0,
    }


def test_offline_devices_leave_the_task_scheduling(desk: Desk, published: MemoryPublisher) -> None:
    desk.offline(desk.scene.device)
    desk.online(desk.scene.other_device, seen=desk.hub.clock().replace(year=2025))
    response = desk.create(desk.scene.writer)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["session"]["device_id"] is None
    assert body["session"]["estimate_ms"] is None
    assert (body["task"]["state"], body["task"]["state_reason"]) == ("scheduling", "device_offline")
    assert hints(published) == []
    transitions = desk.rows(
        "SELECT state, reason FROM hub.task_transition WHERE task_id = %s ORDER BY seq", body["task"]["task_id"]
    )
    assert transitions == [("scheduling", None), ("scheduling", "device_offline")]


def test_full_devices_leave_the_task_scheduling_with_capacity_exhausted(desk: Desk) -> None:
    desk.set_capacity(desk.scene.device, _capacity(free=0))
    desk.set_capacity(desk.scene.other_device, _capacity(free=0))
    body = desk.create(desk.scene.writer).json()
    assert (body["task"]["state"], body["task"]["state_reason"]) == ("scheduling", "capacity_exhausted")
    # a ready pool VM is a place for a new VM even without a free slot (it holds its slot already, ADR-060 section 3)
    desk.set_capacity(desk.scene.device, {**_capacity(free=0), "pool_ready": 1})
    assert desk.create(desk.scene.writer).json()["session"]["device_id"] == desk.scene.device.id


def test_without_an_active_device_the_task_waits_for_the_operator(desk: Desk, published: MemoryPublisher) -> None:
    """ADR-060 section 7: the task is always created; an org without platform access and without an own active device
    waits with `platform_access_required`."""
    desk.execute(
        "UPDATE hub.device SET state = 'decertified' WHERE id = ANY(%s::uuid[])",
        [desk.scene.device.id, desk.scene.other_device.id],
    )
    task_id = new_id()
    response = desk.create(desk.scene.writer, task_id)
    assert response.status_code == 200
    body = response.json()
    assert (body["task"]["state"], body["task"]["state_reason"]) == ("scheduling", "platform_access_required")
    assert body["session"]["device_id"] is None
    assert len(desk.rows("SELECT 1 FROM hub.event WHERE task_id = %s", task_id)) == 2
    assert published.items() != []


def test_a_private_org_without_assigned_devices_waits_for_an_assignment(world: World, fleet: Fleet) -> None:
    draft = world.private_org()
    desk = Desk.build(world)
    # a private org without assigned devices, and one certified platform device
    fleet.platform_device()
    task_id = new_id()
    founder = draft.founder
    meta = draft.event(founder, "task.meta", scope="wg", scope_id=draft.default_wg, wg=draft.default_wg, task=task_id)
    prompt = draft.event(founder, "prompt", scope="wg", scope_id=draft.default_wg, wg=draft.default_wg, task=task_id)
    response = desk.create(founder, task_id, wg=draft.default_wg, events=[meta, prompt])
    assert response.status_code == 200
    assert response.json()["task"]["state_reason"] == "no_device"


def test_reader_and_stranger_may_not_create(desk: Desk) -> None:
    for actor in (desk.scene.reader, desk.scene.stranger):
        task_id = new_id()
        response = desk.create(actor, task_id, events=desk.task_events(desk.scene.writer, task_id))
        assert response.status_code == 403
        assert response.json() == {"error": "forbidden"}


def test_unknown_workgroup_is_not_found(desk: Desk) -> None:
    wg, task_id = new_id(), new_id()
    response = desk.create(desk.scene.writer, task_id, wg=wg, events=desk.task_events(desk.scene.writer, task_id, wg))
    assert response.json() == {"error": "not_found"}


def test_archived_workgroup_refuses_writes(desk: Desk) -> None:
    desk.execute("UPDATE hub.wg SET archived_seq = 99 WHERE id = %s", desk.wg)
    assert desk.create(desk.scene.writer).status_code == 403


@pytest.mark.parametrize(
    ("change", "pointer"),
    [
        (lambda events: events.reverse(), "/events/0/header/kind"),
        (lambda events: events[1]["header"].update(kind="task.meta"), "/events/1/header/kind"),
        (lambda events: events[0]["header"].update(task=new_id()), "/events/0/header/task"),
        (lambda events: events[1]["header"].update(scope="org"), "/events/1/header/scope"),
        (lambda events: events[1]["header"].update(scope_id=new_id()), "/events/1/header/scope_id"),
        (lambda events: events[0].update(header="x"), "/events/0/header"),
        (lambda events: events[1].pop("header"), "/events/1/header"),
    ],
)
def test_events_must_name_the_task_in_the_workgroup(desk: Desk, change: Any, pointer: str) -> None:
    task_id = new_id()
    events = desk.task_events(desk.scene.writer, task_id)
    change(events)
    response = desk.create(desk.scene.writer, task_id, events=events, extensions={"contract": False})
    assert response.status_code == 400
    assert response.json() == {"error": "request_invalid", "detail": {"pointers": [pointer]}}


@pytest.mark.parametrize(("field", "value"), [("task_id", "not-a-uuid"), ("wg_id", "01900000-0000-7000-8000-0000")])
def test_ids_must_be_uuids(desk: Desk, field: str, value: str) -> None:
    task_id = new_id()
    body = {"task_id": task_id, "wg_id": desk.wg, "events": desk.task_events(desk.scene.writer, task_id)}
    body[field] = value
    response = desk.scene.writer.browser.post("/v1/tasks", json=body, extensions={"contract": False})
    assert response.status_code == 400
    assert response.json() == {"error": "request_invalid", "detail": {"pointers": [f"/{field}"]}}


def test_a_bad_signature_is_refused_by_the_event_checks(desk: Desk) -> None:
    task_id = new_id()
    events = desk.task_events(desk.scene.writer, task_id)
    events[1]["sig"] = events[0]["sig"]
    response = desk.create(desk.scene.writer, task_id, events=events)
    assert response.json() == {"error": "signature_invalid"}
    assert desk.rows("SELECT 1 FROM hub.task WHERE id = %s", task_id) == []


def test_repeated_create_is_replayed(desk: Desk, published: MemoryPublisher) -> None:
    writer = desk.scene.writer
    task_id = new_id()
    events = desk.task_events(writer, task_id)
    first = desk.create(writer, task_id, events=events).json()
    published.clear()
    again = desk.create(writer, task_id, events=events)
    assert again.status_code == 200
    body = again.json()
    assert body["replayed"] is True
    assert body["task"] == first["task"]
    assert body["session"] == first["session"]
    assert [item["scope_seq"] for item in body["events"]] == [item["scope_seq"] for item in first["events"]]
    assert all(item["replayed"] for item in body["events"])
    assert published.items() == []
    assert desk.rows("SELECT count(*) FROM hub.session WHERE task_id = %s", task_id) == [(1,)]


def test_a_known_task_id_with_other_content_is_a_conflict(desk: Desk) -> None:
    writer = desk.scene.writer
    task_id = new_id()
    events = desk.task_events(writer, task_id)
    assert desk.create(writer, task_id, events=events).status_code == 200
    other = desk.task_events(writer, task_id)
    assert desk.create(writer, task_id, events=other).json() == {"error": "id_conflict"}
    changed = [events[0], dict(events[1], sig=events[0]["sig"])]
    assert desk.create(writer, task_id, events=changed).json() == {"error": "event_conflict"}
    other_task = new_id()
    foreign = desk.task_events(writer, other_task)
    assert desk.create(writer, other_task, events=foreign).status_code == 200
    borrowed = dict(foreign[1], header=dict(foreign[1]["header"], task=task_id))
    assert desk.create(writer, task_id, events=[events[0], borrowed]).json() == {"error": "id_conflict"}


def test_a_known_task_id_of_another_owner_is_a_conflict(desk: Desk) -> None:
    task_id = new_id()
    assert desk.create(desk.scene.writer, task_id).status_code == 200
    response = desk.create(desk.scene.admin, task_id)
    assert response.status_code == 409
    assert response.json() == {"error": "id_conflict"}


def test_events_stored_before_without_task_are_a_conflict(desk: Desk) -> None:
    writer = desk.scene.writer
    for stored in (1, 2):
        task_id = new_id()
        events = desk.task_events(writer, task_id)
        assert desk.scene.post(writer, *events[2 - stored :]).status_code == 200
        response = desk.create(writer, task_id, events=events)
        assert response.status_code == 409
        assert response.json() == {"error": "id_conflict"}
        assert desk.rows("SELECT 1 FROM hub.task WHERE id = %s", task_id) == []


def test_a_concurrent_create_of_the_same_task_id_is_a_conflict(desk: Desk, monkeypatch: pytest.MonkeyPatch) -> None:
    """Another request with the same `task_id` (other events) commits between the check and the insert."""
    writer = desk.scene.writer
    task_id = new_id()
    real = load.decide

    def racing(*args: Any, **options: Any) -> Any:
        desk.execute(
            "INSERT INTO hub.task (id, org_id, wg_id, owner_id, state, created_at, state_at, last_activity_at)"
            " VALUES (%s, %s, %s, %s, 'scheduling', now(), now(), now())",
            task_id,
            desk.org,
            desk.wg,
            writer.id,
        )
        return real(*args, **options)

    monkeypatch.setattr(load, "decide", racing)
    response = desk.create(writer, task_id)
    assert (response.status_code, response.json()) == (409, {"error": "id_conflict"})
    assert desk.rows("SELECT count(*) FROM hub.event WHERE task_id = %s", task_id) == [(0,)]
