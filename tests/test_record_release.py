"""The release rule over the record (RECORD-PROTOCOL.md section 9), as a pure function over the state."""

from __future__ import annotations

from types import MappingProxyType

import pytest

from agent_core.record.release import linked_tasks, release_answer
from agent_core.record.state import RecordState
from tests.test_record_reducer import (
    ALICE,
    DAVE,
    EVENTS,
    LINK,
    LINK_2,
    OTHER_TEAM,
    STANDARD,
    WATCH,
    WATCHER,
    World,
    create_security,
    create_work,
    new_id,
    started_work,
    status,
)

REPO = "example-org/app"


def answer(world: World, number: int) -> tuple[bool, str | None]:
    return release_answer(world.state, REPO, number)


def test_no_linked_task() -> None:
    world = World(STANDARD)
    assert answer(world, 1) == (False, "task_missing")
    create_work(world, ALICE.id, [("links", None, LINK_2)])
    assert answer(world, 1) == (False, "task_missing")


def test_a_linked_task_that_is_not_ready() -> None:
    world = World(STANDARD)
    work = create_work(world, ALICE.id, [("links", None, LINK)])
    assert answer(world, 1) == (False, "task_not_ready")  # open, without assignee
    world.write(ALICE.id, "task", work, [("assignee", None, "alice")], EVENTS)
    assert answer(world, 1) == (False, "task_not_ready")  # open, with assignee
    status(world, ALICE.id, work, "open", "in_progress", EVENTS)
    assert answer(world, 1) == (True, None)
    status(world, ALICE.id, work, "in_progress", "ready_for_review", EVENTS)
    assert answer(world, 1) == (True, None)
    status(world, WATCHER.id, work, "ready_for_review", "done", WATCH)
    assert answer(world, 1) == (False, "task_not_ready")  # done
    merged = started_work(world, [("links", None, LINK_2), ("completion", None, "verified")])
    status(world, WATCHER.id, merged, "in_progress", "merged", WATCH)
    assert answer(world, 2) == (False, "task_not_ready")  # merged


def test_two_linked_tasks_one_ready_and_one_not() -> None:
    world = World(STANDARD)
    started_work(world, [("links", None, LINK)])
    create_work(world, ALICE.id, [("links", None, LINK)])
    assert answer(world, 1) == (False, "task_not_ready")


def test_a_dropped_task_alone_and_a_link_removed_again() -> None:
    world = World(STANDARD)
    work = create_work(world, ALICE.id, [("links", None, LINK)])
    status(world, ALICE.id, work, "open", "dropped", EVENTS)
    assert answer(world, 1) == (False, "task_missing")
    other = started_work(world, [("links", None, LINK_2)])
    assert answer(world, 2) == (True, None)
    world.write(ALICE.id, "task", other, [("links", LINK_2, None)], EVENTS)
    assert answer(world, 2) == (False, "task_missing")


def test_a_security_issue_and_a_task_of_another_team_count() -> None:
    world = World(STANDARD)
    create_security(world, ALICE.id, "login-bypass", [("links", None, LINK)])
    assert answer(world, 1) == (False, "task_not_ready")
    foreign = new_id()
    world.write(DAVE.id, "task", foreign, [("kind", None, "work"), ("team", None, OTHER_TEAM), ("title", None, "x"),
                                            ("links", None, LINK_2), ("assignee", None, "dave"),
                                            ("status", None, "in_progress")], EVENTS)
    assert answer(world, 2) == (True, None)
    assert [task.object_id for task in linked_tasks(world.state, REPO, 2)] == [foreign]


def test_a_link_index_that_names_a_comment_fails_loudly() -> None:
    world = World(STANDARD)
    work = create_work(world, ALICE.id, [])
    comment = new_id()
    world.write(ALICE.id, "comment", comment, [("task", None, work), ("body", None, "x")], EVENTS)
    broken = RecordState(
        head=world.state.head,
        objects=world.state.objects,
        chains=world.state.chains,
        event_ids=world.state.event_ids,
        event_hashes=world.state.event_hashes,
        short_names=world.state.short_names,
        linked_tasks=MappingProxyType({LINK: frozenset({comment})}),
        object_events=world.state.object_events,
    )
    with pytest.raises(ValueError):
        release_answer(broken, REPO, 1)
