"""U-HUB-scheduler (capacity refusal, KOR-14): a device that has no free VM slot fails a session start with
`capacity_exhausted` (05-runner.md section 3.4: "the hub then re-schedules"); the hub starts the next attempt of the
task at once, on another device with room or waiting until a device reports capacity, delivers the first prompt
exactly once and, from `AGENT_HUB_CAPACITY_REFUSALS` refusals on, holds the next attempt back instead of failing the
task (03-api.md section 4.6.2, ADR-049 amended by ADR-075 section 7; R-3.12)."""

from __future__ import annotations

import threading
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest

from agent_hub import signals
from agent_hub.scheduler import jobs, load, settings
from agent_hub.scheduler import signals as scheduler_signals
from agent_hub.scheduler.core import NoPlacement, Placement
from agent_hub.sessions import prompts
from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.device.support import Fleet, org_device_scene
from tests.platform.hub.orgs.support import Actor, World
from tests.platform.hub.tasks.support import CAPACITY, Desk, audit_rows, frames, hints

FULL = {**CAPACITY, "vm_slots_free": 0, "pool_ready": 0}
"""What the refusing device reports in its next heartbeat when no slot freed up meanwhile."""


def refuse(desk: Desk, session: str, device: Actor | None = None) -> None:
    """The runner's report of a refused start (`agent_runner.sessions.start`: `vm_assigning`, then `failed` with the
    reason `capacity_exhausted` raised by `VmManager` when no slot is free and no idle VM can be stopped)."""
    desk.walk(session, "vm_assigning", device=device)
    desk.transition(session, "failed", "capacity_exhausted", device=device)


def mains(desk: Desk, task: str) -> list[tuple[str, str, str | None, str | None]]:
    """`(session, state, device, first transition reason)` of the task's main sessions, oldest first."""
    return [
        (str(row[0]), row[1], None if row[2] is None else str(row[2]), row[3])
        for row in desk.rows(
            "SELECT s.id, s.state, s.device_id, (SELECT t.reason FROM hub.session_transition t"
            " WHERE t.session_id = s.id AND t.seq = 1) FROM hub.session s WHERE s.task_id = %s AND s.depth = 0"
            " ORDER BY s.created_at, s.id",
            task,
        )
    ]


def pending(desk: Desk, task: str) -> dict[str, list[str]]:
    with desk.hub.pool.connection() as connection:
        routed = prompts.routing(connection, [task])
    return {session: [p.event_id for p in item.pending] for session, item in routed.items()}


def first_prompt(desk: Desk, task: str) -> str:
    return str(
        desk.rows("SELECT id FROM hub.event WHERE task_id = %s AND kind = 'prompt' ORDER BY scope_seq", task)[0][0]
    )


def task_transitions(desk: Desk, task: str) -> list[tuple[str, str | None]]:
    return [
        (row[0], row[1])
        for row in desk.rows("SELECT state, reason FROM hub.task_transition WHERE task_id = %s ORDER BY seq", task)
    ]


def heartbeat(desk: Desk, device: Actor, capacity: dict[str, int]) -> None:
    """A heartbeat after the refusal (a later hub time) with the capacity the device reports then."""
    desk.hub.clock.advance(1)
    desk.online(device, capacity)


