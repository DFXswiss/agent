"""U-HUB-failover (KOR-58, ADR-086): a device that stays away beyond `AGENT_HUB_DEVICE_FAILOVER_SECONDS` interrupts
its active sessions; the hub moves each task deterministically to another suitable device, ends the lost session
(`stopped`/`device_failed`), hands its prompts on in order (the interrupted instruction first), fails its running test
runs and never lets the lost device report for it again. Without another device the task waits and says so; a device
that comes back in time simply goes on."""

from __future__ import annotations

import threading
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest

from agent_hub.config import DEFAULT_DEVICE_FAILOVER_SECONDS
from agent_hub.crypto.encoding import format_time
from agent_hub.crypto.ids import uuid7
from agent_hub.db.pool import transaction
from agent_hub.errors import HubError
from agent_hub.events import store
from agent_hub.failover import jobs as failover_jobs
from agent_hub.failover import sweep as failover
from agent_hub.sessions import prompts
from agent_hub.sessions.effects import Effects
from agent_hub.stream import presence
from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.device.support import Fleet, org_device_scene
from tests.platform.hub.orgs.support import Actor, World
from tests.platform.hub.tasks.support import CAPACITY, Desk, audit_rows, hints

FAILOVER = DEFAULT_DEVICE_FAILOVER_SECONDS


@pytest.fixture
def two(world: World) -> Desk:
    """Two devices of the scene's organization, both online with room."""
    desk = Desk(org_device_scene(world))
    desk.online(desk.scene.device)
    desk.online(desk.scene.other_device)
    return desk


def context(desk: Desk) -> Any:
    return desk.hub.app.state.hub


def sweep(desk: Desk) -> int:
    return failover.sweep(context(desk))


def fail(desk: Desk, device: Actor, seconds: int = FAILOVER) -> None:
    """The device stops sending heartbeats: the expiry deletes its presence (`offline_since` = then), and `seconds`
    pass; the other devices that were online keep reporting."""
    window = context(desk).config.device_online_seconds
    others = [
        other
        for other in (desk.scene.device, desk.scene.other_device)
        if other.id != device.id and desk.rows("SELECT 1 FROM hub.device_presence WHERE device_id = %s", other.id)
    ]
    desk.hub.clock.advance(window + 1)
    for other in others:
        desk.online(other)
    with transaction(desk.hub.pool) as connection:
        gone = presence.expire(connection, desk.hub.clock(), window)
    assert gone == [UUID(device.id)]
    desk.hub.clock.advance(seconds)
    for other in others:
        desk.online(other)


def mains(desk: Desk, task: str) -> list[tuple[str, str, str | None, str | None, str | None]]:
    """`(session, state, state_reason, device, failover_of)` of the task's main sessions, oldest first."""
    return [
        (str(row[0]), row[1], row[2], None if row[3] is None else str(row[3]), None if row[4] is None else str(row[4]))
        for row in desk.rows(
            "SELECT id, state, state_reason, device_id, failover_of FROM hub.session WHERE task_id = %s AND depth = 0"
            " ORDER BY created_at, id",
            task,
        )
    ]


def routed(desk: Desk, task: str) -> dict[str, prompts.Routed]:
    with desk.hub.pool.connection() as connection:
        return prompts.routing(connection, [task])


def prompt_ids(desk: Desk, task: str) -> list[str]:
    return [
        str(row[0])
        for row in desk.rows("SELECT id FROM hub.event WHERE task_id = %s AND kind = 'prompt' ORDER BY scope_seq", task)
    ]


def state(fleet: Fleet, device: Actor) -> dict[str, Any]:
    response = fleet.client(device).get("/v1/device/state")
    assert response.status_code == 200, response.text
    return dict(response.json())


def attention(desk: Desk, task: str) -> tuple[str, str]:
    row = desk.rows("SELECT category, reason FROM hub.task_attention WHERE task_id = %s", task)[0]
    return row[0], row[1]


def working(desk: Desk, device: Actor | None = None) -> tuple[str, str]:
    """A task whose main session works on `device` (default the scene's device): the other device is away while the
    task is created and comes back afterwards."""
    chosen = device or desk.scene.device
    other = desk.scene.other_device if chosen.id == desk.scene.device.id else desk.scene.device
    present = bool(desk.rows("SELECT 1 FROM hub.device_presence WHERE device_id = %s", other.id))
    desk.offline(other)
    task, session = desk.started()
    desk.walk(session, "vm_assigning", "prompt_delivering", "working", device=chosen)
    if present:
        desk.online(other)
    return task, session


