"""U-HUB-mainagents (reads): `GET /v1/orgs/{org}/people/agents` and `GET /v1/orgs/{org}/main-agents` - the caller's
row first, then the people by owner, their counters, the open main agents of a person newest activity first, both
cursors page by page and the rights: a member who cannot read a workgroup never sees its tasks, not in a row and not in
a counter (03-api.md section 4.6, ADR-095 section 5; KOR-52). Every answer is checked against its contract by the test
client."""

from __future__ import annotations

from datetime import timedelta

import pytest

from agent_hub.mainagents import read
from tests.platform.hub.attention.support import Situation
from tests.platform.hub.events.support import Json
from tests.platform.hub.mainagents.support import (
    NOT_CONTRACT,
    agents,
    all_agents,
    all_people,
    hidden_workgroup,
    people,
    task,
)
from tests.platform.hub.orgs.support import new_id


def _person(owner: str, agents_: int, *, nd: int = 0, blocked: int = 0, working: int = 0, active: int = 0) -> Json:
    return {
        "owner_id": owner,
        "agents": agents_,
        "needs_decision": nd,
        "blocked": blocked,
        "working": working,
        "subagents_active": active,
    }


def test_the_caller_comes_first_then_the_people_by_owner(situation: Situation) -> None:
    scene = situation.scene
    reader = scene.reader
    now = situation.hub.clock()
    others = sorted(new_id() for _ in range(5))
    task(situation, reader.id, category="needs_decision")
    task(situation, reader.id, category="working", active=2)
    task(situation, reader.id, category="blocked", active=1)
    task(situation, reader.id)  # no attention record yet: an agent counted in no category
    task(situation, reader.id, state="closed", category="done")  # closed: not a main agent to show
    for index, owner in enumerate(others):
        for minute in range(index + 1):
            task(situation, owner, at=now - timedelta(minutes=minute), category="working", active=1)
    gone = new_id()
    task(situation, gone, state="closed")  # a person with only closed tasks is not listed
    expected = [_person(reader.id, 4, nd=1, blocked=1, working=1, active=3)] + [
        _person(owner, index + 1, working=index + 1, active=index + 1) for index, owner in enumerate(others)
    ]
    for limit in ("1", "2", "4", "6", "100"):
        assert all_people(situation, reader, limit=limit) == expected, limit
    assert all_people(situation, reader) == expected
    first = people(reader, situation.desk.org, limit="1").json()
    assert first == {"people": expected[:1], "next_after": reader.id}  # after the caller: the first of the others
    second = people(reader, situation.desk.org, limit="2", after=reader.id).json()
    assert second == {"people": expected[1:3], "next_after": others[1]}
    assert people(reader, situation.desk.org, after=others[3]).json() == {"people": expected[5:], "next_after": None}
    assert people(reader, situation.desk.org, after=others[4]).json() == {"people": [], "next_after": None}
    # a caller without open main agents starts with the others
    everybody = sorted([reader.id, *others])
    assert [row["owner_id"] for row in all_people(situation, scene.writer, limit="2")] == everybody
    assert read.LIST_DEFAULT == 50


def test_a_member_never_sees_a_workgroup_it_cannot_read(situation: Situation) -> None:
    scene = situation.scene
    reader, writer = scene.reader, scene.writer
    hidden = hidden_workgroup(situation)
    shared, secret = new_id(), new_id()
    visible = task(situation, shared, category="needs_decision", active=1)
    task(situation, shared, wg=hidden, category="needs_decision", active=5)
    task(situation, shared, wg=hidden, category="blocked")
    task(situation, secret, wg=hidden, category="working", active=2)
    task(situation, reader.id, wg=hidden)  # even the caller's own task in a workgroup it cannot read
    assert all_people(situation, reader) == [_person(shared, 1, nd=1, active=1)]
    assert [item["task"]["task_id"] for item in all_agents(situation, reader, shared)] == [visible]
    assert all_agents(situation, reader, secret) == []
    assert all_agents(situation, reader, reader.id) == []
    seen = {row["owner_id"]: row for row in all_people(situation, writer)}
    assert seen[shared] == _person(shared, 3, nd=2, blocked=1, active=6)
    assert seen[secret] == _person(secret, 1, working=1, active=2)
    assert seen[reader.id]["agents"] == 1
    assert len(all_agents(situation, writer, shared)) == 3
    lonely = scene.world.user()
    scene.world.join(scene.draft, lonely)
    assert all_people(situation, lonely) == []
    assert all_agents(situation, lonely, shared) == []


