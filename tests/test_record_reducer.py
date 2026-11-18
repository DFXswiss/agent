"""The reducer of the team record (RECORD-PROTOCOL.md section 6 and section 14, "The reducer").

Pure functions: no database and no HTTP. "Through the endpoint" here means the checks of section 6.7
that `check_append` runs for an append endpoint (checks 3 to 18); "in the fold directly" means the
state rules alone (section 6.1), named as `<error value>` / `<failure>` of section 12.1.

The `World` of this file is shared by the other record test files.
"""

from __future__ import annotations

import uuid
from typing import Sequence

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_core.keys import KeyEntry, KeyRing
from agent_core.record.event import (
    Change,
    RecordRefusal,
    build_event_text,
    parse_event,
    record_hash,
)
from agent_core.record.objects import Task
from agent_core.record.reducer import (
    STATUS_OF_ERROR,
    AppendContext,
    check_append,
    check_stored,
    reduce_append,
    reduce_stored,
    refusal_status,
)
from agent_core.record.state import EMPTY_STATE, RecordState
from agent_core.record.verify import RowCopies, StoredEvent, fold_events
from agent_core.signing import parse_time

T0 = parse_time("2026-11-17T10:00:00Z")
assert T0 is not None
STEP = 60
FAR = T0 + 10**8
TEAM = "example"
OTHER_TEAM = "other"
REPO = "example-org/app"
LINK = "example-org/app#1"
LINK_2 = "example-org/app#2"
FOREIGN_REPO = "other-org/lib"
FOREIGN_LINK = "other-org/lib#3"
EVENTS = "events"
WATCH = "watch"

READ = "record:read"
WRITE = "record:write"
SECURITY = "record:security"
WATCHES = "record:watch"
FULL = (READ, WRITE, SECURITY, "device:controller")
NO_SECURITY = (READ, WRITE, "device:controller")


def private(seed: int) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes([seed]) * 32)


def public(seed: int) -> bytes:
    return private(seed).public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


SEEDS: dict[str, int] = {}


def key(
    key_id: str,
    seed: int,
    principal: str,
    handle: str,
    team: str,
    scopes: Sequence[str],
    subjects: Sequence[str] | None,
    expires: int | None,
    revoked: int | None,
) -> KeyEntry:
    """A keys-file entry. A `staff` entry is a passkey (`webauthn`), so it verifies no Ed25519 signature."""
    SEEDS[key_id] = seed
    staff = principal == "staff"
    return KeyEntry(
        id=key_id,
        kind="webauthn" if staff else "ed25519",
        principal=principal,
        handle=handle,
        team=team,
        public_key=b"\x04" + bytes([seed]) * 64 if staff else public(seed),
        scopes=tuple(scopes),
        credential_id=bytes([seed]) * 16 if staff else None,
        device=None if staff else "laptop-1",
        program=None,
        subjects=None if subjects is None else tuple(subjects),
        expires=expires,
        revoked=revoked,
    )


ALICE = key("alice-laptop-1", 1, "agent", "alice", TEAM, FULL, None, None, None)
BOB = key("bob-laptop-1", 2, "agent", "bob", TEAM, FULL, None, None, None)
CAROL = key("carol-laptop-1", 3, "agent", "carol", TEAM, NO_SECURITY, None, None, None)
DAVE = key("dave-laptop-1", 4, "agent", "dave", OTHER_TEAM, FULL, None, None, None)
ERIN = key("erin-passkey-1", 5, "staff", "erin", TEAM, (READ, WRITE, SECURITY), None, None, None)
WATCHER = key(
    "example-watcher-1", 6, "service", "example-watcher", TEAM, (READ, WATCHES, SECURITY), (REPO,), FAR, None
)
PLAIN_WATCHER = key(
    "example-watcher-2", 7, "service", "example-watcher", TEAM, (READ, WATCHES), (REPO,), FAR, None
)
RITA = key("rita-laptop-1", 8, "agent", "rita", TEAM, (READ, SECURITY, "device:executor"), None, None, None)
STANDARD = (ALICE, BOB, CAROL, DAVE, ERIN, WATCHER, PLAIN_WATCHER, RITA)


def new_id() -> str:
    return str(uuid.uuid4())


def encode(value: str | bytes | None) -> bytes | None:
    if value is None or isinstance(value, bytes):
        return value
    return value.encode("utf-8")


Changes = Sequence[tuple[str, "str | bytes | None", "str | bytes | None"]]


class World:
    """A record state with a keys file, a clock that moves one minute per accepted event, and the
    stored events that a hub would hold."""

    def __init__(self, entries: Sequence[KeyEntry]) -> None:
        self.keys = KeyRing({entry.id: entry for entry in entries})
        self.state: RecordState = EMPTY_STATE
        self.now = T0
        self.stored: list[StoredEvent] = []

    def prev(self, key_id: str) -> str:
        head = self.state.chains.get(key_id)
        return "-" if head is None else head.chain.event_hash

    def sign(
        self, key_id: str, object_type: str, object_id: str, changes: Changes, event_id: str
    ) -> tuple[bytes, bytes]:
        text = build_event_text(
            event_id=event_id,
            object_type=object_type,
            object_id=object_id,
            actor_key_id=key_id,
            signed_at=self.now,
            prev_hash=self.prev(key_id),
            changes=[Change(name, encode(old), encode(new)) for name, old, new in changes],
            key_certificate=None,
        )
        return text, private(SEEDS[key_id]).sign(text)

    def context(self, key_id: str, endpoint: str) -> AppendContext:
        return AppendContext(keys=self.keys, actor=self.keys.entries[key_id], endpoint=endpoint, received_at=self.now)

    def submit(self, key_id: str, text: bytes, signature: bytes, endpoint: str) -> None:
        accepted = reduce_append(self.state, text, signature, self.context(key_id, endpoint))
        applied = accepted.applied
        event = applied.event
        self.stored.append(
            StoredEvent(
                position=applied.head.position,
                received_at=applied.head.received_at,
                event_hash=event.event_hash,
                record_hash=applied.head.record_hash,
                text=text,
                signature=signature,
                copies=RowCopies(event.event_id, event.actor_key_id, event.prev_hash),
            )
        )
        self.state = accepted.state
        self.now += STEP

    def write(self, key_id: str, object_type: str, object_id: str, changes: Changes, endpoint: str) -> None:
        text, signature = self.sign(key_id, object_type, object_id, changes, new_id())
        self.submit(key_id, text, signature, endpoint)

    def refused(self, key_id: str, object_type: str, object_id: str, changes: Changes, endpoint: str) -> str:
        before = self.state
        with pytest.raises(RecordRefusal) as refusal:
            self.write(key_id, object_type, object_id, changes, endpoint)
        assert self.state is before
        assert str(refusal.value) == refusal.value.error
        return refusal.value.error

    def fold_write(self, key_id: str, object_type: str, object_id: str, changes: Changes) -> None:
        """Apply an event by the state rules alone, as a fold does (no actor rules, no signature)."""
        text, _ = self.sign(key_id, object_type, object_id, changes, new_id())
        self.state = reduce_stored(self.state, parse_event(text), self.now)
        self.now += STEP

    def fold_refused(self, key_id: str, object_type: str, object_id: str, changes: Changes) -> tuple[str, str]:
        """The reducer's error value in a fold, and the verification failure it maps to."""
        text, signature = self.sign(key_id, object_type, object_id, changes, new_id())
        event = parse_event(text)
        with pytest.raises(RecordRefusal) as refusal:
            check_stored(self.state, event, self.now)
        head = self.state.head
        position = 1 if head is None else head.position + 1
        stored = StoredEvent(
            position=position,
            received_at=self.now,
            event_hash=event.event_hash,
            record_hash=record_hash(position, self.now, event.event_hash, None if head is None else head.record_hash),
            text=text,
            signature=signature,
            copies=None,
        )
        result = fold_events(self.state, [stored], self.keys, None)
        assert result.failure is not None
        assert result.failure.position == position
        assert result.state == self.state
        return refusal.value.error, result.failure.failure

    def task(self, object_id: str) -> Task:
        task = self.state.task(object_id)
        assert task is not None
        return task


