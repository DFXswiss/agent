"""U-HUB-scheduler (inputs and re-scheduling): candidates from the database, and waiting sessions placed when a device
connects, reports capacity or is assigned (03-api.md section 4.6.2; ADR-013; R-0.3, R-0.4)."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest

from agent_hub import signals
from agent_hub.jobs import check_jobs
from agent_hub.registry import JOBS, SIGNAL_SUBSCRIBERS, concat_sequences, discover
from agent_hub.scheduler import jobs, load, reschedule
from agent_hub.scheduler import signals as scheduler_signals
from agent_hub.scheduler.core import NoPlacement, Placement
from agent_hub.sessions import transitions
from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.orgs.support import World
from tests.platform.hub.tasks.support import Desk, hints


def test_candidates_come_from_assignments_presence_capacity_and_vms(desk: Desk) -> None:
    device, other = desk.scene.device, desk.scene.other_device
    desk.vm(other, "stopped")
    desk.vm(other, "deleted")
    desk.vm(device, "pool", wg=desk.scene.draft.default_wg)
    with desk.hub.pool.connection() as connection:
        found = load.candidates(connection, desk.wg, desk.hub.clock(), 30)
        assert [(item.device_id, item.online, item.wg_vm) for item in found] == sorted(
            [(device.id, True, None), (other.id, True, "stopped")]
        )
        assert load.decide(connection, desk.wg, desk.hub.clock(), 30) == Placement(other.id, 6500)
    desk.online(device, seen=desk.hub.clock() - timedelta(seconds=30))
    desk.offline(other)
    with desk.hub.pool.connection() as connection:
        assert load.decide(connection, desk.wg, desk.hub.clock(), 30) == NoPlacement("device_offline")
        assert load.decide(connection, desk.scene.draft.default_wg, desk.hub.clock(), 30) is None


def test_removed_or_inactive_devices_are_no_candidates(desk: Desk) -> None:
    desk.execute("UPDATE hub.device SET state = 'decertified' WHERE id = %s", desk.scene.device.id)
    desk.execute("UPDATE hub.org_member SET removed_seq = 99 WHERE principal_id = %s", desk.scene.other_device.id)
    with desk.hub.pool.connection() as connection:
        assert load.candidates(connection, desk.wg, desk.hub.clock(), 30) == []


def waiting(desk: Desk) -> tuple[str, str]:
    desk.offline(desk.scene.device)
    desk.offline(desk.scene.other_device)
    return desk.started()


def test_presence_places_waiting_sessions(desk: Desk, published: MemoryPublisher) -> None:
    task, session = waiting(desk)
    scheduler_signals.bind(desk.hub.app.state.hub)
    published.clear()
    desk.online(desk.scene.other_device)
    signals.emit("device.presence", device_id=UUID(desk.scene.other_device.id), online=1)
    scheduler_signals.wait()
    assert desk.session(session)["device_id"] == desk.scene.other_device.id
    assert desk.task(task)["state"] == "starting"
    assert hints(published, desk.scene.other_device, "session.start") == [
        {"type": "session.start", "session_id": session}
    ]


def test_capacity_changes_the_reason_and_then_places(desk: Desk, published: MemoryPublisher) -> None:
    full = {
        "vm_memory_budget_mib": 8,
        "vm_memory_used_mib": 8,
        "vm_slots_total": 8,
        "vm_slots_free": 0,
        "pool_ready": 0,
    }
    task, session = waiting(desk)
    scheduler_signals.bind(desk.hub.app.state.hub)
    desk.online(desk.scene.device, full)
    signals.emit("device.presence", device_id=UUID(desk.scene.device.id), online=1)
    scheduler_signals.wait()
    detail = desk.task(task)
    assert (detail["state"], detail["state_reason"]) == ("scheduling", "capacity_exhausted")
    transitions = desk.rows("SELECT count(*) FROM hub.task_transition WHERE task_id = %s", task)
    # the same reason again changes nothing
    signals.emit("device.capacity", device_id=UUID(desk.scene.device.id))
    scheduler_signals.wait()
    assert desk.rows("SELECT count(*) FROM hub.task_transition WHERE task_id = %s", task) == transitions
    desk.set_capacity(desk.scene.device, {**full, "vm_slots_free": 2})
    signals.emit("device.capacity", device_id=UUID(desk.scene.device.id))
    scheduler_signals.wait()
    assert desk.session(session)["device_id"] == desk.scene.device.id


def test_an_assignment_in_the_log_places_waiting_sessions(world: World) -> None:
    desk = Desk.build(world)
    desk.offline(desk.scene.device)
    desk.online(desk.scene.device)
    desk.execute("UPDATE hub.wg_device SET unassigned_seq = 99 WHERE wg_id = %s", desk.wg)
    response = desk.create(desk.scene.writer)
    assert response.json()["error"] == "no_device"
    # a task that waits because its devices went away after it was created
    desk.execute("UPDATE hub.wg_device SET unassigned_seq = NULL WHERE wg_id = %s", desk.wg)
    desk.offline(desk.scene.device)
    task, session = desk.started()
    scheduler_signals.bind(desk.hub.app.state.hub)
    desk.online(desk.scene.device)
    signals.emit("log.appended", org_id=UUID(desk.org), seq=99)
    scheduler_signals.wait()
    assert desk.session(session)["device_id"] == desk.scene.device.id
    assert desk.task(task)["state"] == "starting"


def test_the_sweep_job_binds_and_places(desk: Desk) -> None:
    _, session = waiting(desk)
    closed_task, closed_session = desk.started()
    desk.scene.writer.browser.post(f"/v1/tasks/{closed_task}/close", json={})
    desk.online(desk.scene.device)
    assert jobs.sweep(desk.hub.app.state.hub) == 1
    assert scheduler_signals._context["hub"] is desk.hub.app.state.hub
    assert desk.session(session)["device_id"] == desk.scene.device.id
    assert desk.session(closed_session)["device_id"] is None
    assert jobs.sweep(desk.hub.app.state.hub) == 0


def test_a_session_that_changed_meanwhile_is_skipped(desk: Desk, monkeypatch: pytest.MonkeyPatch) -> None:
    _, session = waiting(desk)
    desk.online(desk.scene.device)
    context = desk.hub.app.state.hub
    real = transitions.lock_session

    def concurrent(connection: Any, session_id: Any) -> Any:
        """Another writer placed the session between the list and the lock."""
        connection.execute("UPDATE hub.session SET state = 'failed' WHERE id = %s", (session_id,))
        return real(connection, session_id)

    monkeypatch.setattr(reschedule, "lock_session", concurrent)
    assert reschedule.reschedule(context, [UUID(desk.wg)]) == 0
    assert desk.session(session)["device_id"] is None
    with desk.hub.pool.connection() as connection:
        assert reschedule.waiting_wgs(connection) == []


def test_without_assigned_devices_the_reason_is_device_offline(desk: Desk) -> None:
    full = {
        "vm_memory_budget_mib": 8,
        "vm_memory_used_mib": 8,
        "vm_slots_total": 8,
        "vm_slots_free": 0,
        "pool_ready": 0,
    }
    desk.online(desk.scene.device, full)
    desk.online(desk.scene.other_device, full)
    task, _ = desk.started()
    assert desk.task(task)["state_reason"] == "capacity_exhausted"
    desk.execute("UPDATE hub.wg_device SET unassigned_seq = 99 WHERE wg_id = %s", desk.wg)
    reschedule.reschedule(desk.hub.app.state.hub, [UUID(desk.wg)])
    assert desk.task(task)["state_reason"] == "device_offline"


def test_signals_without_context_or_with_a_closed_pool_do_nothing(desk: Desk, monkeypatch: pytest.MonkeyPatch) -> None:
    _, session = waiting(desk)
    desk.online(desk.scene.device)
    scheduler_signals._context.clear()  # the tasks route bound it
    signals.emit("device.presence", device_id=UUID(desk.scene.device.id), online=1)
    assert scheduler_signals._pending == []

    class Closed:
        closed = True

    class Context:
        pool = Closed()

    scheduler_signals.bind(Context())
    signals.emit("device.capacity", device_id=UUID(desk.scene.device.id))
    scheduler_signals.wait()
    assert desk.session(session)["device_id"] is None


def test_a_failing_reschedule_is_logged(
    desk: Desk, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(*_args: Any) -> int:
        raise RuntimeError("database gone")

    monkeypatch.setattr(scheduler_signals, "reschedule", broken)
    scheduler_signals.bind(desk.hub.app.state.hub)
    waiting(desk)
    with caplog.at_level(logging.ERROR, logger="agent_hub.scheduler"):
        signals.emit("log.appended", org_id=UUID(desk.org), seq=1)
        scheduler_signals.wait()
    assert "could not be re-scheduled" in caplog.text


def test_registrations_are_discovered() -> None:
    subscribers = dict(discover(SIGNAL_SUBSCRIBERS))["scheduler"]
    assert set(subscribers) == {"device.presence", "device.capacity", "log.appended", "credit.changed"}
    names = [job.name for job in check_jobs(concat_sequences(JOBS))]
    assert "scheduler.reschedule" in names
    assert jobs.JOBS[0].interval_seconds == 15.0


def test_placements_are_booked_on_the_candidates(desk: Desk) -> None:
    """Three waiting sessions and two devices with one free slot each: every placement uses the booked capacity."""
    one = {"vm_memory_budget_mib": 8, "vm_memory_used_mib": 0, "vm_slots_total": 8, "vm_slots_free": 1, "pool_ready": 0}
    sessions = sorted(waiting(desk)[1] for _ in range(3))  # same creation time: placed in ID order
    desk.online(desk.scene.device, one)
    desk.online(desk.scene.other_device, one)
    assert reschedule.reschedule(desk.hub.app.state.hub, [UUID(desk.wg)]) == 3
    first, second = sorted([desk.scene.device.id, desk.scene.other_device.id])
    assert [desk.session(session)["device_id"] for session in sessions] == [first, second, first]