def test_the_main_agents_of_a_person_newest_activity_first(situation: Situation) -> None:
    scene = situation.scene
    owner = scene.writer.id
    now = situation.hub.clock()
    minutes = [5, 1, 9, 1, 3, 7, 1]
    created = [
        task(situation, owner, at=now - timedelta(minutes=minute), category="working" if minute % 2 else None)
        for minute in minutes
    ]
    task(situation, owner, state="closed", at=now)
    task(situation, new_id(), at=now)  # another person's
    expected = [created_id for _key, created_id in sorted(zip(minutes, created, strict=True), key=_newest)]
    for limit in ("1", "2", "3", "7", "100"):
        listed = all_agents(situation, scene.reader, owner, limit=limit)
        assert [item["task"]["task_id"] for item in listed] == expected, limit
    listed = all_agents(situation, scene.reader, owner)
    for item in listed:
        assert item["task"]["owner_id"] == owner
        assert item["task"]["state"] != "closed"
        assert item["task"]["number"] >= 1
        assert item["task"]["name_event_id"] is None
        attention = item["attention"]
        assert attention is None or attention["task_id"] == item["task"]["task_id"]
    plain = task(situation, owner, at=now + timedelta(seconds=1))
    newest = agents(scene.reader, situation.desk.org, owner=owner, limit="1").json()
    assert newest["items"] == [{"task": newest["items"][0]["task"], "attention": None}]
    assert newest["items"][0]["task"]["task_id"] == plain
    rest = agents(scene.reader, situation.desk.org, owner=owner, before=newest["next_before"]).json()
    assert [item["task"]["task_id"] for item in rest["items"]] == expected
    assert rest["next_before"] is None


def _newest(pair: tuple[int, str]) -> tuple[int, str]:
    """Newest activity first, then the higher task ID: fewer minutes ago, ties by descending ID."""
    minute, task_id = pair
    return minute, "".join(chr(255 - ord(char)) for char in task_id)


@pytest.mark.parametrize(
    ("suffix", "params", "pointer"),
    [
        ("people/agents", {"after": "not-an-id"}, "/after"),
        ("people/agents", {"after": "0193000A-0000-7000-8000-00000000000A"}, "/after"),
        ("people/agents", {"limit": "0"}, "/limit"),
        ("people/agents", {"limit": "101"}, "/limit"),
        ("people/agents", {"limit": "01"}, "/limit"),
        ("main-agents", {}, "/owner"),
        ("main-agents", {"owner": "someone"}, "/owner"),
        ("main-agents", {"owner": "OWNER", "limit": "101"}, "/limit"),
        ("main-agents", {"owner": "OWNER", "before": "not-a-cursor"}, "/before"),
    ],
)
def test_malformed_queries_are_refused(situation: Situation, suffix: str, params: dict[str, str], pointer: str) -> None:
    query = {name: situation.scene.writer.id if value == "OWNER" else value for name, value in params.items()}
    response = situation.scene.reader.browser.get(
        f"/v1/orgs/{situation.desk.org}/{suffix}", params=query, extensions=NOT_CONTRACT
    )
    assert response.status_code == 400, response.text
    assert response.json() == {"error": "request_invalid", "detail": {"pointers": [pointer]}}


@pytest.mark.parametrize("suffix", ["people/agents", "main-agents"])
def test_strangers_and_unknown_orgs_get_not_found(situation: Situation, suffix: str) -> None:
    params = {"owner": situation.scene.writer.id} if suffix == "main-agents" else {}
    for actor, org in (
        (situation.scene.stranger, situation.desk.org),
        (situation.scene.reader, new_id()),
        (situation.scene.reader, "not-an-id"),
    ):
        response = actor.browser.get(f"/v1/orgs/{org}/{suffix}", params=params)
        assert response.status_code == 404, (actor, org)
        assert response.json()["error"] == "not_found"


def test_the_reads_are_logged(situation: Situation) -> None:
    reader = situation.scene.reader
    assert people(reader, situation.desk.org).status_code == 200
    assert agents(reader, situation.desk.org, owner=reader.id).status_code == 200
    situation.hub.flush_read_log()
    logged = situation.desk.rows(
        "SELECT action, actor_id::text FROM hub.audit"
        " WHERE action IN ('read.org.people.agents', 'read.org.main_agents') ORDER BY seq"
    )
    assert logged == [("read.org.people.agents", reader.id), ("read.org.main_agents", reader.id)]