def create_work(world: World, key_id: str, extra: Changes) -> str:
    object_id = new_id()
    world.write(
        key_id,
        "task",
        object_id,
        [("kind", None, "work"), ("team", None, TEAM), ("title", None, "Fix login"), *extra],
        EVENTS,
    )
    return object_id


def security_create(short_name: str, extra: Changes) -> list[tuple[str, str | None, str | None]]:
    return [
        ("kind", None, "security"),
        ("team", None, TEAM),
        ("title", None, "Login bypass"),
        ("repository", None, REPO),
        ("short_name", None, short_name),
        ("severity", None, "high"),
        ("urgency", None, "8"),
        *extra,
    ]


def create_security(world: World, key_id: str, short_name: str, extra: Changes) -> str:
    object_id = new_id()
    world.write(key_id, "task", object_id, security_create(short_name, extra), EVENTS)
    return object_id


def started_work(world: World, extra: Changes) -> str:
    """A task of kind `work` in `in_progress` assigned to alice."""
    return create_work(world, ALICE.id, [("assignee", None, "alice"), ("status", None, "in_progress"), *extra])


def status(world: World, key_id: str, object_id: str, old: str, new: str, endpoint: str) -> None:
    world.write(key_id, "task", object_id, [("status", old, new)], endpoint)


# --- objects, defaults and the result of an accepted event --------------------------------------------


def test_defaults_of_status_and_completion_per_kind() -> None:
    world = World(STANDARD)
    work = create_work(world, ALICE.id, [])
    security = create_security(world, ALICE.id, "login-bypass", [])
    assert world.task(work).status == "open"
    assert world.task(work).completion == "merged"
    assert world.task(security).status == "open"
    assert world.task(security).completion == "verified"
    assert world.task(work).completed_links is None
    assert world.task(work).comments == 0


def test_an_accepted_event_has_its_position_head_and_stamps() -> None:
    world = World(STANDARD)
    work = create_work(world, ALICE.id, [])
    world.write(BOB.id, "task", work, [("title", "Fix login", "Fix the login")], EVENTS)
    task = world.task(work)
    assert task.created.position == 1
    assert task.created.key == ALICE.id
    assert task.changed.position == 2
    assert task.changed.key == BOB.id
    assert task.changed.received_at == T0 + STEP
    head = world.state.head
    assert head is not None
    assert head.position == 2
    assert head.record_hash == world.stored[1].record_hash
    assert world.state.chains[ALICE.id].position == 1
    assert world.state.chains[BOB.id].position == 2


def test_a_comment_counts_on_its_task_and_leaves_its_changed() -> None:
    world = World(STANDARD)
    work = create_work(world, ALICE.id, [])
    comment = new_id()
    world.write(BOB.id, "comment", comment, [("task", None, work), ("body", None, "Reproduced.\n\tTwice.")], EVENTS)
    task = world.task(work)
    assert task.comments == 1
    assert task.changed.position == 1
    written = world.state.objects[comment]
    assert written.object_type == "comment"
    assert written.task == work  # type: ignore[union-attr]
    assert written.body == b"Reproduced.\n\tTwice."  # type: ignore[union-attr]


def test_refusal_status_of_every_error_value() -> None:
    assert refusal_status("transition_invalid") == 409
    assert refusal_status("object_not_found") == 404
    assert refusal_status("change_not_allowed") == 403
    assert refusal_status("value_invalid") == 400
    assert refusal_status("field_not_allowed") == 400
    assert refusal_status("field_immutable") == 409
    assert refusal_status("subject_not_allowed") == 403
    assert refusal_status("assignee_invalid") == 409
    assert refusal_status("key_not_valid_at_receipt") == 409
    assert refusal_status("event_time_invalid") == 400
    assert len(STATUS_OF_ERROR) == 22
    with pytest.raises(KeyError):
        refusal_status("record_unverified")


def test_the_context_names_one_of_the_two_append_endpoints() -> None:
    with pytest.raises(ValueError):
        AppendContext(keys=KeyRing({}), actor=ALICE, endpoint="records", received_at=T0)


def test_received_at_never_earlier_than_the_head() -> None:
    world = World(STANDARD)
    create_work(world, ALICE.id, [])
    text, signature = world.sign(ALICE.id, "task", new_id(), [("kind", None, "work")], new_id())
    context = AppendContext(keys=world.keys, actor=ALICE, endpoint=EVENTS, received_at=T0 - 1)
    with pytest.raises(ValueError):
        check_append(world.state, text, signature, context)


def test_no_record_content_in_a_repr() -> None:
    world = World(STANDARD)
    work = create_work(world, ALICE.id, [("description", None, "secret-marker")])
    shown = repr(world.state) + repr(world.task(work)) + repr(world.context(ALICE.id, EVENTS))
    assert "secret-marker" not in shown
    assert "Fix login" not in shown
    assert work not in shown
    assert repr(world.task(work)) == "Task()"


# --- checks 3 to 11 of section 6.7 -----------------------------------------------------------------


def test_event_invalid_at_check_3() -> None:
    world = World(STANDARD)
    with pytest.raises(RecordRefusal) as refusal:
        check_append(world.state, b"agent-event-v1", bytes(64), world.context(ALICE.id, EVENTS))
    assert refusal.value.error == "event_invalid"
    assert refusal_status("event_invalid") == 400


def test_actor_mismatch_certificate_and_signature() -> None:
    world = World(STANDARD)
    object_id = new_id()
    changes = [("kind", None, "work"), ("team", None, TEAM), ("title", None, "Fix login")]
    text, signature = world.sign(ALICE.id, "task", object_id, changes, new_id())
    for actor, sent, error in (
        (BOB.id, signature, "actor_mismatch"),
        (ALICE.id, private(2).sign(text), "event_signature_invalid"),
        (ALICE.id, private(1).sign(text + b"x"), "event_signature_invalid"),
    ):
        with pytest.raises(RecordRefusal) as refusal:
            check_append(world.state, text, sent, world.context(actor, EVENTS))
        assert refusal.value.error == error
    certified = build_event_text(
        event_id=new_id(),
        object_type="task",
        object_id=object_id,
        actor_key_id=ALICE.id,
        signed_at=T0,
        prev_hash="-",
        changes=[Change(name, None, encode(new)) for name, _, new in changes],
        key_certificate=b"certificate",
    )
    with pytest.raises(RecordRefusal) as refusal:
        check_append(world.state, certified, private(1).sign(certified), world.context(ALICE.id, EVENTS))
    assert refusal.value.error == "certificate_not_supported"


def test_a_staff_passkey_never_signs_an_event() -> None:
    world = World(STANDARD)
    assert world.refused(ERIN.id, "task", new_id(), [("kind", None, "work")], EVENTS) == "event_signature_invalid"


def test_event_duplicate_and_event_id_taken() -> None:
    world = World(STANDARD)
    object_id = new_id()
    changes = [("kind", None, "work"), ("team", None, TEAM), ("title", None, "Fix login")]
    text, signature = world.sign(ALICE.id, "task", object_id, changes, new_id())
    world.submit(ALICE.id, text, signature, EVENTS)
    with pytest.raises(RecordRefusal) as refusal:
        world.submit(ALICE.id, text, signature, EVENTS)
    assert refusal.value.error == "event_duplicate"
    used = parse_event(text).event_id
    other, other_signature = world.sign(BOB.id, "task", new_id(), changes, used)
    with pytest.raises(RecordRefusal) as refusal:
        world.submit(BOB.id, other, other_signature, EVENTS)
    assert refusal.value.error == "event_id_taken"


def test_chain_broken_for_a_wrong_prev_hash_and_a_second_first_event() -> None:
    world = World(STANDARD)
    create_work(world, ALICE.id, [])
    second_first = build_event_text(
        event_id=new_id(),
        object_type="task",
        object_id=new_id(),
        actor_key_id=ALICE.id,
        signed_at=world.now,
        prev_hash="-",
        changes=[Change("kind", None, b"work")],
        key_certificate=None,
    )
    wrong = second_first.replace(b"\n-\n-\n", b"\n" + b"0" * 64 + b"\n-\n", 1)
    for text in (second_first, wrong):
        with pytest.raises(RecordRefusal) as refusal:
            world.submit(ALICE.id, text, private(1).sign(text), EVENTS)
        assert refusal.value.error == "chain_broken"