def test_the_job_is_registered() -> None:
    assert [job.name for job in failover_jobs.JOBS] == ["failover.sweep"]
    assert failover_jobs.JOBS[0].interval_seconds == failover_jobs.INTERVAL_SECONDS


def test_a_device_away_for_less_than_the_deadline_changes_nothing(two: Desk) -> None:
    task, session = working(two)
    fail(two, two.scene.device, FAILOVER - 1)
    assert sweep(two) == 0
    assert two.session(session)["interrupted_at"] is None
    assert two.task(task)["state"] == "working"


def test_with_one_device_the_task_is_interrupted_and_waits(two: Desk, published: MemoryPublisher) -> None:
    """Only the failed device could run the task: the session is marked, the task waits with `device_failed` (the
    category `interrupted`), one attention record `blocked/device_failed`; nothing moves, the next sweep changes
    nothing."""
    two.offline(two.scene.other_device)
    task, session = working(two)
    fail(two, two.scene.device)
    published.clear()
    assert sweep(two) == 0
    row = two.session(session)
    assert row["state"] == "working"
    assert row["interrupted_at"] is not None
    found = two.task(task)
    assert (found["state"], found["state_reason"], found["main_session_id"]) == ("scheduling", "device_failed", session)
    assert attention(two, task) == ("blocked", "device_failed")
    assert [(item[0], item[2]) for item in audit_rows(two, "session.interrupted")] == [("hub", session)]
    assert hints(published, kind="session.start") == []
    assert sweep(two) == 0
    assert len(audit_rows(two, "session.interrupted")) == 1


def test_a_device_back_in_time_goes_on(two: Desk) -> None:
    two.offline(two.scene.other_device)
    task, session = working(two)
    fail(two, two.scene.device)
    sweep(two)
    two.online(two.scene.device)
    assert sweep(two) == 0
    assert two.session(session)["interrupted_at"] is None
    assert two.task(task)["state"] == "working"
    assert [item[2] for item in audit_rows(two, "session.interruption_ended")] == [session]
    # the device reports as before
    two.transition(session, "idle")
    assert two.task(task)["state"] == "idle"


def test_a_working_task_moves_with_its_interrupted_instruction_first(
    two: Desk, fleet: Fleet, published: MemoryPublisher
) -> None:
    """The lost session ends `stopped`/`device_failed`; the successor starts on the other device, continues the lost
    session and gets the interrupted instruction again, then the waiting one; the lost device can no longer report."""
    device, other = two.scene.device, two.scene.other_device
    task, lost = working(two)
    two.hub.clock.advance(1)
    follow = two.prompt_event(two.scene.writer, task)
    assert two.prompt(two.scene.writer, task, follow).json()["session_id"] == lost
    fail(two, device)
    published.clear()
    assert sweep(two) == 1
    found = mains(two, task)
    successor = found[1][0]
    assert found == [
        (lost, "stopped", "device_failed", device.id, None),
        (successor, "requested", None, other.id, lost),
    ]
    first = desk_reason(two, successor)
    assert first == "device_failed"
    assert hints(published, other, "session.start") == [{"type": "session.start", "session_id": successor}]
    assert two.task(task)["state"] == "starting"
    assert two.session(lost)["interrupted_at"] is not None
    assert [p.event_id for p in routed(two, task)[successor].pending] == prompt_ids(two, task)
    listed = state(fleet, other)
    assert [item["session_id"] for item in listed["sessions"]] == [successor]
    assert listed["sessions"][0]["resume_from"] == lost
    assert listed["failovers"] == [{"session_id": successor, "from_session": lost, "cancelled_runs": 0}]
    assert listed["failover_seconds"] == FAILOVER
    assert [item["event_id"] for item in listed["pending_prompts"]] == prompt_ids(two, task)
    # the lost device comes back: nothing is listed for it, its reports are refused
    two.online(device)
    back = state(fleet, device)
    assert (back["sessions"], back["pending_prompts"], back["failovers"]) == ([], [], [])
    with pytest.raises(HubError):
        two.transition(lost, "idle", device=device)
    assert [(row[0], row[2], row[3]["reason"]) for row in audit_rows(two, "session.stopped")] == [
        ("hub", lost, "device_failed")
    ]
    # the interrupted instruction was delivered once: it cannot be withdrawn any more
    again = prompt_ids(two, task)[0]
    response = two.scene.writer.browser.post(f"/v1/tasks/{task}/prompts/{again}/withdraw", json={})
    assert response.status_code == 409, response.text
    # the successor accepts both instructions in order
    two.walk(successor, "vm_assigning", "prompt_delivering", "working", device=other)
    assert [p.event_id for p in routed(two, task)[successor].pending] == [follow["header"]["id"]]


