"""U-HUB-device-api (sessions sections): `sessions`, `pending_prompts` and `delivered_prompt_event_ids` of
`GET /v1/device/state` (03-api.md sections 4.6.3, 6.1)."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import UUID

from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.device.access import device_view
from agent_hub.device.state import StateRequest
from agent_hub.sessions import prompts
from agent_hub.sessions.device_state import provide
from tests.platform.hub.device.support import Fleet, org_device_scene
from tests.platform.hub.orgs.support import Actor, World, new_id
from tests.platform.hub.tasks.support import Desk


def org_desk(world: World) -> Desk:
    desk = Desk(org_device_scene(world))
    desk.online(desk.scene.device)
    return desk


def state(fleet: Fleet, device: Actor) -> dict[str, Any]:
    response = fleet.client(device).get("/v1/device/state")
    assert response.status_code == 200, response.text
    return dict(response.json())


def prompt_ids(desk: Desk, task: str) -> list[str]:
    return [
        str(row[0])
        for row in desk.rows("SELECT id FROM hub.event WHERE task_id = %s AND kind = 'prompt' ORDER BY scope_seq", task)
    ]


def test_a_new_session_with_its_first_prompt(world: World, fleet: Fleet) -> None:
    desk = org_desk(world)
    task, session = desk.started()
    answer = state(fleet, desk.scene.device)
    assert answer["sessions"] == [
        {
            "session_id": session,
            "task_id": task,
            "org_id": desk.org,
            "wg_id": desk.wg,
            "state": "requested",
            "parent_session_id": None,
            "depth": 0,
            "owner_id": desk.scene.writer.id,
            "model_access_pref": "auto",
            "model_id_pref": None,
            "github_identity": None,
            "task_github_identity": None,
            "fallback": {"own_to_dfx": False, "operator_to_platform": True},
            "model_consent": False,
        }
    ]
    first = prompt_ids(desk, task)
    scope_seq = desk.rows("SELECT scope_seq FROM hub.event WHERE id = %s", first[0])[0][0]
    assert answer["pending_prompts"] == [
        {"task_id": task, "session_id": session, "scope_id": desk.wg, "scope_seq": scope_seq, "event_id": first[0]}
    ]
    assert answer["delivered_prompt_event_ids"] == {session: []}
    assert state(fleet, desk.scene.other_device)["sessions"] == []


def test_delivery_follows_the_working_transitions(world: World, fleet: Fleet) -> None:
    desk = org_desk(world)
    desk.execute(
        "INSERT INTO hub.user_settings (user_id, model_access, model_id, locale, updated_at)"
        " VALUES (%s, 'platform', 'deepseek/deepseek-v4.1-flash-1017', 'de', %s)",
        desk.scene.writer.id,
        desk.hub.clock(),
    )
    task, session = desk.started()
    desk.walk(session, "vm_assigning", "prompt_delivering", "working")
    desk.prompt(desk.scene.writer, task)
    desk.prompt(desk.scene.writer, task)
    ids = prompt_ids(desk, task)
    answer = state(fleet, desk.scene.device)
    assert answer["sessions"][0]["model_access_pref"] == "platform"
    assert answer["sessions"][0]["model_id_pref"] == "deepseek/deepseek-v4.1-flash-1017"
    # the owner's fallback settings and the task's consent (KOR-50, ADR-081 sections 1 and 5)
    desk.execute(
        "UPDATE hub.user_settings SET fallback_own_to_dfx = true, fallback_operator_to_platform = false"
        " WHERE user_id = %s",
        desk.scene.writer.id,
    )
    desk.execute("UPDATE hub.task SET model_consent = true WHERE id = %s", task)
    found = state(fleet, desk.scene.device)["sessions"][0]
    assert (found["fallback"], found["model_consent"]) == ({"own_to_dfx": True, "operator_to_platform": False}, True)
    assert [item["event_id"] for item in answer["pending_prompts"]] == ids[1:]
    assert answer["delivered_prompt_event_ids"] == {session: ids[:1]}
    desk.walk(session, "idle", "working")
    answer = state(fleet, desk.scene.device)
    assert [item["event_id"] for item in answer["pending_prompts"]] == ids[2:]
    assert answer["delivered_prompt_event_ids"] == {session: ids[:2]}
    desk.transition(session, "stopping")
    answer = state(fleet, desk.scene.device)
    assert answer["sessions"][0]["state"] == "stopping"
    assert answer["pending_prompts"] == []
    assert answer["delivered_prompt_event_ids"] == {session: ids[:2]}
    desk.transition(session, "stopped")
    answer = state(fleet, desk.scene.device)
    # the undelivered prompt goes to the next main session the hub started for it (KOR-11, NA-04)
    (following,) = answer["sessions"]
    assert following["session_id"] != session
    assert following["resume_from"] == session
    assert [(item["session_id"], item["event_id"]) for item in answer["pending_prompts"]] == [
        (following["session_id"], ids[2])
    ]
    assert answer["delivered_prompt_event_ids"] == {following["session_id"]: []}


def test_a_restart_on_the_same_device_resumes(world: World, fleet: Fleet) -> None:
    desk = org_desk(world)
    task, first = desk.started()
    desk.walk(first, "prompt_delivering", "working")
    desk.prompt(desk.scene.writer, task)  # never delivered: the session fails first
    desk.transition(first, "failed", "agent_exited")
    desk.hub.clock.advance(3)
    second = desk.prompt(desk.scene.writer, task).json()["session_id"]
    ids = prompt_ids(desk, task)
    answer = state(fleet, desk.scene.device)
    assert [item["session_id"] for item in answer["sessions"]] == [second]
    assert answer["sessions"][0]["resume_from"] == first
    assert [item["event_id"] for item in answer["pending_prompts"]] == ids[2:]
    assert answer["delivered_prompt_event_ids"] == {second: []}


def test_a_restart_on_another_device_resumes_too(world: World, fleet: Fleet) -> None:
    """The conversation continues on any device (ADR-072): the next main session names the previous one wherever it
    ran; the runner restores the task's checkpoint there."""
    desk = org_desk(world)
    task, first = desk.started()
    desk.transition(first, "stopped")
    desk.offline(desk.scene.device)
    desk.online(desk.scene.other_device)
    desk.hub.clock.advance(1)
    second = desk.prompt(desk.scene.writer, task).json()["session_id"]
    answer = state(fleet, desk.scene.other_device)
    assert [item["session_id"] for item in answer["sessions"]] == [second]
    assert answer["sessions"][0]["resume_from"] == first
    # a third main session back on the first device resumes from the second, which ran on the other device
    desk.transition(second, "stopped", device=desk.scene.other_device)
    desk.offline(desk.scene.other_device)
    desk.online(desk.scene.device)
    desk.hub.clock.advance(1)
    third = desk.prompt(desk.scene.writer, task).json()["session_id"]
    found = state(fleet, desk.scene.device)["sessions"]
    assert [item["session_id"] for item in found] == [third]
    assert found[0]["resume_from"] == second