def test_signed_at_bounds_of_check_10() -> None:
    world = World(STANDARD)
    first = create_work(world, ALICE.id, [])
    changes = [("title", "Fix login", "Fix it")]
    for ahead, accepted in ((121, False), (120, True)):
        world.now -= ahead
        text, signature = world.sign(ALICE.id, "task", first, changes, new_id())
        world.now += ahead
        later = text.replace(
            b"\n" + parse_time_text(world.now - ahead) + b"\n",
            b"\n" + parse_time_text(world.now + ahead) + b"\n",
        )
        signature = private(1).sign(later)
        if accepted:
            world.submit(ALICE.id, later, signature, EVENTS)
        else:
            with pytest.raises(RecordRefusal) as refusal:
                world.submit(ALICE.id, later, signature, EVENTS)
            assert refusal.value.error == "event_time_invalid"
    # Earlier than the key's previous event.
    world.now -= 2 * STEP
    text, signature = world.sign(ALICE.id, "task", first, [("title", "Fix it", "Fix")], new_id())
    world.now += 2 * STEP
    with pytest.raises(RecordRefusal) as refusal:
        world.submit(ALICE.id, text, signature, EVENTS)
    assert refusal.value.error == "event_time_invalid"


def parse_time_text(seconds: int) -> bytes:
    from agent_core.record.event import time_text

    text = time_text(seconds)
    assert text is not None
    return text.encode("ascii")


def test_key_not_valid_at_receipt() -> None:
    revoked = key("alice-laptop-2", 9, "agent", "alice", TEAM, FULL, None, None, T0 + 30)
    expired = key("alice-laptop-3", 10, "agent", "alice", TEAM, FULL, None, T0 + 30, None)
    world = World((*STANDARD, revoked, expired))
    world.now = T0 + 60
    for entry in (revoked, expired):
        assert world.refused(entry.id, "task", new_id(), [("kind", None, "work")], EVENTS) == "key_not_valid_at_receipt"
    assert refusal_status("key_not_valid_at_receipt") == 409


def test_after_a_refusal_the_next_event_chained_to_the_last_accepted_one_is_accepted() -> None:
    world = World(STANDARD)
    work = create_work(world, ALICE.id, [])
    assert world.refused(ALICE.id, "task", work, [("status", "open", "done")], EVENTS) == "transition_invalid"
    world.write(ALICE.id, "task", work, [("title", "Fix login", "Fix")], EVENTS)
    assert world.task(work).value("title") == b"Fix"


# --- check 12 ---------------------------------------------------------------------------------------


def test_check_12_b_field_required_for_a_create_without_kind_or_task() -> None:
    world = World(STANDARD)
    assert world.refused(ALICE.id, "task", new_id(), [("team", None, TEAM)], EVENTS) == "field_required"
    assert world.refused(ALICE.id, "comment", new_id(), [("body", None, "text")], EVENTS) == "field_required"
    assert refusal_status("field_required") == 409


def test_check_12_a_the_watch_endpoint_needs_an_existing_task() -> None:
    world = World(STANDARD)
    assert world.refused(WATCHER.id, "task", new_id(), [("status", "open", "done")], WATCH) == "object_not_found"
    assert world.refused(WATCHER.id, "task", new_id(), [("kind", None, "work")], WATCH) == "object_not_found"
    work = create_work(world, ALICE.id, [("links", None, LINK)])
    comment = new_id()
    world.write(ALICE.id, "comment", comment, [("task", None, work), ("body", None, "text")], EVENTS)
    assert world.refused(WATCHER.id, "comment", comment, [("body", "text", "new")], WATCH) == "object_not_found"


def test_check_12_c_a_task_create_for_another_team() -> None:
    world = World(STANDARD)
    other = [("kind", None, "work"), ("team", None, OTHER_TEAM), ("title", None, "Fix")]
    assert world.refused(ALICE.id, "task", new_id(), other, EVENTS) == "change_not_allowed"
    # A used short name of the other team is never revealed.
    world.write(DAVE.id, "task", new_id(), [*security_create("login-bypass", [])[:1], ("team", None, OTHER_TEAM),
                *security_create("login-bypass", [])[2:]], EVENTS)
    taken = [("team", None, OTHER_TEAM) if name == "team" else (name, old, new)
             for name, old, new in security_create("login-bypass", [])]
    assert world.refused(ALICE.id, "task", new_id(), taken, EVENTS) == "change_not_allowed"
    assert world.refused(DAVE.id, "task", new_id(), [*security_create("login-bypass", [])[:1],
                         ("team", None, OTHER_TEAM), *security_create("login-bypass", [])[2:]],
                         EVENTS) == "short_name_taken"
    # Without a change of `team`: check 17.
    assert world.refused(ALICE.id, "task", new_id(), [("kind", None, "work"), ("title", None, "x")], EVENTS) == "field_required"


def test_check_12_d_object_not_found() -> None:
    world = World(STANDARD)
    foreign = new_id()
    world.write(DAVE.id, "task", foreign, [("kind", None, "work"), ("team", None, OTHER_TEAM), ("title", None, "x")], EVENTS)
    # A create whose object_id is that of an object the actor does not see is an update of it.
    assert world.refused(ALICE.id, "task", foreign, [("kind", None, "work")], EVENTS) == "object_not_found"
    assert world.refused(ALICE.id, "comment", new_id(), [("task", None, foreign), ("body", None, "x")], EVENTS) == "object_not_found"
    for value in ("not-a-uuid", new_id(), "é"):
        assert world.refused(ALICE.id, "comment", new_id(), [("task", None, value), ("body", None, "x")], EVENTS) == "object_not_found"
    work = create_work(world, ALICE.id, [])
    comment = new_id()
    world.write(ALICE.id, "comment", comment, [("task", None, work), ("body", None, "x")], EVENTS)
    # A comment id is not a task.
    assert world.refused(ALICE.id, "comment", new_id(), [("task", None, comment), ("body", None, "x")], EVENTS) == "object_not_found"
    # A comment create whose `task` change removes a value has no task.
    assert world.refused(ALICE.id, "comment", new_id(), [("task", work, None), ("body", None, "x")], EVENTS) == "object_not_found"


def test_check_12_d_a_comment_value_that_is_no_uuid_gets_object_not_found_not_value_invalid() -> None:
    world = World(STANDARD)
    assert world.refused(ALICE.id, "comment", new_id(), [("task", None, "x"), ("body", None, "\x00")], EVENTS) == "object_not_found"


def test_check_13_scope_missing_for_a_security_create() -> None:
    world = World(STANDARD)
    assert world.refused(CAROL.id, "task", new_id(), security_create("login-bypass", []), EVENTS) == "scope_missing"


# --- checks 14 to 17 ----------------------------------------------------------------------------------


def test_value_invalid_at_check_14_for_old_and_new() -> None:
    world = World(STANDARD)
    work = create_work(world, ALICE.id, [])
    assert world.refused(ALICE.id, "task", work, [("title", "Fix login", "x" * 201)], EVENTS) == "value_invalid"
    assert world.refused(ALICE.id, "task", work, [("title", "\x01", "Fix")], EVENTS) == "value_invalid"
    assert world.refused(ALICE.id, "task", work, [("urgency", None, "07")], EVENTS) == "value_invalid"


def test_team_of_100_and_101_bytes() -> None:
    long_team = "t" * 100
    entry = key("tess-laptop-1", 11, "agent", "tess", long_team, FULL, None, None, None)
    world = World((*STANDARD, entry))
    world.write(entry.id, "task", new_id(), [("kind", None, "work"), ("team", None, long_team), ("title", None, "x")], EVENTS)
    too_long = [("kind", None, "work"), ("team", None, "t" * 101), ("title", None, "x")]
    assert world.refused(entry.id, "task", new_id(), too_long, EVENTS) == "change_not_allowed"
    assert world.fold_refused(entry.id, "task", new_id(), too_long) == ("value_invalid", "event_invalid")