def desk_reason(desk: Desk, session: str) -> str | None:
    row = desk.rows("SELECT reason FROM hub.session_transition WHERE session_id = %s AND seq = 1", session)[0]
    return None if row[0] is None else str(row[0])


def test_an_idle_task_ends_quietly_and_its_next_prompt_restores_elsewhere(two: Desk) -> None:
    device, other = two.scene.device, two.scene.other_device
    task, lost = working(two)
    two.transition(lost, "idle")
    fail(two, device)
    assert sweep(two) == 1
    assert mains(two, task) == [(lost, "stopped", "device_failed", device.id, None)]
    assert two.session(lost)["interrupted_at"] is None
    assert two.task(task)["state"] == "idle"
    two.hub.clock.advance(1)
    session = two.prompt(two.scene.writer, task).json()["session_id"]
    assert mains(two, task)[1][3] == other.id
    assert session != lost


def test_an_idle_session_without_another_device_is_left_alone(two: Desk) -> None:
    two.offline(two.scene.other_device)
    task, lost = working(two)
    two.transition(lost, "idle")
    fail(two, two.scene.device)
    assert sweep(two) == 0
    assert mains(two, task)[0][1] == "idle"
    assert two.task(task)["state"] == "idle"


def test_a_session_of_an_own_device_never_moves_to_a_dfx_device(two: Desk) -> None:
    two.execute("UPDATE hub.device SET kind = 'platform', owner_org = NULL WHERE id = %s", two.scene.other_device.id)
    task, _lost = working(two)
    fail(two, two.scene.device)
    assert sweep(two) == 0
    assert two.task(task)["state_reason"] == "device_failed"


def test_a_session_of_a_dfx_device_moves_to_any_candidate(two: Desk) -> None:
    two.execute("UPDATE hub.device SET kind = 'platform', owner_org = NULL WHERE id = %s", two.scene.device.id)
    task, _lost = working(two)
    fail(two, two.scene.device)
    assert sweep(two) == 1
    assert mains(two, task)[1][3] == two.scene.other_device.id


def test_an_own_device_without_another_own_device_waits(two: Desk) -> None:
    two.execute("DELETE FROM hub.device_presence WHERE device_id = %s", two.scene.other_device.id)
    two.execute("UPDATE hub.device SET kind = 'platform', owner_org = NULL WHERE id = %s", two.scene.other_device.id)
    task, _lost = working(two)
    fail(two, two.scene.device)
    assert sweep(two) == 0
    assert two.task(task)["state_reason"] == "device_failed"


def test_a_full_device_takes_nothing(two: Desk) -> None:
    two.set_capacity(two.scene.other_device, {**CAPACITY, "vm_slots_free": 0, "pool_ready": 0})
    task, _lost = working(two)
    fail(two, two.scene.device)
    two.set_capacity(two.scene.other_device, {**CAPACITY, "vm_slots_free": 0, "pool_ready": 0})
    assert sweep(two) == 0
    assert two.task(task)["state_reason"] == "device_failed"


def test_a_task_pinned_to_the_failed_device_waits_until_automatic(two: Desk) -> None:
    task, _lost = working(two)
    two.execute("UPDATE hub.task SET placement_device_id = %s WHERE id = %s", two.scene.device.id, task)
    fail(two, two.scene.device)
    assert sweep(two) == 0
    two.execute("UPDATE hub.task SET placement_device_id = NULL WHERE id = %s", task)
    assert sweep(two) == 1


