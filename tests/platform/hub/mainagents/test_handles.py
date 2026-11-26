"""U-HUB-mainagents (handles and names, ADR-095 sections 1 and 2; KOR-52): the number of a task in its organization
(trigger `task_number`), the place of a subagent below its main agent (trigger `session_agent_path`, serialised by the
task lock its callers hold), the event kind `task.agent_name` (people with the right to write in the workgroup and
their own-identity agents, never a reader) and the new fields of the task and session rows in the browser and device
API."""

from __future__ import annotations

import threading
from typing import Any

import psycopg
import pytest

from tests.platform.hub.agents.own import own_agent
from tests.platform.hub.attention.support import Situation
from tests.platform.hub.mainagents.support import NOT_CONTRACT
from tests.platform.hub.operations.support import Ops
from tests.platform.hub.orgs.support import new_id

SUBAGENT = "subagent.request"


def _child(ops: Ops, session: str) -> str:
    """The child session of an accepted `subagent.request` of `session` (known to the scene for its own requests)."""
    _operation, answer = ops.decide(session, SUBAGENT)
    child = str(answer["child_session_id"])
    ops.scene.sessions[child] = ops.scene.sessions[session]
    return child


def _path(ops: Ops, session: str) -> str | None:
    found = ops.desk.rows("SELECT agent_path FROM hub.session WHERE id = %s", session)
    return None if found[0][0] is None else str(found[0][0])


def _main(ops: Ops, task: str) -> str:
    """A further main session of the task on the scene's device (a follow-up after the first one ended)."""
    session = new_id()
    ops.desk.execute(
        "INSERT INTO hub.session (id, task_id, org_id, wg_id, owner_id, device_id, depth, state, created_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, 0, 'working', %s)",
        session,
        task,
        ops.desk.org,
        ops.desk.wg,
        ops.scene.writer.id,
        ops.device.id,
        ops.hub.clock(),
    )
    ops.scene.sessions[session] = (task, ops.desk.wg)
    return session


def test_tasks_are_numbered_per_organization(situation: Situation) -> None:
    first, _session = situation.started()
    second, _other = situation.started()
    numbers = situation.desk.rows("SELECT id::text, number FROM hub.task WHERE org_id = %s", situation.desk.org)
    assert sorted(number for _id, number in numbers) == [1, 2]
    assert dict(numbers)[first] < dict(numbers)[second]
    for task_id, number in numbers:
        row = situation.scene.writer.browser.get(f"/v1/tasks/{task_id}").json()["task"]
        assert (row["number"], row["name_event_id"]) == (number, None)
    # another organization counts on its own; after a number inserted explicitly (a restore, a repair) the counter
    # continues behind it instead of running into it
    other_org = new_id()
    with situation.hub.pool.connection() as connection, connection.transaction():
        for number in (None, 7, None):
            connection.execute(
                "INSERT INTO hub.task (id, org_id, wg_id, owner_id, state, created_at, state_at, last_activity_at,"
                " number) VALUES (%s, %s, %s, %s, 'working', now(), now(), now(), %s)",
                (new_id(), other_org, new_id(), new_id(), number),
            )
    assert situation.desk.rows("SELECT number FROM hub.task WHERE org_id = %s ORDER BY number", other_org) == [
        (1,),
        (7,),
        (8,),
    ]
    assert situation.desk.rows("SELECT last FROM hub.task_counter WHERE org_id = %s", other_org) == [(8,)]
    # a counter behind a number of the org (inserted before the counter knew it) skips over it
    situation.desk.execute("UPDATE hub.task_counter SET last = 2 WHERE org_id = %s", other_org)
    assert _numbered(situation, other_org) == 9
    # the first task of an org whose numbers were inserted explicitly starts behind them
    fresh_org = new_id()
    with situation.hub.pool.connection() as connection, connection.transaction():
        connection.execute(
            "INSERT INTO hub.task (id, org_id, wg_id, owner_id, state, created_at, state_at, last_activity_at,"
            " number) VALUES (%s, %s, %s, %s, 'working', now(), now(), now(), 4)",
            (new_id(), fresh_org, new_id(), new_id()),
        )
    assert _numbered(situation, fresh_org) == 5
    situation.insert_task(situation.desk.wg, situation.scene.writer.id)  # number 3 of the org
    with pytest.raises(psycopg.errors.UniqueViolation):
        situation.desk.execute(
            "INSERT INTO hub.task (id, org_id, wg_id, owner_id, state, created_at, state_at, last_activity_at, number)"
            " VALUES (%s, %s, %s, %s, 'working', now(), now(), now(), 3)",
            new_id(),
            situation.desk.org,
            situation.desk.wg,
            situation.scene.writer.id,
        )