def test_a_refused_session_is_placed_on_a_second_device(
    desk: Desk, published: MemoryPublisher, task_signals: list[dict[str, Any]]
) -> None:
    device, other = desk.scene.device, desk.scene.other_device
    task, first = desk.started()
    assert desk.session(first)["device_id"] == device.id
    published.clear()
    desk.hub.clock.advance(1)
    refuse(desk, first)
    found = mains(desk, task)
    assert [(state, placed, reason) for _, state, placed, reason in found] == [
        ("failed", device.id, None),
        ("requested", other.id, "capacity_exhausted"),
    ]
    second = found[1][0]
    assert hints(published, kind="session.start") == [{"type": "session.start", "session_id": second}]
    assert hints(published, other, "session.start") != []
    detail = desk.task(task)
    assert (detail["state"], detail["state_reason"], detail["main_session_id"]) == ("starting", None, second)
    # the user never sees a failed task in between: no task transition, no `task.state` signal
    assert task_transitions(desk, task) == [("scheduling", None), ("starting", None)]
    assert [item["state"] for item in task_signals if item["task_id"] == UUID(task)] == ["starting"]
    assert pending(desk, task) == {first: [first_prompt(desk, task)], second: [first_prompt(desk, task)]}
    # the browser sees the new attempt with the reason of its first transition
    shown = [item for item in frames(published, "session") if item["session"]["session_id"] == second]
    assert shown[0]["transition"]["state"] == "requested"
    assert shown[0]["transition"]["reason"] == "capacity_exhausted"
    # audit: the device's refusal, then the hub's new attempt with its reason
    assert [(row[0], row[2], row[3]["reason"]) for row in audit_rows(desk, "session.failed")] == [
        ("device", first, "capacity_exhausted")
    ]
    assert [(row[0], row[2], row[3]["reason"]) for row in audit_rows(desk, "session.requested")] == [
        ("user", first, None),
        ("hub", second, "capacity_exhausted"),
    ]
    assert desk.session(second)["estimate_ms"] is not None


def test_the_first_prompt_is_delivered_exactly_once(world: World, fleet: Fleet) -> None:
    """The device state lists the next attempt only on the device it was placed on, with the first prompt pending;
    the refused session is left out everywhere, and once the new session works the prompt counts as delivered."""
    desk = Desk(org_device_scene(world))
    desk.online(desk.scene.device)
    desk.online(desk.scene.other_device, {**CAPACITY, "vm_slots_free": 1})
    task, first = desk.started()
    refuse(desk, first)
    second = mains(desk, task)[1][0]
    prompt = first_prompt(desk, task)
    refused = _state(fleet, desk.scene.device)
    assert refused["sessions"] == []
    assert refused["pending_prompts"] == []
    placed = _state(fleet, desk.scene.other_device)
    assert [item["session_id"] for item in placed["sessions"]] == [second]
    assert "resume_from" not in placed["sessions"][0]
    assert [(item["session_id"], item["event_id"]) for item in placed["pending_prompts"]] == [(second, prompt)]
    # a follow-up while the attempt starts is routed to it and delivered after the first prompt
    desk.hub.clock.advance(1)
    follow = desk.prompt_event(desk.scene.writer, task)
    assert desk.prompt(desk.scene.writer, task, follow).json()["session_id"] == second
    assert pending(desk, task)[second] == [prompt, follow["header"]["id"]]
    desk.walk(second, "vm_assigning", "prompt_delivering", "working", device=desk.scene.other_device)
    after = _state(fleet, desk.scene.other_device)
    assert [item["event_id"] for item in after["pending_prompts"]] == [follow["header"]["id"]]
    assert after["delivered_prompt_event_ids"] == {second: [prompt]}
    assert desk.task(task)["state"] == "working"


def _state(fleet: Fleet, device: Actor) -> dict[str, Any]:
    response = fleet.client(device).get("/v1/device/state")
    assert response.status_code == 200, response.text
    return dict(response.json())


def test_without_a_second_device_the_attempt_waits_for_capacity(desk: Desk, published: MemoryPublisher) -> None:
    """The refusing device counts as full until its next heartbeat (the hub's view of its capacity was behind): the
    attempt waits with the reason `capacity_exhausted` and starts when the device reports a free slot."""
    device = desk.scene.device
    desk.offline(desk.scene.other_device)
    task, first = desk.started()
    refuse(desk, first)
    second = mains(desk, task)[1][0]
    detail = desk.task(task)
    assert (detail["state"], detail["state_reason"]) == ("scheduling", "capacity_exhausted")
    assert desk.session(second)["device_id"] is None
    assert task_transitions(desk, task)[-1] == ("scheduling", "capacity_exhausted")
    # the stored capacity still shows free slots, but nothing is placed before the device reported again
    published.clear()
    assert jobs.sweep(desk.hub.app.state.hub) == 0
    assert hints(published, kind="session.start") == []
    # the next heartbeat reports the device full: still waiting, same reason
    heartbeat(desk, device, FULL)
    assert jobs.sweep(desk.hub.app.state.hub) == 0
    # a slot frees up: the capacity signal places the attempt on the same device
    scheduler_signals.bind(desk.hub.app.state.hub)
    heartbeat(desk, device, {**FULL, "vm_slots_free": 1})
    signals.emit("device.capacity", device_id=UUID(device.id))
    scheduler_signals.wait()
    assert desk.session(second)["device_id"] == device.id
    assert hints(published, device, "session.start") == [{"type": "session.start", "session_id": second}]
    assert desk.task(task)["state"] == "starting"
    assert pending(desk, task)[second] == [first_prompt(desk, task)]