def test_state_mismatch() -> None:
    world = World(STANDARD)
    work = create_work(world, ALICE.id, [])
    comment = new_id()
    world.write(ALICE.id, "comment", comment, [("task", None, work), ("body", None, "text")], EVENTS)
    assert world.refused(ALICE.id, "task", work, [("title", "Stale", "New")], EVENTS) == "state_mismatch"
    assert world.refused(ALICE.id, "task", new_id(), [("kind", "work", "security")], EVENTS) == "state_mismatch"
    assert world.refused(ALICE.id, "comment", work, [("title", "Fix login", "New")], EVENTS) == "state_mismatch"
    assert world.refused(ALICE.id, "task", comment, [("body", "text", "new")], EVENTS) == "state_mismatch"
    assert world.fold_refused(ALICE.id, "task", work, [("title", "Stale", "New")]) == ("state_mismatch", "event_refused")


def test_field_not_allowed_and_field_immutable() -> None:
    world = World(STANDARD)
    work = create_work(world, ALICE.id, [])
    for name, value in (("repository", REPO), ("short_name", "x"), ("severity", "low"), ("urgency", "1"),
                        ("source", "CVE-1"), ("fix", LINK)):
        assert world.refused(ALICE.id, "task", work, [(name, None, value)], EVENTS) == "field_not_allowed"
        assert world.refused(ALICE.id, "task", new_id(), [("kind", None, "work"), ("team", None, TEAM),
                             ("title", None, "x"), (name, None, value)], EVENTS) == "field_not_allowed"
    assert world.refused(ALICE.id, "task", work, [("priority", None, "x")], EVENTS) == "field_not_allowed"
    assert world.refused(ALICE.id, "task", work, [("kind", "work", "security")], EVENTS) == "field_immutable"
    assert world.refused(ALICE.id, "task", work, [("team", TEAM, "x")], EVENTS) == "field_immutable"
    # Check 16: a change of `kind` together with a field the kind does not have.
    assert world.refused(ALICE.id, "task", work, [("kind", "work", "security"), ("severity", None, "low")],
                         EVENTS) == "field_not_allowed"
    comment = new_id()
    world.write(ALICE.id, "comment", comment, [("task", None, work), ("body", None, "text")], EVENTS)
    assert world.refused(ALICE.id, "comment", comment, [("body", "text", "new")], EVENTS) == "field_immutable"
    other = create_work(world, ALICE.id, [])
    assert world.refused(ALICE.id, "comment", comment, [("task", work, other)], EVENTS) == "field_immutable"
    assert world.refused(ALICE.id, "comment", comment, [("title", None, "x")], EVENTS) == "field_not_allowed"
    assert world.refused(ALICE.id, "comment", new_id(), [("task", None, work), ("body", None, "x"),
                         ("priority", None, "x")], EVENTS) == "field_not_allowed"
    assert world.fold_refused(ALICE.id, "task", work, [("kind", "work", "security")]) == ("field_immutable", "event_refused")


def test_field_required_for_each_required_field() -> None:
    world = World(STANDARD)
    work = started_work(world, [])
    security = create_security(world, ALICE.id, "login-bypass", [])
    assert world.refused(ALICE.id, "task", work, [("title", "Fix login", None)], EVENTS) == "field_required"
    assert world.refused(ALICE.id, "task", work, [("completion", "merged", None)], EVENTS) == "field_required"
    for name, old in (("repository", REPO), ("short_name", "login-bypass"), ("severity", "high"), ("urgency", "8"),
                      ("title", "Login bypass")):
        assert world.refused(ALICE.id, "task", security, [(name, old, None)], EVENTS) == "field_required"
    assert world.refused(ALICE.id, "task", work, [("assignee", "alice", None)], EVENTS) == "field_required"
    assert world.refused(ALICE.id, "comment", new_id(), [("task", None, work)], EVENTS) == "field_required"
    assert world.fold_refused(ALICE.id, "comment", new_id(), [("task", None, new_id()), ("body", None, "x")]) == (
        "object_not_found", "event_refused")
    assert world.fold_refused(ALICE.id, "task", work, [("title", "Fix login", None)]) == ("field_required", "event_refused")


def test_field_not_allowed_for_an_unknown_field_on_a_comment_create_in_the_fold() -> None:
    world = World(STANDARD)
    work = create_work(world, ALICE.id, [])
    assert world.fold_refused(ALICE.id, "comment", new_id(), [("task", None, work), ("body", None, "x"),
                              ("priority", None, "1")]) == ("field_not_allowed", "event_refused")
    assert world.fold_refused(ALICE.id, "comment", new_id(), [("task", None, "x"), ("body", None, "x")]) == (
        "value_invalid", "event_invalid")


def test_short_name_taken_in_the_same_team_and_free_in_another() -> None:
    world = World(STANDARD)
    create_security(world, ALICE.id, "login-bypass", [])
    assert world.refused(BOB.id, "task", new_id(), security_create("login-bypass", []), EVENTS) == "short_name_taken"
    other = [("team", None, OTHER_TEAM) if name == "team" else (name, old, new)
             for name, old, new in security_create("login-bypass", [])]
    world.write(DAVE.id, "task", new_id(), other, EVENTS)
    second = create_security(world, BOB.id, "other-bypass", [])
    assert world.refused(BOB.id, "task", second, [("short_name", "other-bypass", "login-bypass")], EVENTS) == "short_name_taken"
    world.write(BOB.id, "task", second, [("severity", "high", "low")], EVENTS)


def test_links_frozen_in_merged_and_done() -> None:
    world = World(STANDARD)
    merged = started_work(world, [("completion", None, "verified")])
    status(world, ALICE.id, merged, "in_progress", "merged", EVENTS)
    assert world.refused(ALICE.id, "task", merged, [("links", None, LINK)], EVENTS) == "links_frozen"
    done = started_work(world, [])
    status(world, ALICE.id, done, "in_progress", "done", EVENTS)
    assert world.refused(ALICE.id, "task", done, [("links", None, LINK)], EVENTS) == "links_frozen"
    # A change of links into `done` in the same event.
    linked = started_work(world, [("links", None, LINK)])
    assert world.refused(WATCHER.id, "task", linked, [("links", LINK, LINK_2), ("status", "in_progress", "done")],
                         WATCH) == "links_frozen"
    assert refusal_status("links_frozen") == 409
    assert world.fold_refused(ALICE.id, "task", done, [("links", None, LINK)]) == ("links_frozen", "event_refused")


# --- section 6.4: every row accepted ------------------------------------------------------------------


def test_rows_1_to_7() -> None:
    world = World(STANDARD)
    work = create_work(world, BOB.id, [])  # row 1
    world.write(BOB.id, "task", work, [("assignee", None, "alice"), ("status", "open", "in_progress")], EVENTS)  # row 2
    status(world, BOB.id, work, "in_progress", "ready_for_review", EVENTS)  # row 3
    status(world, BOB.id, work, "ready_for_review", "in_progress", EVENTS)  # row 4
    status(world, BOB.id, work, "in_progress", "open", EVENTS)  # row 5
    world.write(BOB.id, "task", work, [("status", "open", "in_progress")], EVENTS)  # row 2, assignee kept
    status(world, BOB.id, work, "in_progress", "ready_for_review", EVENTS)
    status(world, BOB.id, work, "ready_for_review", "open", EVENTS)  # row 5
    status(world, BOB.id, work, "open", "dropped", EVENTS)  # row 6
    status(world, BOB.id, work, "dropped", "open", EVENTS)  # row 7
    assert world.task(work).status == "open"
    created = create_work(world, BOB.id, [("assignee", None, "alice"), ("status", None, "in_progress")])  # row 2
    assert world.task(created).status == "in_progress"


def test_row_8_and_9_by_the_watcher() -> None:
    world = World(STANDARD)
    work = started_work(world, [("links", None, LINK)])
    status(world, WATCHER.id, work, "in_progress", "done", WATCH)  # row 8
    assert world.task(work).status == "done"
    verified = create_work(world, ALICE.id, [("links", None, LINK), ("completion", None, "verified")])
    status(world, WATCHER.id, verified, "open", "merged", WATCH)  # row 9 from open
    assert world.task(verified).status == "merged"


