"""The endpoints under /api/key/record/ and the release query over the record (RECORD-PROTOCOL.md
sections 7, 8, 9, 12 and 13, with the tests of section 14 that go through the application).

Every request goes through the real application (`create_app`, `start_record`) against the real
PostgreSQL of AGENT_CORE_TEST_POSTGRES. Every case asserts the exact status, the exact body bytes and
the hub signature, with a fixed wall clock and a fixed monotonic clock.
"""

from __future__ import annotations

import gzip
import json
import re
import threading
from pathlib import Path
from typing import Any, Iterator

import pytest
from psycopg import sql

from agent_core.config import Config
from agent_core.record.checkpoint import Checkpoint
from agent_core.record.event import event_hash, record_hash
from tests.conftest import HUB_KEY_ID, RecordDatabase
from tests.record_helpers import (
    Parallel,
    insert_as_superuser,
    insert_checkpoint,
    migrated,
    stored_event,
    tamper,
    wait_for_append_lock,
)
from tests.record_hub_helpers import (
    ALICE,
    BOB,
    CAROL,
    DAVE,
    GUARD,
    INTERNAL,
    JSON,
    LINK,
    OTHER_LINK,
    OTHER_TEAM,
    READER,
    RECORD_UNVERIFIED,
    REPO,
    TEAM,
    WATCHER,
    Hub,
    append_body,
    b64,
    error,
    key_entries,
    new_id,
    security_create,
    start_hub,
    task_create,
)
from tests.test_signed_requests import NOW, assert_signed, fmt
from tests.test_signed_requests import log_lines  # noqa: F401 - a fixture, used by name

EVENTS = "/api/key/record/events"
WATCH = "/api/key/record/watch"
OBJECT = "/api/key/record/object"
TASKS = "/api/key/record/tasks"
COMMENTS = "/api/key/record/comments"
HEAD = "/api/key/record/head"
VERIFY = "/api/key/record/verify"
CHECKPOINTS = "/api/key/record/checkpoints"
RELEASE = "/api/key/release"
QUERY_INVALID = b'{"error":"query_invalid"}'
SCOPE_MISSING = b'{"error":"scope_missing"}'
NOT_FOUND = b'{"error":"not_found"}'
OBJECT_NOT_FOUND = b'{"error":"object_not_found"}'
BODY_INVALID = b'{"error":"body_invalid"}'


