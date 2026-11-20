"""The reducer of the team record (RECORD-PROTOCOL.md section 6).

Deterministic pure functions. `check_append` runs the checks of section 6.7 that need neither an
HTTP request nor a database (checks 3 to 18) for an event that arrives at an append endpoint;
`check_stored` runs only the state rules (section 6.1) for an event of a fold. Both read only the
state before the event, the event, and what their arguments carry; both return the `Applied`
change or raise a `RecordRefusal` whose message is only the error value (section 13).

State rules (sections 6.2 to 6.4 and 6.6) and actor rules (section 6.5) are separate functions,
so that the fold rechecks the state rules without the actor rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from types import MappingProxyType
from typing import Mapping

from ..keys import KeyEntry, KeyRing
from ..signing import ERROR_SCOPE_MISSING
from ..signing import ERROR_SUBJECT as ERROR_SUBJECT_NOT_ALLOWED
from .event import (
    ERROR_CHAIN_BROKEN,
    ERROR_EVENT_INVALID,
    ERROR_EVENT_SIGNATURE_INVALID,
    ERROR_EVENT_TIME_INVALID,
    Change,
    ChainHead,
    Event,
    RecordRefusal,
    check_chain,
    check_signed_at,
    parse_event,
    record_hash,
    signature_valid,
)
from .objects import (
    COMMENT,
    COMPLETION_MERGED,
    COMPLETION_VERIFIED,
    DEFAULT_COMPLETION,
    DEFAULT_STATUS,
    DONE,
    DROPPED,
    FIELDS,
    IMMUTABLE_TASK_FIELDS,
    IN_PROGRESS,
    KIND_SECURITY,
    KIND_WORK,
    MERGED,
    OPEN,
    READY_FOR_REVIEW,
    REFUSED_TASK_FIELDS,
    REQUIRED_COMMENT_FIELDS,
    REQUIRED_TASK_FIELDS,
    TASK,
    Comment,
    RecordObject,
    Stamp,
    Task,
    reference_repository,
    valid_value,
)
from .state import Applied, Head, RecordState, StateView, applied_state
from .visibility import SCOPE_READ, SCOPE_SECURITY, SERVICE, sees_named_task, sees_object

ENDPOINT_EVENTS = "events"
ENDPOINT_WATCH = "watch"
ENDPOINTS = (ENDPOINT_EVENTS, ENDPOINT_WATCH)

SCOPE_WRITE = "record:write"
SCOPE_WATCH = "record:watch"

# Section 6.7, check 12 (e), the field limit of actor rule 3: the only fields an event through
# `POST /api/key/record/watch` may name.
WATCH_FIELDS = frozenset({"status", "fix"})

# Section 6.7, check 10: `signed_at` is at most this many seconds after `received_at`.
SIGNED_AT_AHEAD_SECONDS = 120

ERROR_ACTOR_MISMATCH = "actor_mismatch"
ERROR_CERTIFICATE_NOT_SUPPORTED = "certificate_not_supported"
ERROR_EVENT_DUPLICATE = "event_duplicate"
ERROR_EVENT_ID_TAKEN = "event_id_taken"
ERROR_KEY_NOT_VALID_AT_RECEIPT = "key_not_valid_at_receipt"
ERROR_OBJECT_NOT_FOUND = "object_not_found"
ERROR_FIELD_REQUIRED = "field_required"
ERROR_CHANGE_NOT_ALLOWED = "change_not_allowed"
ERROR_VALUE_INVALID = "value_invalid"
ERROR_STATE_MISMATCH = "state_mismatch"
ERROR_FIELD_NOT_ALLOWED = "field_not_allowed"
ERROR_FIELD_IMMUTABLE = "field_immutable"
ERROR_TRANSITION_INVALID = "transition_invalid"
ERROR_SHORT_NAME_TAKEN = "short_name_taken"
ERROR_LINKS_FROZEN = "links_frozen"
ERROR_ASSIGNEE_INVALID = "assignee_invalid"

# The status of every error value of sections 6.5 to 6.7. Sections 6.4 and 6.7 (check 17) give
# `transition_invalid` the status 409.
STATUS_OF_ERROR: Mapping[str, int] = MappingProxyType(
    {
        ERROR_EVENT_INVALID: 400,
        ERROR_ACTOR_MISMATCH: 403,
        ERROR_CERTIFICATE_NOT_SUPPORTED: 400,
        ERROR_EVENT_SIGNATURE_INVALID: 400,
        ERROR_EVENT_DUPLICATE: 409,
        ERROR_EVENT_ID_TAKEN: 409,
        ERROR_CHAIN_BROKEN: 409,
        ERROR_EVENT_TIME_INVALID: 400,
        ERROR_KEY_NOT_VALID_AT_RECEIPT: 409,
        ERROR_OBJECT_NOT_FOUND: 404,
        ERROR_FIELD_REQUIRED: 409,
        ERROR_CHANGE_NOT_ALLOWED: 403,
        ERROR_SCOPE_MISSING: 403,
        ERROR_VALUE_INVALID: 400,
        ERROR_STATE_MISMATCH: 409,
        ERROR_FIELD_NOT_ALLOWED: 400,
        ERROR_FIELD_IMMUTABLE: 409,
        ERROR_TRANSITION_INVALID: 409,
        ERROR_SHORT_NAME_TAKEN: 409,
        ERROR_LINKS_FROZEN: 409,
        ERROR_ASSIGNEE_INVALID: 409,
        ERROR_SUBJECT_NOT_ALLOWED: 403,
    }
)

# Section 2, "Writer": principal `agent`; section 10 adds `staff` once session keys exist.
WRITER_PRINCIPALS = frozenset({"agent"})
# Section 6.4, "Assignee": principal `agent`; section 10 adds `staff` once session keys exist.
ASSIGNEE_PRINCIPALS = frozenset({"agent"})
# Section 6.5, "can act on": principal `agent`; section 10 adds certified `staff` entries.
ACTING_PRINCIPALS = frozenset({"agent"})
WORK_ASSIGNEE_PRINCIPALS = frozenset({"staff", "agent"})
ACTING_SCOPES = frozenset({SCOPE_READ, SCOPE_WRITE, SCOPE_SECURITY})

_ACTIVE = (OPEN, IN_PROGRESS, READY_FOR_REVIEW)
_STARTED = (IN_PROGRESS, READY_FOR_REVIEW)
_FINISHED = (MERGED, DONE)
_KIND_OF_VALUE = MappingProxyType({b"work": KIND_WORK, b"security": KIND_SECURITY})
_BYTES = {name: name.encode("ascii") for name in (DONE, IN_PROGRESS)}
_VERIFIED = COMPLETION_VERIFIED.encode("ascii")
_MERGED_RULE = COMPLETION_MERGED.encode("ascii")

# Section 6.4, column "Who", by row.
WHO_WRITER = "writer"
WHO_WATCHER = "watcher"
WHO_ASSIGNEE = "assignee"
WHO_STAFF = "staff"
WHO_OF_ROW: Mapping[int, str] = MappingProxyType(
    {
        1: WHO_WRITER,
        2: WHO_WRITER,
        3: WHO_WRITER,
        4: WHO_WRITER,
        5: WHO_WRITER,
        6: WHO_WRITER,
        7: WHO_WRITER,
        8: WHO_WATCHER,
        9: WHO_WATCHER,
        10: WHO_ASSIGNEE,
        11: WHO_STAFF,
        12: WHO_ASSIGNEE,
        13: WHO_WRITER,
        14: WHO_WRITER,
    }
)


def refusal_status(error: str) -> int:
    """The HTTP status of an error value of section 6; `KeyError` for any other value."""
    return STATUS_OF_ERROR[error]


def _refuse(error: str) -> RecordRefusal:
    return RecordRefusal(error)


@dataclass(frozen=True)
class AppendContext:
    """What the checks of one append may read beyond the state and the event (sections 2 and 6.1).

    `actor` is the keys-file entry of the request's `Agent-Key`, which the request checks of
    KEY-PROTOCOL.md section 5 established; `endpoint` is `events` or `watch`; `received_at` is the
    one clock reading of step 2 of the append transaction (section 4.6), already the later of that
    reading and the `received_at` of the head.
    """

    keys: KeyRing = dataclass_field(repr=False)
    actor: KeyEntry = dataclass_field(repr=False)
    endpoint: str
    received_at: int = dataclass_field(repr=False)

    def __post_init__(self) -> None:
        if self.endpoint not in ENDPOINTS:
            raise ValueError("the endpoint is events or watch")


@dataclass(frozen=True)
class Accepted:
    """An accepted append: the change and the state after it."""

    applied: Applied
    state: RecordState = dataclass_field(repr=False)


@dataclass(frozen=True)
class _Outcome:
    """What the state rules computed, for the actor rules: before, after and the matching rows."""

    objects: tuple[RecordObject, ...]
    before: Task | None
    after: Mapping[str, bytes]
    changes: Mapping[str, Change]
    rows: tuple[int, ...]


def _changes(event: Event) -> dict[str, Change]:
    return {change.field: change for change in event.changes}


def _next_head(state: StateView, event: Event, received_at: int) -> Head:
    head = state.head
    position = 1 if head is None else head.position + 1
    previous = None if head is None else head.record_hash
    return Head(position, received_at, record_hash(position, received_at, event.event_hash, previous))


# --- checks shared with the fold ----------------------------------------------------------------


def check_event_time(chain: ChainHead | None, event: Event, received_at: int) -> None:
    """Check 10 of section 6.7 and its form in section 12.1: `signed_at` is at most 120 seconds after
    `received_at` and not earlier than `signed_at` of the key's last accepted event."""
    if event.signed_at > received_at + SIGNED_AT_AHEAD_SECONDS:
        raise _refuse(ERROR_EVENT_TIME_INVALID)
    check_signed_at(chain, event)