def test_running_test_runs_of_the_lost_session_are_cancelled(two: Desk, fleet: Fleet) -> None:
    task, lost = working(two)
    runs = [operation(two, task, lost, "test.run", "executing"), operation(two, task, lost, "test.run", "accepted")]
    other = operation(two, task, lost, "github.create_pull_request", "executing")
    done = operation(two, task, lost, "test.run", "succeeded")
    fail(two, two.scene.device)
    assert sweep(two) == 1
    found = dict(two.rows("SELECT id::text, state || '/' || COALESCE(error_code, '') FROM hub.operation"))
    assert [found[item] for item in runs] == ["failed/device_failed", "failed/device_failed"]
    assert (found[other], found[done]) == ("failed/device_failed", "succeeded/")
    assert len(audit_rows(two, "operation.failed")) == 3
    successor = mains(two, task)[1][0]
    assert state(fleet, two.scene.other_device)["failovers"] == [
        {"session_id": successor, "from_session": lost, "cancelled_runs": 2}
    ]


def operation(desk: Desk, task: str, session: str, kind: str, phase: str) -> str:
    """An operation row of the session in the given state (the registration's columns)."""
    operation_id = uuid7()
    now = desk.hub.clock()
    desk.execute(
        "INSERT INTO hub.operation (id, org_id, wg_id, task_id, session_id, device_id, actor_id, type, state,"
        " created_at, decided_at) SELECT %s, org_id, wg_id, task_id, id, device_id, owner_id, %s, %s, %s, %s"
        " FROM hub.session WHERE id = %s",
        operation_id,
        kind,
        phase,
        now,
        now,
        session,
    )
    return operation_id


def test_a_closed_task_ends_its_sessions_on_the_failed_device(two: Desk) -> None:
    task, lost = working(two)
    two.transition(lost, "stopping")
    two.execute("UPDATE hub.task SET state = 'closed', closed_at = state_at WHERE id = %s", task)
    fail(two, two.scene.device)
    assert sweep(two) == 0
    assert mains(two, task) == [(lost, "stopped", "device_failed", two.scene.device.id, None)]


def test_a_subagent_on_the_failed_device_ends_when_its_main_session_lives_elsewhere(two: Desk) -> None:
    device, other = two.scene.device, two.scene.other_device
    task, main = working(two, other)
    child = uuid7()
    now = two.hub.clock()
    two.execute(
        "INSERT INTO hub.session (id, task_id, org_id, wg_id, owner_id, device_id, parent_session_id, depth, state,"
        " agent_kind, created_at) SELECT %s, task_id, org_id, wg_id, owner_id, %s, id, 1, 'working', agent_kind, %s"
        " FROM hub.session WHERE id = %s",
        child,
        device.id,
        now,
        main,
    )
    fail(two, device)
    assert sweep(two) == 0
    assert two.rows("SELECT state, state_reason FROM hub.session WHERE id = %s", child) == [
        ("stopped", "device_failed")
    ]
    assert two.task(task)["state"] == "working"


def test_a_task_another_writer_holds_is_taken_at_the_next_sweep(two: Desk) -> None:
    task, _lost = working(two)
    fail(two, two.scene.device)
    held, release = threading.Event(), threading.Event()

    def hold() -> None:
        with transaction(two.hub.pool) as connection:
            connection.execute("SELECT 1 FROM hub.task WHERE id = %s FOR UPDATE", (task,))
            held.set()
            release.wait(10)

    worker = threading.Thread(target=hold)
    worker.start()
    held.wait(10)
    try:
        assert sweep(two) == 0
    finally:
        release.set()
        worker.join()
    assert sweep(two) == 1


def test_a_device_that_reconnects_during_the_sweep_is_left_alone(two: Desk) -> None:
    task, _lost = working(two)
    fail(two, two.scene.device)
    two.online(two.scene.device)
    with transaction(two.hub.pool) as connection:
        settings = (FAILOVER, context(two).config.device_online_seconds)
        assert failover.handle(connection, UUID(task), two.hub.clock(), settings, Effects()) is False


def test_an_unknown_task_is_skipped(two: Desk) -> None:
    with transaction(two.hub.pool) as connection:
        assert failover.handle(connection, UUID(uuid7()), two.hub.clock(), (FAILOVER, 30), Effects()) is False
        failover.resume(connection, UUID(uuid7()), two.hub.clock(), 30, Effects())