def body(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("ascii")


@pytest.fixture
def hub(record_db: RecordDatabase, cfg: Config, tmp_path: Path) -> Iterator[Hub]:
    migrated(record_db)
    made = start_hub(record_db, cfg, tmp_path)
    try:
        yield made
    finally:
        made.close()


def request_lines(lines: list[str]) -> list[str]:
    prefix = "uvicorn.error INFO record request "
    return [line[len(prefix):] for line in lines if line.startswith(prefix)]


def hub_lines(lines: list[str]) -> list[str]:
    """Every line of the hub's log, as `LEVEL text`."""
    out = []
    for line in lines:
        name, _, rest = line.partition(" ")
        if name == "uvicorn.error":
            out.append(rest)
    return out


def record_part(lines: list[str]) -> list[str]:
    """Every line of the record part (section 13), as `LEVEL text`."""
    return [line for line in hub_lines(lines) if line.partition(" ")[2].startswith("record ")]


# `<SQLSTATE or ->` of section 13: a failed connection may or may not carry a SQLSTATE.
SQLSTATE = r"([0-9A-Z]{5}|-)"


def assert_lines(lines: list[str], patterns: list[str]) -> None:
    """The lines match the patterns, one by one; a pattern is a regular expression."""
    assert len(lines) == len(patterns), lines
    for line, pattern in zip(lines, patterns):
        assert re.fullmatch(pattern, line), (line, pattern)


def no_login(db: RecordDatabase) -> None:
    """The writer role can no longer log in: every new connection fails (section 15)."""
    with db.admin() as admin:
        admin.execute(sql.SQL("ALTER ROLE {} NOLOGIN").format(sql.Identifier(db.writer_role)))


def task_body(hub: Hub, object_id: str) -> dict[str, Any]:
    answer = hub.signed(ALICE, f"{OBJECT}?id={object_id}", 200)
    return answer.json()["object"]


def break_after_the_state(hub: Hub, db: RecordDatabase) -> None:
    """Insert, as database superuser, a row after the head of the hub's state whose `event_id`
    column differs from its event text (`row_mismatch`), after the hub folded every row."""
    hub.signed(ALICE, HEAD, 200)
    head = hub.runtime.memory.head()
    position = 1 if head is None else head.position + 1
    text, signature = hub.event(BOB, "task", new_id(), task_create("Broken"))
    stored = stored_event(position, NOW, text, signature, None if head is None else head.record_hash)
    insert_as_superuser(db, stored, event_id=new_id())


def make_unverified(hub: Hub, db: RecordDatabase) -> None:
    """Put the hub into the state `record_unverified` through the fold of a request."""
    break_after_the_state(hub, db)
    hub.signed(ALICE, HEAD, 503)
    assert hub.runtime.unverified


# --- POST /api/key/record/events (sections 6.7 and 7.2) --------------------------------------------


def test_an_accepted_event_gets_a_signed_201_with_its_position_hashes_and_received_at(hub: Hub) -> None:
    text, signature = hub.event(ALICE, "task", new_id(), task_create())
    wire, answer = hub.post(ALICE, EVENTS, text, signature)
    assert_signed(wire, answer, 201, NOW)
    expected_event_hash = event_hash(text)
    assert answer.body == body(
        {
            "position": 1,
            "event_hash": expected_event_hash,
            "record_hash": record_hash(1, NOW, expected_event_hash, None),
            "received_at": fmt(NOW),
            "hub_key": HUB_KEY_ID,
        }
    )


def test_a_gzip_encoded_append_is_accepted(hub: Hub) -> None:
    text, signature = hub.event(ALICE, "task", new_id(), task_create())
    raw = append_body(text, signature)
    wire = hub.wire(
        ALICE,
        EVENTS,
        method="POST",
        body=raw,
        wire_body=gzip.compress(raw),
        extra_headers=(*JSON, (b"content-encoding", b"gzip")),
    )
    answer = hub.send(wire)
    assert_signed(wire, answer, 201, NOW)


def _valid_parts(hub: Hub) -> tuple[str, str]:
    text, signature = hub.event(ALICE, "task", new_id(), task_create())
    return b64(text), b64(signature)


BODY_CASES = {
    "not JSON": lambda event, signature: b"{",
    "not UTF-8": lambda event, signature: b'{"event":"\xff"}',
    "a JSON array": lambda event, signature: body([event, signature]),
    "an extra member": lambda event, signature: body({"event": event, "signature": signature, "x": "y"}),
    "a missing member": lambda event, signature: body({"event": event}),
    "a member twice": lambda event, signature: (
        '{"event":"%s","event":"%s","signature":"%s"}' % (event, event, signature)
    ).encode("ascii"),
    "an empty event": lambda event, signature: body({"event": "", "signature": signature}),
    "an event that is no string": lambda event, signature: body({"event": 1, "signature": signature}),
    "a padded event": lambda event, signature: body({"event": event + "=", "signature": signature}),
    "a signature of 63 bytes": lambda event, signature: body({"event": event, "signature": b64(bytes(63))}),
    "a signature that is no string": lambda event, signature: body({"event": event, "signature": None}),
}


@pytest.mark.parametrize("case", sorted(BODY_CASES))
def test_a_body_outside_check_2_gets_body_invalid(hub: Hub, case: str) -> None:
    raw = BODY_CASES[case](*_valid_parts(hub))
    wire = hub.wire(ALICE, EVENTS, method="POST", body=raw, extra_headers=JSON)
    answer = hub.send(wire)
    assert_signed(wire, answer, 400, NOW)
    assert answer.body == BODY_INVALID


CONTENT_TYPES = {
    "no Content-Type": (),
    "text/plain": ((b"content-type", b"text/plain"),),
    "with a charset": ((b"content-type", b"application/json; charset=utf-8"),),
    "in other case": ((b"content-type", b"Application/JSON"),),
    "two headers": ((b"content-type", b"application/json"), (b"content-type", b"application/json")),
}


@pytest.mark.parametrize("case", sorted(CONTENT_TYPES))
def test_a_content_type_other_than_exactly_application_json_gets_body_invalid(hub: Hub, case: str) -> None:
    text, signature = hub.event(ALICE, "task", new_id(), task_create())
    wire = hub.wire(ALICE, EVENTS, method="POST", body=append_body(text, signature), extra_headers=CONTENT_TYPES[case])
    answer = hub.send(wire)
    assert_signed(wire, answer, 400, NOW)
    assert answer.body == BODY_INVALID


def _text_of_length(hub: Hub, length: int) -> tuple[bytes, bytes]:
    """A correctly signed and chained task create of exactly `length` bytes whose title is far above
    the 200 bytes of its form."""

    def make(size: int, description: str) -> tuple[bytes, bytes]:
        return hub.event(ALICE, "task", new_id(), [*task_create("t" * size), ("description", None, description)])

    base = len(make(1, "a")[0])
    start = (length - base) * 3 // 4
    for size in range(start - 4, start + 6):
        for description in ("a", "ab", "abc"):
            text, signature = make(size, description)
            if len(text) == length:
                return text, signature
    raise AssertionError("no event text of that length")


def test_an_event_text_of_65536_bytes_passes_checks_2_and_3_and_gets_value_invalid(hub: Hub) -> None:
    wire, answer = hub.post(ALICE, EVENTS, *_text_of_length(hub, 65536))
    assert_signed(wire, answer, 400, NOW)
    assert answer.body == error("value_invalid")


def test_an_event_of_65537_bytes_gets_body_invalid(hub: Hub) -> None:
    wire, answer = hub.post(ALICE, EVENTS, b"a" * 65537, bytes(64))
    assert_signed(wire, answer, 400, NOW)
    assert answer.body == BODY_INVALID


def test_a_query_on_an_append_endpoint_gets_query_invalid(hub: Hub) -> None:
    text, signature = hub.event(ALICE, "task", new_id(), task_create())
    for path in (EVENTS, WATCH):
        key = ALICE if path == EVENTS else WATCHER
        wire, answer = hub.post(key, f"{path}?x=1", text, signature)
        assert_signed(wire, answer, 400, NOW)
        assert answer.body == QUERY_INVALID


@pytest.mark.parametrize(("key_id", "path"), [(WATCHER, EVENTS), (CAROL, EVENTS), (ALICE, WATCH), (READER, WATCH)])
def test_a_key_without_the_scope_of_an_append_endpoint_gets_scope_missing(hub: Hub, key_id: str, path: str) -> None:
    text, signature = hub.event(key_id, "task", new_id(), task_create())
    wire, answer = hub.post(key_id, path, text, signature)
    assert_signed(wire, answer, 403, NOW)
    assert answer.body == SCOPE_MISSING


def test_the_refusals_of_section_6_7_keep_their_status_and_value(hub: Hub) -> None:
    work = new_id()
    hub.write(ALICE, "task", work, task_create())
    # Check 3, the form of the event text.
    text, signature = hub.event(ALICE, "task", work, [("title", "Fix login", "Fix it")])
    assert hub.post(ALICE, EVENTS, text.replace(b"agent-event-v1", b"agent-event-v2"), signature)[1].body == error(
        "event_invalid"
    )
    # Check 4, the actor; check 5, the certificate; check 6, the signature.
    text, signature = hub.event(ALICE, "task", work, [("title", "Fix login", "Fix it")])
    wire, answer = hub.post(BOB, EVENTS, text, signature)
    assert_signed(wire, answer, 403, NOW)
    assert answer.body == error("actor_mismatch")
    text, signature = hub.event(ALICE, "task", work, [("title", "Fix login", "Fix it")], certificate=b"cert")
    assert hub.post(ALICE, EVENTS, text, signature)[1].body == error("certificate_not_supported")
    text, _ = hub.event(ALICE, "task", work, [("title", "Fix login", "Fix it")])
    assert hub.post(ALICE, EVENTS, text, bytes(64))[1].body == error("event_signature_invalid")
    # Check 9, the chain, and check 17 by status and value.
    assert hub.refused(ALICE, "task", work, [("title", "Fix", "Other")]) == (409, error("state_mismatch"))
    text, signature = hub.event(ALICE, "task", work, [("title", "Fix login", "Fix it")], prev_hash="1" * 64)
    assert hub.post(ALICE, EVENTS, text, signature)[1].body == error("chain_broken")
    # Check 12 (d): a comment on a task of another team.
    other = new_id()
    hub.write(DAVE, "task", other, task_create("Other", OTHER_TEAM))
    assert hub.refused(ALICE, "comment", new_id(), [("body", None, "x"), ("task", None, other)]) == (
        404,
        OBJECT_NOT_FOUND,
    )
    # Check 13: a security create by a key without `record:security`.
    assert hub.refused(BOB, "task", new_id(), security_create("login-bypass")) == (403, SCOPE_MISSING)


def test_the_same_event_sent_twice_gets_event_duplicate(hub: Hub) -> None:
    text, signature = hub.event(ALICE, "task", new_id(), task_create())
    assert hub.post(ALICE, EVENTS, text, signature)[1].status == 201
    wire, answer = hub.post(ALICE, EVENTS, text, signature)
    assert_signed(wire, answer, 409, NOW)
    assert answer.body == error("event_duplicate")


def test_after_a_refusal_the_next_event_chained_to_the_last_accepted_one_is_accepted(hub: Hub) -> None:
    work = new_id()
    hub.write(ALICE, "task", work, task_create())
    assert hub.refused(ALICE, "task", work, [("status", "open", "done")])[0] == 409
    hub.write(ALICE, "task", work, [("title", "Fix login", "Fix it")])


def test_an_already_unverified_process_answers_503_after_check_2_and_reads_nothing(
    hub: Hub, record_db: RecordDatabase
) -> None:
    make_unverified(hub, record_db)
    taken = hub.runtime.pool.taken
    # An invalid body still gets `body_invalid`.
    wire = hub.wire(ALICE, EVENTS, method="POST", body=b"{", extra_headers=JSON)
    answer = hub.send(wire)
    assert_signed(wire, answer, 400, NOW)
    assert answer.body == BODY_INVALID
    # A valid body with an invalid event text gets the 503, before check 3.
    text, signature = hub.event(ALICE, "task", new_id(), task_create())
    for event_text in (b"not an event", text):
        wire, answer = hub.post(ALICE, EVENTS, event_text, signature)
        assert_signed(wire, answer, 503, NOW)
        assert answer.body == RECORD_UNVERIFIED
    assert hub.runtime.pool.taken == taken


def test_an_append_refused_at_checks_1_to_6_runs_no_fold_and_a_valid_one_gets_503_after_check_6(
    hub: Hub, record_db: RecordDatabase, log_lines: list[str]  # noqa: F811
) -> None:
    break_after_the_state(hub, record_db)
    text, signature = hub.event(ALICE, "task", new_id(), task_create())
    wire, answer = hub.post(ALICE, EVENTS, b"not an event", signature)
    assert_signed(wire, answer, 400, NOW)
    assert answer.body == error("event_invalid")
    assert not hub.runtime.unverified
    log_lines.clear()
    wire, answer = hub.post(ALICE, EVENTS, text, signature)
    assert_signed(wire, answer, 503, NOW)
    assert answer.body == RECORD_UNVERIFIED
    assert hub.runtime.unverified
    assert request_lines(log_lines) == [f"{ALICE} POST {EVENTS} 503 verify_failed 1 row_mismatch"]


def test_a_database_failure_in_an_append_answers_500_and_logs_only_the_request_line(
    hub: Hub, record_db: RecordDatabase, log_lines: list[str]  # noqa: F811
) -> None:
    def end_the_connection() -> None:
        with record_db.admin() as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = %s", (record_db.writer_role,)
            )

    hub.points.append_before_insert = end_the_connection
    text, signature = hub.event(ALICE, "task", new_id(), task_create())
    wire, answer = hub.post(ALICE, EVENTS, text, signature)
    assert_signed(wire, answer, 500, NOW)
    assert answer.body == INTERNAL
    assert_lines(record_part(log_lines), [f"INFO record request {ALICE} POST {EVENTS} 500 database_error append {SQLSTATE}"])
    assert not hub.runtime.unverified
    # The pool dropped that connection; the next append opens a new one and is accepted.
    hub.points.append_before_insert = lambda: None
    wire, answer = hub.post(ALICE, EVENTS, text, signature)
    assert_signed(wire, answer, 201, NOW)