def check_value_forms(event: Event) -> None:
    """Check 14 of section 6.7: every value of a field the object type has is in its form.

    Both `old` and `new` are values; a field the object type does not have is left to check 16.
    """
    fields = FIELDS[event.object_type]
    for change in event.changes:
        if change.field not in fields:
            continue
        for value in (change.old, change.new):
            if value is not None and not valid_value(event.object_type, change.field, value):
                raise _refuse(ERROR_VALUE_INVALID)


# --- state rules ---------------------------------------------------------------------------------


def _check_identity(existing: RecordObject | None, event: Event) -> None:
    """Check 12 (b): a create of a task changes `kind`, a create of a comment changes `task`."""
    if existing is not None:
        return
    names = {change.field for change in event.changes}
    needed = "kind" if event.object_type == TASK else "task"
    if needed not in names:
        raise _refuse(ERROR_FIELD_REQUIRED)


def _check_state_match(existing: RecordObject | None, event: Event) -> None:
    """Check 15, section 6.2."""
    if existing is None:
        if any(change.old is not None for change in event.changes):
            raise _refuse(ERROR_STATE_MISMATCH)
        return
    if existing.object_type != event.object_type:
        raise _refuse(ERROR_STATE_MISMATCH)
    if any(change.old != existing.value(change.field) for change in event.changes):
        raise _refuse(ERROR_STATE_MISMATCH)