def test_the_attempt_resumes_nothing_on_the_refusing_device(world: World, fleet: Fleet) -> None:
    """A refused session never ran an agent: an attempt on the same device does not name it in `resume_from`."""
    desk = Desk(org_device_scene(world))
    desk.online(desk.scene.device)
    task, first = desk.started()
    refuse(desk, first)
    second = mains(desk, task)[1][0]
    heartbeat(desk, desk.scene.device, CAPACITY)
    assert jobs.sweep(desk.hub.app.state.hub) == 1
    listed = _state(fleet, desk.scene.device)["sessions"]
    assert [item["session_id"] for item in listed] == [second]
    assert "resume_from" not in listed[0]


def test_refusals_at_the_limit_hold_the_attempt_back_and_never_fail_the_task(
    desk: Desk, published: MemoryPublisher, caplog: pytest.LogCaptureFixture
) -> None:
    """No back and forth faster than the hold: from the limit on the attempt waits (`capacity_exhausted`, never
    `failed`), is held back for `AGENT_HUB_REFUSAL_HOLD_SECONDS` and is then placed by the sweep; a follow-up starts a
    new row."""
    device = desk.scene.device
    desk.offline(desk.scene.other_device)
    task, session = desk.started()
    limit = settings.capacity_refusals()
    for _ in range(limit - 1):
        refuse(desk, session)
        session = mains(desk, task)[-1][0]
        heartbeat(desk, device, CAPACITY)
        assert jobs.sweep(desk.hub.app.state.hub) == 1
    published.clear()
    refuse(desk, session)
    held = mains(desk, task)[-1]
    assert len(mains(desk, task)) == limit + 1
    assert (held[1], held[2], held[3]) == ("requested", None, "capacity_exhausted")
    assert any("capacity_refusal_limit" in record.getMessage() for record in caplog.records)
    assert hints(published, kind="session.start") == []
    detail = desk.task(task)
    assert (detail["state"], detail["state_reason"]) == ("scheduling", "capacity_exhausted")
    heartbeat(desk, device, CAPACITY)
    assert jobs.sweep(desk.hub.app.state.hub) == 0  # held back
    desk.hub.clock.advance(settings.refusal_hold_seconds())
    desk.online(device, CAPACITY)
    assert jobs.sweep(desk.hub.app.state.hub) == 1
    assert desk.session(held[0])["device_id"] == device.id
    # a follow-up: a new row of attempts, placed at once
    desk.hub.clock.advance(1)
    again = desk.prompt_event(desk.scene.writer, task)
    desk.prompt(desk.scene.writer, task, again)
    refuse(desk, held[0])
    newest = mains(desk, task)[-1]
    assert (newest[1], newest[3]) == ("requested", "capacity_exhausted")


def test_a_refused_resume_carries_only_the_undelivered_prompts(desk: Desk) -> None:
    """A suspended session whose VM was idle-stopped needs a slot to resume; the refusal hands the follow-up (not the
    prompts it delivered before) to the next attempt."""
    task, first = desk.started()
    desk.walk(first, "vm_assigning", "prompt_delivering", "working", "idle", "suspended")
    desk.hub.clock.advance(1)
    follow = desk.prompt_event(desk.scene.writer, task)
    assert desk.prompt(desk.scene.writer, task, follow).json()["session_id"] == first
    desk.transition(first, "resuming")
    desk.transition(first, "failed", "capacity_exhausted")
    second = mains(desk, task)[1][0]
    assert pending(desk, task)[second] == [follow["header"]["id"]]
    assert desk.task(task)["state"] == "starting"