def test_row_10_from_open_needs_row_2_first() -> None:
    world = World(STANDARD)
    work = create_work(world, ALICE.id, [("assignee", None, "alice")])
    assert world.refused(ALICE.id, "task", work, [("status", "open", "done")], EVENTS) == "transition_invalid"
    status(world, ALICE.id, work, "open", "in_progress", EVENTS)
    status(world, ALICE.id, work, "in_progress", "done", EVENTS)  # row 10
    assert world.task(work).status == "done"
    linked = create_work(world, ALICE.id, [("assignee", None, "alice"), ("links", None, LINK)])
    # Row 8 matches by its state conditions; the assignee is no watcher.
    assert world.refused(ALICE.id, "task", linked, [("status", "open", "done")], EVENTS) == "change_not_allowed"


def test_row_10_under_verified_reaches_merged_and_rule_switch_then_row_12() -> None:
    world = World(STANDARD)
    work = started_work(world, [("completion", None, "verified")])
    status(world, ALICE.id, work, "in_progress", "merged", EVENTS)  # row 10
    # A task in `merged` without links keeps its status when the rule changes.
    world.write(BOB.id, "task", work, [("completion", "verified", "merged")], EVENTS)
    assert world.task(work).status == "merged"
    assert world.refused(BOB.id, "task", work, [("status", "merged", "done")], EVENTS) == "change_not_allowed"
    status(world, ALICE.id, work, "merged", "done", EVENTS)  # row 12
    assert world.task(work).status == "done"
    assert world.task(work).completed_links is None
    # A rule change on a `done` task keeps `done`.
    world.write(BOB.id, "task", work, [("completion", "merged", "verified")], EVENTS)
    assert world.task(work).status == "done"


def test_row_13_with_done_in_the_same_event_and_without_a_change_of_status() -> None:
    world = World(STANDARD)
    work = started_work(world, [("links", None, LINK), ("completion", None, "verified")])
    status(world, WATCHER.id, work, "in_progress", "merged", WATCH)
    assert world.refused(BOB.id, "task", work, [("completion", "verified", "merged")], EVENTS) == "transition_invalid"
    world.write(BOB.id, "task", work, [("completion", "verified", "merged"), ("status", "merged", "done")], EVENTS)
    task = world.task(work)
    assert task.status == "done"
    assert task.completion == "merged"
    assert task.completed_links is None


def test_row_14_sets_completed_links_and_row_10_compares_them() -> None:
    world = World(STANDARD)
    work = started_work(world, [("links", None, LINK), ("completion", None, "verified")])
    status(world, WATCHER.id, work, "in_progress", "merged", WATCH)
    status(world, BOB.id, work, "merged", "in_progress", EVENTS)  # row 14
    assert world.task(work).completed_links == LINK.encode("ascii")
    assert world.task(work).completed_link_references == (LINK,)
    status(world, ALICE.id, work, "in_progress", "merged", EVENTS)  # row 10, links equal completed_links
    status(world, BOB.id, work, "merged", "in_progress", EVENTS)  # row 14
    world.write(BOB.id, "task", work, [("links", LINK, f"{LINK} {LINK_2}")], EVENTS)
    # Row 10 no longer matches; row 9 does, and the assignee is no watcher.
    assert world.refused(ALICE.id, "task", work, [("status", "in_progress", "merged")], EVENTS) == "change_not_allowed"


def test_row_14_together_with_a_rule_switch_to_merged() -> None:
    world = World(STANDARD)
    work = started_work(world, [("links", None, LINK), ("completion", None, "verified")])
    status(world, WATCHER.id, work, "in_progress", "merged", WATCH)
    world.write(BOB.id, "task", work, [("completion", "verified", "merged"), ("status", "merged", "in_progress")], EVENTS)
    task = world.task(work)
    assert (task.status, task.completion) == ("in_progress", "merged")
    assert task.completed_links == LINK.encode("ascii")
    # The same in the fold directly.
    fold = World(STANDARD)
    other = started_work(world, [("links", None, LINK), ("completion", None, "verified")])
    status(world, WATCHER.id, other, "in_progress", "merged", WATCH)
    fold.state, fold.now = world.state, world.now
    fold.fold_write(BOB.id, "task", other, [("completion", "verified", "merged"), ("status", "merged", "in_progress")])
    assert fold.task(other).status == "in_progress"


def test_row_14_from_done() -> None:
    world = World(STANDARD)
    work = started_work(world, [])
    status(world, ALICE.id, work, "in_progress", "done", EVENTS)
    status(world, BOB.id, work, "done", "in_progress", EVENTS)
    assert world.task(work).status == "in_progress"
    assert world.task(work).completed_links is None


def test_row_11_in_the_fold_directly_and_its_repetition() -> None:
    world = World(STANDARD)
    work = started_work(world, [("links", None, LINK), ("completion", None, "verified")])
    status(world, WATCHER.id, work, "in_progress", "merged", WATCH)
    # An agent key is no staff member.
    assert world.refused(ALICE.id, "task", work, [("status", "merged", "done"), ("verification", None, "Checked")],
                         EVENTS) == "change_not_allowed"
    assert world.refused(WATCHER.id, "task", work, [("status", "merged", "done"), ("verification", None, "Checked")],
                         WATCH) == "change_not_allowed"
    world.fold_write(ERIN.id, "task", work, [("status", "merged", "done"), ("verification", None, "Checked")])
    assert world.task(work).status == "done"
    assert world.task(work).completed_links is None
    world.fold_write(BOB.id, "task", work, [("status", "done", "in_progress")])
    world.fold_write(ALICE.id, "task", work, [("status", "in_progress", "merged")])
    with pytest.raises(RecordRefusal) as refusal:
        world.sign(ERIN.id, "task", work, [("status", "merged", "done"), ("verification", "Checked", "Checked")], new_id())
    assert refusal.value.error == "event_invalid"
    world.fold_write(ERIN.id, "task", work, [("status", "merged", "done"), ("verification", "Checked", "Checked again")])
    assert world.task(work).value("verification") == b"Checked again"
    assert world.task(work).completed_links == LINK.encode("ascii")


def test_verification_outside_row_11() -> None:
    world = World(STANDARD)
    work = started_work(world, [])
    assert world.refused(ALICE.id, "task", work, [("verification", None, "Checked")], EVENTS) == "transition_invalid"
    assert world.fold_refused(ALICE.id, "task", work, [("verification", None, "Checked")]) == (
        "transition_invalid", "event_refused")
    verified = started_work(world, [("links", None, LINK), ("completion", None, "verified")])
    status(world, WATCHER.id, verified, "in_progress", "merged", WATCH)
    world.fold_write(ERIN.id, "task", verified, [("status", "merged", "done"), ("verification", None, "Checked")])
    # Removing the value, through the endpoint and in the fold.
    assert world.refused(BOB.id, "task", verified, [("verification", "Checked", None)], EVENTS) == "transition_invalid"
    assert world.fold_refused(BOB.id, "task", verified, [("verification", "Checked", None)]) == (
        "transition_invalid", "event_refused")
    world.fold_write(BOB.id, "task", verified, [("status", "done", "in_progress")])
    world.fold_write(ALICE.id, "task", verified, [("status", "in_progress", "merged")])
    # Row 11 with `new` `-`. The state rules do not read the actor (section 6.1), so an event of an
    # `ed25519` key passes the rows before `event_refused` of section 12.1 and reaches the reducer.
    assert world.fold_refused(BOB.id, "task", verified, [("status", "merged", "done"), ("verification", "Checked", None)]) == (
        "transition_invalid", "event_refused")
    # The same event signed by the staff passkey: the reducer still refuses it, but a fold stops
    # earlier, at `key_unknown`, because a `webauthn` entry is no `ed25519` entry (section 12.1).
    assert world.fold_refused(ERIN.id, "task", verified, [("status", "merged", "done"), ("verification", "Checked", None)]) == (
        "transition_invalid", "key_unknown")


# --- section 6.4: transitions that no row reaches ----------------------------------------------------