def _kind_of(existing: RecordObject | None, changes: Mapping[str, Change]) -> str:
    """The kind of a task event: the new `kind` of a create, the current kind of an update."""
    if isinstance(existing, Task):
        return existing.kind
    return _KIND_OF_VALUE[changes["kind"].new]  # type: ignore[index]


def _check_fields(existing: RecordObject | None, event: Event, changes: Mapping[str, Change]) -> None:
    """Check 16, section 6.3: first `field_not_allowed`, then `field_immutable`."""
    allowed = FIELDS[event.object_type]
    refused = (
        REFUSED_TASK_FIELDS[_kind_of(existing, changes)] if event.object_type == TASK else frozenset()
    )
    if any(name not in allowed or name in refused for name in changes):
        raise _refuse(ERROR_FIELD_NOT_ALLOWED)
    if existing is None:
        return
    immutable = IMMUTABLE_TASK_FIELDS if event.object_type == TASK else allowed
    if any(name in immutable for name in changes):
        raise _refuse(ERROR_FIELD_IMMUTABLE)


def _matching_rows(
    before: Task | None, after: Mapping[str, bytes], changes: Mapping[str, Change]
) -> tuple[int, ...]:
    """The rows of section 6.4 whose state conditions match a status change (create included).

    Section 6.4, "Which state a condition reads": "From" reads the status before the event and "To"
    the status after it; a condition that says "before", "after", "unchanged by the event" or "the
    event changes" reads what it says; every other condition, here those on `links` and
    `completed_links` (rows 8, 9, 10, 12 and 13), reads the state before the event, also when the
    event changes `links`. Rows 1 and 2 of a create have no condition on `links`.
    """
    old = None if before is None else before.status
    new_value = after.get("status")
    new = None if new_value is None else new_value.decode("ascii")
    rule_before = None if before is None else before.values["completion"]
    rule_after = after.get("completion")
    rule_kept = "completion" not in changes
    links = None if before is None else before.value("links")
    assignee = after.get("assignee") is not None
    completion = changes.get("completion")
    verification = changes.get("verification")
    completed = None if before is None else before.completed_links
    conditions = {
        1: before is None and new == OPEN,
        2: (before is None or old == OPEN) and new == IN_PROGRESS and assignee,
        3: old == IN_PROGRESS and new == READY_FOR_REVIEW,
        4: old == READY_FOR_REVIEW and new == IN_PROGRESS,
        5: old in _STARTED and new == OPEN,
        6: old in _ACTIVE and new == DROPPED,
        7: old == DROPPED and new == OPEN,
        8: old in _ACTIVE
        and new == DONE
        and rule_before == _MERGED_RULE
        and rule_after == _MERGED_RULE
        and links is not None,
        9: old in _ACTIVE
        and new == MERGED
        and rule_before == _VERIFIED
        and rule_after == _VERIFIED
        and links is not None,
        10: old in _STARTED
        and rule_kept
        and ((new == DONE and rule_after == _MERGED_RULE) or (new == MERGED and rule_after == _VERIFIED))
        and (links is None or links == completed),
        11: old == MERGED
        and new == DONE
        and rule_before == _VERIFIED
        and rule_after == _VERIFIED
        and verification is not None
        and verification.new is not None,
        12: old == MERGED
        and new == DONE
        and rule_before == _MERGED_RULE
        and rule_after == _MERGED_RULE
        and links is None,
        13: old == MERGED
        and new == DONE
        and completion is not None
        and completion.old == _VERIFIED
        and completion.new == _MERGED_RULE
        and links is not None,
        14: old in _FINISHED and new == IN_PROGRESS and assignee,
    }
    return tuple(row for row, holds in conditions.items() if holds)


