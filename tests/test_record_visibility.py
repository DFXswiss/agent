"""Visibility of the team record (RECORD-PROTOCOL.md sections 3, 7.4 and 7.9), as pure functions."""

from __future__ import annotations

import pytest

from agent_core.record.event import parse_event, time_text
from agent_core.record.visibility import (
    WITHHELD,
    answered_failure,
    deciding_task,
    event_entry,
    readable_object,
    sees_event,
    sees_object,
    sees_task,
    task_repositories,
)
from agent_core.signing import b64url_encode
from tests.test_record_reducer import (
    ALICE,
    BOB,
    CAROL,
    DAVE,
    EVENTS,
    FAR,
    FOREIGN_LINK,
    FOREIGN_REPO,
    LINK,
    OTHER_TEAM,
    PLAIN_WATCHER,
    READ,
    REPO,
    STANDARD,
    TEAM,
    WATCHER,
    World,
    create_security,
    create_work,
    key,
    new_id,
)

NO_READ = key("nora-laptop-1", 20, "agent", "nora", TEAM, ("record:write", "device:executor"), None, None, None)
FOREIGN_WATCHER = key("lib-watcher-1", 21, "service", "lib-watcher", TEAM, (READ,), (FOREIGN_REPO,), FAR, None)


def test_a_key_sees_the_tasks_of_its_own_team() -> None:
    world = World(STANDARD)
    work = create_work(world, ALICE.id, [])
    task = world.task(work)
    assert sees_task(ALICE, task)
    assert sees_task(CAROL, task)
    assert not sees_task(DAVE, task)
    assert not sees_task(NO_READ, task)
    assert readable_object(DAVE, world.state, work) is None
    assert readable_object(ALICE, world.state, work) == task


def test_an_id_that_no_object_has_reads_as_an_unseen_object() -> None:
    world = World(STANDARD)
    unknown = new_id()
    assert not sees_object(ALICE, world.state, unknown)
    assert readable_object(ALICE, world.state, unknown) is None
    assert deciding_task(world.state, unknown) is None


def test_a_security_issue_needs_record_security() -> None:
    world = World(STANDARD)
    issue = create_security(world, ALICE.id, "login-bypass", [])
    task = world.task(issue)
    assert sees_task(BOB, task)
    assert not sees_task(CAROL, task)
    # A service key with record:security sees it through its `repository`.
    assert sees_task(WATCHER, task)
    assert not sees_task(PLAIN_WATCHER, task)
    assert task_repositories(task) == {REPO}


def test_a_comment_is_seen_with_its_task() -> None:
    world = World(STANDARD)
    work = create_work(world, ALICE.id, [])
    comment = new_id()
    world.write(ALICE.id, "comment", comment, [("task", None, work), ("body", None, "x")], EVENTS)
    assert deciding_task(world.state, comment) == world.task(work)
    assert sees_object(BOB, world.state, comment)
    assert not sees_object(DAVE, world.state, comment)
    assert readable_object(BOB, world.state, comment) is world.state.objects[comment]
    text, _ = world.sign(ALICE.id, "comment", comment, [("body", "x", "y")], new_id())
    assert sees_event(BOB, world.state, parse_event(text))
    assert not sees_event(DAVE, world.state, parse_event(text))


def test_a_service_key_sees_a_task_only_through_its_subjects() -> None:
    world = World((*STANDARD, FOREIGN_WATCHER))
    work = create_work(world, ALICE.id, [])
    assert not sees_object(WATCHER, world.state, work)
    world.write(ALICE.id, "task", work, [("links", None, FOREIGN_LINK)], EVENTS)
    assert not sees_object(WATCHER, world.state, work)
    assert sees_object(FOREIGN_WATCHER, world.state, work)
    # A link change makes it visible, and the key reads every link.
    world.write(ALICE.id, "task", work, [("links", FOREIGN_LINK, f"{LINK} {FOREIGN_LINK}")], EVENTS)
    seen = readable_object(WATCHER, world.state, work)
    assert seen is not None
    assert seen.links == (LINK, FOREIGN_LINK)  # type: ignore[union-attr]
    assert task_repositories(world.task(work)) == {REPO, FOREIGN_REPO}


