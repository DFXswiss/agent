"""The state of the team record (RECORD-PROTOCOL.md sections 6 and 11.6) as an immutable value.

The state holds every object, the head of every key's chain, the `event_id` and the event hash of
every folded event, the index of `short_name` per team, the index from pull-request references to
tasks, and the record head with its `received_at` (section 11.6). It also holds, per object, the
positions of its events, which `GET /api/key/record/events?object=` reads (section 7.4).

`RecordState` is never changed. A new state comes from `StateBuilder`, which copies a state once,
applies events and is then frozen; a fold of many events therefore copies the state once, not once
per event.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from types import MappingProxyType
from typing import AbstractSet, Mapping, Protocol

from .event import ChainHead, Event, chain_head
from .objects import KIND_SECURITY, RecordObject, Task


@dataclass(frozen=True)
class Head:
    """The head of the record: the position and record hash of its last event, with `received_at`."""

    position: int
    received_at: int = dataclass_field(repr=False)
    record_hash: str


@dataclass(frozen=True)
class KeyHead:
    """The last accepted event of one key: its chain head (section 4.5) and its position (section 7.8)."""

    chain: ChainHead
    position: int


@dataclass(frozen=True)
class Applied:
    """What one accepted event changes: the event, the new head and the new values of its objects.

    `objects` holds the object of the event and, for the create of a comment, its task with the
    new number of comments.
    """

    event: Event
    head: Head
    objects: tuple[RecordObject, ...] = dataclass_field(repr=False)


class StateView(Protocol):
    """What the reducer reads; both `RecordState` and `StateBuilder` provide it."""

    @property
    def head(self) -> Head | None: ...

    @property
    def objects(self) -> Mapping[str, RecordObject]: ...

    @property
    def chains(self) -> Mapping[str, KeyHead]: ...

    @property
    def event_ids(self) -> AbstractSet[str]: ...

    @property
    def event_hashes(self) -> AbstractSet[str]: ...

    @property
    def short_names(self) -> Mapping[tuple[bytes, str], str]: ...

    @property
    def linked_tasks(self) -> Mapping[str, frozenset[str]]: ...


@dataclass(frozen=True)
class RecordState:
    """The state after a prefix of the record. Section 13: its `repr` shows only the head."""

    head: Head | None
    objects: Mapping[str, RecordObject] = dataclass_field(repr=False)
    chains: Mapping[str, KeyHead] = dataclass_field(repr=False)
    event_ids: AbstractSet[str] = dataclass_field(repr=False)
    event_hashes: AbstractSet[str] = dataclass_field(repr=False)
    # (team, short_name) of every task of kind `security` to its object id.
    short_names: Mapping[tuple[bytes, str], str] = dataclass_field(repr=False)
    # Pull-request reference to the ids of the tasks whose `links` contain it.
    linked_tasks: Mapping[str, frozenset[str]] = dataclass_field(repr=False)
    # Object id to the positions of its events, ascending (section 7.4 with `object`).
    object_events: Mapping[str, tuple[int, ...]] = dataclass_field(repr=False)

    def task(self, object_id: str) -> Task | None:
        """The task with this id; None when no object or another type has it."""
        found = self.objects.get(object_id)
        return found if isinstance(found, Task) else None


EMPTY_STATE = RecordState(
    head=None,
    objects=MappingProxyType({}),
    chains=MappingProxyType({}),
    event_ids=frozenset(),
    event_hashes=frozenset(),
    short_names=MappingProxyType({}),
    linked_tasks=MappingProxyType({}),
    object_events=MappingProxyType({}),
)


def _short_name_key(task: Task | None) -> tuple[bytes, str] | None:
    if task is None or task.kind != KIND_SECURITY:
        return None
    short_name = task.short_name
    return None if short_name is None else (task.team, short_name)


class StateBuilder:
    """A private, changeable copy of a state. `freeze` ends it; it is never used after that."""

    def __init__(self, state: RecordState) -> None:
        self.head: Head | None = state.head
        self.objects: dict[str, RecordObject] = dict(state.objects)
        self.chains: dict[str, KeyHead] = dict(state.chains)
        self.event_ids: set[str] = set(state.event_ids)
        self.event_hashes: set[str] = set(state.event_hashes)
        self.short_names: dict[tuple[bytes, str], str] = dict(state.short_names)
        self.linked_tasks: dict[str, frozenset[str]] = dict(state.linked_tasks)
        self.object_events: dict[str, tuple[int, ...]] = dict(state.object_events)
        self._frozen = False

    def apply(self, applied: Applied) -> None:
        if self._frozen:
            raise RuntimeError("a frozen state builder is never changed")
        event = applied.event
        for item in applied.objects:
            if isinstance(item, Task):
                previous = self.objects.get(item.object_id)
                self._reindex(previous if isinstance(previous, Task) else None, item)
            self.objects[item.object_id] = item
        self.event_ids.add(event.event_id)
        self.event_hashes.add(event.event_hash)
        self.chains[event.actor_key_id] = KeyHead(chain_head(event), applied.head.position)
        earlier = self.object_events.get(event.object_id)
        position = (applied.head.position,)
        self.object_events[event.object_id] = position if earlier is None else earlier + position
        self.head = applied.head

    def _reindex(self, previous: Task | None, task: Task) -> None:
        old_key = _short_name_key(previous)
        new_key = _short_name_key(task)
        if old_key is not None and old_key != new_key:
            del self.short_names[old_key]
        if new_key is not None:
            self.short_names[new_key] = task.object_id
        old_links = frozenset(previous.links) if previous is not None else frozenset()
        new_links = frozenset(task.links)
        for reference in old_links - new_links:
            remaining = self.linked_tasks[reference] - {task.object_id}
            if remaining:
                self.linked_tasks[reference] = remaining
            else:
                del self.linked_tasks[reference]
        for reference in new_links - old_links:
            current = self.linked_tasks.get(reference)
            self.linked_tasks[reference] = (
                frozenset({task.object_id}) if current is None else current | {task.object_id}
            )

    def freeze(self) -> RecordState:
        if self._frozen:
            raise RuntimeError("a state builder is frozen only once")
        self._frozen = True
        return RecordState(
            head=self.head,
            objects=MappingProxyType(self.objects),
            chains=MappingProxyType(self.chains),
            event_ids=frozenset(self.event_ids),
            event_hashes=frozenset(self.event_hashes),
            short_names=MappingProxyType(self.short_names),
            linked_tasks=MappingProxyType(self.linked_tasks),
            object_events=MappingProxyType(self.object_events),
        )


def applied_state(state: RecordState, applied: Applied) -> RecordState:
    """The state after one accepted event, as a new value; `state` stays unchanged."""
    builder = StateBuilder(state)
    builder.apply(applied)
    return builder.freeze()