def test_the_deadline_counts_from_the_first_moment_offline(two: Desk) -> None:
    """A second expiry or disconnect keeps the first time; a connect clears it."""
    device = two.scene.device
    first = two.hub.clock()
    with transaction(two.hub.pool) as connection:
        presence.went_offline(connection, [UUID(device.id)], first)
        presence.went_offline(connection, [UUID(device.id)], first + timedelta(seconds=9))
        presence.went_offline(connection, [], first)
    assert two.rows("SELECT offline_since FROM hub.device WHERE id = %s", device.id) == [(first,)]


def context_object(desk: Desk, task: str, session: str) -> str:
    """A complete checkpoint of the session (the columns the commit writes)."""
    object_id = uuid7()
    now = desk.hub.clock()
    desk.execute(
        "INSERT INTO hub.context_object (id, org_id, wg_id, task_id, session_id, author_id, state, chunks, size, epoch,"
        " digest, manifest_text, sig, created_at, updated_at, completed_at) SELECT %s, org_id, wg_id, task_id, id,"
        " device_id, 'complete', 1, 64, 1, 'digest', '{}', '\\x00', %s, %s, %s FROM hub.session WHERE id = %s",
        object_id,
        now,
        now,
        now,
        session,
    )
    return object_id


def test_the_successor_restores_the_checkpoint_pinned_at_the_move(two: Desk, fleet: Fleet) -> None:
    """A checkpoint the lost device uploads late (it comes back) never changes what the successor restores; the
    newest one is taken again once the pinned one is gone."""
    task, lost = working(two)
    pinned = context_object(two, task, lost)
    fail(two, two.scene.device)
    sweep(two)
    successor = mains(two, task)[1][0]
    two.hub.clock.advance(1)
    late = context_object(two, task, lost)
    listed = state(fleet, two.scene.other_device)["sessions"][0]
    assert listed["context"]["object_id"] == pinned
    two.execute("DELETE FROM hub.context_object WHERE id = %s", pinned)
    listed = state(fleet, two.scene.other_device)["sessions"][0]
    assert (listed["session_id"], listed["context"]["object_id"]) == (successor, late)


def test_a_session_a_user_stopped_gets_no_successor(two: Desk) -> None:
    task, lost = working(two)
    response = two.scene.writer.browser.post(f"/v1/sessions/{lost}/control", json={"action": "stop"})
    assert response.status_code == 200, response.text
    fail(two, two.scene.device)
    assert sweep(two) == 1
    assert [item[1:3] for item in mains(two, task)] == [("stopped", "device_failed")]
    assert two.task(task)["state"] == "idle"


def test_a_turn_a_user_interrupted_is_not_repeated(two: Desk) -> None:
    task, lost = working(two)
    two.hub.clock.advance(1)
    response = two.scene.writer.browser.post(f"/v1/sessions/{lost}/control", json={"action": "interrupt"})
    assert response.status_code == 200, response.text
    fail(two, two.scene.device)
    assert sweep(two) == 1
    assert len(mains(two, task)) == 1
    two.hub.clock.advance(1)
    follow = two.prompt_event(two.scene.writer, task)
    session = two.prompt(two.scene.writer, task, follow).json()["session_id"]
    assert [p.event_id for p in routed(two, task)[session].pending] == [follow["header"]["id"]]


def test_a_device_back_after_a_restart_continues_the_interrupted_session(two: Desk, published: MemoryPublisher) -> None:
    """With one device: the restarted runner reports the interrupted session failed; the hub starts its successor on
    any device - the same one - with the interrupted instruction again, instead of a failed task."""
    device = two.scene.device
    two.offline(two.scene.other_device)
    task, lost = working(two)
    fail(two, device)
    sweep(two)
    two.online(device)
    published.clear()
    two.transition(lost, "failed", "device_shutdown")
    found = mains(two, task)
    assert found[0][1:3] == ("failed", "device_shutdown")
    assert found[1][2:] == (None, device.id, lost)
    assert two.task(task)["state"] == "starting"
    assert [p.event_id for p in routed(two, task)[found[1][0]].pending] == prompt_ids(two, task)


def test_a_restart_report_of_a_session_that_was_not_interrupted_fails_as_before(two: Desk) -> None:
    task, lost = working(two)
    two.transition(lost, "failed", "device_shutdown")
    assert len(mains(two, task)) == 1
    assert two.task(task)["state"] == "failed"


