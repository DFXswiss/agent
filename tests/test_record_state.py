"""The state of the team record as an immutable value (RECORD-PROTOCOL.md sections 6 and 11.6)."""

from __future__ import annotations

import pytest

from agent_core.record.event import parse_event
from agent_core.record.reducer import check_stored
from agent_core.record.state import EMPTY_STATE, StateBuilder, applied_state
from tests.test_record_reducer import (
    ALICE,
    BOB,
    EVENTS,
    LINK,
    LINK_2,
    STANDARD,
    TEAM,
    World,
    create_security,
    create_work,
    new_id,
)


def test_the_empty_state() -> None:
    assert EMPTY_STATE.head is None
    assert len(EMPTY_STATE.objects) == 0
    assert EMPTY_STATE.task(new_id()) is None
    assert repr(EMPTY_STATE) == "RecordState(head=None)"


def test_a_reduction_leaves_the_earlier_state_unchanged() -> None:
    world = World(STANDARD)
    create_work(world, ALICE.id, [])
    earlier = world.state
    work = create_work(world, ALICE.id, [("links", None, LINK)])
    assert work not in earlier.objects
    assert LINK not in earlier.linked_tasks
    assert earlier.head is not None and earlier.head.position == 1
    assert world.state.linked_tasks[LINK] == frozenset({work})
    with pytest.raises(TypeError):
        world.state.objects[work] = None  # type: ignore[index]


def test_task_returns_only_tasks() -> None:
    world = World(STANDARD)
    work = create_work(world, ALICE.id, [])
    comment = new_id()
    world.write(ALICE.id, "comment", comment, [("task", None, work), ("body", None, "x")], EVENTS)
    assert world.state.task(work) is not None
    assert world.state.task(comment) is None


def test_the_link_index_follows_every_change_of_links() -> None:
    world = World(STANDARD)
    first = create_work(world, ALICE.id, [("links", None, LINK)])
    second = create_work(world, ALICE.id, [("links", None, f"{LINK} {LINK_2}")])
    assert world.state.linked_tasks[LINK] == frozenset({first, second})
    world.write(ALICE.id, "task", second, [("links", f"{LINK} {LINK_2}", LINK_2)], EVENTS)
    assert world.state.linked_tasks[LINK] == frozenset({first})
    world.write(ALICE.id, "task", first, [("links", LINK, None)], EVENTS)
    assert LINK not in world.state.linked_tasks
    assert world.state.linked_tasks[LINK_2] == frozenset({second})


def test_the_short_name_index_follows_a_rename() -> None:
    world = World(STANDARD)
    issue = create_security(world, ALICE.id, "login-bypass", [])
    assert world.state.short_names[(TEAM.encode(), "login-bypass")] == issue
    world.write(ALICE.id, "task", issue, [("short_name", "login-bypass", "renamed")], EVENTS)
    assert (TEAM.encode(), "login-bypass") not in world.state.short_names
    assert world.state.short_names[(TEAM.encode(), "renamed")] == issue
    # The old name is free again.
    create_security(world, BOB.id, "login-bypass", [])


def test_a_builder_is_frozen_once_and_never_changed_after() -> None:
    world = World(STANDARD)
    text, _ = world.sign(ALICE.id, "task", new_id(), [("kind", None, "work"), ("team", None, TEAM),
                                                       ("title", None, "x")], new_id())
    applied = check_stored(EMPTY_STATE, parse_event(text), world.now)
    builder = StateBuilder(EMPTY_STATE)
    builder.freeze()
    with pytest.raises(RuntimeError):
        builder.apply(applied)
    with pytest.raises(RuntimeError):
        builder.freeze()
    state = applied_state(EMPTY_STATE, applied)
    assert state.head == applied.head
    assert state.chains[ALICE.id].position == 1
    assert EMPTY_STATE.head is None