@pytest.mark.parametrize("reason", ["vm_start_failed", "guest_unreachable", "agent_start_failed"])
def test_other_failures_are_not_rescheduled(desk: Desk, published: MemoryPublisher, reason: str) -> None:
    task, first = desk.started()
    desk.walk(first, "vm_assigning")
    published.clear()
    desk.transition(first, "failed", reason)
    assert len(mains(desk, task)) == 1
    assert hints(published, kind="session.start") == []
    assert (desk.task(task)["state"], desk.task(task)["state_reason"]) == ("failed", reason)


def test_a_refused_subagent_or_older_session_starts_nothing(desk: Desk) -> None:
    """Only the newest main session of a task is re-scheduled: a refused child session or an older main session
    (a follow-up replaced it meanwhile) is left failed."""
    task, first = desk.started()
    desk.walk(first, "vm_assigning", "prompt_delivering", "working")
    child = "01900000-0000-7000-8000-00000000c41d"
    desk.execute(
        "INSERT INTO hub.session (id, task_id, org_id, wg_id, owner_id, device_id, parent_session_id, depth, state,"
        " agent_kind, created_at) SELECT %s, task_id, org_id, wg_id, owner_id, device_id, id, 1, 'requested',"
        " agent_kind, created_at FROM hub.session WHERE id = %s",
        child,
        first,
    )
    desk.transition(child, "failed", "capacity_exhausted")
    assert len(mains(desk, task)) == 1
    desk.transition(first, "stopped")
    desk.hub.clock.advance(1)
    second = desk.prompt(desk.scene.writer, task).json()["session_id"]
    assert second != first
    assert len(mains(desk, task)) == 2


def test_a_refusal_without_pending_prompts_starts_nothing(desk: Desk) -> None:
    """A resume the device started on its own (no prompt waiting) and could not get a slot for."""
    task, first = desk.started()
    desk.walk(first, "vm_assigning", "prompt_delivering", "working", "idle", "suspended", "resuming")
    desk.transition(first, "failed", "capacity_exhausted")
    assert len(mains(desk, task)) == 1
    assert (desk.task(task)["state"], desk.task(task)["state_reason"]) == ("failed", "capacity_exhausted")


@pytest.mark.parametrize("how", ["stop", "close"])
def test_a_user_stop_or_close_before_the_refusal_starts_nothing(
    desk: Desk, published: MemoryPublisher, how: str
) -> None:
    task, first = desk.started()
    desk.walk(first, "vm_assigning")
    browser = desk.scene.writer.browser
    if how == "stop":
        response = browser.post(f"/v1/sessions/{first}/control", json={"action": "stop"})
    else:
        response = browser.post(f"/v1/tasks/{task}/close", json={})
    assert response.status_code == 200, response.text
    published.clear()
    desk.transition(first, "failed", "capacity_exhausted")
    assert len(mains(desk, task)) == 1
    assert hints(published, kind="session.start") == []
    assert desk.task(task)["state"] == ("failed" if how == "stop" else "closed")


@pytest.mark.parametrize("placed", [True, False])
def test_closing_after_the_refusal_stops_the_attempt(desk: Desk, placed: bool) -> None:
    if not placed:
        desk.offline(desk.scene.other_device)
    task, first = desk.started()
    refuse(desk, first)
    second = mains(desk, task)[1][0]
    response = desk.scene.writer.browser.post(f"/v1/tasks/{task}/close", json={})
    assert response.status_code == 200, response.text
    assert desk.session(second)["state"] == ("stopping" if placed else "stopped")
    assert desk.task(task)["state"] == "closed"
    assert jobs.sweep(desk.hub.app.state.hub) == 0


