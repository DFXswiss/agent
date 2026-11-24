"""The endpoints under `/api/key/record/` (RECORD-PROTOCOL.md sections 7, 8 and 13) and the answers
of the release query that come from the record (section 9).

Each endpoint runs after checks 1 to 9 of KEY-PROTOCOL.md section 5 and after its query validation
(`KeyService`). Every answer is a `KeyAnswer` whose result is written by the request line of section
13; the line is written by the log area that `register_record` adds, for every key.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Callable

import psycopg

from ..keys import KeyEntry, KeyRing, valid_handle, valid_subject
from ..signing import (
    ERROR_INTERNAL,
    LOG,
    SIGNATURE_BYTES,
    KeyAnswer,
    KeyRequest,
    KeyRoutes,
    b64url_decode,
    b64url_encode,
    valid_key_id,
)
from .checkpoint import Checkpoint
from .event import MAX_EVENT_BYTES, MAX_POSITION, parse_event, time_text, valid_uuid
from .objects import KINDS, STATUSES, Comment, RecordObject, Stamp, Task, parse_urgency
from .reducer import ENDPOINT_EVENTS, ENDPOINT_WATCH, SCOPE_WATCH, SCOPE_WRITE, refusal_status
from .runtime import (
    AlreadyUnverified,
    DatabaseFailed,
    Read,
    RecordRuntime,
    Verified,
    VerifyBusy,
    log_database_error,
    log_verify_failed,
)
from .state import RecordState
from .storage import Appended, Refused, Unverified, read_checkpoints, read_events, read_events_at
from .visibility import (
    SCOPE_READ,
    answered_failure,
    event_entry,
    readable_object,
    sees_object,
    sees_task,
    task_repositories,
)

RECORD_PREFIX = "/api/key/record/"
PATH_EVENTS = RECORD_PREFIX + "events"
PATH_WATCH = RECORD_PREFIX + "watch"
PATH_OBJECT = RECORD_PREFIX + "object"
PATH_TASKS = RECORD_PREFIX + "tasks"
PATH_COMMENTS = RECORD_PREFIX + "comments"
PATH_HEAD = RECORD_PREFIX + "head"
PATH_VERIFY = RECORD_PREFIX + "verify"
PATH_CHECKPOINTS = RECORD_PREFIX + "checkpoints"
# The paths of the endpoint table of section 7.1; the request line writes only these (section 13).
RECORD_PATHS = frozenset(
    {
        PATH_EVENTS,
        PATH_WATCH,
        PATH_OBJECT,
        PATH_TASKS,
        PATH_COMMENTS,
        PATH_HEAD,
        PATH_VERIFY,
        PATH_CHECKPOINTS,
    }
)
LOGGED_METHODS = frozenset({"GET", "POST", "HEAD"})

ERROR_BODY_INVALID = "body_invalid"
ERROR_OBJECT_NOT_FOUND = "object_not_found"
ERROR_RECORD_UNVERIFIED = "record_unverified"
ERROR_VERIFY_BUSY = "verify_busy"
CONTENT_TYPE = b"application/json"

# Section 7: page sizes and defaults (decision 28).
EVENTS_LIMIT = 100
LIST_LIMIT = 200
LIST_DEFAULT = 50
NO_ASSIGNEE = "-"

_POSITION_RE = re.compile(r"[1-9][0-9]{0,18}")
_LIMIT_RE = re.compile(r"[1-9][0-9]{0,2}")
_ENCODED_SLASH_RE = re.compile(r"%2[Ff]")


# --- queries (section 7.1) -----------------------------------------------------------------------


def query_pairs(raw: str, names: frozenset[str]) -> dict[str, str] | None:
    """The pairs of a raw query as signed: `name=value` joined by `&`, each name of `names` at most
    once, no empty value, no parameter without `=`, no empty pair and no `+`. A `%` is left in the
    value, where only the form of `repository` accepts it, as `%2F` or `%2f`. None when the query
    breaks a rule; an empty query has no pairs."""
    if raw == "":
        return {}
    values: dict[str, str] = {}
    for part in raw.split("&"):
        name, equals, value = part.partition("=")
        if equals != "=" or name not in names or name in values or value == "" or "+" in value:
            return None
        values[name] = value
    return values


def no_parameters(raw: str) -> tuple[()] | None:
    """An endpoint without parameters takes only the empty query."""
    return () if raw == "" else None


def position_value(text: str) -> int | None:
    """A position of section 2."""
    if _POSITION_RE.fullmatch(text) is None or int(text) > MAX_POSITION:
        return None
    return int(text)


def limit_value(text: str, top: int) -> int | None:
    """A `limit` from 1 to `top`, decimal without leading zero."""
    if _LIMIT_RE.fullmatch(text) is None or int(text) > top:
        return None
    return int(text)


def repository_value(text: str) -> str | None:
    """A repository; its `/` may be written as `%2F` or `%2f`, no other `%` is allowed."""
    repository = _ENCODED_SLASH_RE.sub("/", text)
    return repository if valid_subject(repository) else None


class _Invalid(Exception):
    """A query value outside its form."""


def _parsed(values: dict[str, str], name: str, parse: Callable[[str], Any]) -> Any:
    """The parsed value of an optional parameter, None when it is not given."""
    if name not in values:
        return None
    parsed = parse(values[name])
    if parsed is None:
        raise _Invalid()
    return parsed


def _limit(values: dict[str, str], top: int, default: int) -> int:
    given = _parsed(values, "limit", lambda text: limit_value(text, top))
    return default if given is None else given


def _choice(choices: tuple[str, ...]) -> Callable[[str], str | None]:
    return lambda text: text if text in choices else None


def _assignee(text: str) -> str | None:
    return text if text == NO_ASSIGNEE or valid_handle(text) else None


def _uuid(text: str) -> str | None:
    return text if valid_uuid(text) else None


def _key_id(text: str) -> str | None:
    return text if valid_key_id(text) else None


def _after(text: str) -> int | None:
    return 0 if text == "0" else position_value(text)


@dataclass(frozen=True)
class EventsQuery:
    after: int
    limit: int
    object_id: str | None


@dataclass(frozen=True)
class ObjectQuery:
    object_id: str


@dataclass(frozen=True)
class TasksQuery:
    kind: str | None
    status: str | None
    assignee: str | None
    repository: str | None
    before: int | None
    limit: int


@dataclass(frozen=True)
class CommentsQuery:
    task: str
    before: int | None
    limit: int


@dataclass(frozen=True)
class CheckpointsQuery:
    before: tuple[int, str] | None
    limit: int


def _query(raw: str, names: frozenset[str], build: Callable[[dict[str, str]], Any]) -> Any:
    values = query_pairs(raw, names)
    if values is None:
        return None
    try:
        return build(values)
    except _Invalid:
        return None


def parse_events_query(raw: str) -> EventsQuery | None:
    """Section 7.4: `after` (`0` or a position, default 0), `limit` (1 to 100, default 100) and
    `object` (a UUID)."""

    def build(values: dict[str, str]) -> EventsQuery:
        after = _parsed(values, "after", _after)
        return EventsQuery(
            after=0 if after is None else after,
            limit=_limit(values, EVENTS_LIMIT, EVENTS_LIMIT),
            object_id=_parsed(values, "object", _uuid),
        )

    return _query(raw, frozenset({"after", "limit", "object"}), build)


def parse_object_query(raw: str) -> ObjectQuery | None:
    """Section 7.5: `id`, a UUID, required."""

    def build(values: dict[str, str]) -> ObjectQuery:
        object_id = _parsed(values, "id", _uuid)
        if object_id is None:
            raise _Invalid()
        return ObjectQuery(object_id)

    return _query(raw, frozenset({"id"}), build)


def parse_tasks_query(raw: str) -> TasksQuery | None:
    """Section 7.6: the filters `kind`, `status`, `assignee` and `repository`, `before` and
    `limit` (1 to 200, default 50)."""

    def build(values: dict[str, str]) -> TasksQuery:
        return TasksQuery(
            kind=_parsed(values, "kind", _choice(KINDS)),
            status=_parsed(values, "status", _choice(STATUSES)),
            assignee=_parsed(values, "assignee", _assignee),
            repository=_parsed(values, "repository", repository_value),
            before=_parsed(values, "before", position_value),
            limit=_limit(values, LIST_LIMIT, LIST_DEFAULT),
        )

    return _query(raw, frozenset({"kind", "status", "assignee", "repository", "before", "limit"}), build)


def parse_comments_query(raw: str) -> CommentsQuery | None:
    """Section 7.7: `task` (a UUID, required), `before` and `limit` (1 to 200, default 50)."""

    def build(values: dict[str, str]) -> CommentsQuery:
        task = _parsed(values, "task", _uuid)
        if task is None:
            raise _Invalid()
        return CommentsQuery(
            task=task, before=_parsed(values, "before", position_value), limit=_limit(values, LIST_LIMIT, LIST_DEFAULT)
        )

    return _query(raw, frozenset({"task", "before", "limit"}), build)


def parse_checkpoints_query(raw: str) -> CheckpointsQuery | None:
    """Section 7.10: `before` and `before_key` together or not at all, and `limit` (1 to 200,
    default 50)."""

    def build(values: dict[str, str]) -> CheckpointsQuery:
        before = _parsed(values, "before", position_value)
        before_key = _parsed(values, "before_key", _key_id)
        if (before is None) != (before_key is None):
            raise _Invalid()
        return CheckpointsQuery(
            before=None if before is None else (before, before_key),
            limit=_limit(values, LIST_LIMIT, LIST_DEFAULT),
        )

    return _query(raw, frozenset({"before", "before_key", "limit"}), build)


# --- the body of an append (section 6.7, check 2) ------------------------------------------------


def _unique_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    members: dict[str, Any] = {}
    for name, value in pairs:
        if name in members:
            raise ValueError("a member twice")
        members[name] = value
    return members


def parse_append_body(request: KeyRequest) -> tuple[bytes, bytes] | None:
    """Check 2 of section 6.7: exactly one `Content-Type` of exactly `application/json`, and a body
    that is one JSON object with exactly the string members `event` (canonical base64url of 1 to
    65536 bytes) and `signature` (canonical base64url of 64 bytes), no member twice. Returns the
    event text and the signature; None for `body_invalid`."""
    if request.header_values("content-type") != [CONTENT_TYPE]:
        return None
    try:
        value = json.loads(request.body.decode("utf-8"), object_pairs_hook=_unique_members)
    except (ValueError, RecursionError):
        # UnicodeDecodeError and json.JSONDecodeError are both ValueError; a body nested deeper
        # than the parser's recursion limit raises RecursionError and is no JSON object either.
        return None
    if not isinstance(value, dict) or value.keys() != {"event", "signature"}:
        return None
    text = b64url_decode(value["event"])
    signature = b64url_decode(value["signature"])
    if text is None or not 1 <= len(text) <= MAX_EVENT_BYTES:
        return None
    if signature is None or len(signature) != SIGNATURE_BYTES:
        return None
    return text, signature


# --- the answers ----------------------------------------------------------------------------------


def _error(error: str) -> dict[str, str]:
    return {"error": error}


def _record_failure_answer(outcome: AlreadyUnverified | Unverified | DatabaseFailed) -> KeyAnswer:
    """The `503` of section 7.1 and the `500` of a database failure, with their results (section
    13): `record_unverified` when the process was already in that state, `verify_failed <position>
    <failure>` when the fold of this request failed, and `database_error <step> <SQLSTATE or ->`."""
    if isinstance(outcome, AlreadyUnverified):
        return KeyAnswer(503, _error(ERROR_RECORD_UNVERIFIED), None)
    if isinstance(outcome, Unverified):
        failure = outcome.failure
        return KeyAnswer(503, _error(ERROR_RECORD_UNVERIFIED), f"verify_failed {failure.position} {failure.failure}")
    return KeyAnswer(500, _error(ERROR_INTERNAL), f"database_error {outcome.step} {outcome.sqlstate}")


def _not_found() -> KeyAnswer:
    return KeyAnswer(404, _error(ERROR_OBJECT_NOT_FOUND), None)


def _time(seconds: int) -> str:
    text = time_text(seconds)
    if text is None:
        raise ValueError("a stored time is no time of section 2")
    return text


def _text(value: bytes | None) -> str | None:
    return None if value is None else value.decode("utf-8")


def stamp_body(stamp: Stamp, keys: KeyRing) -> dict[str, Any]:
    """`created` and `changed` of section 7.5; `handle` from the keys file of this process."""
    return {
        "position": stamp.position,
        "received_at": _time(stamp.received_at),
        "key": stamp.key,
        "handle": keys.entries[stamp.key].handle,
    }


def object_body(item: RecordObject, keys: KeyRing) -> dict[str, Any]:
    """An object in the form of section 7.5, its members in the order given there."""
    if isinstance(item, Comment):
        return {
            "type": "comment",
            "id": item.object_id,
            "task": item.task,
            "body": item.body.decode("utf-8"),
            "created": stamp_body(item.created, keys),
        }
    value = item.value
    urgency = value("urgency")
    return {
        "type": "task",
        "id": item.object_id,
        "team": item.team.decode("utf-8"),
        "kind": item.kind,
        "title": _text(value("title")),
        "description": _text(value("description")),
        "assignee": item.assignee,
        "status": item.status,
        "completion": item.completion,
        "links": list(item.links),
        "completed_links": list(item.completed_link_references),
        "repository": item.repository,
        "short_name": item.short_name,
        "severity": _text(value("severity")),
        "urgency": None if urgency is None else parse_urgency(urgency),
        "source": _text(value("source")),
        "fix": _text(value("fix")),
        "verification": _text(value("verification")),
        "comments": item.comments,
        "created": stamp_body(item.created, keys),
        "changed": stamp_body(item.changed, keys),
    }


def _head_position(state: RecordState) -> int:
    head = state.head
    return 0 if head is None else head.position


def events_answer(
    state: RecordState, connection: psycopg.Connection[Any], entry: KeyEntry, query: EventsQuery, hub_key: str
) -> KeyAnswer:
    """Section 7.4. Without `object`, every event after `after` up to the head of the state, each in
    full when the key sees its object and withheld otherwise; with `object`, the events of that
    object the key sees, or `object_not_found`."""
    head = _head_position(state)
    if query.object_id is None:
        stored = read_events(connection, query.after, head, query.limit + 1)
        visible = [sees_object(entry, state, parse_event(item.text).object_id) for item in stored]
    else:
        if not sees_object(entry, state, query.object_id):
            return _not_found()
        positions = [position for position in state.object_events[query.object_id] if position > query.after]
        stored = read_events_at(connection, positions[: query.limit + 1])
        visible = [True] * len(stored)
    page = stored[: query.limit]
    entries = [
        event_entry(
            position=item.position,
            received_at=item.received_at,
            event_hash=item.event_hash,
            record_hash=item.record_hash,
            text=item.text,
            signature=item.signature,
            visible=shown,
        )
        for item, shown in zip(page, visible)
    ]
    next_after = page[-1].position if len(stored) > query.limit else None
    return KeyAnswer(200, {"events": entries, "next_after": next_after, "head": head, "hub_key": hub_key}, None)


def object_answer(state: RecordState, keys: KeyRing, entry: KeyEntry, query: ObjectQuery, hub_key: str) -> KeyAnswer:
    """Section 7.5."""
    item = readable_object(entry, state, query.object_id)
    if item is None:
        return _not_found()
    return KeyAnswer(200, {"object": object_body(item, keys), "hub_key": hub_key}, None)


def _task_matches(task: Task, query: TasksQuery) -> bool:
    if query.kind is not None and task.kind != query.kind:
        return False
    if query.status is not None and task.status != query.status:
        return False
    if query.assignee is not None and task.assignee != (None if query.assignee == NO_ASSIGNEE else query.assignee):
        return False
    if query.repository is not None and query.repository not in task_repositories(task):
        return False
    return query.before is None or task.changed.position < query.before


def tasks_answer(state: RecordState, keys: KeyRing, entry: KeyEntry, query: TasksQuery, hub_key: str) -> KeyAnswer:
    """Section 7.6: the tasks the key sees that match every given filter, newest first by the
    position of `changed`."""
    tasks = sorted(
        (
            item
            for item in state.objects.values()
            if isinstance(item, Task) and sees_task(entry, item) and _task_matches(item, query)
        ),
        key=lambda task: task.changed.position,
        reverse=True,
    )
    page = tasks[: query.limit]
    next_before = page[-1].changed.position if len(tasks) > query.limit else None
    body = {"tasks": [object_body(task, keys) for task in page], "next_before": next_before, "hub_key": hub_key}
    return KeyAnswer(200, body, None)


def comments_answer(
    state: RecordState, keys: KeyRing, entry: KeyEntry, query: CommentsQuery, hub_key: str
) -> KeyAnswer:
    """Section 7.7: the comments of a task the key sees, newest first by position."""
    task = state.task(query.task)
    if task is None or not sees_task(entry, task):
        return _not_found()
    comments = sorted(
        (
            item
            for item in state.objects.values()
            if isinstance(item, Comment)
            and item.task == query.task
            and (query.before is None or item.created.position < query.before)
        ),
        key=lambda comment: comment.created.position,
        reverse=True,
    )
    page = comments[: query.limit]
    next_before = page[-1].created.position if len(comments) > query.limit else None
    body = {"comments": [object_body(item, keys) for item in page], "next_before": next_before, "hub_key": hub_key}
    return KeyAnswer(200, body, None)


def head_answer(state: RecordState, key_id: str, hub_key: str) -> KeyAnswer:
    """Section 7.8: the head of the record and of the caller's own chain. `key_id` is the request's
    `Agent-Key`: for a session key its own id, whose chain is its own (section 10), not the id of
    its passkey entry."""
    head = state.head
    own = state.chains.get(key_id)
    body = {
        "position": _head_position(state),
        "record_hash": None if head is None else head.record_hash,
        "key": key_id,
        "key_position": None if own is None else own.position,
        "key_event_hash": None if own is None else own.chain.event_hash,
        "key_signed_at": None if own is None else _time(own.chain.signed_at),
        "hub_key": hub_key,
    }
    return KeyAnswer(200, body, None)


def verify_answer(outcome: Verified, hub_key: str) -> KeyAnswer:
    """Section 7.9: `200` in both cases; a failure is named only when it is record-wide, and the
    request line names the true value."""
    result = outcome.result
    failure = result.failure
    if failure is None:
        head = result.state.head
        body = {
            "verified": True,
            "position": 0 if head is None else head.position,
            "record_hash": None if head is None else head.record_hash,
            "failure": None,
            "hub_key": hub_key,
        }
        return KeyAnswer(200, body, None)
    body = {
        "verified": False,
        "position": failure.position,
        "record_hash": None,
        "failure": answered_failure(failure.failure),
        "hub_key": hub_key,
    }
    return KeyAnswer(200, body, f"verify_failed {failure.position} {failure.failure}")


def checkpoint_entry(checkpoint: Checkpoint) -> dict[str, Any]:
    return {
        "position": checkpoint.position,
        "record_hash": checkpoint.record_hash,
        "published_at": _time(checkpoint.published_at),
        "signed_by": checkpoint.hub_key_id,
        "signature": b64url_encode(checkpoint.signature),
    }


def checkpoints_answer(connection: psycopg.Connection[Any], query: CheckpointsQuery, hub_key: str) -> KeyAnswer:
    """Section 7.10: the checkpoints of every hub key id, newest first."""
    stored = read_checkpoints(connection, query.before, query.limit + 1)
    page = stored[: query.limit]
    further = len(stored) > query.limit
    body = {
        "checkpoints": [checkpoint_entry(item) for item in page],
        "next_before": page[-1].position if further else None,
        "next_before_key": page[-1].hub_key_id if further else None,
        "hub_key": hub_key,
    }
    return KeyAnswer(200, body, None)


def appended_answer(outcome: Appended, hub_key: str) -> KeyAnswer:
    """Section 7.2: the hub's signed acknowledgement, `201`."""
    body = {
        "position": outcome.position,
        "event_hash": outcome.event_hash,
        "record_hash": outcome.record_hash,
        "received_at": _time(outcome.received_at),
        "hub_key": hub_key,
    }
    return KeyAnswer(201, body, f"appended {outcome.position}")


