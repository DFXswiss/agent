"""Which key sees which object and event, and the answered failure (RECORD-PROTOCOL.md sections 3,
7.4 and 7.9).

Pure functions over a state and a key entry of the keys file. A key that sees a task sees the
whole task, every link included (section 3); a key that does not see an object reads nothing of it.
The answered failure of section 7.9 is the one answer here that depends on no key.
"""

from __future__ import annotations

from typing import Any

from ..keys import KeyEntry
from ..signing import b64url_encode
from .event import Event, time_text
from .failures import CHECKPOINT_INVALID, FILE_INVALID, POSITION_GAP, RECORD_HASH_MISMATCH
from .objects import COMMENT, KIND_WORK, Comment, RecordObject, Task, reference_repository, valid_value
from .state import StateView

SCOPE_READ = "record:read"
SCOPE_SECURITY = "record:security"
SERVICE = "service"

WITHHELD = "withheld"
# Section 7.9: the only failures the answer names, for every key; they concern only record-wide
# positions, `received_at` values, hashes and checkpoints. Every other failure is `withheld`.
RECORD_WIDE_FAILURES = frozenset({POSITION_GAP, RECORD_HASH_MISMATCH, CHECKPOINT_INVALID})


def task_repositories(task: Task) -> frozenset[str]:
    """The repositories of a task: of each reference in `links`, and `repository` for kind `security`."""
    repositories = {reference_repository(reference) for reference in task.links}
    repository = task.repository
    if repository is not None:
        repositories.add(repository)
    return frozenset(repositories)


def sees_task(entry: KeyEntry, task: Task) -> bool:
    """The four conditions of section 3, "Which key sees which object"."""
    if SCOPE_READ not in entry.scopes:
        return False
    if entry.team.encode("utf-8") != task.team:
        return False
    if task.kind != KIND_WORK and SCOPE_SECURITY not in entry.scopes:
        return False
    if entry.principal != SERVICE:
        return True
    return entry.subjects is not None and not task_repositories(task).isdisjoint(entry.subjects)


def deciding_task(state: StateView, object_id: str) -> Task | None:
    """The task whose visibility decides for an object: the task itself, or a comment's task."""
    found: RecordObject | None = state.objects.get(object_id)
    if isinstance(found, Comment):
        found = state.objects.get(found.task)
    return found if isinstance(found, Task) else None


def sees_object(entry: KeyEntry, state: StateView, object_id: str) -> bool:
    """A key sees a task by section 3 and a comment when it sees the comment's task."""
    task = deciding_task(state, object_id)
    return task is not None and sees_task(entry, task)


def sees_named_task(entry: KeyEntry, state: StateView, value: bytes | None) -> bool:
    """Whether the `task` value of a comment create names a task the key sees.

    The value names a task when it is a UUID (section 5.3) that is the id of a task; the id of a
    comment, or of nothing, names no task. Check 12 (d) of section 6.7 uses this test.
    """
    if value is None or not valid_value(COMMENT, "task", value):
        return False
    task = state.objects.get(value.decode("ascii"))
    return isinstance(task, Task) and sees_task(entry, task)


def readable_object(entry: KeyEntry, state: StateView, object_id: str) -> RecordObject | None:
    """The whole object when the key sees it; None, as for an id that no object has, otherwise."""
    if not sees_object(entry, state, object_id):
        return None
    return state.objects[object_id]


def sees_event(entry: KeyEntry, state: StateView, event: Event) -> bool:
    """A key sees an event when it sees the event's object in the given state (section 3)."""
    return sees_object(entry, state, event.object_id)


def event_entry(
    *,
    position: int,
    received_at: int,
    event_hash: str,
    record_hash: str,
    text: bytes,
    signature: bytes,
    visible: bool,
) -> dict[str, Any]:
    """One entry of section 7.4, in its member order; a withheld entry has `null` text and signature."""
    received = time_text(received_at)
    if received is None:
        raise ValueError("received_at is not a time")
    return {
        "position": position,
        "received_at": received,
        "event_hash": event_hash,
        "record_hash": record_hash,
        "event": b64url_encode(text) if visible else None,
        "signature": b64url_encode(signature) if visible else None,
    }


def answered_failure(failure: str) -> str:
    """The `failure` of the answer of section 7.9 for the true value `failure` of section 12.1.

    The answer does not depend on what the calling key sees, so this takes no key and no state
    (section 3, decision 73): a record-wide failure is named; every failure that concerns an event
    is `withheld`, for every key, also one that sees the failing event's object. The true value
    stays in `verify.Failure.failure` for the log line of section 13.
    """
    if failure == FILE_INVALID:
        raise ValueError("file_invalid never arises in the hub")
    return failure if failure in RECORD_WIDE_FAILURES else WITHHELD