def test_a_restart_report_of_an_idle_interrupted_session_ends_it(two: Desk) -> None:
    """Nothing to continue (the user interrupted the turn): the session ends, the task waits for the next prompt."""
    two.offline(two.scene.other_device)
    task, lost = working(two)
    two.scene.writer.browser.post(f"/v1/sessions/{lost}/control", json={"action": "interrupt"})
    fail(two, two.scene.device)
    sweep(two)
    two.online(two.scene.device)
    two.transition(lost, "failed", "guest_unreachable")
    assert len(mains(two, task)) == 1
    assert two.task(task)["state"] == "failed"


def test_an_attempt_after_a_refused_successor_keeps_the_failover_marks(two: Desk, fleet: Fleet) -> None:
    task, lost = working(two)
    pinned = context_object(two, task, lost)
    fail(two, two.scene.device)
    sweep(two)
    successor = mains(two, task)[1][0]
    two.walk(successor, "vm_assigning", device=two.scene.other_device)
    two.transition(successor, "failed", "capacity_exhausted", device=two.scene.other_device)
    attempt = mains(two, task)[2]
    assert attempt[4] == lost
    assert two.rows("SELECT restore_object::text FROM hub.session WHERE id = %s", attempt[0]) == [(pinned,)]


def test_a_device_that_is_connecting_is_not_moved(two: Desk) -> None:
    task, _lost = working(two)
    fail(two, two.scene.device)
    held, release = threading.Event(), threading.Event()

    def connecting() -> None:
        with transaction(two.hub.pool) as connection:
            connection.execute("SELECT 1 FROM hub.device WHERE id = %s FOR SHARE", (two.scene.device.id,))
            held.set()
            release.wait(10)

    worker = threading.Thread(target=connecting)
    worker.start()
    held.wait(10)
    try:
        assert sweep(two) == 0
    finally:
        release.set()
        worker.join()
    assert two.task(task)["state"] == "working"


def test_a_session_another_writer_holds_is_taken_at_the_next_sweep(two: Desk) -> None:
    task, lost = working(two)
    fail(two, two.scene.device)
    held, release = threading.Event(), threading.Event()

    def control() -> None:
        with transaction(two.hub.pool) as connection:
            connection.execute("SELECT 1 FROM hub.session WHERE id = %s FOR UPDATE", (lost,))
            held.set()
            release.wait(10)

    worker = threading.Thread(target=control)
    worker.start()
    held.wait(10)
    try:
        assert sweep(two) == 0
    finally:
        release.set()
        worker.join()
    assert sweep(two) == 1
    assert task


def test_events_a_cut_off_device_stamps_after_the_move_are_refused(two: Desk) -> None:
    task, lost = working(two)
    fail(two, two.scene.device)
    sweep(two)
    ended = two.rows("SELECT ended_at FROM hub.session WHERE id = %s", lost)[0][0]
    header = {"session": lost, "task": task, "scope_id": two.wg, "kind": "session.status"}
    with two.hub.pool.connection() as connection:
        before = store.session_device(connection, {**header, "ts": format_time(ended - timedelta(seconds=1))})
        after = store.session_device(connection, {**header, "ts": format_time(ended + timedelta(seconds=1))})
        unknown = store.session_device(connection, {**header, "session": uuid7(), "ts": format_time(ended)})
    assert (before, after, unknown) == (two.scene.device.id, None, None)


def test_a_heartbeat_clears_an_offline_mark_an_older_hub_left(two: Desk) -> None:
    device = two.scene.device
    two.execute("UPDATE hub.device SET offline_since = %s WHERE id = %s", two.hub.clock(), device.id)
    connected = two.rows("SELECT connected_at FROM hub.device_presence WHERE device_id = %s", device.id)[0][0]
    with transaction(two.hub.pool) as connection:
        beat = presence.heartbeat(
            connection, UUID(device.id), instance="test", connected_at=connected, now=two.hub.clock(), capacity=CAPACITY
        )
    assert beat.current
    assert two.rows("SELECT offline_since FROM hub.device WHERE id = %s", device.id) == [(None,)]