def _numbered(situation: Situation, org: str) -> int:
    """The number the trigger gives a new task of `org`."""
    task = new_id()
    situation.desk.execute(
        "INSERT INTO hub.task (id, org_id, wg_id, owner_id, state, created_at, state_at, last_activity_at)"
        " VALUES (%s, %s, %s, %s, 'working', now(), now(), now())",
        task,
        org,
        new_id(),
        new_id(),
    )
    return int(situation.desk.rows("SELECT number FROM hub.task WHERE id = %s", task)[0][0])


def test_concurrent_task_creations_of_one_org_get_consecutive_numbers(situation: Situation) -> None:
    org = new_id()
    first = _numbered(situation, org)
    inserted, release = threading.Event(), threading.Event()
    numbers: dict[str, int] = {}

    def create(name: str, *, hold: bool) -> None:
        with situation.hub.pool.connection() as connection, connection.transaction():
            task = new_id()
            connection.execute(
                "INSERT INTO hub.task (id, org_id, wg_id, owner_id, state, created_at, state_at, last_activity_at)"
                " VALUES (%s, %s, %s, %s, 'working', now(), now(), now())",
                (task, org, new_id(), new_id()),
            )
            found = connection.execute("SELECT number FROM hub.task WHERE id = %s", (task,)).fetchone()
            numbers[name] = int(found[0]) if found is not None else 0
            if hold:
                inserted.set()
                assert release.wait(30)

    holder = threading.Thread(target=create, args=("first",), kwargs={"hold": True})
    holder.start()
    assert inserted.wait(30)
    waiter = threading.Thread(target=create, args=("second",), kwargs={"hold": False})
    waiter.start()
    waiter.join(0.5)
    assert waiter.is_alive()  # it waits for the counter row of the org
    release.set()
    holder.join(30)
    waiter.join(30)
    assert numbers == {"first": first + 1, "second": first + 2}


def test_subagents_get_their_places_below_the_main_agent(ops: Ops) -> None:
    task, main = ops.session()
    assert _path(ops, main) is None
    first, second = _child(ops, main), _child(ops, main)
    grandchild = _child(ops, first)
    assert [_path(ops, session) for session in (first, second, grandchild)] == ["1", "2", "1.1"]
    # ended children keep their places (and make room under the limit of running children)
    ops.desk.execute("UPDATE hub.session SET state = 'stopped' WHERE id IN (%s, %s)", second, grandchild)
    assert _path(ops, _child(ops, first)) == "1.2"
    # a child of a later main session continues the ordinals of the task's depth-1 sessions
    later = _main(ops, task)
    assert _path(ops, later) is None
    assert _path(ops, _child(ops, later)) == "3"
    # the session rows of the browser and the device API name the place
    sessions = ops.scene.writer.browser.get(f"/v1/tasks/{task}").json()["sessions"]
    assert {row["session"]["session_id"]: row["session"]["agent_path"] for row in sessions}[grandchild] == "1.1"
    assert ops.scene.writer.browser.get(f"/v1/sessions/{second}").json()["session"]["agent_path"] == "2"
    device = ops.fleet.client(ops.device).get(f"/v1/device/tasks/{task}").json()
    paths = {row["session_id"]: row["agent_path"] for row in device["sessions"]}
    assert (paths[main], paths[later], paths[first], paths[second]) == (None, None, "1", "2")
    assert device["task"]["number"] >= 1
    listed = ops.fleet.client(ops.device).get(f"/v1/device/workgroups/{ops.desk.wg}/tasks").json()["tasks"]
    assert {row["task_id"]: row["number"] for row in listed}[task] == device["task"]["number"]