def _check_transitions(
    before: Task | None, after: Mapping[str, bytes], changes: Mapping[str, Change]
) -> tuple[int, ...]:
    """Check 17, the state conditions of section 6.4; returns the rows that match."""
    rows: tuple[int, ...] = ()
    if before is None or "status" in changes:
        rows = _matching_rows(before, after, changes)
        if not rows:
            raise _refuse(ERROR_TRANSITION_INVALID)
    if "verification" in changes and 11 not in rows:
        raise _refuse(ERROR_TRANSITION_INVALID)
    completion = changes.get("completion")
    if (
        before is not None
        and before.status == MERGED
        and before.value("links") is not None
        and completion is not None
        and completion.old == _VERIFIED
        and completion.new == _MERGED_RULE
        and after.get("status") not in (_BYTES[DONE], _BYTES[IN_PROGRESS])
    ):
        raise _refuse(ERROR_TRANSITION_INVALID)
    return rows


def _check_task_invariants(
    state: StateView,
    object_id: str,
    before: Task | None,
    after: Mapping[str, bytes],
    changes: Mapping[str, Change],
) -> None:
    """Check 17, section 6.6 for a task, in the order of its table."""
    kind = _KIND_OF_VALUE[after["kind"]]
    if not REQUIRED_TASK_FIELDS[kind] <= after.keys():
        raise _refuse(ERROR_FIELD_REQUIRED)
    status = after["status"].decode("ascii")
    if status in _STARTED and "assignee" not in after:
        raise _refuse(ERROR_FIELD_REQUIRED)
    if kind == KIND_SECURITY and status == DONE and "fix" not in after:
        raise _refuse(ERROR_FIELD_REQUIRED)
    if kind == KIND_SECURITY:
        holder = state.short_names.get((after["team"], after["short_name"].decode("ascii")))
        if holder is not None and holder != object_id:
            raise _refuse(ERROR_SHORT_NAME_TAKEN)
    # The event changes `links` when its `new` value differs from `links` before the event (section
    # 6.6). Check 15 made every `old` equal the value before, and `old` and `new` of a change differ
    # (section 4.2), so that is exactly an event with a change of `links`.
    if "links" in changes:
        status_before = None if before is None else before.status
        if status_before in _FINISHED or status in _FINISHED:
            raise _refuse(ERROR_LINKS_FROZEN)


