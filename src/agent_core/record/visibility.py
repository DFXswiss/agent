"""Which key sees which object, event and failure (RECORD-PROTOCOL.md sections 3, 7.4 and 7.9).

Pure functions over a state and a key entry of the keys file. A key that sees a task sees the
whole task, every link included (section 3); a key that does not see an object reads nothing of it.
"""

from __future__ import annotations

from typing import Any

from ..keys import KeyEntry
from ..signing import b64url_encode
from .event import Event, time_text
from .objects import KIND_WORK, Comment, RecordObject, Task, reference_repository
from .state import StateView

SCOPE_READ = "record:read"
SCOPE_SECURITY = "record:security"
SERVICE = "service"

WITHHELD = "withheld"
# Section 7.9: failures that concern only record-wide positions, hashes and checkpoints.
RECORD_WIDE_FAILURES = frozenset({"position_gap", "record_hash_mismatch", "checkpoint_invalid"})
FILE_INVALID = "file_invalid"


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


def answered_failure(entry: KeyEntry, failure: str, event: Event | None, state: StateView) -> str:
    """The `failure` of section 7.9 for the calling key.

    `event` is the failing event when its text could be read, `state` the state that the
    verification folded from the events before it. A record-wide failure is named to every key;
    every other failure only to a key that sees the failing event's object in that state.
    """
    if failure == FILE_INVALID:
        raise ValueError("file_invalid never arises in the hub")
    if failure in RECORD_WIDE_FAILURES:
        return failure
    if event is None:
        return WITHHELD
    existing = state.objects.get(event.object_id)
    if existing is not None:
        return failure if sees_object(entry, state, event.object_id) else WITHHELD
    if event.object_type != "comment":
        return WITHHELD
    named = [change.new for change in event.changes if change.field == "task"]
    if len(named) != 1 or named[0] is None:
        return WITHHELD
    try:
        task_id = named[0].decode("ascii")
    except UnicodeDecodeError:
        return WITHHELD
    return failure if sees_object(entry, state, task_id) else WITHHELD
