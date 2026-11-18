"""Visibility of the team record (RECORD-PROTOCOL.md sections 3, 7.4 and 7.9), as pure functions."""

from __future__ import annotations

from dataclasses import replace

import pytest

from agent_core.record.event import MAX_TIME, STORED_INFINITY, Change, parse_event, time_text
from agent_core.record.verify import verify_record
from agent_core.record.visibility import (
    WITHHELD,
    answered_failure,
    deciding_task,
    event_entry,
    readable_object,
    sees_event,
    sees_named_task,
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
from tests.test_record_verify import forged, rehash, story, text_of

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


RECORD_WIDE = ["position_gap", "record_hash_mismatch", "checkpoint_invalid"]
# Section 7.9: every failure of section 12.1 that concerns an event.
EVENT_FAILURES = ["event_invalid", "certificate_not_supported", "row_mismatch", "key_unknown",
                  "event_signature_invalid", "event_id_taken", "chain_broken", "event_time_invalid",
                  "event_refused"]


@pytest.mark.parametrize("failure", RECORD_WIDE)
def test_verify_answer_names_a_record_wide_failure(failure: str) -> None:
    assert answered_failure(failure) == failure


@pytest.mark.parametrize("failure", EVENT_FAILURES)
def test_verify_answer_withholds_every_failure_that_concerns_an_event(failure: str) -> None:
    assert answered_failure(failure) == WITHHELD


def test_file_invalid_never_reaches_an_answer() -> None:
    with pytest.raises(ValueError):
        answered_failure("file_invalid")


def test_verify_answer_is_withheld_for_a_key_that_sees_the_failing_event() -> None:
    """Section 14, `withheld` to a key that sees the failing event: `chain_broken` of an alice event
    on a work task that bob sees, chained to an earlier event of alice that is not her previous
    one. The answer is `withheld`, the same as for a key of another team; the true value and the
    position stay in the failure for the log line `verify_failed <position> <failure>`."""
    world, work = story()
    first = world.stored[0]
    fork = text_of(ALICE.id, new_id(), "task", work, world.now, parse_event(first.text).event_hash,
                   [Change("title", b"Fix it", b"Fix")])
    events = rehash([*world.stored, forged(world, ALICE.id, fork, 4, world.now)])
    result = verify_record(events, [], world.keys, {}, None)
    assert result.failure is not None
    assert (result.failure.failure, result.failure.position) == ("chain_broken", 4)
    assert sees_event(BOB, result.state, parse_event(fork))
    assert not sees_event(DAVE, result.state, parse_event(fork))
    assert answered_failure(result.failure.failure) == WITHHELD


def test_verify_answer_is_withheld_for_an_unwritable_received_at() -> None:
    """Section 14: a stored `received_at` of 10000-01-01 and of `infinity` at the last position
    answers `withheld` there, not `record_hash_mismatch`; the log keeps `event_time_invalid`."""
    world, _ = story()
    for received_at in (MAX_TIME + 1, STORED_INFINITY):
        events = [*world.stored[:2], replace(world.stored[2], received_at=received_at)]
        result = verify_record(events, [], world.keys, {}, None)
        assert result.failure is not None
        assert (result.failure.failure, result.failure.position) == ("event_time_invalid", 3)
        assert answered_failure(result.failure.failure) == WITHHELD


def test_a_comment_create_names_a_task_only_by_a_task_id() -> None:
    """Check 12 (d) of section 6.7: the `task` value of a comment create names a task only when it
    is the id of a task; a comment id names no task, although the key sees the comment's task."""
    world = World(STANDARD)
    work = create_work(world, ALICE.id, [])
    comment = new_id()
    world.write(ALICE.id, "comment", comment, [("task", None, work), ("body", None, "x")], EVENTS)
    state = world.state
    assert sees_object(BOB, state, comment)
    assert sees_named_task(BOB, state, work.encode("ascii"))
    assert not sees_named_task(BOB, state, comment.encode("ascii"))
    assert not sees_named_task(DAVE, state, work.encode("ascii"))
    assert not sees_named_task(BOB, state, None)
    assert not sees_named_task(BOB, state, "é".encode())


def test_another_team_sees_nothing() -> None:
    world = World(STANDARD)
    foreign = new_id()
    world.write(DAVE.id, "task", foreign, [("kind", None, "work"), ("team", None, OTHER_TEAM), ("title", None, "x")], EVENTS)
    assert not sees_object(ALICE, world.state, foreign)
    assert sees_object(DAVE, world.state, foreign)