def _task_outcome(
    state: StateView,
    event: Event,
    before: Task | None,
    changes: Mapping[str, Change],
    stamp: Stamp,
) -> _Outcome:
    after: dict[str, bytes] = {} if before is None else dict(before.values)
    for change in changes.values():
        if change.new is None:
            del after[change.field]
        else:
            after[change.field] = change.new
    if before is None:
        kind = _KIND_OF_VALUE[after["kind"]]
        if "status" not in after:
            after["status"] = DEFAULT_STATUS.encode("ascii")
        if "completion" not in after:
            after["completion"] = DEFAULT_COMPLETION[kind].encode("ascii")
    rows = _check_transitions(before, after, changes)
    _check_task_invariants(state, event.object_id, before, after, changes)
    # Row 14 sets `completed_links` to `links` before the event (section 6.4); rows 1 and 2 of a
    # create never match together with row 14, so `before` is a task there.
    if before is not None and 14 in rows:
        completed = before.value("links")
    else:
        completed = None if before is None else before.completed_links
    task = Task(
        object_id=event.object_id,
        values=MappingProxyType(after),
        completed_links=completed,
        comments=0 if before is None else before.comments,
        created=stamp if before is None else before.created,
        changed=stamp,
    )
    return _Outcome((task,), before, MappingProxyType(after), changes, rows)


def _comment_outcome(state: StateView, event: Event, changes: Mapping[str, Change], stamp: Stamp) -> _Outcome:
    """Check 17, section 6.6 for the create of a comment (every other comment event stops at 16)."""
    after = {name: change.new for name, change in changes.items() if change.new is not None}
    if not REQUIRED_COMMENT_FIELDS <= after.keys():
        raise _refuse(ERROR_FIELD_REQUIRED)
    task_id = after["task"].decode("ascii")
    task = state.objects.get(task_id)
    if not isinstance(task, Task):
        raise _refuse(ERROR_OBJECT_NOT_FOUND)
    comment = Comment(object_id=event.object_id, task=task_id, body=after["body"], created=stamp)
    counted = Task(
        object_id=task.object_id,
        values=task.values,
        completed_links=task.completed_links,
        comments=task.comments + 1,
        created=task.created,
        changed=task.changed,
    )
    return _Outcome((comment, counted), None, MappingProxyType(after), changes, ())


def _state_outcome(
    state: StateView, event: Event, existing: RecordObject | None, head: Head
) -> _Outcome:
    """Checks 15 to 17: the state rules after checks 12 (b) and 14."""
    _check_state_match(existing, event)
    changes = _changes(event)
    _check_fields(existing, event, changes)
    stamp = Stamp(position=head.position, received_at=head.received_at, key=event.actor_key_id)
    if event.object_type == TASK:
        return _task_outcome(state, event, existing if isinstance(existing, Task) else None, changes, stamp)
    return _comment_outcome(state, event, changes, stamp)


def check_stored(state: StateView, event: Event, received_at: int) -> Applied:
    """The state rules of section 6 for one event of a fold (section 6.1), without actor rules.

    The checks run in the order of section 6.7 with every actor check left out: 12 (b), 14, 15,
    16 and 17. The event takes the position after the head of `state`.
    """
    existing = state.objects.get(event.object_id)
    _check_identity(existing, event)
    check_value_forms(event)
    head = _next_head(state, event, received_at)
    outcome = _state_outcome(state, event, existing, head)
    return Applied(event=event, head=head, objects=outcome.objects)


# --- actor rules ---------------------------------------------------------------------------------


def _valid_at(entry: KeyEntry, moment: int) -> bool:
    return not entry.revoked_at(moment) and not entry.expired_at(moment)


def is_writer(entry: KeyEntry) -> bool:
    return entry.principal in WRITER_PRINCIPALS and SCOPE_WRITE in entry.scopes


def is_watcher(entry: KeyEntry) -> bool:
    return entry.principal == SERVICE and SCOPE_WATCH in entry.scopes


def can_act_on(entry: KeyEntry, team: bytes, moment: int) -> bool:
    """Section 6.5, "can act on" a task of kind `security` of `team` at `moment`."""
    return (
        entry.principal in ACTING_PRINCIPALS
        and entry.team.encode("utf-8") == team
        and _valid_at(entry, moment)
        and ACTING_SCOPES <= set(entry.scopes)
    )