def test_stopping_the_waiting_attempt_ends_it(desk: Desk) -> None:
    desk.offline(desk.scene.other_device)
    task, first = desk.started()
    refuse(desk, first)
    second = mains(desk, task)[1][0]
    response = desk.scene.writer.browser.post(f"/v1/sessions/{second}/control", json={"action": "stop"})
    assert response.status_code == 200, response.text
    assert desk.session(second)["state"] == "stopped"
    assert desk.task(task)["state"] == "idle"
    heartbeat(desk, desk.scene.device, CAPACITY)
    assert jobs.sweep(desk.hub.app.state.hub) == 0


def test_an_unassignment_before_the_refusal_places_the_attempt_elsewhere_or_waits(desk: Desk) -> None:
    """The refusing device was unassigned meanwhile: the attempt goes to the other device, and without one it waits
    as if every device were offline."""
    task, first = desk.started()
    desk.execute("UPDATE hub.wg_device SET unassigned_seq = 99 WHERE device_id = %s", desk.scene.device.id)
    refuse(desk, first)
    assert mains(desk, task)[1][2] == desk.scene.other_device.id
    alone, single = desk.started()
    desk.execute("UPDATE hub.wg_device SET unassigned_seq = 99 WHERE device_id = %s", desk.scene.other_device.id)
    refuse(desk, single, desk.scene.other_device)
    waiting = mains(desk, alone)[1]
    assert (waiting[1], waiting[2]) == ("requested", None)
    assert (desk.task(alone)["state"], desk.task(alone)["state_reason"]) == ("scheduling", "no_device")


def online_candidates(desk: Desk, wg: str | None = None) -> list[tuple[str, int, bool]]:
    """`(device, effective free slots, refused)` of the online candidates."""
    with desk.hub.pool.connection() as connection:
        found = load.candidates(connection, wg or desk.wg, desk.hub.clock(), 30)
    return [(item.device_id, item.capacity.vm_slots_free, item.refused) for item in found if item.online]


def decide(desk: Desk, wg: str | None = None) -> object:
    with desk.hub.pool.connection() as connection:
        return load.decide(connection, wg or desk.wg, desk.hub.clock(), 30)


def test_the_refusing_device_has_no_free_slot_until_its_next_heartbeat(desk: Desk) -> None:
    """Until the device reports again its stored capacity is older than the refusal: no free slot, for every
    workgroup; afterwards its report counts."""
    device = desk.scene.device
    desk.offline(desk.scene.other_device)
    _, first = desk.started()
    refuse(desk, first)
    assert online_candidates(desk) == [(device.id, 0, True)]
    assert decide(desk) == NoPlacement("capacity_exhausted")
    other_wg = desk.scene.draft.default_wg
    desk.execute(
        "INSERT INTO hub.wg_device (wg_id, device_id, assigned_seq) SELECT %s, device_id, assigned_seq"
        " FROM hub.wg_device WHERE wg_id = %s AND device_id = %s",
        other_wg,
        desk.wg,
        device.id,
    )
    assert online_candidates(desk, other_wg) == [(device.id, 0, False)]
    heartbeat(desk, device, CAPACITY)
    assert online_candidates(desk) == [(device.id, CAPACITY["vm_slots_free"], True)]
    assert isinstance(decide(desk), Placement)


@pytest.mark.parametrize(
    ("vm", "free", "placed"),
    [("running", 0, True), ("stopped", 0, False), ("assigning", 0, False), ("stopped", 1, True)],
)
def test_a_refusing_device_with_a_vm_of_the_workgroup(desk: Desk, vm: str, free: int, placed: bool) -> None:
    """A running VM of the workgroup needs no slot, so the device stays a candidate; after the refusal a stopped VM
    (its restart takes a slot) or one still starting no longer exempts the device from needing a free slot - no back
    and forth while the device stays full."""
    device = desk.scene.device
    desk.offline(desk.scene.other_device)
    _, first = desk.started()
    refuse(desk, first)
    desk.vm(device, vm)
    heartbeat(desk, device, {**FULL, "vm_slots_free": free})
    assert isinstance(decide(desk), Placement) is placed