def test_a_failed_connection_of_a_request_answers_500_with_database_error_connect(
    hub: Hub, record_db: RecordDatabase, log_lines: list[str]  # noqa: F811
) -> None:
    no_login(record_db)
    wire, answer = hub.get(ALICE, HEAD)
    assert_signed(wire, answer, 500, NOW)
    assert answer.body == INTERNAL
    assert_lines(record_part(log_lines), [f"INFO record request {ALICE} GET {HEAD} 500 database_error connect {SQLSTATE}"])


# --- POST /api/key/record/watch (section 8) ---------------------------------------------------------


def test_a_watcher_sets_done_by_row_8_and_merged_by_row_9(hub: Hub) -> None:
    work = new_id()
    hub.write(ALICE, "task", work, [*task_create(), ("links", None, LINK)])
    answer = hub.write(WATCHER, "task", work, [("status", "open", "done")], WATCH)
    assert answer["position"] == 2
    issue = new_id()
    hub.write(ALICE, "task", issue, [*security_create("login-bypass"), ("links", None, LINK)])
    hub.write(WATCHER, "task", issue, [("fix", None, LINK), ("status", "open", "merged")], WATCH)
    assert task_body(hub, issue)["status"] == "merged"
    assert task_body(hub, issue)["fix"] == LINK


def test_the_refusals_of_the_watcher(hub: Hub) -> None:
    work = new_id()
    hub.write(ALICE, "task", work, [*task_create(), ("links", None, LINK)])
    # Check 12 (e): any field other than status and fix.
    assert hub.refused(WATCHER, "task", work, [("title", "Fix login", "x")], WATCH) == (403, error("change_not_allowed"))
    assert hub.refused(WATCHER, "task", work, [("kind", "work", "security")], WATCH) == (
        403,
        error("change_not_allowed"),
    )
    # Check 12 (a): a task that does not exist.
    assert hub.refused(WATCHER, "task", new_id(), [("status", "open", "done")], WATCH) == (404, OBJECT_NOT_FOUND)
    # Actor rule 6: a link outside its subjects.
    both = new_id()
    hub.write(ALICE, "task", both, [*task_create(), ("links", None, f"{LINK} {OTHER_LINK}")])
    assert hub.refused(WATCHER, "task", both, [("status", "open", "done")], WATCH) == (403, error("subject_not_allowed"))
    # `fix` on a task of kind `work`.
    assert hub.refused(WATCHER, "task", work, [("fix", None, LINK), ("status", "open", "done")], WATCH) == (
        400,
        error("field_not_allowed"),
    )


# --- GET /api/key/record/events (section 7.4) ------------------------------------------------------


def test_events_of_another_team_are_withheld_and_the_chain_checks_over_the_page(hub: Hub) -> None:
    first = hub.write(ALICE, "task", new_id(), task_create())
    other = hub.write(DAVE, "task", new_id(), task_create("Other", OTHER_TEAM))
    answer = hub.signed(ALICE, f"{EVENTS}?after=0", 200)
    events = answer.json()["events"]
    assert [entry["position"] for entry in events] == [1, 2]
    assert events[0]["event"] is not None and events[0]["signature"] is not None
    assert events[1] == {
        "position": 2,
        "received_at": fmt(NOW),
        "event_hash": other["event_hash"],
        "record_hash": other["record_hash"],
        "event": None,
        "signature": None,
    }
    assert answer.json()["next_after"] is None and answer.json()["head"] == 2
    previous = None
    for position, entry in enumerate(events, start=1):
        assert record_hash(position, NOW, entry["event_hash"], previous) == entry["record_hash"]
        previous = entry["record_hash"]
    assert events[0]["record_hash"] == first["record_hash"]


def test_events_of_an_object_are_those_of_the_object_and_not_of_its_comments(hub: Hub) -> None:
    work = new_id()
    hub.write(ALICE, "task", work, task_create())
    hub.write(ALICE, "comment", new_id(), [("body", None, "x"), ("task", None, work)])
    hub.write(BOB, "task", work, [("title", "Fix login", "Fix it")])
    answer = hub.signed(ALICE, f"{EVENTS}?object={work}", 200)
    assert [entry["position"] for entry in answer.json()["events"]] == [1, 3]
    answer = hub.signed(ALICE, f"{EVENTS}?object={work}&after=1&limit=1", 200)
    assert [entry["position"] for entry in answer.json()["events"]] == [3]
    assert answer.json()["next_after"] is None


def test_object_not_found_has_the_same_answer_for_an_unseen_and_a_missing_object(hub: Hub) -> None:
    other = new_id()
    hub.write(DAVE, "task", other, task_create("Other", OTHER_TEAM))
    comment = new_id()
    hub.write(DAVE, "comment", comment, [("body", None, "x"), ("task", None, other)])
    for object_id in (other, comment, new_id()):
        for target in (f"{OBJECT}?id={object_id}", f"{EVENTS}?object={object_id}", f"{COMMENTS}?task={object_id}"):
            assert hub.signed(ALICE, target, 404).body == OBJECT_NOT_FOUND
    # A comment id is no task for the comment list, also for a key that sees it.
    assert hub.signed(DAVE, f"{COMMENTS}?task={comment}", 404).body == OBJECT_NOT_FOUND


def test_paging_of_the_events(hub: Hub) -> None:
    for index in range(3):
        hub.write(ALICE, "task", new_id(), task_create(f"Task {index}"))
    page = hub.signed(ALICE, f"{EVENTS}?limit=2", 200).json()
    assert [entry["position"] for entry in page["events"]] == [1, 2] and page["next_after"] == 2
    page = hub.signed(ALICE, f"{EVENTS}?after=2&limit=1", 200).json()
    assert [entry["position"] for entry in page["events"]] == [3] and page["next_after"] is None
    page = hub.signed(ALICE, f"{EVENTS}?after=3", 200).json()
    assert page["events"] == [] and page["next_after"] is None and page["head"] == 3
    page = hub.signed(ALICE, f"{EVENTS}?limit=100&after=9223372036854775807", 200).json()
    assert page["events"] == []


INVALID_QUERIES = {
    EVENTS: [
        "after=01", "after=-1", "after=9223372036854775808", "limit=0", "limit=101", "limit=1000",
        "object=x", "x=1", "after=1&after=2", "after=", "after", "&after=1", "after=1&", "after=1&&limit=1",
        "after=+1", "after=%31", "object=5D1C2B3A-4E5F-4A6B-8C7D-9E0F1A2B3C4D",
    ],
    OBJECT: ["", "id=x", "id=" + "0" * 36, "x=1"],
    TASKS: [
        "kind=other", "status=open2", "assignee=Alice", "assignee=--", "repository=example-org",
        "repository=example-org%2fx%2fy", "repository=example-org%3Fx", "before=0", "limit=201", "limit=0",
    ],
    COMMENTS: ["", "before=1", "task=x", "task=5d1c2b3a-4e5f-4a6b-8c7d-9e0f1a2b3c4d&limit=201"],
    HEAD: ["x=1", "a"],
    VERIFY: ["x=1"],
    CHECKPOINTS: ["before=1", "before_key=hub-1", "before=1&before_key=Hub", "limit=201", "before=0&before_key=hub-1"],
}