def is_orphaned(keys: KeyRing, handle: bytes, team: bytes, moment: int) -> bool:
    """Section 6.5: no entry of this handle can act on the task at `moment`."""
    return not any(
        entry.handle.encode("ascii") == handle and can_act_on(entry, team, moment)
        for entry in keys.entries.values()
    )


def _assignee_allowed(keys: KeyRing, handle: bytes, kind: str, team: bytes, moment: int) -> bool:
    """Actor rule 5."""
    for entry in keys.entries.values():
        if entry.handle.encode("ascii") != handle:
            continue
        if kind == KIND_SECURITY:
            if can_act_on(entry, team, moment):
                return True
        elif (
            entry.principal in WORK_ASSIGNEE_PRINCIPALS
            and entry.team.encode("utf-8") == team
            and _valid_at(entry, moment)
        ):
            return True
    return False


def _who_allows(row: int, actor: KeyEntry, outcome: _Outcome) -> bool:
    who = WHO_OF_ROW[row]
    handle = actor.handle.encode("ascii")
    if who == WHO_WRITER:
        if not is_writer(actor):
            return False
        if row != 2 or outcome.after["kind"] == KIND_WORK.encode("ascii"):
            return True
        # Row 2 for kind `security`: only the writer whose handle is the assignee after the event.
        return outcome.after.get("assignee") == handle
    if who == WHO_WATCHER:
        return is_watcher(actor)
    if who == WHO_STAFF:
        return actor.principal == "staff"
    before = outcome.before
    return (
        actor.principal in ASSIGNEE_PRINCIPALS
        and "assignee" not in outcome.changes
        and before is not None
        and before.value("assignee") == handle
    )


def _check_actor_rules(outcome_task: Task, outcome: _Outcome, context: AppendContext) -> None:
    """Check 18: actor rules 2 to 6 of section 6.5, in the order of its table."""
    actor = context.actor
    changes = outcome.changes
    # Rule 2.
    if outcome.rows and not any(_who_allows(row, actor, outcome) for row in outcome.rows):
        raise _refuse(ERROR_CHANGE_NOT_ALLOWED)
    # Rule 3.
    if context.endpoint == ENDPOINT_EVENTS:
        if not is_writer(actor):
            raise _refuse(ERROR_CHANGE_NOT_ALLOWED)
    else:
        # Check 12 (e) already refused every field other than `status` and `fix`.
        if "status" not in changes:
            raise _refuse(ERROR_CHANGE_NOT_ALLOWED)
        fix = changes.get("fix")
        if fix is not None and not (
            outcome_task.kind == KIND_SECURITY
            and fix.new is not None
            and fix.new.decode("ascii") in outcome_task.links
        ):
            raise _refuse(ERROR_CHANGE_NOT_ALLOWED)
    kind = outcome_task.kind
    team = outcome_task.team
    handle = actor.handle.encode("ascii")
    assignee = changes.get("assignee")
    # Rule 4.
    if kind == KIND_SECURITY and assignee is not None:
        old, new = assignee.old, assignee.new
        taken = old is None and new == handle
        handed = old is not None and old == handle
        rescued = (
            old is not None
            and new == handle
            and is_orphaned(context.keys, old, team, context.received_at)
        )
        if not (taken or handed or rescued):
            raise _refuse(ERROR_CHANGE_NOT_ALLOWED)
    # Rule 5.
    if assignee is not None and assignee.new is not None:
        if not _assignee_allowed(context.keys, assignee.new, kind, team, context.received_at):
            raise _refuse(ERROR_ASSIGNEE_INVALID)
    # Rule 6.
    if context.endpoint == ENDPOINT_WATCH:
        repositories = {reference_repository(reference) for reference in outcome_task.links}
        if actor.subjects is None or not repositories <= set(actor.subjects):
            raise _refuse(ERROR_SUBJECT_NOT_ALLOWED)


# --- the append ----------------------------------------------------------------------------------