def test_children_and_unassigned_workgroups(world: World, fleet: Fleet) -> None:
    desk = org_desk(world)
    task, main = desk.started()
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
        desk.hub.clock() + timedelta(seconds=1),
    )
    answer = state(fleet, desk.scene.device)
    assert [(item["session_id"], item["depth"], item["parent_session_id"]) for item in answer["sessions"]] == [
        (main, 0, None),
        (child, 1, main),
    ]
    assert "resume_from" not in answer["sessions"][1]
    assert set(answer["delivered_prompt_event_ids"]) == {main}
    # a device unassigned in the verified log reads no session of that workgroup any more
    with desk.hub.pool.connection() as connection:
        view = device_view(connection, desk.scene.device.id, desk.hub.config.platform_root_key)
        unassigned = replace(view, orgs=tuple(replace(item, wgs=(), reads_org=False) for item in view.orgs))
        request = StateRequest(
            connection=connection,
            device_id=UUID(desk.scene.device.id),
            view=unassigned,
            now=desk.hub.clock(),
            config=desk.hub.config,
        )
        assert provide(request) == {
            "sessions": [],
            "pending_prompts": [],
            "delivered_prompt_event_ids": {},
            "prompt_queue": {"via_agent": [], "withdrawn": []},
        }


def test_only_the_last_hundred_delivered_prompts(world: World, fleet: Fleet) -> None:
    desk = org_desk(world)
    task, session = desk.started()
    desk.scene.append(desk.scene.writer, *[desk.prompt_event(desk.scene.writer, task) for _ in range(101)])
    now = desk.hub.clock()
    with desk.hub.pool.connection() as connection, connection.transaction():
        for seq in range(2, 2 + 2 * 102):
            connection.execute(
                "INSERT INTO hub.session_transition (session_id, seq, state, at, received_at) VALUES (%s, %s, %s, %s,"
                " %s)",
                (session, seq, "working" if seq % 2 == 0 else "idle", now, now),
            )
        connection.execute("UPDATE hub.session SET state = 'idle' WHERE id = %s", (session,))
    ids = prompt_ids(desk, task)
    assert len(ids) == 102
    answer = state(fleet, desk.scene.device)
    assert answer["delivered_prompt_event_ids"] == {session: ids[2:]}
    assert answer["pending_prompts"] == []