@pytest.mark.parametrize(("path", "query"), [(path, query) for path, queries in INVALID_QUERIES.items() for query in queries])
def test_an_invalid_query_gets_query_invalid(hub: Hub, path: str, query: str) -> None:
    target = f"{path}?{query}" if query else path
    assert hub.signed(ALICE, target, 400).body == QUERY_INVALID


@pytest.mark.parametrize("path", [EVENTS, OBJECT, TASKS, COMMENTS, HEAD, VERIFY, CHECKPOINTS])
def test_a_key_without_record_read_gets_scope_missing_also_with_an_invalid_query(hub: Hub, path: str) -> None:
    assert hub.signed(CAROL, f"{path}?x=1", 403).body == SCOPE_MISSING


def test_a_get_endpoint_answers_as_without_a_body_and_its_content_type(hub: Hub) -> None:
    plain = hub.signed(ALICE, HEAD, 200).body
    with_body = hub.signed(ALICE, HEAD, 200, body=b"{not json", extra_headers=((b"content-type", b"text/plain"),))
    assert with_body.body == plain


# --- GET /api/key/record/object, tasks, comments and head (sections 7.5 to 7.8) --------------------


def test_the_object_answer_of_a_task_and_of_a_comment(hub: Hub) -> None:
    work = new_id()
    created = hub.write(ALICE, "task", work, [*task_create(), ("description", None, "Line one\nline two")])
    comment = new_id()
    hub.write(BOB, "comment", comment, [("body", None, "Seen"), ("task", None, work)])
    changed = hub.write(ALICE, "task", work, [("assignee", None, "alice"), ("status", "open", "in_progress")])
    stamp_created = {"position": created["position"], "received_at": fmt(NOW), "key": ALICE, "handle": "alice"}
    stamp_changed = {"position": changed["position"], "received_at": fmt(NOW), "key": ALICE, "handle": "alice"}
    answer = hub.signed(ALICE, f"{OBJECT}?id={work}", 200)
    assert answer.body == body(
        {
            "object": {
                "type": "task", "id": work, "team": TEAM, "kind": "work", "title": "Fix login",
                "description": "Line one\nline two", "assignee": "alice", "status": "in_progress",
                "completion": "merged", "links": [], "completed_links": [], "repository": None,
                "short_name": None, "severity": None, "urgency": None, "source": None, "fix": None,
                "verification": None, "comments": 1, "created": stamp_created, "changed": stamp_changed,
            },
            "hub_key": HUB_KEY_ID,
        }
    )
    answer = hub.signed(ALICE, f"{OBJECT}?id={comment}", 200)
    assert answer.body == body(
        {
            "object": {
                "type": "comment", "id": comment, "task": work, "body": "Seen",
                "created": {"position": 2, "received_at": fmt(NOW), "key": BOB, "handle": "bob"},
            },
            "hub_key": HUB_KEY_ID,
        }
    )


def test_a_security_issue_is_seen_only_with_record_security(hub: Hub) -> None:
    issue = new_id()
    hub.write(ALICE, "task", issue, security_create("login-bypass"))
    shown = task_body(hub, issue)
    assert (shown["repository"], shown["short_name"], shown["severity"], shown["urgency"]) == (
        REPO, "login-bypass", "high", 8,
    )
    assert hub.signed(BOB, f"{OBJECT}?id={issue}", 404).body == OBJECT_NOT_FOUND
    assert hub.signed(BOB, f"{TASKS}?kind=security", 200).json()["tasks"] == []
    assert hub.signed(BOB, f"{EVENTS}?after=0", 200).json()["events"][0]["event"] is None


def test_a_service_key_sees_a_task_only_through_its_subjects_and_then_all_its_links(hub: Hub) -> None:
    work = new_id()
    hub.write(ALICE, "task", work, [*task_create(), ("links", None, OTHER_LINK)])
    assert hub.signed(READER, f"{OBJECT}?id={work}", 404).body == OBJECT_NOT_FOUND
    hub.write(ALICE, "task", work, [("links", OTHER_LINK, f"{LINK} {OTHER_LINK}")])
    assert hub.signed(READER, f"{OBJECT}?id={work}", 200).json()["object"]["links"] == [LINK, OTHER_LINK]
    events = hub.signed(READER, f"{EVENTS}?after=0", 200).json()["events"]
    assert all(entry["event"] is not None for entry in events)


def test_the_task_list_filters_and_pages_newest_first(hub: Hub) -> None:
    first, second, third = new_id(), new_id(), new_id()
    hub.write(ALICE, "task", first, [*task_create("One"), ("links", None, LINK)])
    hub.write(ALICE, "task", second, [*task_create("Two"), ("assignee", None, "bob")])
    issue = new_id()
    hub.write(ALICE, "task", issue, security_create("login-bypass"))
    hub.write(ALICE, "task", third, [*task_create("Three"), ("assignee", None, "alice"), ("status", None, "in_progress")])

    def ids(query: str, key_id: str = ALICE) -> list[str]:
        target = f"{TASKS}?{query}" if query else TASKS
        return [task["id"] for task in hub.signed(key_id, target, 200).json()["tasks"]]

    assert ids("") == [third, issue, second, first]
    assert ids("kind=work") == [third, second, first]
    assert ids("kind=security") == [issue]
    assert ids("status=in_progress") == [third]
    assert ids("status=open") == [issue, second, first]
    assert ids("assignee=bob") == [second]
    assert ids("assignee=-") == [issue, first]
    assert ids(f"repository={REPO.replace('/', '%2F')}") == [issue, first]
    assert ids("repository=example-org/other-repo") == []
    assert ids("kind=work&status=open&assignee=bob") == [second]
    assert ids("before=3") == [second, first]
    page = hub.signed(ALICE, f"{TASKS}?limit=2", 200).json()
    assert [task["id"] for task in page["tasks"]] == [third, issue] and page["next_before"] == 3
    page = hub.signed(ALICE, f"{TASKS}?limit=4", 200).json()
    assert page["next_before"] is None
    assert ids("", DAVE) == []


def test_the_comment_list_pages_newest_first(hub: Hub) -> None:
    work = new_id()
    hub.write(ALICE, "task", work, task_create())
    comments = [new_id() for _ in range(3)]
    for comment in comments:
        hub.write(ALICE, "comment", comment, [("body", None, "x"), ("task", None, work)])
    page = hub.signed(ALICE, f"{COMMENTS}?task={work}&limit=2", 200).json()
    assert [item["id"] for item in page["comments"]] == [comments[2], comments[1]] and page["next_before"] == 3
    page = hub.signed(ALICE, f"{COMMENTS}?task={work}&before=3", 200).json()
    assert [item["id"] for item in page["comments"]] == [comments[0]] and page["next_before"] is None


def test_the_head_of_the_record_and_of_the_own_chain(hub: Hub) -> None:
    answer = hub.signed(ALICE, HEAD, 200)
    assert answer.body == body(
        {"position": 0, "record_hash": None, "key": ALICE, "key_position": None, "key_event_hash": None,
         "key_signed_at": None, "hub_key": HUB_KEY_ID}
    )
    first = hub.write(ALICE, "task", new_id(), task_create())
    second = hub.write(BOB, "task", new_id(), task_create())
    answer = hub.signed(ALICE, HEAD, 200)
    assert answer.body == body(
        {"position": 2, "record_hash": second["record_hash"], "key": ALICE, "key_position": 1,
         "key_event_hash": first["event_hash"], "key_signed_at": fmt(NOW), "hub_key": HUB_KEY_ID}
    )