# --- the request line (section 13) ----------------------------------------------------------------


def request_line(request: KeyRequest, status: int, result: str) -> None:
    """`record request <key id> <method> <path> <status> <result>`: `-` for a method other than
    `GET`, `POST` and `HEAD`, and for a raw path that is not byte for byte a path of section 7.1."""
    method = request.method if request.method in LOGGED_METHODS else "-"
    path = request.raw_path if request.raw_path in RECORD_PATHS else "-"
    LOG.info("record request %s %s %s %d %s", request.key_id, method, path, status, result)


# --- the release query (section 9) ----------------------------------------------------------------


def release_refusal(outcome: AlreadyUnverified | Unverified | DatabaseFailed) -> tuple[int, dict[str, str]]:
    """The answer of the release query when the record cannot answer it, with the one record line
    of section 13 that a failed fold or a failed database operation adds to its line of
    KEY-PROTOCOL.md section 5."""
    if isinstance(outcome, AlreadyUnverified):
        return 503, _error(ERROR_RECORD_UNVERIFIED)
    if isinstance(outcome, Unverified):
        log_verify_failed(outcome.failure.position, outcome.failure.failure)
        return 503, _error(ERROR_RECORD_UNVERIFIED)
    log_database_error(outcome.step, outcome.sqlstate)
    return 500, _error(ERROR_INTERNAL)