def test_session_of_a_prompt_before_any_session(desk: Desk) -> None:
    task, session = desk.started()
    with desk.hub.pool.connection() as connection:
        assert prompts.session_of(connection, task, desk.hub.clock()) == session
        assert prompts.session_of(connection, task, desk.hub.clock() - timedelta(seconds=1)) is None


def test_a_prompt_while_stopping_never_resumes_the_stopping_session(world: World, fleet: Fleet) -> None:
    desk = org_desk(world)
    task, first = desk.started()
    desk.walk(first, "prompt_delivering", "working", "stopping")
    desk.hub.clock.advance(1)
    second = desk.prompt(desk.scene.writer, task).json()["session_id"]
    assert second != first
    desk.hub.clock.advance(1)
    desk.transition(first, "stopped")
    found = state(fleet, desk.scene.device)["sessions"]
    assert [item["session_id"] for item in found] == [second]
    assert "resume_from" not in found[0]


def test_a_refused_prompt_is_consumed(world: World, fleet: Fleet) -> None:
    desk = org_desk(world)
    task, session = desk.started()
    desk.walk(session, "prompt_delivering", "working", "idle")
    desk.prompt(desk.scene.writer, task)
    desk.prompt(desk.scene.writer, task)
    ids = prompt_ids(desk, task)
    with desk.hub.pool.connection() as connection, connection.transaction():
        append_audit(
            connection,
            AuditRow(
                at=desk.hub.clock(),
                actor_kind="device",
                actor_id=UUID(desk.scene.device.id),
                action="session.prompt_refused",
                result="ok",
                session_id=UUID(session),
                detail={"reason": "unregistered"},
            ),
        )
    desk.transition(session, "working")
    answer = state(fleet, desk.scene.device)
    assert answer["pending_prompts"] == []
    assert answer["delivered_prompt_event_ids"] == {session: ids}


def test_a_session_of_a_closed_task_says_so(world: World, fleet: Fleet) -> None:
    """ADR-080 section 5: the runner deletes the work VMs of a closed task at once; absent means open."""
    desk = org_desk(world)
    task, session = desk.started()
    _other, open_session = desk.started()
    desk.execute("UPDATE hub.task SET state = 'closed', closed_at = %s WHERE id = %s", desk.hub.clock(), task)
    found = {item["session_id"]: item for item in state(fleet, desk.scene.device)["sessions"]}
    assert found[session]["task_closed"] is True
    assert "task_closed" not in found[open_session]