def task_in(world: World, wanted: str) -> str:
    """A task of kind `work` assigned to alice, in the status `wanted`."""
    if wanted == "open":
        return create_work(world, ALICE.id, [("assignee", None, "alice")])
    if wanted == "dropped":
        work = create_work(world, ALICE.id, [("assignee", None, "alice")])
        status(world, ALICE.id, work, "open", "dropped", EVENTS)
        return work
    rule = "verified" if wanted == "merged" else "merged"
    work = started_work(world, [("completion", None, rule)])
    if wanted == "ready_for_review":
        status(world, ALICE.id, work, "in_progress", "ready_for_review", EVENTS)
    if wanted in ("merged", "done"):
        status(world, ALICE.id, work, "in_progress", wanted, EVENTS)
    return work


UNREACHABLE = [
    ("open", "ready_for_review"),
    ("merged", "open"),
    ("merged", "ready_for_review"),
    ("merged", "dropped"),
    ("done", "open"),
    ("done", "ready_for_review"),
    ("done", "merged"),
    ("done", "dropped"),
    ("dropped", "in_progress"),
    ("dropped", "ready_for_review"),
    ("dropped", "merged"),
    ("dropped", "done"),
]


@pytest.mark.parametrize(("old", "new"), UNREACHABLE)
def test_transition_invalid_for_every_target_no_row_reaches(old: str, new: str) -> None:
    world = World(STANDARD)
    work = task_in(world, old)
    assert world.task(work).status == old
    assert world.refused(ALICE.id, "task", work, [("status", old, new)], EVENTS) == "transition_invalid"
    assert world.fold_refused(ALICE.id, "task", work, [("status", old, new)]) == ("transition_invalid", "event_refused")


@pytest.mark.parametrize("new", ["ready_for_review", "merged", "done", "dropped"])
def test_a_create_reaches_only_open_and_in_progress(new: str) -> None:
    world = World(STANDARD)
    changes = [("kind", None, "work"), ("team", None, TEAM), ("title", None, "x"), ("assignee", None, "alice"),
               ("status", None, new), ("links", None, LINK)]
    assert world.refused(ALICE.id, "task", new_id(), changes, EVENTS) == "transition_invalid"


def test_removing_the_status_matches_no_row() -> None:
    world = World(STANDARD)
    work = create_work(world, ALICE.id, [])
    assert world.refused(ALICE.id, "task", work, [("status", "open", None)], EVENTS) == "transition_invalid"


def test_in_progress_needs_an_assignee() -> None:
    world = World(STANDARD)
    work = create_work(world, ALICE.id, [])
    assert world.refused(ALICE.id, "task", work, [("status", "open", "in_progress")], EVENTS) == "transition_invalid"
    done = started_work(world, [])
    status(world, ALICE.id, done, "in_progress", "done", EVENTS)
    assert world.refused(BOB.id, "task", done, [("status", "done", "in_progress"), ("assignee", "alice", None)],
                         EVENTS) == "transition_invalid"


# --- section 6.4, column "Who" (actor rule 2) ---------------------------------------------------------


def test_writer_rows_refused_for_the_watcher() -> None:
    world = World(STANDARD)
    work = started_work(world, [("links", None, LINK)])
    for old, new in (("in_progress", "ready_for_review"), ("in_progress", "open"), ("in_progress", "dropped")):
        assert world.refused(WATCHER.id, "task", work, [("status", old, new)], WATCH) == "change_not_allowed"
    status(world, ALICE.id, work, "in_progress", "ready_for_review", EVENTS)
    assert world.refused(WATCHER.id, "task", work, [("status", "ready_for_review", "in_progress")], WATCH) == "change_not_allowed"
    status(world, ALICE.id, work, "ready_for_review", "open", EVENTS)
    assert world.refused(WATCHER.id, "task", work, [("status", "open", "in_progress")], WATCH) == "change_not_allowed"
    status(world, ALICE.id, work, "open", "dropped", EVENTS)
    assert world.refused(WATCHER.id, "task", work, [("status", "dropped", "open")], WATCH) == "change_not_allowed"
    verified = started_work(world, [("links", None, LINK), ("completion", None, "verified")])
    status(world, WATCHER.id, verified, "in_progress", "merged", WATCH)
    assert world.refused(WATCHER.id, "task", verified, [("status", "merged", "in_progress")], WATCH) == "change_not_allowed"
    assert world.refused(WATCHER.id, "task", verified, [("completion", "verified", "merged"), ("status", "merged", "done")],
                         WATCH) == "change_not_allowed"


def test_watcher_rows_refused_for_a_writer() -> None:
    world = World(STANDARD)
    work = create_work(world, ALICE.id, [("links", None, LINK)])
    assert world.refused(BOB.id, "task", work, [("status", "open", "done")], EVENTS) == "change_not_allowed"
    verified = create_work(world, ALICE.id, [("links", None, LINK), ("completion", None, "verified")])
    assert world.refused(BOB.id, "task", verified, [("status", "open", "merged")], EVENTS) == "change_not_allowed"


def test_assignee_rows_refused_for_others() -> None:
    world = World(STANDARD)
    work = started_work(world, [])
    assert world.refused(BOB.id, "task", work, [("status", "in_progress", "done")], EVENTS) == "change_not_allowed"
    # A task of kind `work` without links has no repository, so the `service` key of the watcher
    # does not see it (section 3, condition 4) and check 12 (d) answers before the actor rules.
    assert world.refused(WATCHER.id, "task", work, [("status", "in_progress", "done")], WATCH) == "object_not_found"
    # The event changes `assignee`: nobody is the assignee for row 10.
    assert world.refused(ALICE.id, "task", work, [("status", "in_progress", "done"), ("assignee", "alice", "bob")],
                         EVENTS) == "change_not_allowed"
    merged = started_work(world, [("completion", None, "verified")])
    status(world, ALICE.id, merged, "in_progress", "merged", EVENTS)
    world.write(BOB.id, "task", merged, [("completion", "verified", "merged")], EVENTS)
    assert world.refused(BOB.id, "task", merged, [("status", "merged", "done")], EVENTS) == "change_not_allowed"
    assert world.refused(WATCHER.id, "task", merged, [("status", "merged", "done")], WATCH) == "object_not_found"
    # Rows 10 and 12 for the watcher on a task it sees: a security issue without links, whose
    # `repository` is in the watcher's `subjects`. Without links, rows 8 and 9 do not match.
    issue = create_security(world, ALICE.id, "login-bypass",
                            [("assignee", None, "alice"), ("status", None, "in_progress"), ("fix", None, LINK)])
    assert world.refused(WATCHER.id, "task", issue, [("status", "in_progress", "merged")], WATCH) == "change_not_allowed"
    status(world, ALICE.id, issue, "in_progress", "merged", EVENTS)  # row 10
    world.write(BOB.id, "task", issue, [("completion", "verified", "merged")], EVENTS)
    assert world.task(issue).status == "merged"
    assert world.refused(BOB.id, "task", issue, [("status", "merged", "done")], EVENTS) == "change_not_allowed"
    assert world.refused(WATCHER.id, "task", issue, [("status", "merged", "done")], WATCH) == "change_not_allowed"
    status(world, ALICE.id, issue, "merged", "done", EVENTS)  # row 12
    assert world.task(issue).status == "done"


def test_an_agent_without_record_write_is_no_writer() -> None:
    world = World(STANDARD)
    work = started_work(world, [])
    assert world.refused(RITA.id, "task", work, [("status", "in_progress", "open")], EVENTS) == "change_not_allowed"
    assert world.refused(RITA.id, "task", work, [("title", "Fix login", "x")], EVENTS) == "change_not_allowed"
    assert world.refused(RITA.id, "comment", new_id(), [("task", None, work), ("body", None, "x")], EVENTS) == "change_not_allowed"


# --- security issues: who may edit, assignee rules 4 and 5 ---------------------------------------------