def test_two_hub_instances_on_one_database_fold_each_others_events(
    hub: Hub, record_db: RecordDatabase, cfg: Config, tmp_path: Path
) -> None:
    other = start_hub(record_db, cfg, tmp_path, name="other")
    try:
        work = new_id()
        hub.write(ALICE, "task", work, task_create())
        other.prev = dict(hub.prev)
        other.write(ALICE, "task", work, [("title", "Fix login", "Fix it")])
        assert task_body(hub, work)["title"] == "Fix it"
    finally:
        other.close()


def test_a_process_with_an_older_keys_file_fails_its_fold_with_key_unknown_until_restarted(
    hub: Hub, record_db: RecordDatabase, cfg: Config, tmp_path: Path, log_lines: list[str]  # noqa: F811
) -> None:
    older = tmp_path / "older"
    older.mkdir()
    without_alice = [entry for entry in key_entries() if entry["id"] != ALICE]
    other = start_hub(record_db, cfg, older, entries=without_alice, name="older")
    try:
        hub.write(ALICE, "task", new_id(), task_create())
        log_lines.clear()
        assert other.signed(BOB, HEAD, 503).body == RECORD_UNVERIFIED
        assert request_lines(log_lines) == [f"{BOB} GET {HEAD} 503 verify_failed 1 key_unknown"]
        # The process with the newer file keeps answering.
        assert hub.signed(BOB, HEAD, 200).json()["position"] == 1
    finally:
        other.close()
    restarted = start_hub(record_db, cfg, tmp_path, name="restarted")
    try:
        assert restarted.signed(BOB, HEAD, 200).json()["position"] == 1
    finally:
        restarted.close()


def test_the_example_answers_of_the_story_of_section_4_2(
    record_db: RecordDatabase, cfg: Config, tmp_path: Path
) -> None:
    from agent_core.record.storage import read_snapshot
    from tests.record_helpers import (
        HUB_SIGNER,
        STAFF,
        STORY_LINK,
        STORY_TASK,
        load_ring,
        store_story,
        write_story_key_files,
        writer,
    )
    from tests.record_helpers import SEEDS as STORY_SEEDS
    from tests.test_signed_requests import sha256_hex, unb64

    keys_path, teams_path = write_story_key_files(tmp_path)
    store_story(record_db, load_ring(keys_path, teams_path))
    with writer(record_db) as connection:
        snapshot = read_snapshot(connection)
    events = {item.position: item for item in snapshot.events}
    newest = snapshot.checkpoints[-1]
    made = start_hub(
        record_db,
        cfg,
        tmp_path,
        keys_and_teams=(keys_path, teams_path),
        seeds={STAFF: STORY_SEEDS[STAFF]},
        cfg_changes={"hub_key_id": HUB_SIGNER.key_id, "signing_key": "61" * 32},
    )

    def answer(target: str) -> bytes:
        wire, got = made.get(STAFF, target)
        assert got.status == 200
        assert got.header("agent-hub-key") == "hub-1"
        text = "\n".join(
            ["agent-response-v1", STAFF, wire.nonce, wire.time, "200", got.header("agent-hub-time"), sha256_hex(got.body)]
        ).encode("utf-8")
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        Ed25519PublicKey.from_public_bytes(HUB_SIGNER.public_key).verify(unb64(got.header("agent-hub-signature")), text)
        return got.body

    try:
        assert answer(f"{EVENTS}?after=16&limit=2") == body(
            {
                "events": [
                    {
                        "position": 17, "received_at": "2026-11-17T10:05:01Z", "event_hash": events[17].event_hash,
                        "record_hash": events[17].record_hash, "event": b64(events[17].text),
                        "signature": b64(events[17].signature),
                    },
                    {
                        "position": 18, "received_at": "2026-11-17T10:06:00Z", "event_hash": events[18].event_hash,
                        "record_hash": events[18].record_hash, "event": None, "signature": None,
                    },
                ],
                "next_after": 18,
                "head": 21,
                "hub_key": "hub-1",
            }
        )
        stamp = {"key": STAFF, "handle": "example-staff"}
        assert answer(f"{OBJECT}?id={STORY_TASK}") == body(
            {
                "object": {
                    "type": "task", "id": STORY_TASK, "team": "example", "kind": "work", "title": "Fix login",
                    "description": None, "assignee": "example-staff", "status": "in_progress",
                    "completion": "merged", "links": [STORY_LINK], "completed_links": [], "repository": None,
                    "short_name": None, "severity": None, "urgency": None, "source": None, "fix": None,
                    "verification": None, "comments": 2,
                    "created": {"position": 16, "received_at": "2026-11-17T10:00:01Z", **stamp},
                    "changed": {"position": 21, "received_at": "2026-11-17T11:20:00Z", **stamp},
                },
                "hub_key": "hub-1",
            }
        )
        assert answer(HEAD) == body(
            {
                "position": 21, "record_hash": events[21].record_hash, "key": STAFF, "key_position": 21,
                "key_event_hash": events[21].event_hash, "key_signed_at": "2026-11-17T11:20:00Z", "hub_key": "hub-1",
            }
        )
        assert answer(VERIFY) == body(
            {"verified": True, "position": 21, "record_hash": events[21].record_hash, "failure": None, "hub_key": "hub-1"}
        )
        assert answer(f"{CHECKPOINTS}?limit=1") == body(
            {
                "checkpoints": [
                    {
                        "position": 21, "record_hash": events[21].record_hash, "published_at": "2026-11-17T12:00:00Z",
                        "signed_by": "hub-1", "signature": b64(newest.signature),
                    }
                ],
                "next_before": 21,
                "next_before_key": "hub-1",
                "hub_key": "hub-1",
            }
        )
    finally:
        made.close()


# --- GET /api/key/record/verify (section 7.9) ------------------------------------------------------


def test_verification_on_request_of_an_empty_and_of_a_sound_record(hub: Hub) -> None:
    assert hub.signed(ALICE, VERIFY, 200).body == body(
        {"verified": True, "position": 0, "record_hash": None, "failure": None, "hub_key": HUB_KEY_ID}
    )
    written = hub.write(ALICE, "task", new_id(), task_create())
    hub.monotonic.now += 60
    assert hub.signed(ALICE, VERIFY, 200).body == body(
        {"verified": True, "position": 1, "record_hash": written["record_hash"], "failure": None, "hub_key": HUB_KEY_ID}
    )


def test_verification_on_request_runs_at_most_once_within_60_seconds_of_the_monotonic_clock(hub: Hub) -> None:
    hub.signed(ALICE, VERIFY, 200)
    hub.monotonic.now += 59
    assert hub.signed(BOB, VERIFY, 429).body == error("verify_busy")
    # Setting the wall clock back does not extend the 60 seconds.
    hub.clock.now -= 100
    hub.monotonic.now += 1
    hub.signed(BOB, VERIFY, 200)


def test_a_second_verification_request_while_one_runs_gets_verify_busy(hub: Hub) -> None:
    answers: list[Any] = []

    def second_request() -> None:
        answers.append(hub.signed(BOB, VERIFY, 429).body)

    hub.points.snapshot_between_reads = second_request
    hub.signed(ALICE, VERIFY, 200)
    assert answers == [error("verify_busy")]