def test_a_session_still_starting_hands_on_its_first_prompt(two: Desk) -> None:
    """Nothing was accepted yet: the successor takes the undelivered first prompt, nothing is repeated."""
    two.offline(two.scene.other_device)
    task, lost = two.started()
    two.walk(lost, "vm_assigning")
    two.online(two.scene.other_device)
    fail(two, two.scene.device)
    assert sweep(two) == 1
    successor = mains(two, task)[1][0]
    assert [p.event_id for p in routed(two, task)[successor].pending] == prompt_ids(two, task)
    assert two.rows("SELECT redeliver_event_id FROM hub.session WHERE id = %s", lost) == [(None,)]


def test_the_slots_of_subagent_and_question_requests_are_freed(two: Desk) -> None:
    task, lost = working(two)
    held = [operation(two, task, lost, kind, "succeeded") for kind in ("subagent.request", "human.question")]
    kept = operation(two, task, lost, "github.create_pull_request", "succeeded")
    now = two.hub.clock()
    for index, operation_id in enumerate([*held, kept]):
        two.execute(
            "INSERT INTO hub.dedupe_slot (wg_id, epoch, tag, operation_id, expires_at) VALUES (%s, 1, %s, %s, %s)",
            two.wg,
            f"tag{index}",
            operation_id,
            now + timedelta(days=1),
        )
    fail(two, two.scene.device)
    sweep(two)
    assert [str(row[0]) for row in two.rows("SELECT operation_id FROM hub.dedupe_slot")] == [kept]


def test_a_task_without_a_returned_session_is_left_alone(two: Desk) -> None:
    task, _session = working(two)
    effects = Effects()
    with transaction(two.hub.pool) as connection:
        failover.resume(connection, UUID(task), two.hub.clock(), 30, effects)
    assert effects.frames == []


def test_a_sweep_between_the_reconnect_and_the_restart_report_keeps_the_continuation(two: Desk) -> None:
    """The restarted device is online (the sweep ends the interruption) before it reports the session failed: the
    successor still starts."""
    device = two.scene.device
    two.offline(two.scene.other_device)
    task, lost = working(two)
    fail(two, device)
    sweep(two)
    two.online(device)
    sweep(two)
    assert two.session(lost)["interrupted_at"] is None
    assert two.task(task)["state"] == "working"
    two.transition(lost, "failed", "guest_unreachable")
    assert mains(two, task)[1][4] == lost
    # a second failure later interrupts it again
    assert len(audit_rows(two, "session.interruption_ended")) == 1


def test_a_session_a_user_holds_is_resumed_at_the_next_sweep(two: Desk) -> None:
    two.offline(two.scene.other_device)
    task, lost = working(two)
    fail(two, two.scene.device)
    sweep(two)
    two.online(two.scene.device)
    held, release = threading.Event(), threading.Event()

    def control() -> None:
        with transaction(two.hub.pool) as connection:
            connection.execute("SELECT 1 FROM hub.session WHERE id = %s FOR UPDATE", (lost,))
            held.set()
            release.wait(10)

    worker = threading.Thread(target=control)
    worker.start()
    held.wait(10)
    try:
        sweep(two)
        assert two.task(task)["state_reason"] == "device_failed"
    finally:
        release.set()
        worker.join()
    sweep(two)
    assert two.task(task)["state"] == "working"


def test_the_end_of_a_test_run_from_the_lost_device_is_still_taken(two: Desk) -> None:
    task, lost = working(two)
    fail(two, two.scene.device)
    sweep(two)
    ended = two.rows("SELECT ended_at FROM hub.session WHERE id = %s", lost)[0][0]
    header = {"session": lost, "task": task, "scope_id": two.wg, "ts": format_time(ended + timedelta(minutes=5))}
    with two.hub.pool.connection() as connection:
        run = store.session_device(connection, {**header, "kind": "test.run"})
        other = store.session_device(connection, {**header, "kind": "session.status"})
    assert (run, other) == (two.scene.device.id, None)


def test_an_unchanged_task_is_not_touched_by_later_sweeps(two: Desk) -> None:
    two.offline(two.scene.other_device)
    task, _lost = working(two)
    fail(two, two.scene.device)
    sweep(two)
    before = two.task(task)["last_activity_at"]
    two.hub.clock.advance(60)
    sweep(two)
    assert two.task(task)["last_activity_at"] == before