def test_a_security_issue_is_started_by_its_own_assignee() -> None:
    world = World(STANDARD)
    issue = create_security(world, ALICE.id, "login-bypass", [])
    assert world.refused(ALICE.id, "task", issue, [("assignee", None, "bob"), ("status", "open", "in_progress")],
                         EVENTS) == "change_not_allowed"
    assert world.refused(ALICE.id, "task", issue, [("assignee", None, "bob")], EVENTS) == "change_not_allowed"
    world.write(ALICE.id, "task", issue, [("assignee", None, "alice"), ("status", "open", "in_progress")], EVENTS)
    assert world.task(issue).assignee == "alice"
    created = create_security(world, BOB.id, "other-bypass", [("assignee", None, "bob"), ("status", None, "in_progress")])
    assert world.task(created).status == "in_progress"
    # Taken over by another handle while the assignee can act on it.
    assert world.refused(BOB.id, "task", issue, [("assignee", "alice", "bob")], EVENTS) == "change_not_allowed"
    # Handed over by its assignee.
    world.write(ALICE.id, "task", issue, [("assignee", "alice", "bob")], EVENTS)
    assert world.task(issue).assignee == "bob"


def test_who_may_edit_a_security_issue_assigned_to_another_handle() -> None:
    world = World(STANDARD)
    issue = create_security(world, ALICE.id, "login-bypass", [("assignee", None, "alice"), ("status", None, "in_progress"),
                                                               ("links", None, LINK)])
    world.write(BOB.id, "task", issue, [("severity", "high", "low"), ("urgency", "8", "10"),
                                         ("title", "Login bypass", "Bypass")], EVENTS)
    status(world, BOB.id, issue, "in_progress", "ready_for_review", EVENTS)
    status(world, BOB.id, issue, "ready_for_review", "open", EVENTS)
    status(world, BOB.id, issue, "open", "dropped", EVENTS)
    status(world, BOB.id, issue, "dropped", "open", EVENTS)
    assert world.refused(BOB.id, "task", issue, [("status", "open", "in_progress")], EVENTS) == "change_not_allowed"
    status(world, ALICE.id, issue, "open", "in_progress", EVENTS)
    world.write(WATCHER.id, "task", issue, [("status", "in_progress", "merged"), ("fix", None, LINK)], WATCH)
    status(world, BOB.id, issue, "merged", "in_progress", EVENTS)  # row 14
    status(world, WATCHER.id, issue, "in_progress", "merged", WATCH)
    world.write(BOB.id, "task", issue, [("completion", "verified", "merged"), ("status", "merged", "done")], EVENTS)  # row 13
    assert world.task(issue).status == "done"
    unlinked = create_security(world, ALICE.id, "unlinked", [("assignee", None, "alice"), ("status", None, "in_progress")])
    assert world.refused(BOB.id, "task", unlinked, [("assignee", "alice", "bob")], EVENTS) == "change_not_allowed"
    assert world.refused(BOB.id, "task", unlinked, [("status", "in_progress", "merged")], EVENTS) == "change_not_allowed"


def test_a_security_issue_in_done_needs_fix() -> None:
    world = World(STANDARD)
    issue = create_security(world, ALICE.id, "login-bypass", [("assignee", None, "alice"), ("status", None, "in_progress"),
                                                               ("links", None, LINK)])
    status(world, WATCHER.id, issue, "in_progress", "merged", WATCH)
    assert world.refused(BOB.id, "task", issue, [("completion", "verified", "merged"), ("status", "merged", "done")],
                         EVENTS) == "field_required"


def orphan_world(extra: Sequence[KeyEntry], alice_revoked: int | None, alice_expires: int | None) -> tuple[World, str]:
    alice = key("alice-laptop-1", 1, "agent", "alice", TEAM, FULL, None, alice_expires, alice_revoked)
    world = World((alice, BOB, CAROL, DAVE, *extra))
    issue = create_security(world, alice.id, "login-bypass", [("assignee", None, "alice"), ("status", None, "in_progress")])
    world.now = T0 + 3600
    return world, issue


def test_rule_4_case_c_takes_over_an_orphaned_assignee() -> None:
    world, issue = orphan_world((), T0 + 30, None)
    assert world.refused(BOB.id, "task", issue, [("assignee", "alice", "carol")], EVENTS) == "change_not_allowed"
    world.write(BOB.id, "task", issue, [("assignee", "alice", "bob")], EVENTS)
    assert world.task(issue).assignee == "bob"


def test_rule_4_case_c_refused_while_the_assignee_can_act() -> None:
    world, issue = orphan_world((), FAR, None)
    assert world.refused(BOB.id, "task", issue, [("assignee", "alice", "bob")], EVENTS) == "change_not_allowed"
    second = key("alice-laptop-2", 12, "agent", "alice", TEAM, FULL, None, None, None)
    world, issue = orphan_world((second,), T0 + 30, None)
    assert world.refused(BOB.id, "task", issue, [("assignee", "alice", "bob")], EVENTS) == "change_not_allowed"


@pytest.mark.parametrize(
    "remaining",
    [
        key("alice-passkey-1", 13, "staff", "alice", TEAM, (READ, WRITE, SECURITY), None, None, None),
        key("alice-laptop-4", 14, "agent", "alice", TEAM, NO_SECURITY, None, None, None),
        key("alice-laptop-5", 15, "agent", "alice", TEAM, (READ, SECURITY, "device:executor"), None, None, None),
        key("alice-laptop-6", 16, "agent", "alice", OTHER_TEAM, FULL, None, None, None),
    ],
    ids=["only a staff entry", "agent without record:security", "agent without record:write", "agent of another team"],
)
def test_rule_4_case_c_orphaned_assignees(remaining: KeyEntry) -> None:
    world, issue = orphan_world((remaining,), T0 + 30, None)
    world.write(BOB.id, "task", issue, [("assignee", "alice", "bob")], EVENTS)
    assert world.task(issue).assignee == "bob"


def test_rule_4_case_c_with_every_agent_key_expired() -> None:
    world, issue = orphan_world((), None, T0 + 30)
    world.write(BOB.id, "task", issue, [("assignee", "alice", "bob")], EVENTS)
    assert world.task(issue).assignee == "bob"


def test_rule_4_case_c_a_writer_without_record_security_does_not_see_the_issue() -> None:
    world, issue = orphan_world((), T0 + 30, None)
    assert world.refused(CAROL.id, "task", issue, [("assignee", "alice", "carol")], EVENTS) == "object_not_found"


@pytest.mark.parametrize(
    ("handle", "entry"),
    [
        ("carol", None),
        ("rita", RITA),
        ("erin", ERIN),
        ("ed", key("ed-laptop-1", 17, "agent", "ed", TEAM, FULL, None, T0 + 30, None)),
        ("ron", key("ron-laptop-1", 18, "agent", "ron", TEAM, FULL, None, None, T0 + 30)),
        ("dave", None),
        ("zoe", None),
    ],
)
def test_rule_5_a_security_issue_is_handed_only_to_a_handle_that_can_act(handle: str, entry: KeyEntry | None) -> None:
    world = World((*STANDARD, *(() if entry is None else (entry,))))
    issue = create_security(world, ALICE.id, "login-bypass", [("assignee", None, "alice"), ("status", None, "in_progress")])
    world.now = T0 + 3600
    assert world.refused(ALICE.id, "task", issue, [("assignee", "alice", handle)], EVENTS) == "assignee_invalid"
    world.write(ALICE.id, "task", issue, [("assignee", "alice", "bob")], EVENTS)


@pytest.mark.parametrize(
    ("handle", "entry"),
    [
        ("zoe", None),
        ("dave", None),
        ("ron", key("ron-laptop-1", 18, "agent", "ron", TEAM, FULL, None, None, T0 + 30)),
        ("ed", key("ed-laptop-1", 17, "agent", "ed", TEAM, FULL, None, T0 + 30, None)),
        ("example-watcher", None),
    ],
)
def test_rule_5_assignee_invalid_on_a_task_of_kind_work(handle: str, entry: KeyEntry | None) -> None:
    world = World((*STANDARD, *(() if entry is None else (entry,))))
    work = create_work(world, ALICE.id, [])
    world.now = T0 + 3600
    assert world.refused(ALICE.id, "task", work, [("assignee", None, handle)], EVENTS) == "assignee_invalid"


def test_rule_5_a_staff_handle_and_any_writer_on_a_task_of_kind_work() -> None:
    world = World(STANDARD)
    work = create_work(world, ALICE.id, [])
    world.write(BOB.id, "task", work, [("assignee", None, "erin")], EVENTS)
    world.write(CAROL.id, "task", work, [("assignee", "erin", "rita")], EVENTS)
    world.write(CAROL.id, "task", work, [("assignee", "rita", None)], EVENTS)
    assert world.task(work).assignee is None