def test_a_stopped_vm_exempts_a_full_device_without_a_refusal(desk: Desk) -> None:
    """Unchanged rule of 03-api.md section 4.6.2: a stopped VM of the workgroup makes a full device a candidate (the
    runner may stop an idle VM of another workgroup); a later session of the workgroup there clears a refusal."""
    device = desk.scene.device
    desk.offline(desk.scene.other_device)
    desk.vm(device, "stopped")
    heartbeat(desk, device, FULL)
    assert decide(desk) == Placement(device.id, 6500)
    task, first = desk.started()
    refuse(desk, first)
    heartbeat(desk, device, FULL)
    assert decide(desk) == NoPlacement("capacity_exhausted")
    heartbeat(desk, device, CAPACITY)
    assert jobs.sweep(desk.hub.app.state.hub) == 1
    heartbeat(desk, device, FULL)
    assert online_candidates(desk) == [(device.id, 0, False)]
    assert decide(desk) == Placement(device.id, 6500)
    assert desk.task(task)["state"] == "starting"


def test_routing_hands_pending_prompts_only_to_an_attempt(desk: Desk) -> None:
    """The prompts of a failed session go to the next main session only when that one is the hub's attempt after a
    refusal; a follow-up after any other failure gets its own prompt only ("Retry", 06-web.md section 4.4)."""
    at = desk.hub.clock()
    one, two = prompts.Prompt("p1", desk.wg, 1, at), prompts.Prompt("p2", desk.wg, 2, at + timedelta(seconds=2))
    failed = prompts.Main("s1", at, 0, "failed")
    attempt = prompts.Main("s2", at + timedelta(seconds=1), 0, "requested", retry=True)
    follow_up = prompts.Main("s2", at + timedelta(seconds=1), 0, "requested")
    assert [item.pending for item in prompts.route([one, two], [failed, attempt], "t")] == [(one,), (one, two)]
    assert [item.pending for item in prompts.route([one, two], [failed, follow_up], "t")] == [(one,), (two,)]


def test_an_attempt_that_worked_starts_a_new_row_of_refusals(desk: Desk) -> None:
    """Only refusals before a session ever worked count towards the limit: an attempt that ran and is refused later
    (a resume that needs its stopped VM restarted) is re-scheduled again."""
    device = desk.scene.device
    desk.offline(desk.scene.other_device)
    task, session = desk.started()
    refuse(desk, session)
    heartbeat(desk, device, CAPACITY)
    assert jobs.sweep(desk.hub.app.state.hub) == 1
    second = mains(desk, task)[-1][0]
    refuse(desk, second)
    heartbeat(desk, device, CAPACITY)
    assert jobs.sweep(desk.hub.app.state.hub) == 1
    third = mains(desk, task)[-1][0]
    desk.walk(third, "vm_assigning", "prompt_delivering", "working", "idle", "suspended")
    desk.hub.clock.advance(1)
    follow = desk.prompt_event(desk.scene.writer, task)
    assert desk.prompt(desk.scene.writer, task, follow).json()["session_id"] == third
    desk.transition(third, "resuming")
    desk.transition(third, "failed", "capacity_exhausted")
    fourth = mains(desk, task)[-1]
    assert (fourth[1], fourth[3]) == ("requested", "capacity_exhausted")
    assert pending(desk, task)[fourth[0]] == [follow["header"]["id"]]
    assert desk.task(task)["state"] == "scheduling"


def test_resume_from_skips_a_refused_session_that_never_worked(world: World, fleet: Fleet) -> None:
    """A session that ran on the device, then a follow-up's session refused there, then the attempt back on the same
    device: the attempt resumes from the session that ran."""
    desk = Desk(org_device_scene(world))
    desk.online(desk.scene.device)
    task, first = desk.started()
    desk.walk(first, "vm_assigning", "prompt_delivering", "working", "idle")
    desk.transition(first, "stopped")
    desk.hub.clock.advance(1)
    second = desk.prompt(desk.scene.writer, task).json()["session_id"]
    refuse(desk, second)
    third = mains(desk, task)[-1][0]
    heartbeat(desk, desk.scene.device, CAPACITY)
    assert jobs.sweep(desk.hub.app.state.hub) == 1
    listed = _state(fleet, desk.scene.device)["sessions"]
    assert [(item["session_id"], item.get("resume_from")) for item in listed] == [(third, first)]