def test_a_failure_that_concerns_an_event_is_withheld_for_every_key_and_logged_with_its_value(
    hub: Hub, record_db: RecordDatabase, log_lines: list[str]  # noqa: F811
) -> None:
    hub.write(ALICE, "task", new_id(), task_create())
    hub.write(DAVE, "task", new_id(), task_create("Other", OTHER_TEAM))
    tamper(record_db, "record.event", "UPDATE record.event SET signature = %s WHERE position = 2", (bytes(64),))
    log_lines.clear()
    expected = body({"verified": False, "position": 2, "record_hash": None, "failure": "withheld", "hub_key": HUB_KEY_ID})
    assert hub.signed(DAVE, VERIFY, 200).body == expected
    assert request_lines(log_lines) == [f"{DAVE} GET {VERIFY} 200 verify_failed 2 event_signature_invalid"]
    assert hub.runtime.unverified
    for path in (EVENTS, OBJECT + "?id=" + new_id(), TASKS, COMMENTS + "?task=" + new_id(), HEAD, VERIFY, CHECKPOINTS):
        assert hub.signed(ALICE, path, 503).body == RECORD_UNVERIFIED


def test_a_record_wide_failure_is_named_with_its_position(hub: Hub, record_db: RecordDatabase) -> None:
    hub.write(ALICE, "task", new_id(), task_create())
    tamper(record_db, "record.event", "UPDATE record.event SET record_hash = %s WHERE position = 1", ("d" * 64,))
    assert hub.signed(DAVE, VERIFY, 200).body == body(
        {"verified": False, "position": 1, "record_hash": None, "failure": "record_hash_mismatch", "hub_key": HUB_KEY_ID}
    )


def test_a_received_at_in_the_year_10000_fails_with_event_time_invalid_not_record_hash_mismatch(
    hub: Hub, record_db: RecordDatabase, log_lines: list[str]  # noqa: F811
) -> None:
    hub.write(ALICE, "task", new_id(), task_create())
    tamper(record_db, "record.event", "UPDATE record.event SET received_at = '10000-01-01 00:00:00+00' WHERE position = 1")
    log_lines.clear()
    assert hub.signed(ALICE, VERIFY, 200).json()["failure"] == "withheld"
    assert request_lines(log_lines) == [f"{ALICE} GET {VERIFY} 200 verify_failed 1 event_time_invalid"]


def test_a_removal_at_the_end_fails_position_gap_against_the_head_of_the_state(
    hub: Hub, record_db: RecordDatabase
) -> None:
    hub.write(ALICE, "task", new_id(), task_create())
    hub.write(ALICE, "task", new_id(), task_create())
    hub.signed(ALICE, HEAD, 200)
    tamper(record_db, "record.event", "DELETE FROM record.event WHERE position = 2")
    assert hub.signed(ALICE, VERIFY, 200).json() == {
        "verified": False, "position": 2, "record_hash": None, "failure": "position_gap", "hub_key": HUB_KEY_ID,
    }


# --- GET /api/key/record/checkpoints (section 7.10) and the checkpoint attempts (section 12.2) ------


def test_a_checkpoint_at_a_full_hour_is_listed_and_logged(hub: Hub, log_lines: list[str]) -> None:  # noqa: F811
    written = hub.write(ALICE, "task", new_id(), task_create())
    log_lines.clear()
    hub.runtime.checkpoint()
    assert record_part(log_lines) == [f"INFO record checkpoint 1 {written['record_hash']}"]
    page = hub.signed(ALICE, f"{CHECKPOINTS}?limit=1", 200).json()
    (entry,) = page["checkpoints"]
    assert (entry["position"], entry["record_hash"], entry["published_at"], entry["signed_by"]) == (
        1, written["record_hash"], fmt(NOW), HUB_KEY_ID,
    )
    assert page["next_before"] is None and page["next_before_key"] is None
    # The same full hour makes no second attempt, also after the head moved.
    hub.write(ALICE, "task", new_id(), task_create())
    log_lines.clear()
    hub.runtime.checkpoint()
    assert record_part(log_lines) == []
    # The next full hour writes the checkpoint of the new head; the one after it writes nothing.
    hub.clock.now += 3600
    hub.runtime.checkpoint()
    assert [line.split(" ")[:4] for line in record_part(log_lines)] == [["INFO", "record", "checkpoint", "2"]]
    hub.clock.now += 3600
    hub.runtime.checkpoint()
    assert len(record_part(log_lines)) == 1


def test_the_checkpoint_list_pages_by_position_and_hub_key_id(
    hub: Hub, record_db: RecordDatabase
) -> None:
    written = hub.write(ALICE, "task", new_id(), task_create())
    for hub_key_id in ("hub-2", "hub1"):
        insert_checkpoint(record_db, Checkpoint(hub_key_id, 1, written["record_hash"], NOW, bytes(64)))
    seen = []
    target = f"{CHECKPOINTS}?limit=1"
    while True:
        page = hub.signed(ALICE, target, 200).json()
        seen += [(entry["position"], entry["signed_by"]) for entry in page["checkpoints"]]
        if page["next_before"] is None:
            break
        target = f"{CHECKPOINTS}?limit=1&before={page['next_before']}&before_key={page['next_before_key']}"
    assert seen == [(1, "hub1"), (1, "hub-2")]


def test_a_checkpoint_whose_published_at_cannot_be_written_answers_500(
    hub: Hub, record_db: RecordDatabase, log_lines: list[str]  # noqa: F811
) -> None:
    hub.write(ALICE, "task", new_id(), task_create())
    hub.runtime.checkpoint()
    tamper(record_db, "record.checkpoint", "UPDATE record.checkpoint SET published_at = 'infinity'")
    log_lines.clear()
    assert hub.signed(ALICE, CHECKPOINTS, 500).body == INTERNAL
    assert record_part(log_lines) == [f"INFO record request {ALICE} GET {CHECKPOINTS} 500 internal"]


def test_a_changed_newest_checkpoint_makes_the_next_attempt_unverified(
    hub: Hub, record_db: RecordDatabase, log_lines: list[str]  # noqa: F811
) -> None:
    hub.write(ALICE, "task", new_id(), task_create())
    hub.runtime.checkpoint()
    tamper(record_db, "record.checkpoint", "UPDATE record.checkpoint SET signature = %s", (bytes(64),))
    hub.clock.now += 3600
    log_lines.clear()
    hub.runtime.checkpoint()
    assert record_part(log_lines) == ["ERROR record verify failed 1 checkpoint_invalid"]
    assert hub.signed(ALICE, HEAD, 503).body == RECORD_UNVERIFIED
    # In the state `record_unverified` no attempt and no periodic run.
    hub.clock.now += 3600
    hub.monotonic.now += 600
    log_lines.clear()
    hub.runtime.tick()
    assert record_part(log_lines) == []


def test_a_database_failure_in_a_checkpoint_attempt_logs_record_database_error_checkpoint(
    hub: Hub, record_db: RecordDatabase, log_lines: list[str]  # noqa: F811
) -> None:
    hub.write(ALICE, "task", new_id(), task_create())
    hub.runtime.pool.close()
    no_login(record_db)
    log_lines.clear()
    hub.runtime.checkpoint()
    assert_lines(record_part(log_lines), [f"ERROR record database error checkpoint {SQLSTATE}"])
    assert not hub.runtime.unverified


# --- the periodic verification (section 12.1) --------------------------------------------------------


def test_the_periodic_verification_runs_10_minutes_after_the_previous_run_started(
    hub: Hub, record_db: RecordDatabase, cfg: Config, tmp_path: Path, log_lines: list[str]  # noqa: F811
) -> None:
    other = start_hub(record_db, cfg, tmp_path, name="other")
    try:
        hub.write(ALICE, "task", new_id(), task_create())
        tamper(record_db, "record.event", "UPDATE record.event SET signature = %s WHERE position = 1", (bytes(64),))
        log_lines.clear()
        other.monotonic.now += 599
        other.runtime.periodic()
        assert record_part(log_lines) == []
        assert not other.runtime.unverified
        other.monotonic.now += 1
        other.runtime.periodic()
        assert record_part(log_lines) == ["ERROR record verify failed 1 event_signature_invalid"]
        assert other.signed(ALICE, HEAD, 503).body == RECORD_UNVERIFIED
        # The rest of the hub keeps answering.
        assert other.signed(ALICE, "/api/key/whoami", 200).json()["key"] == ALICE
    finally:
        other.close()