# --- the watcher (section 8) -------------------------------------------------------------------------


def test_the_watcher_sets_fix_to_one_of_the_links() -> None:
    world = World(STANDARD)
    issue = create_security(world, ALICE.id, "login-bypass", [("assignee", None, "alice"), ("status", None, "in_progress"),
                                                               ("links", None, f"{LINK} {LINK_2}")])
    assert world.refused(WATCHER.id, "task", issue, [("status", "in_progress", "merged"),
                         ("fix", None, "example-org/app#9")], WATCH) == "change_not_allowed"
    world.write(WATCHER.id, "task", issue, [("status", "in_progress", "merged"), ("fix", None, LINK_2)], WATCH)
    assert world.task(issue).value("fix") == LINK_2.encode("ascii")


def test_the_watcher_changes_nothing_else() -> None:
    world = World(STANDARD)
    work = started_work(world, [("links", None, LINK)])
    assert world.refused(WATCHER.id, "task", work, [("status", "in_progress", "done"), ("title", "Fix login", "x")],
                         WATCH) == "change_not_allowed"
    assert world.refused(WATCHER.id, "task", work, [("status", "in_progress", "done"), ("fix", None, LINK)],
                         WATCH) == "field_not_allowed"
    assert world.refused(WATCHER.id, "task", work, [("kind", "work", "security")], WATCH) == "field_immutable"
    assert world.refused(WATCHER.id, "task", work, [("title", "Fix login", "x")], WATCH) == "change_not_allowed"
    issue = create_security(world, ALICE.id, "login-bypass", [("assignee", None, "alice"), ("status", None, "in_progress"),
                                                               ("links", None, LINK), ("fix", None, LINK)])
    assert world.refused(WATCHER.id, "task", issue, [("status", "in_progress", "merged"), ("fix", LINK, None)],
                         WATCH) == "change_not_allowed"


def test_the_watcher_and_its_subjects() -> None:
    world = World(STANDARD)
    work = started_work(world, [("links", None, f"{LINK} {FOREIGN_LINK}")])
    assert world.refused(WATCHER.id, "task", work, [("status", "in_progress", "done")], WATCH) == "subject_not_allowed"
    assert refusal_status("subject_not_allowed") == 403
    unseen = started_work(world, [("links", None, FOREIGN_LINK)])
    assert world.refused(WATCHER.id, "task", unseen, [("status", "in_progress", "done")], WATCH) == "object_not_found"


def test_a_watcher_without_record_security_does_not_see_a_security_issue() -> None:
    world = World(STANDARD)
    issue = create_security(world, ALICE.id, "login-bypass", [("assignee", None, "alice"), ("status", None, "in_progress"),
                                                               ("links", None, LINK)])
    assert world.refused(PLAIN_WATCHER.id, "task", issue, [("status", "in_progress", "merged")], WATCH) == "object_not_found"


def test_a_service_entry_without_subjects_sees_no_task() -> None:
    broken = key("example-watcher-3", 19, "service", "example-watcher", TEAM, (READ, WATCHES), None, FAR, None)
    world = World((*STANDARD, broken))
    work = started_work(world, [("links", None, LINK)])
    # Without `subjects` a service key sees no task.
    assert world.refused(broken.id, "task", work, [("status", "in_progress", "done")], WATCH) == "object_not_found"


def test_a_writer_through_the_watch_endpoint_and_a_watcher_through_events() -> None:
    world = World(STANDARD)
    work = started_work(world, [("links", None, LINK)])
    # The scopes of the endpoints (check 9 of KEY-PROTOCOL.md section 5) are the endpoint part; the
    # actor rules refuse the same events on their own.
    assert world.refused(ALICE.id, "task", work, [("status", "in_progress", "done")], WATCH) == "change_not_allowed"
    assert world.refused(WATCHER.id, "task", work, [("status", "in_progress", "done")], EVENTS) == "change_not_allowed"


# --- the order of checks 7 to 18 ---------------------------------------------------------------------


def test_the_order_of_checks_7_to_18() -> None:
    revoked = key("alice-laptop-2", 9, "agent", "alice", TEAM, FULL, None, None, T0 + 90)
    world = World((*STANDARD, revoked))
    work = started_work(world, [])
    foreign = new_id()
    world.write(DAVE.id, "task", foreign, [("kind", None, "work"), ("team", None, OTHER_TEAM), ("title", None, "x")], EVENTS)
    # 7 before 8: the same text again has the same hash and the same event_id.
    text, signature = world.sign(ALICE.id, "task", work, [("title", "Fix login", "Fix")], new_id())
    world.submit(ALICE.id, text, signature, EVENTS)
    with pytest.raises(RecordRefusal) as refusal:
        world.submit(ALICE.id, text, signature, EVENTS)
    assert refusal.value.error == "event_duplicate"
    # 8 before 9: a used event_id and a wrong prev_hash.
    used = parse_event(text).event_id
    stale = build_event_text(event_id=used, object_type="task", object_id=work, actor_key_id=ALICE.id,
                             signed_at=world.now, prev_hash="-", changes=[Change("title", b"Fix", b"x")],
                             key_certificate=None)
    with pytest.raises(RecordRefusal) as refusal:
        world.submit(ALICE.id, stale, private(1).sign(stale), EVENTS)
    assert refusal.value.error == "event_id_taken"
    # 9 before 10: a wrong prev_hash and a signed_at too far ahead.
    ahead = build_event_text(event_id=new_id(), object_type="task", object_id=work, actor_key_id=ALICE.id,
                             signed_at=world.now + 500, prev_hash="-", changes=[Change("title", b"Fix", b"x")],
                             key_certificate=None)
    with pytest.raises(RecordRefusal) as refusal:
        world.submit(ALICE.id, ahead, private(1).sign(ahead), EVENTS)
    assert refusal.value.error == "chain_broken"
    # 10 before 11: too far ahead and revoked at received_at.
    world.now = T0 + 3600
    late = build_event_text(event_id=new_id(), object_type="task", object_id=work, actor_key_id=revoked.id,
                            signed_at=world.now + 500, prev_hash="-", changes=[Change("title", b"Fix", b"x")],
                            key_certificate=None)
    with pytest.raises(RecordRefusal) as refusal:
        world.submit(revoked.id, late, private(9).sign(late), EVENTS)
    assert refusal.value.error == "event_time_invalid"
    # 11 before 12: revoked, on an object the key does not see.
    assert world.refused(revoked.id, "task", foreign, [("title", "x", "y")], EVENTS) == "key_not_valid_at_receipt"
    # 12 before 13: a security create for another team without the scope.
    other = [("team", None, OTHER_TEAM) if name == "team" else (name, old, new)
             for name, old, new in security_create("x", [])]
    assert world.refused(CAROL.id, "task", new_id(), other, EVENTS) == "change_not_allowed"
    # 13 before 14: a security create without the scope and with a malformed title.
    malformed = [("title", None, " x") if name == "title" else (name, old, new)
                 for name, old, new in security_create("x", [])]
    assert world.refused(CAROL.id, "task", new_id(), malformed, EVENTS) == "scope_missing"
    # 14 before 15: a malformed value and a stale old value.
    assert world.refused(ALICE.id, "task", work, [("title", "Stale", " x")], EVENTS) == "value_invalid"
    # 15 before 16: a stale old value of a field the kind does not have.
    assert world.refused(ALICE.id, "task", work, [("severity", "low", "high")], EVENTS) == "state_mismatch"
    # 16 before 17: a field the kind does not have and an invalid transition.
    assert world.refused(ALICE.id, "task", work, [("severity", None, "high"), ("status", "in_progress", "done"),
                         ("links", None, LINK)], EVENTS) == "field_not_allowed"
    # 17 before 18: an invalid transition by an actor that rule 3 refuses too (no writer).
    status(world, ALICE.id, work, "in_progress", "done", EVENTS)
    assert world.refused(RITA.id, "task", work, [("status", "done", "merged")], EVENTS) == "transition_invalid"
    assert world.refused(RITA.id, "task", work, [("title", "Fix", "x")], EVENTS) == "change_not_allowed"