def test_the_place_rules_hold_in_the_database(ops: Ops) -> None:
    task, main = ops.session()
    child = _child(ops, main)

    def insert(depth: int, parent: str | None, path: str | None) -> str:
        session = new_id()
        ops.desk.execute(
            "INSERT INTO hub.session (id, task_id, org_id, wg_id, owner_id, parent_session_id, depth, state,"
            " created_at, agent_path) VALUES (%s, %s, %s, %s, %s, %s, %s, 'stopped', now(), %s)",
            session,
            task,
            ops.desk.org,
            ops.desk.wg,
            ops.scene.writer.id,
            parent,
            depth,
            path,
        )
        return session

    # deeper than the hub lets a session ask for today, the rule still holds: the parent's path and a dot
    grandchild = insert(2, child, None)
    assert [_path(ops, grandchild), _path(ops, insert(3, grandchild, None))] == ["1.1", "1.1.1"]
    with pytest.raises(psycopg.errors.UniqueViolation):
        insert(1, main, "1")  # a handle of the task twice
    for depth, parent, path in ((0, None, "4"), (1, main, "0"), (1, main, "1."), (2, child, "1.01")):
        with pytest.raises(psycopg.errors.CheckViolation):
            insert(depth, parent, path)
    insert(1, main, "9")  # a given place stays (a restore); the next one continues after it
    assert _path(ops, _child(ops, main)) == "10"


def test_concurrent_children_under_the_task_lock_get_distinct_places(ops: Ops) -> None:
    task, main = ops.session()
    inserted, release = threading.Event(), threading.Event()
    paths: dict[str, str] = {}

    def start(name: str, *, hold: bool) -> None:
        with ops.hub.pool.connection() as connection, connection.transaction():
            connection.execute("SELECT id FROM hub.task WHERE id = %s FOR UPDATE", (task,))
            child = new_id()
            connection.execute(
                "INSERT INTO hub.session (id, task_id, org_id, wg_id, owner_id, parent_session_id, depth, state,"
                " created_at) VALUES (%s, %s, %s, %s, %s, %s, 1, 'requested', now())",
                (child, task, ops.desk.org, ops.desk.wg, ops.scene.writer.id, main),
            )
            found = connection.execute("SELECT agent_path FROM hub.session WHERE id = %s", (child,)).fetchone()
            paths[name] = str(found[0]) if found is not None else ""
            if hold:
                inserted.set()
                assert release.wait(30)

    holder = threading.Thread(target=start, args=("first",), kwargs={"hold": True})
    holder.start()
    assert inserted.wait(30)
    waiter = threading.Thread(target=start, args=("second",), kwargs={"hold": False})
    waiter.start()
    waiter.join(0.5)
    assert waiter.is_alive()  # it waits for the task lock
    release.set()
    holder.join(30)
    waiter.join(30)
    assert paths == {"first": "1", "second": "2"}


def _name_event(situation: Situation, author: Any, task: str) -> dict[str, Any]:
    return dict(situation.scene.wg_event(author, "task.agent_name", task=task))


def test_a_writer_and_its_agent_name_the_main_agent_a_reader_cannot(situation: Situation) -> None:
    scene = situation.scene
    task, _session = situation.started()
    first = _name_event(situation, scene.writer, task)
    response = scene.post(scene.writer, first)
    assert response.status_code == 200, response.text
    assert scene.writer.browser.get(f"/v1/tasks/{task}").json()["task"]["name_event_id"] == first["header"]["id"]
    # the own-identity agent of a writer writes with its user's right (ADR-082)
    own = own_agent(situation.hub, scene.writer, [scene.draft])
    by_agent = _name_event(situation, own.actor, task)
    response = own.agent.client(situation.hub).post("/v1/events", json={"events": [{"event": by_agent}]})
    assert response.status_code == 200, response.text
    assert situation.desk.rows("SELECT author_kind FROM hub.event WHERE id = %s", by_agent["header"]["id"]) == [
        ("agent",)
    ]
    newest = by_agent["header"]["id"]
    assert scene.writer.browser.get(f"/v1/tasks/{task}").json()["task"]["name_event_id"] == newest
    listed = scene.reader.browser.get(f"/v1/workgroups/{situation.desk.wg}/tasks").json()["tasks"]
    assert {row["task_id"]: row["name_event_id"] for row in listed}[task] == newest
    device = situation.ops.fleet.client(scene.device).get(f"/v1/device/tasks/{task}").json()["task"]
    assert device["name_event_id"] == newest
    # a reader may not rename it, and the name needs its task
    refused = scene.post(scene.reader, _name_event(situation, scene.reader, task), extensions=NOT_CONTRACT)
    assert (refused.status_code, refused.json()) == (400, {"error": "author_not_writer"})
    without_task = scene.post(scene.writer, scene.wg_event(scene.writer, "task.agent_name"), extensions=NOT_CONTRACT)
    assert (without_task.status_code, without_task.json()) == (400, {"error": "header_invalid"})
    assert situation.desk.rows("SELECT count(*) FROM hub.event WHERE kind = 'task.agent_name'") == [(2,)]