def test_a_database_failure_of_a_periodic_run_is_logged_and_does_not_enter_record_unverified(
    hub: Hub, record_db: RecordDatabase, log_lines: list[str]  # noqa: F811
) -> None:
    hub.runtime.pool.close()
    no_login(record_db)
    hub.monotonic.now += 600
    log_lines.clear()
    hub.runtime.periodic()
    assert_lines(record_part(log_lines), [f"ERROR record database error connect {SQLSTATE}"])
    assert not hub.runtime.unverified


def test_a_periodic_run_that_fails_to_read_logs_record_database_error_read(
    hub: Hub, record_db: RecordDatabase, log_lines: list[str]  # noqa: F811
) -> None:
    def end_the_connection() -> None:
        with record_db.admin() as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = %s", (record_db.writer_role,)
            )

    hub.write(ALICE, "task", new_id(), task_create())
    hub.points.snapshot_between_reads = end_the_connection
    hub.monotonic.now += 600
    log_lines.clear()
    hub.runtime.periodic()
    assert_lines(record_part(log_lines), [f"ERROR record database error read {SQLSTATE}"])
    assert not hub.runtime.unverified


def test_the_background_thread_runs_the_due_work(record_db: RecordDatabase, cfg: Config, tmp_path: Path) -> None:
    migrated(record_db)
    made = start_hub(record_db, cfg, tmp_path, poll_seconds=0.01)
    try:
        ran = threading.Event()
        made.points.checkpoint_before_insert = ran.set
        made.write(ALICE, "task", new_id(), task_create())
        made.runtime.start_background()
        assert ran.wait(30)
    finally:
        made.close()


# --- 404 under /api/key/record/ and the request line (sections 7.1 and 13) -------------------------


@pytest.mark.parametrize(
    ("method", "target", "logged_method", "logged_path"),
    [
        ("PUT", EVENTS, "-", EVENTS),
        ("DELETE", HEAD, "-", HEAD),
        ("POST", HEAD, "POST", HEAD),
        ("GET", WATCH, "GET", WATCH),
        ("GET", HEAD + "/", "GET", "-"),
        ("HEAD", HEAD, "HEAD", HEAD),
        ("GET", "/api/key/record/marker-5d1c2b3a", "GET", "-"),
    ],
)
def test_another_method_a_trailing_slash_head_and_an_unknown_path_get_the_signed_404(
    hub: Hub, log_lines: list[str], method: str, target: str, logged_method: str, logged_path: str  # noqa: F811
) -> None:
    wire, answer = hub.get(ALICE, target, method=method)
    assert_signed(wire, answer, 404, NOW)
    assert answer.body == (b"" if method == "HEAD" else NOT_FOUND)
    assert record_part(log_lines) == [f"INFO record request {ALICE} {logged_method} {logged_path} 404 not_found"]
    for line in log_lines:
        assert "marker" not in line


def test_a_percent_encoded_known_path_is_answered_and_logged_with_a_dash(hub: Hub, log_lines: list[str]) -> None:  # noqa: F811
    hub.signed(ALICE, "/api/key/record/%68ead", 200)
    assert request_lines(log_lines) == [f"{ALICE} GET - 200 ok"]


def test_every_request_under_the_record_writes_exactly_one_line_for_every_principal(
    hub: Hub, log_lines: list[str]  # noqa: F811
) -> None:
    work = new_id()
    hub.write(ALICE, "task", work, [*task_create(), ("links", None, LINK)])
    hub.signed(READER, HEAD, 200)
    hub.signed(READER, f"{HEAD}?x=1", 400)
    hub.signed(GUARD, HEAD, 403)
    hub.write(WATCHER, "task", work, [("status", "open", "done")], WATCH)
    # Not past check 8: no line.
    hub.send(hub.wire(ALICE, HEAD, seed=99))
    assert request_lines(log_lines) == [
        f"{ALICE} POST {EVENTS} 201 appended 1",
        f"{READER} GET {HEAD} 200 ok",
        f"{READER} GET {HEAD} 400 query_invalid",
        f"{GUARD} GET {HEAD} 403 scope_missing",
        f"{WATCHER} POST {WATCH} 201 appended 2",
    ]
    assert [line for line in log_lines if " key request " in line] == []


def test_no_log_line_holds_record_content_an_id_or_a_secret(
    hub: Hub, record_db: RecordDatabase, log_lines: list[str]  # noqa: F811
) -> None:
    marker = "markerzq"
    work = new_id()
    issue = new_id()
    comment = new_id()
    marker_repo = f"{marker}-org/{marker}-repo"
    hub.write(ALICE, "task", work, [*task_create(f"{marker} title"), ("description", None, f"{marker} text")])
    hub.write(ALICE, "task", issue, [
        ("kind", None, "security"), ("repository", None, marker_repo), ("severity", None, "low"),
        ("short_name", None, f"{marker}-name"), ("source", None, f"{marker}-source"), ("team", None, TEAM),
        ("title", None, marker), ("urgency", None, "1"), ("links", None, f"{marker_repo}#1"),
    ])
    hub.write(ALICE, "comment", comment, [("body", None, f"{marker} body"), ("task", None, work)])
    for target in (f"{OBJECT}?id={work}", f"{EVENTS}?object={issue}", f"{COMMENTS}?task={work}", TASKS, HEAD, VERIFY,
                   CHECKPOINTS, f"{EVENTS}?after=0"):
        hub.get(ALICE, target)
    text = "\n".join(log_lines)
    for forbidden in (marker, work, issue, comment, *record_db.secret_values()):
        assert forbidden not in text
    for line in request_lines(log_lines):
        assert line.split(" ")[0] == ALICE and "alice " not in line


# --- the order of the 503 (section 7.1) --------------------------------------------------------------


@pytest.mark.parametrize("path", [EVENTS, OBJECT, TASKS, COMMENTS, HEAD, VERIFY, CHECKPOINTS])
def test_in_the_state_record_unverified_scope_and_query_come_before_the_503_and_nothing_is_read(
    hub: Hub, record_db: RecordDatabase, path: str, log_lines: list[str]  # noqa: F811
) -> None:
    make_unverified(hub, record_db)
    taken = hub.runtime.pool.taken
    assert hub.signed(CAROL, path, 403).body == SCOPE_MISSING
    assert hub.signed(ALICE, f"{path}?x=1", 400).body == QUERY_INVALID
    valid = {OBJECT: f"{OBJECT}?id={new_id()}", COMMENTS: f"{COMMENTS}?task={new_id()}"}.get(path, path)
    log_lines.clear()
    assert hub.signed(ALICE, valid, 503).body == RECORD_UNVERIFIED
    assert request_lines(log_lines) == [f"{ALICE} GET {path} 503 record_unverified"]
    assert hub.runtime.pool.taken == taken


def test_in_the_state_record_unverified_the_verify_503_comes_before_the_429(hub: Hub, record_db: RecordDatabase) -> None:
    hub.signed(ALICE, VERIFY, 200)
    make_unverified(hub, record_db)
    assert hub.signed(ALICE, VERIFY, 503).body == RECORD_UNVERIFIED


# --- GET /api/key/release over the record (section 9) ------------------------------------------------


def release(hub: Hub, status: int = 200, pull_request: int = 42, key_id: str = GUARD) -> Any:
    return hub.signed(key_id, f"{RELEASE}?repository={REPO.replace('/', '%2F')}&pull_request={pull_request}", status)


def released(value: bool, failed: str | None, pull_request: int = 42) -> bytes:
    return body(
        {"repository": REPO, "pull_request": pull_request, "released": value, "failed": failed, "hub_key": HUB_KEY_ID}
    )