def test_a_reconnect_without_a_heartbeat_keeps_the_device_full(desk: Desk) -> None:
    """A (re)connect writes the heartbeat time without a capacity report: the stored capacity stays untrusted until the
    first heartbeat after it."""
    device = desk.scene.device
    desk.offline(desk.scene.other_device)
    _, first = desk.started()
    refuse(desk, first)
    desk.hub.clock.advance(1)
    now = desk.hub.clock()
    desk.execute(
        "UPDATE hub.device_presence SET connected_at = %s, last_heartbeat_at = %s WHERE device_id = %s",
        now,
        now,
        device.id,
    )
    assert online_candidates(desk)[0][1] == 0
    heartbeat(desk, device, CAPACITY)
    assert online_candidates(desk)[0][1] == CAPACITY["vm_slots_free"]


def test_a_repeated_refusal_report_starts_no_second_attempt(desk: Desk, published: MemoryPublisher) -> None:
    task, first = desk.started()
    refuse(desk, first)
    published.clear()
    assert not desk.transition(first, "failed", "capacity_exhausted").changed
    assert len(mains(desk, task)) == 2
    assert hints(published, kind="session.start") == []


def test_stopping_the_refused_session_afterwards_leaves_the_attempt(desk: Desk) -> None:
    task, first = desk.started()
    refuse(desk, first)
    second = mains(desk, task)[1][0]
    response = desk.scene.writer.browser.post(f"/v1/sessions/{first}/control", json={"action": "stop"})
    assert response.status_code == 200, response.text
    assert desk.session(second)["state"] == "requested"
    assert desk.task(task)["state"] == "starting"


def test_two_refusals_of_one_device_in_one_heartbeat_window(desk: Desk) -> None:
    """Two tasks placed on the same device in one window, both refused: both attempts go to the other device."""
    other = desk.scene.other_device
    desk.offline(other)
    one, first = desk.started()
    two, second = desk.started()
    desk.online(other, CAPACITY)
    placed = desk.session(first)["device_id"]
    assert desk.session(second)["device_id"] == placed
    refuser = desk.scene.device if placed == desk.scene.device.id else other
    refuse(desk, first, refuser)
    refuse(desk, second, refuser)
    targets = {mains(desk, task)[1][2] for task in (one, two)}
    assert targets == ({other.id} if refuser is desk.scene.device else {desk.scene.device.id})


@pytest.mark.parametrize("how", ["stop", "close"])
def test_a_refusal_and_a_user_action_at_the_same_time(desk: Desk, how: str) -> None:
    """Both writers take the task row first (lock order of the tasks area). Close: whichever wins, the task ends
    closed with no live session. Stop: either the stop came first (no attempt, the task failed) or the refusal (the
    attempt goes on; the stop found the session ended)."""
    task, first = desk.started()
    desk.walk(first, "vm_assigning")
    barrier = threading.Barrier(2)
    failures: list[BaseException] = []
    browser = desk.scene.writer.browser

    def user() -> Any:
        if how == "close":
            return browser.post(f"/v1/tasks/{task}/close", json={})
        return browser.post(f"/v1/sessions/{first}/control", json={"action": "stop"})

    def run(action: Any) -> None:
        try:
            barrier.wait(10)
            action()
        except BaseException as error:  # reported by the main thread
            failures.append(error)

    threads = [
        threading.Thread(target=run, args=(lambda: desk.transition(first, "failed", "capacity_exhausted"),)),
        threading.Thread(target=run, args=(user,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert failures == []
    state = desk.task(task)["state"]
    count = len(mains(desk, task))
    if how == "close":
        assert state == "closed"
        live = desk.rows(
            "SELECT count(*) FROM hub.session WHERE task_id = %s AND state NOT IN ('stopping', 'stopped', 'failed')",
            task,
        )
        assert live == [(0,)]
    else:
        assert (state, count) in {("failed", 1), ("starting", 2)}