# --- registration ---------------------------------------------------------------------------------


def register_record(routes: KeyRoutes, runtime: RecordRuntime, hub_key: str) -> None:
    """The endpoints of section 7 and their request line."""
    keys = runtime.keys
    routes.add_area(RECORD_PREFIX, request_line)

    async def read(reader: Callable[[RecordState, psycopg.Connection[Any]], KeyAnswer]) -> KeyAnswer:
        outcome = await asyncio.to_thread(runtime.read, reader)
        if isinstance(outcome, Read):
            return outcome.value
        return _record_failure_answer(outcome)

    async def append(request: KeyRequest, endpoint: str) -> KeyAnswer:
        parsed = parse_append_body(request)
        if parsed is None:
            return KeyAnswer(400, _error(ERROR_BODY_INVALID), None)
        text, signature = parsed
        outcome = await asyncio.to_thread(runtime.append, request.entry, request.key_id, endpoint, text, signature)
        if isinstance(outcome, Appended):
            return appended_answer(outcome, hub_key)
        if isinstance(outcome, Refused):
            return KeyAnswer(refusal_status(outcome.error), _error(outcome.error), None)
        return _record_failure_answer(outcome)

    async def post_events(request: KeyRequest) -> KeyAnswer:
        return await append(request, ENDPOINT_EVENTS)

    async def post_watch(request: KeyRequest) -> KeyAnswer:
        return await append(request, ENDPOINT_WATCH)

    async def get_events(request: KeyRequest) -> KeyAnswer:
        return await read(lambda state, connection: events_answer(state, connection, request.entry, request.params, hub_key))

    async def get_object(request: KeyRequest) -> KeyAnswer:
        return await read(lambda state, _connection: object_answer(state, keys, request.entry, request.params, hub_key))

    async def get_tasks(request: KeyRequest) -> KeyAnswer:
        return await read(lambda state, _connection: tasks_answer(state, keys, request.entry, request.params, hub_key))

    async def get_comments(request: KeyRequest) -> KeyAnswer:
        return await read(lambda state, _connection: comments_answer(state, keys, request.entry, request.params, hub_key))

    async def get_head(request: KeyRequest) -> KeyAnswer:
        return await read(lambda state, _connection: head_answer(state, request.key_id, hub_key))

    async def get_checkpoints(request: KeyRequest) -> KeyAnswer:
        return await read(lambda _state, connection: checkpoints_answer(connection, request.params, hub_key))

    async def get_verify(request: KeyRequest) -> KeyAnswer:
        outcome = await asyncio.to_thread(runtime.verify)
        if isinstance(outcome, Verified):
            return verify_answer(outcome, hub_key)
        if isinstance(outcome, VerifyBusy):
            return KeyAnswer(429, _error(ERROR_VERIFY_BUSY), None)
        return _record_failure_answer(outcome)

    routes.add("POST", PATH_EVENTS, post_events, scope=SCOPE_WRITE, subject=None, query=no_parameters)
    routes.add("POST", PATH_WATCH, post_watch, scope=SCOPE_WATCH, subject=None, query=no_parameters)
    routes.add("GET", PATH_EVENTS, get_events, scope=SCOPE_READ, subject=None, query=parse_events_query)
    routes.add("GET", PATH_OBJECT, get_object, scope=SCOPE_READ, subject=None, query=parse_object_query)
    routes.add("GET", PATH_TASKS, get_tasks, scope=SCOPE_READ, subject=None, query=parse_tasks_query)
    routes.add("GET", PATH_COMMENTS, get_comments, scope=SCOPE_READ, subject=None, query=parse_comments_query)
    routes.add("GET", PATH_HEAD, get_head, scope=SCOPE_READ, subject=None, query=no_parameters)
    routes.add("GET", PATH_VERIFY, get_verify, scope=SCOPE_READ, subject=None, query=no_parameters)
    routes.add("GET", PATH_CHECKPOINTS, get_checkpoints, scope=SCOPE_READ, subject=None, query=parse_checkpoints_query)