def test_the_release_rule_over_the_record(hub: Hub) -> None:
    assert release(hub).body == released(False, "task_missing")
    work = new_id()
    hub.write(ALICE, "task", work, [*task_create(), ("links", None, LINK)])
    assert release(hub).body == released(False, "task_not_ready")
    hub.write(ALICE, "task", work, [("assignee", None, "alice"), ("status", "open", "in_progress")])
    assert release(hub).body == released(True, None)
    hub.write(ALICE, "task", work, [("status", "in_progress", "ready_for_review")])
    assert release(hub).body == released(True, None)
    # A second linked task that is not ready.
    second = new_id()
    hub.write(ALICE, "task", second, [*task_create("Second"), ("links", None, LINK)])
    assert release(hub).body == released(False, "task_not_ready")
    # A dropped task does not count.
    hub.write(ALICE, "task", second, [("status", "open", "dropped")])
    assert release(hub).body == released(True, None)
    # A link removed again.
    hub.write(ALICE, "task", work, [("links", LINK, None)])
    assert release(hub).body == released(False, "task_missing")


def test_a_security_issue_and_a_task_of_another_team_count_as_linked_tasks(hub: Hub) -> None:
    issue = new_id()
    hub.write(ALICE, "task", issue, [*security_create("login-bypass"), ("links", None, LINK)])
    assert release(hub).body == released(False, "task_not_ready")
    other = new_id()
    hub.write(DAVE, "task", other, [*task_create("Other", OTHER_TEAM), ("links", None, f"{REPO}#43")])
    assert release(hub, pull_request=43).body == released(False, "task_not_ready", 43)


def test_the_release_query_answers_503_after_the_subject_check(
    hub: Hub, record_db: RecordDatabase, log_lines: list[str]  # noqa: F811
) -> None:
    break_after_the_state(hub, record_db)
    log_lines.clear()
    assert release(hub, 503).body == RECORD_UNVERIFIED
    # The unchanged line of KEY-PROTOCOL.md section 5, and in addition one record line.
    assert hub_lines(log_lines) == [
        "ERROR record verify failed 1 row_mismatch",
        f"INFO key request {GUARD} GET {RELEASE} 503",
    ]
    taken = hub.runtime.pool.taken
    log_lines.clear()
    assert release(hub, 503).body == RECORD_UNVERIFIED
    assert hub.signed(GUARD, f"{RELEASE}?repository=example-org%2Fother&pull_request=1", 403).body == error(
        "subject_not_allowed"
    )
    assert hub.signed(GUARD, f"{RELEASE}?pull_request=0", 400).body == QUERY_INVALID
    assert hub.signed(ALICE, f"{RELEASE}?pull_request=0", 403).body == SCOPE_MISSING
    assert hub.runtime.pool.taken == taken
    assert hub_lines(log_lines) == [
        f"INFO key request {GUARD} GET {RELEASE} 503",
        f"INFO key request {GUARD} GET {RELEASE} 403",
        f"INFO key request {GUARD} GET {RELEASE} 400",
    ]


def test_a_database_failure_of_the_release_query_answers_500_and_adds_one_line(
    hub: Hub, record_db: RecordDatabase, log_lines: list[str]  # noqa: F811
) -> None:
    hub.runtime.pool.close()
    no_login(record_db)
    log_lines.clear()
    assert release(hub, 500).body == INTERNAL
    assert_lines(
        hub_lines(log_lines),
        [f"ERROR record database error connect {SQLSTATE}", f"INFO key request {GUARD} GET {RELEASE} 500"],
    )


# --- the state lock and the connections (section 11.6) ----------------------------------------------


def test_a_get_holding_the_state_lock_an_append_a_second_append_and_a_get_on_an_exhausted_pool(
    hub: Hub, record_db: RecordDatabase
) -> None:
    work = new_id()
    hub.write(ALICE, "task", work, task_create())
    in_fold = threading.Event()
    release_fold = threading.Event()

    def hold() -> None:
        in_fold.set()
        assert release_fold.wait(60)

    hub.points.in_read_fold = hold
    first_get = Parallel(lambda: hub.get(ALICE, HEAD)[1])
    assert in_fold.wait(60)
    hub.points.in_read_fold = lambda: None
    first = hub.event(ALICE, "comment", new_id(), [("body", None, "a"), ("task", None, work)])
    second = hub.event(BOB, "comment", new_id(), [("body", None, "b"), ("task", None, work)])
    appends = Parallel(
        lambda: hub.post(ALICE, EVENTS, *first)[1], lambda: hub.post(BOB, EVENTS, *second)[1]
    )
    wait_for_append_lock(record_db, 1)
    second_get = Parallel(lambda: hub.get(BOB, HEAD)[1])
    release_fold.set()
    (first_answer,) = first_get.results()
    assert first_answer.status == 200
    assert sorted(answer.status for answer in appends.results()) == [201, 201]
    (second_answer,) = second_get.results()
    assert second_answer.status == 200
    assert second_answer.json()["position"] >= first_answer.json()["position"]
    hub.monotonic.now += 60
    assert hub.signed(ALICE, VERIFY, 200).json()["verified"] is True


def test_a_get_whose_fold_runs_between_the_commit_and_step_5_folds_the_event(hub: Hub) -> None:
    work = new_id()
    hub.write(ALICE, "task", work, task_create())
    seen: list[int] = []
    hub.points.append_after_commit = lambda: seen.append(hub.signed(BOB, HEAD, 200).json()["position"])
    hub.write(ALICE, "task", work, [("title", "Fix login", "Fix it")])
    assert seen == [2]
    hub.points.append_after_commit = lambda: None
    hub.write(ALICE, "task", work, [("title", "Fix it", "Fixed")])
    hub.monotonic.now += 60
    assert hub.signed(ALICE, VERIFY, 200).json()["verified"] is True


def test_the_received_at_of_an_append_is_the_one_clock_reading_of_step_2(hub: Hub) -> None:
    work = new_id()
    hub.write(ALICE, "task", work, task_create())

    def set_back() -> None:
        hub.clock.now -= 5

    hub.points.append_before_insert = set_back
    hub.clock.now = NOW + 100
    text, signature = hub.event(ALICE, "task", work, [("title", "Fix login", "Fix it")], signed_at=NOW + 220)
    wire, answer = hub.post(ALICE, EVENTS, text, signature)
    assert answer.status == 201
    assert answer.json()["received_at"] == fmt(NOW + 100)
    hub.points.append_before_insert = lambda: None
    hub.monotonic.now += 60
    assert hub.signed(ALICE, VERIFY, 200).json()["verified"] is True


def test_a_kept_received_at_refuses_a_key_revoked_before_it_with_key_not_valid_at_receipt(
    record_db: RecordDatabase, cfg: Config, tmp_path: Path
) -> None:
    entries = key_entries()
    for entry in entries:
        if entry["id"] == BOB:
            entry["revoked"] = fmt(NOW + 30)
    migrated(record_db)
    made = start_hub(record_db, cfg, tmp_path, entries=entries)
    try:
        made.clock.now = NOW + 60
        made.write(ALICE, "task", new_id(), task_create())
        # The clock is set back: received_at keeps NOW + 60, after bob's revocation at NOW + 30,
        # while bob's key is still valid at the request checks (now NOW).
        made.clock.now = NOW
        assert made.refused(BOB, "task", new_id(), task_create()) == (409, error("key_not_valid_at_receipt"))
        # `signed_at` up to 120 seconds after the kept `received_at` is accepted.
        text, signature = made.event(ALICE, "task", new_id(), task_create(), signed_at=NOW + 180)
        wire, answer = made.post(ALICE, EVENTS, text, signature)
        assert_signed(wire, answer, 201, NOW)
        assert answer.json()["received_at"] == fmt(NOW + 60)
    finally:
        made.close()