def check_signed_event(text: bytes, signature: bytes, actor: KeyEntry) -> Event:
    """Checks 3 to 6 of section 6.7, in this order: the form of the event text, the actor, the
    certificate and the signature. They read no state, so the append runs them before its
    transaction and before any fold (section 11.5)."""
    # Check 3.
    event = parse_event(text)
    # Check 4.
    if event.actor_key_id != actor.id:
        raise _refuse(ERROR_ACTOR_MISMATCH)
    # Check 5.
    if event.key_certificate is not None:
        raise _refuse(ERROR_CERTIFICATE_NOT_SUPPORTED)
    # Check 6.
    if not signature_valid(actor.public_key, event.text, signature):
        raise _refuse(ERROR_EVENT_SIGNATURE_INVALID)
    return event


def check_append(state: StateView, text: bytes, signature: bytes, context: AppendContext) -> Applied:
    """Checks 3 to 18 of section 6.7, in this order, for an event at an append endpoint.

    Checks 1 and 2 (query, `Content-Type` and body) and the `503` of section 7.1 belong to the
    endpoint, which runs them before. `ValueError` when `context.received_at` is earlier than the
    `received_at` of the head, which step 2 of section 11.5 never gives.
    """
    head = state.head
    if head is not None and context.received_at < head.received_at:
        raise ValueError("received_at is never earlier than at the previous position")
    actor = context.actor
    # Checks 3 to 6.
    event = check_signed_event(text, signature, actor)
    # Check 7.
    if event.event_hash in state.event_hashes:
        raise _refuse(ERROR_EVENT_DUPLICATE)
    # Check 8.
    if event.event_id in state.event_ids:
        raise _refuse(ERROR_EVENT_ID_TAKEN)
    # Check 9.
    key_head = state.chains.get(actor.id)
    chain = None if key_head is None else key_head.chain
    check_chain(chain, event)
    # Check 10.
    check_event_time(chain, event, context.received_at)
    # Check 11.
    if not _valid_at(actor, context.received_at):
        raise _refuse(ERROR_KEY_NOT_VALID_AT_RECEIPT)
    # Check 12.
    existing = state.objects.get(event.object_id)
    if context.endpoint == ENDPOINT_WATCH and not isinstance(existing, Task):
        raise _refuse(ERROR_OBJECT_NOT_FOUND)  # (a)
    _check_identity(existing, event)  # (b)
    changes = _changes(event)
    team = changes.get("team")
    if existing is None and event.object_type == TASK and team is not None:
        if team.new != actor.team.encode("utf-8"):
            raise _refuse(ERROR_CHANGE_NOT_ALLOWED)  # (c), actor rule 1
    if existing is not None:
        if not sees_object(actor, state, event.object_id):
            raise _refuse(ERROR_OBJECT_NOT_FOUND)  # (d)
    elif event.object_type == COMMENT:
        if not sees_named_task(actor, state, changes["task"].new):
            raise _refuse(ERROR_OBJECT_NOT_FOUND)  # (d)
    # (e): after (a) and (d), which answer a task the watcher does not see like a missing one, and
    # before checks 13 to 18, which read further state of the team (such as `short_name_taken`).
    if context.endpoint == ENDPOINT_WATCH and not changes.keys() <= WATCH_FIELDS:
        raise _refuse(ERROR_CHANGE_NOT_ALLOWED)
    # Check 13.
    if (
        existing is None
        and event.object_type == TASK
        and changes["kind"].new == KIND_SECURITY.encode("ascii")
        and SCOPE_SECURITY not in actor.scopes
    ):
        raise _refuse(ERROR_SCOPE_MISSING)
    # Check 14.
    check_value_forms(event)
    # Checks 15 to 17.
    new_head = _next_head(state, event, context.received_at)
    outcome = _state_outcome(state, event, existing, new_head)
    # Check 18. A comment has no status, no assignee and no links; only rule 3 applies to it.
    first = outcome.objects[0]
    if isinstance(first, Task):
        _check_actor_rules(first, outcome, context)
    elif not is_writer(actor):
        raise _refuse(ERROR_CHANGE_NOT_ALLOWED)
    return Applied(event=event, head=new_head, objects=outcome.objects)


def reduce_append(state: RecordState, text: bytes, signature: bytes, context: AppendContext) -> Accepted:
    """The reducer as a pure function: the state after an accepted append, or a `RecordRefusal`."""
    applied = check_append(state, text, signature, context)
    return Accepted(applied=applied, state=applied_state(state, applied))


def reduce_stored(state: RecordState, event: Event, received_at: int) -> RecordState:
    """The fold step as a pure function: the state rules only (section 6.1)."""
    return applied_state(state, check_stored(state, event, received_at))