def test_a_withheld_entry_and_a_full_entry() -> None:
    received = 1795000000
    full = event_entry(position=17, received_at=received, event_hash="a" * 64, record_hash="b" * 64,
                       text=b"text", signature=b"s" * 64, visible=True)
    assert list(full) == ["position", "received_at", "event_hash", "record_hash", "event", "signature"]
    assert full["event"] == b64url_encode(b"text")
    assert full["signature"] == b64url_encode(b"s" * 64)
    assert full["received_at"] == time_text(received)
    withheld = event_entry(position=18, received_at=received, event_hash="a" * 64, record_hash="b" * 64,
                           text=b"text", signature=b"s" * 64, visible=False)
    assert withheld == {"position": 18, "received_at": time_text(received), "event_hash": "a" * 64,
                        "record_hash": "b" * 64, "event": None, "signature": None}
    with pytest.raises(ValueError):
        event_entry(position=1, received_at=10**20, event_hash="a" * 64, record_hash="b" * 64,
                    text=b"t", signature=b"s", visible=True)


@pytest.mark.parametrize("failure", ["position_gap", "record_hash_mismatch", "checkpoint_invalid"])
def test_record_wide_failures_are_named_to_every_key(failure: str) -> None:
    world = World(STANDARD)
    assert answered_failure(DAVE, failure, None, world.state) == failure


def test_file_invalid_never_reaches_an_answer() -> None:
    with pytest.raises(ValueError):
        answered_failure(ALICE, "file_invalid", None, World(STANDARD).state)


def test_event_failures_are_named_only_to_a_key_that_sees_the_object() -> None:
    world = World(STANDARD)
    work = create_work(world, ALICE.id, [])
    issue = create_security(world, ALICE.id, "login-bypass", [])
    comment = new_id()
    world.write(ALICE.id, "comment", comment, [("task", None, work), ("body", None, "x")], EVENTS)
    state = world.state

    def event(object_type: str, object_id: str, changes: list) -> object:
        text, _ = world.sign(ALICE.id, object_type, object_id, changes, new_id())
        return parse_event(text)

    update = event("task", work, [("title", "Fix login", "x")])
    assert answered_failure(BOB, "event_signature_invalid", update, state) == "event_signature_invalid"
    assert answered_failure(DAVE, "event_signature_invalid", update, state) == WITHHELD
    secret = event("task", issue, [("title", "Login bypass", "x")])
    assert answered_failure(BOB, "chain_broken", secret, state) == "chain_broken"
    assert answered_failure(CAROL, "chain_broken", secret, state) == WITHHELD
    on_comment = event("comment", comment, [("body", "x", "y")])
    assert answered_failure(BOB, "event_refused", on_comment, state) == "event_refused"
    assert answered_failure(DAVE, "event_refused", on_comment, state) == WITHHELD
    create = event("task", new_id(), [("kind", None, "work")])
    assert answered_failure(ALICE, "event_refused", create, state) == WITHHELD
    named = event("comment", new_id(), [("task", None, work), ("body", None, "x")])
    assert answered_failure(BOB, "event_id_taken", named, state) == "event_id_taken"
    assert answered_failure(DAVE, "event_id_taken", named, state) == WITHHELD
    for changes in ([("body", None, "x")], [("task", work, None)], [("task", None, "é")], [("task", None, new_id())]):
        assert answered_failure(BOB, "event_refused", event("comment", new_id(), changes), state) == WITHHELD
    assert answered_failure(BOB, "event_invalid", None, state) == WITHHELD


def test_another_team_sees_nothing() -> None:
    world = World(STANDARD)
    foreign = new_id()
    world.write(DAVE.id, "task", foreign, [("kind", None, "work"), ("team", None, OTHER_TEAM), ("title", None, "x")], EVENTS)
    assert not sees_object(ALICE, world.state, foreign)
    assert sees_object(DAVE, world.state, foreign)
