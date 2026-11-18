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
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

import psycopg
import pytest
import yaml
from fastapi.testclient import TestClient
from psycopg import sql

from agent_core.config import Config
from agent_core.record import commands
from agent_core.record.checkpoint import Checkpoint
from agent_core.record.connection import parse_connection_string
from agent_core.record.event import Change, build_event_text, event_hash, parse_event, record_hash
from agent_core.record.runtime import RecordStartFailed
from agent_core.record.storage import WriterPool, verify_stored
from agent_core.record.verify import StoredEvent
from agent_core.signing import HubSigner, b64url_encode
from tests.conftest import HUB_KEY_ID, HUB_SEED_HEX, RecordDatabase, pair_device, recreate_database
from tests.record_helpers import (
    Parallel,
    append_lock_held,
    counts,
    insert_as_superuser,
    insert_checkpoint,
    migrated,
    silent_listener,
    stored_event,
    tamper,
    unreachable_url,
    wait_for_append_lock,
)
from tests.record_helpers import writer as writer_connection
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
    OTHER_REPO,
    OTHER_TEAM,
    READER,
    RECORD_UNVERIFIED,
    REPO,
    SEEDS,
    TEAM,
    WATCHER,
    Hub,
    Points,
    append_body,
    b64,
    encode,
    error,
    key_entries,
    new_id,
    security_create,
    start_hub,
    task_create,
)
from tests.test_signed_requests import NOW, assert_refused, assert_signed, fmt, private, public_text
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
WHOAMI = "/api/key/whoami"
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


def end_writer_connections(db: RecordDatabase) -> None:
    """As database superuser, end every connection of the writer role: the next statement on a
    connection the hub holds fails."""
    with db.admin() as admin:
        admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = %s", (db.writer_role,))


# The SQLSTATE of a statement on a table on which the writer role lacks the right (insufficient_privilege).
INSUFFICIENT_PRIVILEGE = "42501"


def change_right(db: RecordDatabase, statement: str, table: str) -> None:
    """Run `statement` as database superuser; its two placeholders `{}` are the table `schema.name`
    and the writer role."""
    schema, name = table.split(".")
    with db.admin() as admin:
        admin.execute(sql.SQL(statement).format(sql.Identifier(schema, name), sql.Identifier(db.writer_role)))


def revoke_select(db: RecordDatabase, table: str) -> None:
    """The writer role can no longer read `table`: its next statement on it fails with 42501."""
    change_right(db, "REVOKE SELECT ON {} FROM {}", table)


def grant_select(db: RecordDatabase, table: str) -> None:
    """The right of section 11.2 that `revoke_select` took."""
    change_right(db, "GRANT SELECT ON {} TO {}", table)


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


@contextmanager
def started(
    db: RecordDatabase, cfg: Config, directory: Path, names: Sequence[str], **options: Any
) -> Iterator[list[Hub]]:
    """Several hub instances on one database, one per name, closed at the end of the block."""
    made: list[Hub] = []
    try:
        for name in names:
            made.append(start_hub(db, cfg, directory, name=name, **options))
        yield made
    finally:
        for instance in made:
            instance.close()


def refused_restart(
    db: RecordDatabase, cfg: Config, directory: Path, lines: list[str], position: int, failure: str, **options: Any
) -> None:
    """A restart on `db` refuses to start, with exactly the two lines of section 11.4, step `verify`."""
    lines.clear()
    with pytest.raises(RecordStartFailed) as failed:
        start_hub(db, cfg, directory, name="restarted", **options)
    assert failed.value.step == "verify"
    assert record_part(lines) == [f"ERROR record verify failed {position} {failure}", "ERROR record start failed verify"]


def withheld_at(position: int) -> bytes:
    """The answer of section 7.9 to a failure that concerns an event, for every calling key."""
    return body({"verified": False, "position": position, "record_hash": None, "failure": "withheld", "hub_key": HUB_KEY_ID})


def named_at(position: int, failure: str) -> bytes:
    """The answer of section 7.9 to a record-wide failure, named, for every calling key."""
    return body({"verified": False, "position": position, "record_hash": None, "failure": failure, "hub_key": HUB_KEY_ID})


def drop_constraint(db: RecordDatabase, name: str) -> None:
    """As database superuser, drop a constraint of section 11.3 on `record.event`."""
    with db.admin() as admin:
        admin.execute(sql.SQL("ALTER TABLE record.event DROP CONSTRAINT {}").format(sql.Identifier(name)))


def answers_503_everywhere(hub: Hub) -> None:
    """The signed `503` `record_unverified` on every record endpoint and on the release query."""
    for target in (EVENTS, f"{OBJECT}?id={new_id()}", TASKS, f"{COMMENTS}?task={new_id()}", HEAD, VERIFY, CHECKPOINTS):
        assert hub.signed(ALICE, target, 503).body == RECORD_UNVERIFIED
    for key_id, path, changes in (
        (ALICE, EVENTS, task_create()),
        (WATCHER, WATCH, [("status", "open", "done")]),
    ):
        wire, answer = hub.post(key_id, path, *hub.event(key_id, "task", new_id(), changes))
        assert_signed(wire, answer, 503, hub.clock.now)
        assert answer.body == RECORD_UNVERIFIED
    target = f"{RELEASE}?repository={REPO.replace('/', '%2F')}&pull_request=42"
    assert hub.signed(GUARD, target, 503).body == RECORD_UNVERIFIED


def rest_of_the_hub_answers(hub: Hub, device_id: str) -> None:
    """Section 12.1: `GET /api/key/whoami`, pairing, sync and the session store answer as before."""
    assert hub.signed(ALICE, WHOAMI, 200).json()["key"] == ALICE
    client = TestClient(hub.app)
    token = pair_device(client, "code-alice", device_id)
    headers = {"Authorization": f"Bearer {token}"}
    session_id = f"session-{device_id}"
    event = {
        "origin_device_id": device_id,
        "origin_seq": 1,
        "table": "session",
        "op": "insert",
        "row_id": session_id,
        "payload": {"id": session_id, "kind": "human", "status": "active"},
        "occurred_at": "2026-08-13T12:00:00Z",
    }
    pushed = client.post("/sync/push", headers=headers, json={"events": [event]})
    assert pushed.status_code == 200, pushed.text
    assert pushed.json() == {"accepted": 1}
    pulled = client.get("/sync/pull", headers=headers)
    assert pulled.status_code == 200, pulled.text
    state = client.get("/api/state")
    assert state.status_code == 200, state.text
    assert session_id in [row["id"] for row in state.json()["session"]]


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


# Far below the body limit of 1 MiB, but nested deeper than the JSON parser recurses.
NESTED_TOO_DEEP = b"[" * 100000

BODY_CASES = {
    "not JSON": lambda event, signature: b"{",
    "nested too deep": lambda event, signature: NESTED_TOO_DEEP,
    "objects nested too deep": lambda event, signature: b'{"event":' * 100000,
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


# Each append endpoint with a key that holds its scope.
APPENDS = [(ALICE, EVENTS), (WATCHER, WATCH)]


@pytest.mark.parametrize(("key_id", "path"), APPENDS)
@pytest.mark.parametrize("case", sorted(BODY_CASES))
def test_a_body_outside_check_2_gets_body_invalid(hub: Hub, case: str, key_id: str, path: str) -> None:
    raw = BODY_CASES[case](*_valid_parts(hub))
    wire = hub.wire(key_id, path, method="POST", body=raw, extra_headers=JSON)
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


@pytest.mark.parametrize(("key_id", "path"), APPENDS)
@pytest.mark.parametrize("case", sorted(CONTENT_TYPES))
def test_a_content_type_other_than_exactly_application_json_gets_body_invalid(
    hub: Hub, case: str, key_id: str, path: str
) -> None:
    text, signature = hub.event(key_id, "task", new_id(), task_create())
    wire = hub.wire(key_id, path, method="POST", body=append_body(text, signature), extra_headers=CONTENT_TYPES[case])
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
    def refusal(key_id: str, text: bytes, signature: bytes, status: int, value: str) -> None:
        wire, answer = hub.post(key_id, EVENTS, text, signature)
        assert_signed(wire, answer, status, NOW)
        assert answer.body == error(value)

    text, signature = hub.event(ALICE, "task", work, [("title", "Fix login", "Fix it")])
    refusal(ALICE, text.replace(b"agent-event-v1", b"agent-event-v2"), signature, 400, "event_invalid")
    # Check 4, the actor; check 5, the certificate; check 6, the signature.
    text, signature = hub.event(ALICE, "task", work, [("title", "Fix login", "Fix it")])
    refusal(BOB, text, signature, 403, "actor_mismatch")
    text, signature = hub.event(ALICE, "task", work, [("title", "Fix login", "Fix it")], certificate=b"cert")
    refusal(ALICE, text, signature, 400, "certificate_not_supported")
    text, _ = hub.event(ALICE, "task", work, [("title", "Fix login", "Fix it")])
    refusal(ALICE, text, bytes(64), 400, "event_signature_invalid")
    # Check 9, the chain, and check 17 by status and value.
    assert hub.refused(ALICE, "task", work, [("title", "Fix", "Other")]) == (409, error("state_mismatch"))
    text, signature = hub.event(ALICE, "task", work, [("title", "Fix login", "Fix it")], prev_hash="1" * 64)
    refusal(ALICE, text, signature, 409, "chain_broken")
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


# A key without the scope of each append endpoint.
WITHOUT_SCOPE = {EVENTS: WATCHER, WATCH: ALICE}
# A valid change of each append endpoint's writer.
VALID_CHANGES = {EVENTS: task_create(), WATCH: [("status", "open", "done")]}


@pytest.mark.parametrize(("key_id", "path"), APPENDS)
def test_an_already_unverified_process_answers_503_after_check_2_and_reads_nothing(
    hub: Hub, record_db: RecordDatabase, key_id: str, path: str
) -> None:
    make_unverified(hub, record_db)
    taken = hub.runtime.pool.taken
    text, signature = hub.event(key_id, "task", new_id(), VALID_CHANGES[path])
    # A key without the scope still gets `scope_missing`, and a query `query_invalid`.
    other = WITHOUT_SCOPE[path]
    wire, answer = hub.post(other, path, *hub.event(other, "task", new_id(), VALID_CHANGES[path]))
    assert_signed(wire, answer, 403, NOW)
    assert answer.body == SCOPE_MISSING
    wire, answer = hub.post(key_id, f"{path}?x=1", text, signature)
    assert_signed(wire, answer, 400, NOW)
    assert answer.body == QUERY_INVALID
    # An invalid body, also one nested too deep, still gets `body_invalid`.
    for raw in (b"{", NESTED_TOO_DEEP):
        wire = hub.wire(key_id, path, method="POST", body=raw, extra_headers=JSON)
        answer = hub.send(wire)
        assert_signed(wire, answer, 400, NOW)
        assert answer.body == BODY_INVALID
    # A valid body with an invalid event text gets the 503, before check 3.
    for event_text in (b"not an event", text):
        wire, answer = hub.post(key_id, path, event_text, signature)
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
    hub.points.append_before_insert = lambda: end_writer_connections(record_db)
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


# Every reading `GET` endpoint of section 7.1 other than verify, which runs no fold (section 7.9).
READING_GETS = (EVENTS, OBJECT, TASKS, COMMENTS, HEAD, CHECKPOINTS)
REQUIRED_QUERY = {OBJECT: "id", COMMENTS: "task"}


@pytest.mark.parametrize("path", READING_GETS)
def test_a_failed_read_of_the_fold_of_a_get_answers_a_signed_500_with_database_error_read(
    hub: Hub, record_db: RecordDatabase, log_lines: list[str], path: str  # noqa: F811
) -> None:
    """Sections 7.1 and 13: the signed `500` `internal`, and only the request line with
    `database_error read`; no line `record database error`, which belongs to the periodic run, the
    checkpoint attempt and the release query."""
    work = new_id()
    hub.write(ALICE, "task", work, task_create())
    target = f"{path}?{REQUIRED_QUERY[path]}={work}" if path in REQUIRED_QUERY else path
    revoke_select(record_db, "record.event")
    log_lines.clear()
    wire, answer = hub.get(ALICE, target)
    assert_signed(wire, answer, 500, NOW)
    assert answer.body == INTERNAL
    assert hub_lines(log_lines) == [
        f"INFO record request {ALICE} GET {path} 500 database_error read {INSUFFICIENT_PRIVILEGE}"
    ]
    assert not hub.runtime.unverified
    grant_select(record_db, "record.event")
    hub.signed(ALICE, target, 200)


@pytest.mark.parametrize(
    ("path", "of_object", "table"),
    [(EVENTS, False, "record.event"), (EVENTS, True, "record.event"), (CHECKPOINTS, False, "record.checkpoint")],
)
def test_a_failed_read_of_the_stored_rows_of_a_get_after_its_fold_answers_a_signed_500_with_database_error_read(
    hub: Hub, record_db: RecordDatabase, log_lines: list[str], path: str, of_object: bool, table: str  # noqa: F811
) -> None:
    """The endpoints that read stored rows after the fold (sections 7.4, with and without `object`,
    and 7.10): the fold succeeds, the read of the rows fails."""
    work = new_id()
    hub.write(ALICE, "task", work, task_create())
    target = f"{path}?object={work}" if of_object else path
    hub.points.in_read_fold = lambda: revoke_select(record_db, table)
    log_lines.clear()
    wire, answer = hub.get(ALICE, target)
    assert_signed(wire, answer, 500, NOW)
    assert answer.body == INTERNAL
    assert hub_lines(log_lines) == [
        f"INFO record request {ALICE} GET {path} 500 database_error read {INSUFFICIENT_PRIVILEGE}"
    ]
    assert not hub.runtime.unverified
    hub.points.in_read_fold = lambda: None
    grant_select(record_db, table)
    hub.signed(ALICE, target, 200)


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


def test_a_database_failure_in_a_watcher_append_answers_a_signed_500_and_logs_only_the_request_line(
    hub: Hub, record_db: RecordDatabase, log_lines: list[str]  # noqa: F811
) -> None:
    """Sections 7.1, 7.3 and 13: the watcher's `service` key gets the one request line, and no
    line of KEY-PROTOCOL.md section 5 besides it."""
    work = new_id()
    hub.write(ALICE, "task", work, [*task_create(), ("links", None, LINK)])
    hub.points.append_before_insert = lambda: end_writer_connections(record_db)
    text, signature = hub.event(WATCHER, "task", work, [("status", "open", "done")])
    log_lines.clear()
    wire, answer = hub.post(WATCHER, WATCH, text, signature)
    assert_signed(wire, answer, 500, NOW)
    assert answer.body == INTERNAL
    assert_lines(hub_lines(log_lines), [f"INFO record request {WATCHER} POST {WATCH} 500 database_error append {SQLSTATE}"])
    assert not hub.runtime.unverified
    hub.points.append_before_insert = lambda: None
    wire, answer = hub.post(WATCHER, WATCH, text, signature)
    assert_signed(wire, answer, 201, NOW)


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


# A well-formed task id, so that a query that names it is invalid only by the case under test.
SOME_TASK = "5d1c2b3a-4e5f-4a6b-8c7d-9e0f1a2b3c4d"

INVALID_QUERIES = {
    EVENTS: [
        "after=01", "after=-1", "after=9223372036854775808", "limit=0", "limit=101", "limit=1000",
        "object=x", "x=1", "after=1&after=2", "after=", "after", "&after=1", "after=1&", "after=1&&limit=1",
        "after=+1", "after=%31", "object=5D1C2B3A-4E5F-4A6B-8C7D-9E0F1A2B3C4D",
    ],
    OBJECT: ["", "id=x", "id=" + "0" * 36, "x=1", f"id={SOME_TASK}&x=1", f"id={SOME_TASK}&id={SOME_TASK}", "id="],
    TASKS: [
        "kind=other", "status=open2", "assignee=Alice", "assignee=--", "repository=example-org",
        "repository=example-org%2fx%2fy", "repository=example-org%3Fx", "before=0", "limit=201", "limit=0",
        "x=1", "kind=work&kind=security", "kind=",
    ],
    COMMENTS: [
        "", "before=1", "task=x", f"task={SOME_TASK}&limit=201", f"task={SOME_TASK}&limit=0",
        f"task={SOME_TASK}&x=1", f"task={SOME_TASK}&task={SOME_TASK}", "task=", f"task={SOME_TASK}&before=",
    ],
    HEAD: ["x=1", "a"],
    VERIFY: ["x=1"],
    CHECKPOINTS: [
        "before=1", "before_key=hub-1", "before=1&before_key=Hub", "limit=201", "before=0&before_key=hub-1",
        "limit=0", "x=1", "limit=1&limit=1", "limit=",
    ],
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


def test_the_task_list_filter_status_selects_each_status_value(hub: Hub) -> None:
    """Section 14, "Paging and queries": the filter `status` alone, for each status of section 5.2,
    against tasks that do and do not match."""
    by_status = {status: new_id() for status in ("open", "in_progress", "ready_for_review", "merged", "done", "dropped")}
    assigned = [("assignee", None, "alice"), ("status", None, "in_progress")]
    hub.write(ALICE, "task", by_status["open"], task_create("Open"))
    hub.write(ALICE, "task", by_status["in_progress"], [*task_create("Started"), *assigned])
    review = by_status["ready_for_review"]
    hub.write(ALICE, "task", review, [*task_create("Review"), *assigned])
    hub.write(ALICE, "task", review, [("status", "in_progress", "ready_for_review")])
    merged = by_status["merged"]
    hub.write(ALICE, "task", merged, [*task_create("Merged"), ("completion", None, "verified"), ("links", None, LINK)])
    hub.write(WATCHER, "task", merged, [("status", "open", "merged")], WATCH)
    done = by_status["done"]
    hub.write(ALICE, "task", done, [*task_create("Done"), ("links", None, LINK)])
    hub.write(WATCHER, "task", done, [("status", "open", "done")], WATCH)
    dropped = by_status["dropped"]
    hub.write(ALICE, "task", dropped, task_create("Dropped"))
    hub.write(ALICE, "task", dropped, [("status", "open", "dropped")])
    for status, task in by_status.items():
        assert task_body(hub, task)["status"] == status
        answer = hub.signed(ALICE, f"{TASKS}?status={status}", 200)
        assert answer.body == body({"tasks": [task_body(hub, task)], "next_before": None, "hub_key": HUB_KEY_ID}), status


def test_the_task_list_with_limit_1_exactly_limit_remaining_and_an_empty_page_at_the_cursor(hub: Hub) -> None:
    """Section 14, "Paging and queries", for the task list: `limit` 1, a page with exactly `limit`
    remaining tasks has a `null` cursor, one more task gives a cursor, the cursor at the last task
    gives an empty page; newest first by the position of `changed`."""
    tasks = [new_id() for _ in range(3)]
    for index, task in enumerate(tasks):
        hub.write(ALICE, "task", task, task_create(f"Task {index}"))
    shown = {task: task_body(hub, task) for task in tasks}

    def page(items: list[str], cursor: int | None) -> bytes:
        return body({"tasks": [shown[task] for task in items], "next_before": cursor, "hub_key": HUB_KEY_ID})

    assert hub.signed(ALICE, f"{TASKS}?limit=1", 200).body == page([tasks[2]], 3)
    assert hub.signed(ALICE, f"{TASKS}?before=3&limit=1", 200).body == page([tasks[1]], 2)
    assert hub.signed(ALICE, f"{TASKS}?before=2&limit=1", 200).body == page([tasks[0]], None)
    assert hub.signed(ALICE, f"{TASKS}?before=3&limit=2", 200).body == page([tasks[1], tasks[0]], None)
    assert hub.signed(ALICE, f"{TASKS}?limit=2", 200).body == page([tasks[2], tasks[1]], 2)
    assert hub.signed(ALICE, f"{TASKS}?before=1", 200).body == page([], None)
    assert hub.signed(ALICE, f"{TASKS}?before=1&limit=1", 200).body == page([], None)


def test_the_comment_list_with_limit_1_exactly_limit_remaining_and_an_empty_page_at_the_cursor(hub: Hub) -> None:
    """Section 14, "Paging and queries", for the comment list: `limit` 1, a page with exactly
    `limit` remaining comments has a `null` cursor, one more comment gives a cursor, the cursor at
    the last comment gives an empty page; newest first by position."""
    work = new_id()
    hub.write(ALICE, "task", work, task_create())
    comments = [new_id() for _ in range(3)]
    for comment in comments:
        hub.write(ALICE, "comment", comment, [("body", None, "x"), ("task", None, work)])
    shown = {comment: hub.signed(ALICE, f"{OBJECT}?id={comment}", 200).json()["object"] for comment in comments}

    def page(items: list[str], cursor: int | None) -> bytes:
        return body({"comments": [shown[item] for item in items], "next_before": cursor, "hub_key": HUB_KEY_ID})

    target = f"{COMMENTS}?task={work}"
    assert hub.signed(ALICE, f"{target}&limit=1", 200).body == page([comments[2]], 4)
    assert hub.signed(ALICE, f"{target}&before=4&limit=1", 200).body == page([comments[1]], 3)
    assert hub.signed(ALICE, f"{target}&before=3&limit=1", 200).body == page([comments[0]], None)
    assert hub.signed(ALICE, f"{target}&before=4&limit=2", 200).body == page([comments[1], comments[0]], None)
    assert hub.signed(ALICE, f"{target}&limit=2", 200).body == page([comments[2], comments[1]], 3)
    assert hub.signed(ALICE, f"{target}&before=2", 200).body == page([], None)
    assert hub.signed(ALICE, f"{target}&before=1&limit=1", 200).body == page([], None)


def test_the_checkpoint_list_with_limit_1_exactly_limit_remaining_and_an_empty_page_at_the_cursor(
    hub: Hub, record_db: RecordDatabase
) -> None:
    """Section 14, "Paging and queries", for the checkpoint list: `limit` 1, a page with exactly
    `limit` remaining checkpoints has `null` cursors, one more checkpoint gives cursors, the cursor
    at the last checkpoint gives an empty page; the order of section 7.10."""
    written = [hub.write(ALICE, "task", new_id(), task_create()) for _ in range(2)]
    for position, item in enumerate(written, start=1):
        for hub_key_id in ("hub-2", "hub1"):
            insert_checkpoint(record_db, Checkpoint(hub_key_id, position, item["record_hash"], NOW, bytes(64)))

    def entry(position: int, hub_key_id: str) -> dict[str, Any]:
        return {
            "position": position,
            "record_hash": written[position - 1]["record_hash"],
            "published_at": fmt(NOW),
            "signed_by": hub_key_id,
            "signature": b64url_encode(bytes(64)),
        }

    def page(items: list[tuple[int, str]], cursor: tuple[int, str] | None) -> bytes:
        return body(
            {
                "checkpoints": [entry(*item) for item in items],
                "next_before": None if cursor is None else cursor[0],
                "next_before_key": None if cursor is None else cursor[1],
                "hub_key": HUB_KEY_ID,
            }
        )

    assert hub.signed(ALICE, f"{CHECKPOINTS}?limit=1", 200).body == page([(2, "hub1")], (2, "hub1"))
    assert hub.signed(ALICE, f"{CHECKPOINTS}?limit=3", 200).body == page(
        [(2, "hub1"), (2, "hub-2"), (1, "hub1")], (1, "hub1")
    )
    assert hub.signed(ALICE, f"{CHECKPOINTS}?before=1&before_key=hub1&limit=1", 200).body == page(
        [(1, "hub-2")], None
    )
    assert hub.signed(ALICE, f"{CHECKPOINTS}?before=2&before_key=hub-2&limit=2", 200).body == page(
        [(1, "hub1"), (1, "hub-2")], None
    )
    assert hub.signed(ALICE, f"{CHECKPOINTS}?before=1&before_key=hub-2", 200).body == page([], None)
    assert hub.signed(ALICE, f"{CHECKPOINTS}?before=1&before_key=hub-2&limit=1", 200).body == page([], None)


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


def test_a_database_failure_of_the_verification_on_request_answers_a_signed_500_and_logs_only_the_request_line(
    hub: Hub, record_db: RecordDatabase, log_lines: list[str]  # noqa: F811
) -> None:
    """Sections 7.1, 7.9 and 13: the read of the snapshot fails. The answer is the signed `500`
    `internal`, the only line is the request line with `database_error read`, and the process does
    not enter the state `record_unverified`, which only a failure value of section 12.1 enters. The
    run started, so it counts for the limit of section 7.9."""
    written = hub.write(ALICE, "task", new_id(), task_create())
    hub.points.snapshot_between_reads = lambda: end_writer_connections(record_db)
    log_lines.clear()
    wire, answer = hub.get(ALICE, VERIFY)
    assert_signed(wire, answer, 500, NOW)
    assert answer.body == INTERNAL
    assert_lines(hub_lines(log_lines), [f"INFO record request {ALICE} GET {VERIFY} 500 database_error read {SQLSTATE}"])
    assert not hub.runtime.unverified
    hub.points.snapshot_between_reads = lambda: None
    assert hub.signed(ALICE, VERIFY, 429).body == error("verify_busy")
    hub.monotonic.now += 60
    assert hub.signed(ALICE, VERIFY, 200).body == body(
        {"verified": True, "position": 1, "record_hash": written["record_hash"], "failure": None, "hub_key": HUB_KEY_ID}
    )


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


# Section 14, "Integrity and backup": each event failure of section 12.1 from `event_invalid` to
# `event_refused`, except `record_hash_mismatch`.
EVENT_FAILURES = [
    "event_invalid",
    "certificate_not_supported",
    "row_mismatch",
    "key_unknown",
    "event_signature_invalid",
    "event_id_taken",
    "chain_broken",
    "event_time_invalid",
    "event_refused",
]
UNKNOWN_KEY = "zed-laptop-1"
UNKNOWN_SEED = 99
# The failing event is either an update of a security issue of the team `example`, which alice
# sees and neither dave (another team) nor bob (without `record:security`) sees, or a create.
UPDATE_OF_AN_ISSUE = "an update of a security issue"
A_CREATE = "a create"


def _with_title(changes: Sequence[tuple[str, str | None, str | None]], title: str) -> list[tuple[str, str | None, str | None]]:
    return [(name, old, title) if name == "title" else (name, old, new) for name, old, new in changes]


def _failing_event(
    hub: Hub, db: RecordDatabase, failure: str, variant: str, issue: str, first_event_id: str
) -> tuple[bytes, bytes, dict[str, str]]:
    """The event text, the signature and the overridden copied columns of a row that fails
    verification with `failure`, validly chained after alice's last event unless the failure is
    about the chain."""
    if variant == UPDATE_OF_AN_ISSUE:
        object_id = issue
        good: list[tuple[str, str | None, str | None]] = [("title", "Login bypass", "Login bypass fixed")]
        invalid = [("title", "Login bypass", "x" * 201)]
        refused = [("title", "Stale", "Other")]
    else:
        object_id = new_id()
        good = security_create("second-bypass")
        invalid = _with_title(good, "x" * 201)
        refused = [*good, ("status", None, "merged")]
    columns: dict[str, str] = {}
    if failure == "event_invalid":
        text, signature = hub.event(ALICE, "task", object_id, invalid)
    elif failure == "certificate_not_supported":
        text, signature = hub.event(ALICE, "task", object_id, good, certificate=b"cert")
    elif failure == "row_mismatch":
        text, signature = hub.event(ALICE, "task", object_id, good)
        columns["event_id"] = new_id()
    elif failure == "key_unknown":
        text = build_event_text(
            event_id=new_id(),
            object_type="task",
            object_id=object_id,
            actor_key_id=UNKNOWN_KEY,
            signed_at=NOW,
            prev_hash="-",
            changes=[Change(name, encode(old), encode(new)) for name, old, new in good],
        )
        signature = private(UNKNOWN_SEED).sign(text)
    elif failure == "event_signature_invalid":
        text, _ = hub.event(ALICE, "task", object_id, good)
        signature = private(SEEDS[BOB]).sign(text)
    elif failure == "event_id_taken":
        drop_constraint(db, "event_event_id_key")
        text, signature = hub.event(ALICE, "task", object_id, good, event_id=first_event_id)
    elif failure == "chain_broken":
        text, signature = hub.event(ALICE, "task", object_id, good, prev_hash="1" * 64)
    elif failure == "event_time_invalid":
        text, signature = hub.event(ALICE, "task", object_id, good, signed_at=NOW + 121)
    else:
        text, signature = hub.event(ALICE, "task", object_id, refused)
    return text, signature, columns


@pytest.mark.parametrize("variant", [UPDATE_OF_AN_ISSUE, A_CREATE])
@pytest.mark.parametrize("failure", EVENT_FAILURES)
def test_each_event_failure_is_withheld_alike_for_every_key_then_503_and_a_restart_refuses_to_start(
    record_db: RecordDatabase, cfg: Config, tmp_path: Path, log_lines: list[str], failure: str, variant: str  # noqa: F811
) -> None:
    migrated(record_db)
    with started(record_db, cfg, tmp_path, ["sees", "other-team", "no-security"]) as (sees, other_team, no_security):
        first_text, first_signature = sees.event(ALICE, "task", new_id(), task_create())
        wire, answer = sees.post(ALICE, EVENTS, first_text, first_signature)
        assert_signed(wire, answer, 201, NOW)
        issue = new_id()
        written = sees.write(ALICE, "task", issue, security_create("login-bypass"))
        for instance in (other_team, no_security):
            assert instance.signed(ALICE, HEAD, 200).json()["position"] == 2
        text, signature, columns = _failing_event(sees, record_db, failure, variant, issue, parse_event(first_text).event_id)
        insert_as_superuser(record_db, stored_event(3, NOW, text, signature, written["record_hash"]), **columns)
        log_lines.clear()
        for instance, key_id in ((sees, ALICE), (other_team, DAVE), (no_security, BOB)):
            assert instance.signed(key_id, VERIFY, 200).body == withheld_at(3)
            assert instance.runtime.unverified
        assert request_lines(log_lines) == [
            f"{key_id} GET {VERIFY} 200 verify_failed 3 {failure}" for key_id in (ALICE, DAVE, BOB)
        ]
        for instance in (sees, other_team, no_security):
            answers_503_everywhere(instance)
    refused_restart(record_db, cfg, tmp_path, log_lines, 3, failure)


# A `service` key with `record:read` and `record:security` whose `subjects` hold REPO only.
AUDITOR = "audit-one"
AUDITOR_SEED = 38


def _auditor_entries() -> list[dict[str, Any]]:
    return [
        *key_entries(),
        {
            "id": AUDITOR,
            "kind": "ed25519",
            "principal": "service",
            "handle": "example-auditor",
            "team": TEAM,
            "device": f"runner-{AUDITOR_SEED}",
            "public_key": public_text(AUDITOR_SEED),
            "scopes": ["record:read", "record:security"],
            "subjects": [REPO],
            "expires": fmt(NOW + 365 * 86400),
        },
    ]


def _chain_broken_over_an_unseen_earlier_event(hub: Hub) -> tuple[str, StoredEvent, str | None]:
    """bob sees alice's task and not her security issue. alice's event on the task names as
    `prev_hash` the event hash of her issue's create, which is not her previous event."""
    work = new_id()
    hub.write(ALICE, "task", work, task_create())
    issue = new_id()
    created = hub.write(ALICE, "task", issue, security_create("login-bypass"))
    previous = hub.write(ALICE, "task", issue, [("title", "Login bypass", "Login bypass two")])
    text, signature = hub.event(ALICE, "task", work, [("title", "Fix login", "Fix it")], prev_hash=created["event_hash"])
    return BOB, stored_event(4, NOW, text, signature, previous["record_hash"]), "event_actor_key_id_prev_hash_key"


def _signed_at_before_an_unseen_previous_event(hub: Hub) -> tuple[str, StoredEvent, str | None]:
    """bob sees alice's task and not her security issue. alice's event on the task is chained to
    her issue's create, which she signed later; its own `received_at` order and its 120-second
    bound hold."""
    work = new_id()
    hub.write(ALICE, "task", work, task_create())
    hub.clock.now = NOW + 100
    previous = hub.write(ALICE, "task", new_id(), security_create("login-bypass"))
    text, signature = hub.event(ALICE, "task", work, [("title", "Fix login", "Fix it")], signed_at=NOW + 50)
    return BOB, stored_event(3, NOW + 100, text, signature, previous["record_hash"]), None


def _short_name_of_an_unseen_issue(hub: Hub) -> tuple[str, StoredEvent, str | None]:
    """The auditor sees the security issue T1 (repository REPO) and not T2 (OTHER_REPO). alice
    sets the short name of T1 to that of T2: the reducer refuses it with `short_name_taken`."""
    first = new_id()
    hub.write(ALICE, "task", first, security_create("alpha"))
    previous = hub.write(ALICE, "task", new_id(), security_create("beta", OTHER_REPO))
    text, signature = hub.event(ALICE, "task", first, [("short_name", "alpha", "beta")])
    return AUDITOR, stored_event(3, NOW, text, signature, previous["record_hash"]), None


SEEN_FAILURES: dict[str, tuple[Callable[[Hub], tuple[str, StoredEvent, str | None]], str]] = {
    "chain_broken over an earlier event the caller does not see": (_chain_broken_over_an_unseen_earlier_event, "chain_broken"),
    "event_time_invalid over a previous event the caller does not see": (
        _signed_at_before_an_unseen_previous_event,
        "event_time_invalid",
    ),
    "short_name_taken of an issue the caller does not see": (_short_name_of_an_unseen_issue, "event_refused"),
}


@pytest.mark.parametrize("case", sorted(SEEN_FAILURES))
def test_withheld_to_a_key_that_sees_the_failing_event_byte_for_byte_as_to_another_team(
    record_db: RecordDatabase, cfg: Config, tmp_path: Path, log_lines: list[str], case: str  # noqa: F811
) -> None:
    build, failure = SEEN_FAILURES[case]
    migrated(record_db)
    options = {"entries": _auditor_entries(), "seeds": {**SEEDS, AUDITOR: AUDITOR_SEED}}
    with started(record_db, cfg, tmp_path, ["caller", "other-team"], **options) as (caller_hub, other_team):
        caller, stored, constraint = build(caller_hub)
        head = caller_hub.signed(ALICE, HEAD, 200).json()["position"]
        assert other_team.signed(DAVE, HEAD, 200).json()["position"] == head == stored.position - 1
        if constraint is not None:
            drop_constraint(record_db, constraint)
        insert_as_superuser(record_db, stored)
        log_lines.clear()
        caller_answer = caller_hub.signed(caller, VERIFY, 200).body
        other_answer = other_team.signed(DAVE, VERIFY, 200).body
    assert caller_answer == other_answer == withheld_at(stored.position)
    assert request_lines(log_lines) == [
        f"{caller} GET {VERIFY} 200 verify_failed {stored.position} {failure}",
        f"{DAVE} GET {VERIFY} 200 verify_failed {stored.position} {failure}",
    ]


@pytest.mark.parametrize("value", ["10000-01-01 00:00:00+00", "infinity"])
def test_a_received_at_that_cannot_be_written_is_withheld_as_event_time_invalid_and_refuses_a_restart(
    hub: Hub, record_db: RecordDatabase, cfg: Config, tmp_path: Path, log_lines: list[str], value: str  # noqa: F811
) -> None:
    hub.write(ALICE, "task", new_id(), task_create())
    hub.write(ALICE, "task", new_id(), task_create())
    tamper(record_db, "record.event", "UPDATE record.event SET received_at = %s::timestamptz WHERE position = 2", (value,))
    log_lines.clear()
    assert hub.signed(ALICE, VERIFY, 200).body == withheld_at(2)
    assert request_lines(log_lines) == [f"{ALICE} GET {VERIFY} 200 verify_failed 2 event_time_invalid"]
    refused_restart(record_db, cfg, tmp_path, log_lines, 2, "event_time_invalid")


def _gap(db: RecordDatabase) -> None:
    tamper(db, "record.event", "UPDATE record.event SET position = 3 WHERE position = 2")


def _changed_record_hash(db: RecordDatabase) -> None:
    tamper(db, "record.event", "UPDATE record.event SET record_hash = %s WHERE position = 1", ("d" * 64,))


def _changed_checkpoint_signature(db: RecordDatabase) -> None:
    tamper(db, "record.checkpoint", "UPDATE record.checkpoint SET signature = %s", (bytes(64),))


RECORD_WIDE: dict[str, tuple[Callable[[RecordDatabase], None], int]] = {
    "position_gap": (_gap, 2),
    "record_hash_mismatch": (_changed_record_hash, 1),
    "checkpoint_invalid": (_changed_checkpoint_signature, 2),
}


@pytest.mark.parametrize("failure", sorted(RECORD_WIDE))
def test_a_record_wide_failure_is_named_with_its_position_to_every_key_alike(
    record_db: RecordDatabase, cfg: Config, tmp_path: Path, failure: str
) -> None:
    change, position = RECORD_WIDE[failure]
    migrated(record_db)
    with started(record_db, cfg, tmp_path, ["team", "other-team", "service"]) as instances:
        writer_hub = instances[0]
        writer_hub.write(ALICE, "task", new_id(), task_create())
        writer_hub.write(ALICE, "task", new_id(), task_create())
        writer_hub.runtime.checkpoint()
        for instance in instances:
            assert instance.signed(ALICE, HEAD, 200).json()["position"] == 2
        change(record_db)
        answers = [instance.signed(key_id, VERIFY, 200).body for instance, key_id in zip(instances, (ALICE, DAVE, READER))]
    assert answers == [named_at(position, failure)] * 3


@pytest.mark.parametrize("value", ["10000-01-01 00:00:00+00", "infinity"])
def test_an_own_checkpoint_whose_published_at_cannot_be_written_fails_verify_the_restart_and_the_next_attempt(
    record_db: RecordDatabase, cfg: Config, tmp_path: Path, log_lines: list[str], value: str  # noqa: F811
) -> None:
    migrated(record_db)
    with started(record_db, cfg, tmp_path, ["asked", "running"]) as (asked, running):
        asked.write(ALICE, "task", new_id(), task_create())
        asked.runtime.checkpoint()
        asked.write(ALICE, "task", new_id(), task_create())
        assert running.signed(ALICE, HEAD, 200).json()["position"] == 2
        # The newest checkpoint of the hub's own key id is changed while both processes run.
        tamper(record_db, "record.checkpoint", "UPDATE record.checkpoint SET published_at = %s::timestamptz", (value,))
        assert asked.signed(ALICE, VERIFY, 200).body == named_at(1, "checkpoint_invalid")
        # Before its next periodic run, the other process's next attempt writes nothing.
        running.clock.now += 3600
        log_lines.clear()
        running.runtime.checkpoint()
        assert record_part(log_lines) == ["ERROR record verify failed 1 checkpoint_invalid"]
        assert counts(record_db) == (2, 1)
        assert running.signed(ALICE, HEAD, 503).body == RECORD_UNVERIFIED
    refused_restart(record_db, cfg, tmp_path, log_lines, 1, "checkpoint_invalid")


@pytest.mark.parametrize(
    ("hub_key_id", "published_at"),
    [("hub-2", "10000-01-01 00:00:00+00"), ("Hub-2", "2027-01-15 08:00:00+00")],
)
def test_a_checkpoint_of_another_hub_key_id_without_the_form_of_section_2_fails_verify_and_the_restart(
    hub: Hub, record_db: RecordDatabase, cfg: Config, tmp_path: Path, log_lines: list[str],  # noqa: F811
    hub_key_id: str, published_at: str,
) -> None:
    written = hub.write(ALICE, "task", new_id(), task_create())
    insert_checkpoint(record_db, Checkpoint(hub_key_id, 1, written["record_hash"], NOW, bytes(64)), published_at=published_at)
    assert hub.signed(ALICE, VERIFY, 200).body == named_at(1, "checkpoint_invalid")
    refused_restart(record_db, cfg, tmp_path, log_lines, 1, "checkpoint_invalid")


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


@pytest.mark.parametrize(("column", "value"), [("record_hash", "d" * 64), ("signature", bytes(64))])
def test_a_changed_newest_checkpoint_makes_the_next_attempt_unverified(
    hub: Hub, record_db: RecordDatabase, log_lines: list[str], column: str, value: Any  # noqa: F811
) -> None:
    """Section 12.2, "checkpoints sign only a checked head": the newest checkpoint of the hub's own
    key id changed as database superuser, by its record hash and in a second run by its signature;
    the head moved, so without the change the attempt would write."""
    hub.write(ALICE, "task", new_id(), task_create())
    hub.runtime.checkpoint()
    hub.write(ALICE, "task", new_id(), task_create())
    tamper(record_db, "record.checkpoint", f"UPDATE record.checkpoint SET {column} = %s", (value,))
    hub.clock.now += 3600
    log_lines.clear()
    hub.runtime.checkpoint()
    assert record_part(log_lines) == ["ERROR record verify failed 1 checkpoint_invalid"]
    assert counts(record_db) == (2, 1)
    assert hub.signed(ALICE, HEAD, 503).body == RECORD_UNVERIFIED
    # In the state `record_unverified` no attempt and no periodic run.
    hub.clock.now += 3600
    hub.monotonic.now += 600
    log_lines.clear()
    hub.runtime.tick()
    assert record_part(log_lines) == []


def test_a_checkpoint_attempt_that_waits_while_the_process_enters_record_unverified_writes_nothing(
    hub: Hub, record_db: RecordDatabase, log_lines: list[str]  # noqa: F811
) -> None:
    """Section 12.2: a process in the state `record_unverified` writes no checkpoint, also when its
    attempt started before and waited for the advisory lock while a verification on request of
    the same process failed."""
    hub.write(ALICE, "task", new_id(), task_create())
    # A row before the head of the state: the fold of the attempt does not see it.
    tamper(record_db, "record.event", "UPDATE record.event SET signature = %s WHERE position = 1", (bytes(64),))
    log_lines.clear()
    with append_lock_held(record_db):
        attempt = Parallel(hub.runtime.checkpoint)
        wait_for_append_lock(record_db, 1)
        assert hub.signed(DAVE, VERIFY, 200).body == withheld_at(1)
        assert hub.runtime.unverified
    attempt.results()
    assert counts(record_db) == (1, 0)
    assert record_part(log_lines) == [f"INFO record request {DAVE} GET {VERIFY} 200 verify_failed 1 event_signature_invalid"]


def wait_until(condition: Callable[[], bool]) -> None:
    """Wait until `condition` holds, at most 60 seconds."""
    deadline = time.monotonic() + 60
    while not condition():
        assert time.monotonic() < deadline, "the condition never held"
        time.sleep(0.01)


@pytest.mark.parametrize(("key_id", "path"), APPENDS)
def test_an_append_that_waits_while_the_process_enters_record_unverified_answers_503_and_writes_nothing(
    hub: Hub, record_db: RecordDatabase, log_lines: list[str], key_id: str, path: str  # noqa: F811
) -> None:
    """Section 12.1: from the moment the process enters the state `record_unverified` it answers
    the signed `503` on every record endpoint. An append that passed the first test of the state
    and waited for the advisory lock while a verification on request of the same process failed
    reads and writes nothing once it holds the lock."""
    hub.write(ALICE, "task", new_id(), task_create())
    # A row before the head of the state: the fold of the append does not see it.
    tamper(record_db, "record.event", "UPDATE record.event SET signature = %s WHERE position = 1", (bytes(64),))
    text, signature = hub.event(key_id, "task", new_id(), VALID_CHANGES[path])
    log_lines.clear()
    with append_lock_held(record_db):
        appending = Parallel(lambda: hub.post(key_id, path, text, signature))
        wait_for_append_lock(record_db, 1)
        assert hub.signed(DAVE, VERIFY, 200).body == withheld_at(1)
        assert hub.runtime.unverified
    ((wire, answer),) = appending.results()
    assert_signed(wire, answer, 503, NOW)
    assert answer.body == RECORD_UNVERIFIED
    assert counts(record_db) == (1, 0)
    assert request_lines(log_lines) == [
        f"{DAVE} GET {VERIFY} 200 verify_failed 1 event_signature_invalid",
        f"{key_id} POST {path} 503 record_unverified",
    ]


RELEASE_TARGET = f"{RELEASE}?repository={REPO.replace('/', '%2F')}&pull_request=42"
# A read that waits for the state lock, and the hub lines it writes, as `LEVEL text`. A request
# answers `(wire, answer)`; the periodic run answers nothing.
WAITING_READS: dict[str, tuple[Callable[[Hub], Any], list[str]]] = {
    "a GET endpoint": (
        lambda hub: hub.get(BOB, HEAD),
        [f"INFO record request {BOB} GET {HEAD} 503 record_unverified"],
    ),
    "the release query": (
        lambda hub: hub.get(GUARD, RELEASE_TARGET),
        [f"INFO key request {GUARD} GET {RELEASE} 503"],
    ),
    "the verification on request": (
        lambda hub: hub.get(DAVE, VERIFY),
        [f"INFO record request {DAVE} GET {VERIFY} 503 record_unverified"],
    ),
    "a periodic verification": (lambda hub: hub.runtime.periodic(), []),
}


@pytest.mark.parametrize("case", sorted(WAITING_READS))
def test_a_read_that_waits_for_the_state_lock_while_the_process_enters_record_unverified_reads_nothing(
    hub: Hub, record_db: RecordDatabase, log_lines: list[str], case: str  # noqa: F811
) -> None:
    """Section 12.1: a `GET` endpoint, the release query, a verification on request or a periodic
    run that passed the first test of the state and took its connection waits for the state lock,
    while the request that holds it enters the state `record_unverified`. The waiting read then
    reads nothing, and a request answers the signed `503`: the writer role can no longer read
    `record.event` at that point, so any read would answer `500` or log `record database error`."""
    read, lines = WAITING_READS[case]
    hub.monotonic.now += 600
    hub.write(ALICE, "task", new_id(), task_create())
    break_after_the_state(hub, record_db)
    in_fold = threading.Event()
    release_fold = threading.Event()

    def hold() -> None:
        in_fold.set()
        assert release_fold.wait(60)

    hub.points.in_read_fold = hold
    log_lines.clear()
    first = Parallel(lambda: hub.get(ALICE, HEAD))
    assert in_fold.wait(60)
    hub.points.in_read_fold = lambda: None
    taken = hub.runtime.pool.taken
    second = Parallel(lambda: read(hub))
    # The second read holds its connection and waits for the state lock.
    wait_until(lambda: hub.runtime.pool.taken == taken + 1)
    revoke_select(record_db, "record.event")
    revoke_select(record_db, "record.checkpoint")
    release_fold.set()
    answered = first.results() + [item for item in second.results() if item is not None]
    for wire, answer in answered:
        assert_signed(wire, answer, 503, NOW)
        assert answer.body == RECORD_UNVERIFIED
    assert sorted(hub_lines(log_lines)) == sorted(
        [f"INFO record request {ALICE} GET {HEAD} 503 verify_failed 2 row_mismatch", *lines]
    )


def _fold_gap(hub: Hub, db: RecordDatabase, first_event_id: str, previous: str) -> tuple[int, str]:
    """A row at position 3 after a head at 1: the first missing position is 2."""
    text, signature = hub.event(BOB, "task", new_id(), task_create("Gap"))
    insert_as_superuser(db, stored_event(3, NOW, text, signature, previous))
    return 2, "position_gap"


def _fold_received_at_earlier(hub: Hub, db: RecordDatabase, first_event_id: str, previous: str) -> tuple[int, str]:
    """A `received_at` one second earlier than at the previous position."""
    text, signature = hub.event(BOB, "task", new_id(), task_create("Earlier"))
    insert_as_superuser(db, stored_event(2, NOW - 1, text, signature, previous))
    return 2, "event_time_invalid"


def _fold_signed_at_late(hub: Hub, db: RecordDatabase, first_event_id: str, previous: str) -> tuple[int, str]:
    """A `signed_at` 121 seconds after its `received_at`."""
    text, signature = hub.event(BOB, "task", new_id(), task_create("Late"), signed_at=NOW + 121)
    insert_as_superuser(db, stored_event(2, NOW, text, signature, previous))
    return 2, "event_time_invalid"


def _fold_event_id_column(hub: Hub, db: RecordDatabase, first_event_id: str, previous: str) -> tuple[int, str]:
    """An `event_id` column that differs from the event text."""
    text, signature = hub.event(BOB, "task", new_id(), task_create("Column"))
    insert_as_superuser(db, stored_event(2, NOW, text, signature, previous), event_id=new_id())
    return 2, "row_mismatch"


def _fold_event_id_used(hub: Hub, db: RecordDatabase, first_event_id: str, previous: str) -> tuple[int, str]:
    """An event of another key, validly signed and chained, whose `event_id` the event at position
    1 already has; the unique constraint on `event_id` is dropped first."""
    drop_constraint(db, "event_event_id_key")
    text, signature = hub.event(BOB, "task", new_id(), task_create("Taken"), event_id=first_event_id)
    insert_as_superuser(db, stored_event(2, NOW, text, signature, previous))
    return 2, "event_id_taken"


def _fold_certificate(hub: Hub, db: RecordDatabase, first_event_id: str, previous: str) -> tuple[int, str]:
    """An event of a registered `ed25519` key, validly signed and chained, whose `key_certificate`
    is `=` followed by a value (section 14, `certificate_not_supported`)."""
    text, signature = hub.event(BOB, "task", new_id(), task_create("Certified"), certificate=b"cert")
    insert_as_superuser(db, stored_event(2, NOW, text, signature, previous))
    return 2, "certificate_not_supported"


FOLD_FAILURES: dict[str, Callable[[Hub, RecordDatabase, str, str], tuple[int, str]]] = {
    "position_gap": _fold_gap,
    "event_time_invalid by a received_at earlier than at the previous position": _fold_received_at_earlier,
    "event_time_invalid by a signed_at more than 120 seconds after received_at": _fold_signed_at_late,
    "row_mismatch of the event_id column": _fold_event_id_column,
    "event_id_taken by an event of another key": _fold_event_id_used,
    "certificate_not_supported": _fold_certificate,
}


@pytest.mark.parametrize("case", sorted(FOLD_FAILURES))
def test_the_incremental_fold_of_a_checkpoint_attempt_checks_every_row_writes_nothing_and_the_process_answers_503(
    hub: Hub, record_db: RecordDatabase, log_lines: list[str], case: str  # noqa: F811
) -> None:
    """Section 14: after the state of instance A a row is inserted as database superuser; A's next
    checkpoint attempt writes nothing and logs the failure with its position, and A's next
    `GET /api/key/record/head` answers the signed `503`."""
    text, signature = hub.event(ALICE, "task", new_id(), task_create())
    wire, answer = hub.post(ALICE, EVENTS, text, signature)
    assert_signed(wire, answer, 201, NOW)
    position, failure = FOLD_FAILURES[case](hub, record_db, parse_event(text).event_id, answer.json()["record_hash"])
    log_lines.clear()
    hub.runtime.checkpoint()
    assert counts(record_db) == (2, 0)
    assert record_part(log_lines) == [f"ERROR record verify failed {position} {failure}"]
    assert hub.runtime.unverified
    log_lines.clear()
    assert hub.signed(ALICE, HEAD, 503).body == RECORD_UNVERIFIED
    assert request_lines(log_lines) == [f"{ALICE} GET {HEAD} 503 record_unverified"]


def test_a_row_that_another_instance_appended_and_that_was_then_changed_makes_the_next_attempt_unverified(
    hub: Hub, record_db: RecordDatabase, cfg: Config, tmp_path: Path, log_lines: list[str]  # noqa: F811
) -> None:
    """Section 14, "checkpoints sign only a checked head": between two attempts of instance A,
    instance B appends an event and that row is then changed as database superuser."""
    other = start_hub(record_db, cfg, tmp_path, name="other")
    try:
        hub.write(ALICE, "task", new_id(), task_create())
        hub.runtime.checkpoint()
        assert counts(record_db) == (1, 1)
        assert other.write(BOB, "task", new_id(), task_create())["position"] == 2
        tamper(record_db, "record.event", "UPDATE record.event SET signature = %s WHERE position = 2", (bytes(64),))
        hub.clock.now += 3600
        log_lines.clear()
        hub.runtime.checkpoint()
        assert counts(record_db) == (2, 1)
        assert record_part(log_lines) == ["ERROR record verify failed 2 event_signature_invalid"]
        assert hub.signed(ALICE, HEAD, 503).body == RECORD_UNVERIFIED
    finally:
        other.close()


def test_an_instance_in_record_unverified_writes_no_checkpoint_at_the_next_full_hour_also_when_the_head_moved(
    hub: Hub, record_db: RecordDatabase, cfg: Config, tmp_path: Path, log_lines: list[str]  # noqa: F811
) -> None:
    """Section 12.2: instance A enters the state `record_unverified`; instance B then moves the
    head. A makes no attempt at the next full hour, which would otherwise write the first
    checkpoint."""
    other = start_hub(record_db, cfg, tmp_path, name="other")
    try:
        hub.write(ALICE, "task", new_id(), task_create())
        assert other.signed(ALICE, HEAD, 200).json()["position"] == 1
        # A row before the head of both states: B's appends do not fold it.
        tamper(record_db, "record.event", "UPDATE record.event SET signature = %s WHERE position = 1", (bytes(64),))
        assert hub.signed(DAVE, VERIFY, 200).body == withheld_at(1)
        assert hub.runtime.unverified
        assert other.write(BOB, "task", new_id(), task_create())["position"] == 2
        hub.clock.now += 3600
        hub.monotonic.now += 600
        taken = hub.runtime.pool.taken
        log_lines.clear()
        hub.runtime.tick()
        assert hub.runtime.pool.taken == taken
        assert counts(record_db) == (2, 0)
        assert record_part(log_lines) == []
    finally:
        other.close()


def test_a_checkpoint_after_the_clock_was_set_back_below_the_head_is_published_earlier_and_verifies(
    hub: Hub, log_lines: list[str]  # noqa: F811
) -> None:
    """Section 12.2, `published_at`: the clock is set back below the `received_at` of the head and
    then passes a full hour that no checkpoint of the hub key id covers yet; the checkpoint has a
    `published_at` earlier than that `received_at`, and the next verification on request answers
    `verified` true. While the newest checkpoint has a `published_at` at or after a full hour
    reached again, an attempt for that hour writes nothing."""
    hub.write(ALICE, "task", new_id(), task_create("First"))
    hub.runtime.checkpoint()
    hub.clock.now = NOW + 7200
    head = hub.write(BOB, "task", new_id(), task_create("Second"))
    assert head["received_at"] == fmt(NOW + 7200)
    # Set back below the head's `received_at`, then past the full hour NOW + 3600.
    hub.clock.now = NOW + 3600 + 60
    log_lines.clear()
    hub.runtime.checkpoint()
    assert record_part(log_lines) == [f"INFO record checkpoint 2 {head['record_hash']}"]
    page = hub.signed(ALICE, CHECKPOINTS, 200).json()
    assert [(entry["position"], entry["published_at"]) for entry in page["checkpoints"]] == [
        (2, fmt(NOW + 3600 + 60)),
        (1, fmt(NOW)),
    ]
    assert hub.signed(ALICE, VERIFY, 200).body == body(
        {"verified": True, "position": 2, "record_hash": head["record_hash"], "failure": None, "hub_key": HUB_KEY_ID}
    )
    # The head moves; the clock reaches the full hours NOW and NOW + 3600 again: each attempt runs
    # and writes nothing, because the newest checkpoint was published after both.
    third = hub.write(ALICE, "task", new_id(), task_create("Third"))
    assert third["received_at"] == fmt(NOW + 7200)
    for again in (NOW + 1800, NOW + 3600 + 30):
        hub.clock.now = again
        taken = hub.runtime.pool.taken
        log_lines.clear()
        hub.runtime.checkpoint()
        assert hub.runtime.pool.taken == taken + 1
        assert record_part(log_lines) == []
    # The next full hour that no checkpoint covers writes the checkpoint of the moved head.
    hub.clock.now = NOW + 7200
    hub.runtime.checkpoint()
    assert record_part(log_lines) == [f"INFO record checkpoint 3 {third['record_hash']}"]


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
    """State per process (section 12.1), with two instances A (`hub`) and B (`other`)."""
    other = start_hub(record_db, cfg, tmp_path, name="other")
    try:
        hub.write(ALICE, "task", new_id(), task_create())
        assert other.signed(ALICE, HEAD, 200).json()["position"] == 1
        tamper(record_db, "record.event", "UPDATE record.event SET signature = %s WHERE position = 1", (bytes(64),))
        # A's verification on request fails: A answers the signed 503 on every record endpoint and
        # on the release query.
        assert hub.signed(ALICE, VERIFY, 200).body == withheld_at(1)
        answers_503_everywhere(hub)
        # B keeps answering until its next periodic run.
        log_lines.clear()
        other.monotonic.now += 599
        other.runtime.periodic()
        assert record_part(log_lines) == []
        assert not other.runtime.unverified
        assert other.signed(ALICE, HEAD, 200).json()["position"] == 1
        log_lines.clear()
        other.monotonic.now += 1
        other.runtime.periodic()
        assert record_part(log_lines) == ["ERROR record verify failed 1 event_signature_invalid"]
        answers_503_everywhere(other)
        # In both instances the rest of the hub keeps answering.
        rest_of_the_hub_answers(hub, "record-test-a")
        rest_of_the_hub_answers(other, "record-test-b")
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


def test_a_periodic_run_against_a_listener_that_never_answers_logs_database_error_connect_dash_within_15_seconds(
    hub: Hub, record_db: RecordDatabase, log_lines: list[str]  # noqa: F811
) -> None:
    """Section 14, the connect timeout of section 15: the connection that a periodic run opens
    goes to a listener that accepts TCP connections and never answers."""
    with silent_listener() as port:
        hub.runtime.pool.close()
        hub.runtime.pool = WriterPool(parse_connection_string(unreachable_url(record_db, port)), 3)
        hub.monotonic.now += 600
        log_lines.clear()
        begun = time.monotonic()
        hub.runtime.periodic()
        assert time.monotonic() - begun < 15
    assert record_part(log_lines) == ["ERROR record database error connect -"]
    assert not hub.runtime.unverified


def test_a_periodic_run_that_fails_to_read_logs_record_database_error_read(
    hub: Hub, record_db: RecordDatabase, log_lines: list[str]  # noqa: F811
) -> None:
    hub.write(ALICE, "task", new_id(), task_create())
    hub.points.snapshot_between_reads = lambda: end_writer_connections(record_db)
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


# Every path of the endpoint table of section 7.1, and a method that no endpoint has on it.
RECORD_PATHS = [EVENTS, WATCH, OBJECT, TASKS, COMMENTS, HEAD, VERIFY, CHECKPOINTS]
OTHER_METHOD = {
    EVENTS: "PUT",
    WATCH: "GET",
    OBJECT: "POST",
    TASKS: "POST",
    COMMENTS: "POST",
    HEAD: "POST",
    VERIFY: "POST",
    CHECKPOINTS: "POST",
}
LOGGED_METHOD = {"GET": "GET", "POST": "POST", "PUT": "-"}
NOT_FOUND_CASES = [
    *[(OTHER_METHOD[path], path, LOGGED_METHOD[OTHER_METHOD[path]], path) for path in RECORD_PATHS],
    ("DELETE", HEAD, "-", HEAD),
    *[("GET", path + "/", "GET", "-") for path in RECORD_PATHS],
    *[("HEAD", path, "HEAD", path) for path in RECORD_PATHS],
    ("GET", "/api/key/record/marker-5d1c2b3a", "GET", "-"),
]


@pytest.mark.parametrize(("method", "target", "logged_method", "logged_path"), NOT_FOUND_CASES)
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


# A watcher whose key id contains its handle and whose `subjects` hold the marker repository.
MARKER_WATCHER = "example-watcher-1"
MARKER_WATCHER_SEED = 39


def test_no_log_line_holds_record_content_an_id_or_a_secret(
    record_db: RecordDatabase, cfg: Config, tmp_path: Path, log_lines: list[str]  # noqa: F811
) -> None:
    """Every endpoint of section 7, `POST /api/key/record/watch` included, with markers in every
    text value that can be written in this step: title, description, comment body, short name,
    source, and the repository names of links and `fix`. `verification` is written only by a staff
    member (row 11 of section 6.4), whose key signs no event in this step (section 10)."""
    marker = "markerzq"
    marker_repo = f"{marker}-org/{marker}-repo"
    entries = [
        *key_entries(),
        {
            "id": MARKER_WATCHER,
            "kind": "ed25519",
            "principal": "service",
            "handle": "example-watcher",
            "team": TEAM,
            "device": "runner-marker",
            "public_key": public_text(MARKER_WATCHER_SEED),
            "scopes": ["record:read", "record:watch", "record:security"],
            "subjects": [marker_repo],
            "expires": fmt(NOW + 365 * 86400),
        },
    ]
    migrated(record_db)
    hub = start_hub(record_db, cfg, tmp_path, entries=entries, seeds={**SEEDS, MARKER_WATCHER: MARKER_WATCHER_SEED})
    work = new_id()
    issue = new_id()
    comment = new_id()
    try:
        hub.write(ALICE, "task", work, [*task_create(f"{marker} title"), ("description", None, f"{marker} text")])
        hub.write(ALICE, "task", issue, [
            ("kind", None, "security"), ("repository", None, marker_repo), ("severity", None, "low"),
            ("short_name", None, f"{marker}-name"), ("source", None, f"{marker}-source"), ("team", None, TEAM),
            ("title", None, marker), ("urgency", None, "1"), ("links", None, f"{marker_repo}#1"),
        ])
        hub.write(ALICE, "comment", comment, [("body", None, f"{marker} body"), ("task", None, work)])
        hub.write(MARKER_WATCHER, "task", issue, [("fix", None, f"{marker_repo}#1"), ("status", "open", "merged")], WATCH)
        assert task_body(hub, issue)["fix"] == f"{marker_repo}#1"
        hub.runtime.checkpoint()
        for key_id, target in (
            (ALICE, f"{OBJECT}?id={work}"), (ALICE, f"{EVENTS}?object={issue}"), (ALICE, f"{COMMENTS}?task={work}"),
            (ALICE, TASKS), (ALICE, HEAD), (ALICE, VERIFY), (ALICE, CHECKPOINTS), (ALICE, f"{EVENTS}?after=0"),
            (MARKER_WATCHER, f"{OBJECT}?id={issue}"), (MARKER_WATCHER, f"{EVENTS}?object={issue}"),
        ):
            assert hub.get(key_id, target)[1].status == 200, target
    finally:
        hub.close()
    text = "\n".join(log_lines)
    for forbidden in (marker, work, issue, comment, *record_db.secret_values()):
        assert forbidden not in text
    handles = {ALICE: "alice", MARKER_WATCHER: "example-watcher"}
    lines = request_lines(log_lines)
    assert {line.split(" ")[0] for line in lines} == set(handles)
    for line in lines:
        key_id, *fields = line.split(" ")
        assert handles[key_id] in key_id
        assert not set(fields) & set(handles.values()), line


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
    # A linked task in `done` (row 8 by the watcher) and one in `merged` (row 9 by the watcher, under
    # `completion` `verified`), each with an assignee.
    done = new_id()
    hub.write(ALICE, "task", done, [*task_create("Done"), ("assignee", None, "alice"), ("links", None, f"{REPO}#50")])
    hub.write(WATCHER, "task", done, [("status", "open", "done")], WATCH)
    assert task_body(hub, done)["status"] == "done"
    assert release(hub, pull_request=50).body == released(False, "task_not_ready", 50)
    merged = new_id()
    hub.write(
        ALICE,
        "task",
        merged,
        [*task_create("Merged"), ("assignee", None, "alice"), ("completion", None, "verified"), ("links", None, f"{REPO}#51")],
    )
    hub.write(WATCHER, "task", merged, [("status", "open", "merged")], WATCH)
    assert task_body(hub, merged)["status"] == "merged"
    assert release(hub, pull_request=51).body == released(False, "task_not_ready", 51)
    # A `dropped` task alone is no linked task.
    dropped = new_id()
    hub.write(ALICE, "task", dropped, [*task_create("Dropped"), ("links", None, f"{REPO}#52")])
    assert release(hub, pull_request=52).body == released(False, "task_not_ready", 52)
    hub.write(ALICE, "task", dropped, [("status", "open", "dropped")])
    assert release(hub, pull_request=52).body == released(False, "task_missing", 52)


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


def test_two_appends_of_one_instance_whose_steps_5_and_folds_interleave(hub: Hub, record_db: RecordDatabase) -> None:
    """Section 14, "the state lock": between the commit and step 5 of a first append, a second
    append of the same instance runs completely; its fold of step 2 folds the first event, its step
    5 applies its own, and the step 5 of the first append then changes nothing."""
    work = new_id()
    hub.write(ALICE, "task", work, task_create())
    inner: list[dict[str, Any]] = []

    def second_append() -> None:
        hub.points.append_after_commit = lambda: None
        inner.append(hub.write(BOB, "task", work, [("title", "Fix login", "Fix it")]))

    hub.points.append_after_commit = second_append
    first = hub.write(ALICE, "comment", new_id(), [("body", None, "a"), ("task", None, work)])
    (second,) = inner
    assert (first["position"], second["position"]) == (2, 3)
    head = hub.runtime.memory.head()
    assert head is not None and (head.position, head.record_hash) == (3, second["record_hash"])
    # The state equals the fold of the stored record.
    with writer_connection(record_db) as connection:
        stored = verify_stored(connection, hub.runtime.keys, {}, None)
    assert stored.failure is None
    assert hub.runtime.memory.state == stored.state
    # The next append and the next checkpoint attempt succeed.
    after = hub.write(ALICE, "task", work, [("title", "Fix it", "Fixed")])
    assert after["position"] == 4
    hub.runtime.checkpoint()
    assert counts(record_db) == (4, 1)
    hub.monotonic.now += 60
    assert hub.signed(ALICE, VERIFY, 200).body == body(
        {"verified": True, "position": 4, "record_hash": after["record_hash"], "failure": None, "hub_key": HUB_KEY_ID}
    )


def test_the_received_at_of_an_append_is_the_one_clock_reading_of_step_2(
    hub: Hub, record_db: RecordDatabase, cfg: Config, tmp_path: Path, log_lines: list[str]  # noqa: F811
) -> None:
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
    # A restart verifies the record and starts.
    log_lines.clear()
    restarted = start_hub(record_db, cfg, tmp_path, name="restarted")
    try:
        assert record_part(log_lines) == [f"INFO record verified 2 {answer.json()['record_hash']}"]
        assert restarted.signed(ALICE, HEAD, 200).json()["position"] == 2
    finally:
        restarted.close()


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


def _changed_entries(changes: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """The keys of record_hub_helpers.key_entries with the given members changed per key id."""
    return [{**entry, **changes.get(entry["id"], {})} for entry in key_entries()]


def test_an_event_of_a_revoked_key_and_of_an_expired_service_key_is_refused_at_the_request_checks(
    record_db: RecordDatabase, cfg: Config, tmp_path: Path, log_lines: list[str]  # noqa: F811
) -> None:
    entries = _changed_entries({BOB: {"revoked": fmt(NOW - 60)}, WATCHER: {"expires": fmt(NOW - 60)}})
    migrated(record_db)
    made = start_hub(record_db, cfg, tmp_path, entries=entries)
    try:
        work = new_id()
        made.write(ALICE, "task", work, [*task_create(), ("links", None, LINK)])
        log_lines.clear()
        _, answer = made.post(BOB, EVENTS, *made.event(BOB, "task", new_id(), task_create()))
        assert_refused(answer, 401, "key_revoked")
        _, answer = made.post(WATCHER, WATCH, *made.event(WATCHER, "task", work, [("status", "open", "done")]))
        assert_refused(answer, 401, "key_expired")
        # Refused at checks 1 to 8 of KEY-PROTOCOL.md section 5: no line of the record part.
        assert record_part(log_lines) == []
    finally:
        made.close()
    assert counts(record_db) == (1, 0)


def test_an_event_signed_before_the_revocation_and_sent_after_it_is_refused_and_not_in_the_record(
    record_db: RecordDatabase, cfg: Config, tmp_path: Path
) -> None:
    entries = _changed_entries({BOB: {"revoked": fmt(NOW + 30)}})
    migrated(record_db)
    made = start_hub(record_db, cfg, tmp_path, entries=entries)
    try:
        text, signature = made.event(BOB, "task", new_id(), task_create(), signed_at=NOW)
        made.clock.now = NOW + 60
        _, answer = made.post(BOB, EVENTS, text, signature)
        assert_refused(answer, 401, "key_revoked")
        page = made.signed(ALICE, f"{EVENTS}?after=0", 200).json()
        assert (page["events"], page["head"]) == ([], 0)
    finally:
        made.close()
    assert counts(record_db) == (0, 0)


def test_check_11_with_a_request_that_waits_for_the_advisory_lock_while_the_clock_passes_the_revocation(
    record_db: RecordDatabase, cfg: Config, tmp_path: Path
) -> None:
    entries = _changed_entries({BOB: {"revoked": fmt(NOW + 30)}})
    migrated(record_db)
    made = start_hub(record_db, cfg, tmp_path, entries=entries)
    try:
        text, signature = made.event(BOB, "task", new_id(), task_create())
        # The request checks run at NOW, while bob's key is valid; the append then waits for the
        # advisory lock, and the clock reading of step 2 of section 11.5 is after the revocation.
        with append_lock_held(record_db):
            waiting = Parallel(lambda: made.post(BOB, EVENTS, text, signature))
            wait_for_append_lock(record_db, 1)
            made.clock.now = NOW + 60
        ((wire, answer),) = waiting.results()
        assert_signed(wire, answer, 409, NOW + 60)
        assert answer.body == error("key_not_valid_at_receipt")
    finally:
        made.close()
    assert counts(record_db) == (0, 0)


def test_a_clock_jump_keeps_received_at_and_refuses_a_watcher_whose_expires_lies_before_it(
    record_db: RecordDatabase, cfg: Config, tmp_path: Path
) -> None:
    entries = _changed_entries({WATCHER: {"expires": fmt(NOW + 30)}})
    migrated(record_db)
    made = start_hub(record_db, cfg, tmp_path, entries=entries)
    try:
        work = new_id()
        made.write(ALICE, "task", work, [*task_create(), ("links", None, LINK)])
        # The clock jumps forward and back.
        made.clock.now = NOW + 60
        made.write(ALICE, "task", work, [("title", "Fix login", "Fix it")])
        made.clock.now = NOW
        text, signature = made.event(ALICE, "task", work, [("title", "Fix it", "Fix the login")], signed_at=NOW + 60)
        wire, answer = made.post(ALICE, EVENTS, text, signature)
        assert_signed(wire, answer, 201, NOW)
        assert answer.json()["received_at"] == fmt(NOW + 60)
        # The watcher's key is valid at the request checks (NOW) and expired at the kept
        # `received_at` (NOW + 60).
        assert made.refused(WATCHER, "task", work, [("status", "open", "done")], WATCH) == (
            409,
            error("key_not_valid_at_receipt"),
        )
        # Once the clock passes the kept value, `received_at` follows the clock again.
        made.clock.now = NOW + 90
        text, signature = made.event(ALICE, "task", work, [("title", "Fix the login", "Fixed")])
        wire, answer = made.post(ALICE, EVENTS, text, signature)
        assert_signed(wire, answer, 201, NOW + 90)
        assert answer.json()["received_at"] == fmt(NOW + 90)
    finally:
        made.close()


# --- the hub never connects with the owner role (section 14) ------------------------------------------


def test_the_hub_never_connects_with_the_owner_role(
    record_db: RecordDatabase, cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A start, every endpoint and a checkpoint attempt: every session the hub opens on the real
    server is a session of the writer role. The role of a session is the one the server reports
    when the session starts (`session_authorization`); the pool is emptied before every step, so
    that each step opens a session of its own."""
    migrated(record_db)
    real_connect = psycopg.connect
    sessions: list[tuple[Any, Any]] = []

    def observed(*args: Any, **kwargs: Any) -> Any:
        connection = real_connect(*args, **kwargs)
        sessions.append((kwargs.get("user"), connection.info.parameter_status("session_authorization")))
        return connection

    monkeypatch.setattr(psycopg, "connect", observed)
    made = start_hub(record_db, cfg, tmp_path)
    work = new_id()
    release_target = f"{RELEASE}?repository={REPO.replace('/', '%2F')}&pull_request=42"
    steps = [
        lambda: made.write(ALICE, "task", work, [*task_create(), ("links", None, LINK)]),
        lambda: made.write(WATCHER, "task", work, [("status", "open", "done")], WATCH),
        lambda: made.signed(ALICE, f"{EVENTS}?after=0", 200),
        lambda: made.signed(ALICE, f"{OBJECT}?id={work}", 200),
        lambda: made.signed(ALICE, TASKS, 200),
        lambda: made.signed(ALICE, f"{COMMENTS}?task={work}", 200),
        lambda: made.signed(ALICE, HEAD, 200),
        lambda: made.signed(ALICE, VERIFY, 200),
        lambda: made.signed(ALICE, CHECKPOINTS, 200),
        lambda: made.signed(GUARD, release_target, 200),
        made.runtime.checkpoint,
    ]
    try:
        for step in steps:
            made.runtime.pool.close()
            step()
    finally:
        made.close()
    assert len(sessions) == 1 + len(steps)
    assert set(sessions) == {(record_db.writer_role, record_db.writer_role)}
    assert record_db.owner_role != record_db.writer_role


# --- visibility: a changed handle and the `prev_hash` exception (sections 3 and 7.5) ------------------


def test_after_a_restart_with_a_changed_handle_the_stamps_show_the_new_handle_and_the_same_key(
    record_db: RecordDatabase, cfg: Config, tmp_path: Path
) -> None:
    migrated(record_db)
    work = new_id()
    first = start_hub(record_db, cfg, tmp_path, name="first")
    try:
        first.write(ALICE, "task", work, task_create())
        first.write(ALICE, "task", work, [("title", "Fix login", "Fix it")])
        before = task_body(first, work)
    finally:
        first.close()
    assert (before["created"]["handle"], before["changed"]["handle"]) == ("alice", "alice")
    renamed = tmp_path / "renamed"
    renamed.mkdir()
    entries = key_entries()
    for entry in entries:
        if entry["id"] == ALICE:
            entry["handle"] = "alicia"
    keys = renamed / "keys.yaml"
    keys.write_text(yaml.safe_dump({"keys": entries}, sort_keys=False), encoding="utf-8")
    teams = renamed / "teams.yaml"
    teams.write_text(
        yaml.safe_dump({"teams": {TEAM: {"members": ["alicia", "bob", "carol"]}, OTHER_TEAM: {"members": ["dave"]}}}),
        encoding="utf-8",
    )
    second = start_hub(record_db, cfg, renamed, keys_and_teams=(keys, teams), name="second")
    try:
        after = task_body(second, work)
    finally:
        second.close()
    assert after["created"] == {**before["created"], "handle": "alicia"}
    assert after["changed"] == {**before["changed"], "handle": "alicia"}
    assert (after["created"]["key"], after["changed"]["key"]) == (ALICE, ALICE)


def test_the_prev_hash_exception_a_visible_event_names_the_event_hash_of_a_withheld_one(hub: Hub) -> None:
    issue_text, issue_signature = hub.event(ALICE, "task", new_id(), security_create("login-bypass"))
    wire, answer = hub.post(ALICE, EVENTS, issue_text, issue_signature)
    assert_signed(wire, answer, 201, NOW)
    withheld = answer.json()
    work_text, work_signature = hub.event(ALICE, "task", new_id(), task_create())
    wire, answer = hub.post(ALICE, EVENTS, work_text, work_signature)
    assert_signed(wire, answer, 201, NOW)
    visible = answer.json()
    # bob has no `record:security`: the issue is withheld, the task of his teammate alice is not.
    page = hub.signed(BOB, f"{EVENTS}?after=0", 200)
    assert page.body == body(
        {
            "events": [
                {
                    "position": 1, "received_at": fmt(NOW), "event_hash": withheld["event_hash"],
                    "record_hash": withheld["record_hash"], "event": None, "signature": None,
                },
                {
                    "position": 2, "received_at": fmt(NOW), "event_hash": visible["event_hash"],
                    "record_hash": visible["record_hash"], "event": b64(work_text), "signature": b64(work_signature),
                },
            ],
            "next_after": None,
            "head": 2,
            "hub_key": HUB_KEY_ID,
        }
    )
    # The visible event's `prev_hash` is the event hash of the withheld one, which gives no more
    # than the members of section 7.4.
    assert parse_event(work_text).prev_hash == withheld["event_hash"]
    assert list(page.json()["events"][0]) == ["position", "received_at", "event_hash", "record_hash", "event", "signature"]


# --- one snapshot (section 12.1) ------------------------------------------------------------------------


def own_hub_key() -> str:
    """`ID=PUBLIC_KEY` of the hub of these tests (section 12.3)."""
    signer = HubSigner.from_seed_hex(HUB_KEY_ID, HUB_SEED_HEX)
    return f"{signer.key_id}={signer.public_key_text}"


def test_one_snapshot_in_the_verification_on_request_at_start_and_in_export(
    record_db: RecordDatabase,
    cfg: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    log_lines: list[str],  # noqa: F811
) -> None:
    migrated(record_db)
    first = start_hub(record_db, cfg, tmp_path, name="first")
    second = start_hub(record_db, cfg, tmp_path, name="second")
    work = new_id()
    late: list[dict[str, Any]] = []

    def append_and_checkpoint() -> None:
        # Another process appends an event through its endpoint and writes the checkpoint of the new
        # head; both commit between the read of the events and the read of the checkpoints.
        late.append(second.write(BOB, "comment", new_id(), [("body", None, "Late"), ("task", None, work)]))
        second.clock.now += 3600
        second.runtime.checkpoint()

    out = tmp_path / "export.jsonl"
    try:
        earlier = first.write(ALICE, "task", work, task_create())
        # The verification on request answers the head before the concurrent append.
        first.points.snapshot_between_reads = append_and_checkpoint
        assert first.signed(ALICE, VERIFY, 200).body == body(
            {"verified": True, "position": 1, "record_hash": earlier["record_hash"], "failure": None, "hub_key": HUB_KEY_ID}
        )
        first.points.snapshot_between_reads = lambda: None
        assert [item["position"] for item in late] == [2]
        # The verification at start verifies and builds the state of the head before it.
        points = Points()
        points.snapshot_between_reads = append_and_checkpoint
        log_lines.clear()
        third = start_hub(record_db, cfg, tmp_path, name="third", points=points)
        try:
            started = [line for line in record_part(log_lines) if " record verified " in line]
            assert started == [f"INFO record verified 2 {late[0]['record_hash']}"]
            points.snapshot_between_reads = lambda: None
            head = third.runtime.memory.head()
            assert head is not None and head.position == 2
            assert third.signed(ALICE, HEAD, 200).json()["position"] == 3
        finally:
            third.close()
        assert [item["position"] for item in late] == [2, 3]
        # `export` writes the head before the concurrent append, and its header matches its last
        # event line.
        original = commands.read_snapshot
        monkeypatch.setattr(commands, "read_snapshot", lambda reader: original(reader, append_and_checkpoint))
        exported = commands.run_export(["--out", str(out)], {"AGENT_CORE_RECORD_DATABASE": record_db.writer_url}, lambda: NOW)
        monkeypatch.setattr(commands, "read_snapshot", original)
        assert exported == (f"exported 3 {late[1]['record_hash']}", 0)
        assert [item["position"] for item in late] == [2, 3, 4]
    finally:
        first.close()
        second.close()
    lines = [json.loads(line) for line in out.read_bytes().splitlines()]
    event_lines = [line for line in lines[1:] if line["type"] == "event"]
    assert (lines[0]["position"], lines[0]["record_hash"]) == (event_lines[-1]["position"], event_lines[-1]["record_hash"])
    assert [line["position"] for line in lines[1:] if line["type"] == "checkpoint"] == [2, 3]
    keys, teams = tmp_path / "hub-keys.yaml", tmp_path / "hub-teams.yaml"
    verify_arguments = ["--keys", str(keys), "--teams", str(teams), "--in", str(out), "--hub-key", own_hub_key()]
    assert commands.run_verify(verify_arguments, {}) == (f"verified 3 {late[1]['record_hash']}", 0)
    recreate_database(record_db)
    migrated(record_db)
    environ = {
        "AGENT_CORE_RECORD_DATABASE": record_db.writer_url,
        "AGENT_CORE_KEYS": str(keys),
        "AGENT_CORE_TEAMS": str(teams),
    }
    restored = commands.run_restore(["--in", str(out), "--hub-key", own_hub_key()], environ, lambda: NOW)
    assert restored == (f"restored 3 {late[1]['record_hash']}", 0)


# --- removal and replacement at the end of the record (section 12.1) ----------------------------------


def test_a_removal_at_the_end_fails_the_verification_on_request_and_the_periodic_run_with_position_gap(
    hub: Hub, record_db: RecordDatabase, cfg: Config, tmp_path: Path, log_lines: list[str]  # noqa: F811
) -> None:
    other = start_hub(record_db, cfg, tmp_path, name="other")
    try:
        for index in range(2):
            hub.write(ALICE, "task", new_id(), task_create(f"Task {index}"))
        hub.runtime.checkpoint()
        for index in range(2, 4):
            hub.write(ALICE, "task", new_id(), task_create(f"Task {index}"))
        hub.clock.now += 3600
        hub.runtime.checkpoint()
        assert other.signed(ALICE, HEAD, 200).json()["position"] == 4
        # The last two events and every checkpoint that names them are removed.
        tamper(record_db, "record.event", "DELETE FROM record.event WHERE position >= 3")
        tamper(record_db, "record.checkpoint", "DELETE FROM record.checkpoint WHERE position >= 3")
        assert hub.signed(ALICE, VERIFY, 200).body == body(
            {"verified": False, "position": 3, "record_hash": None, "failure": "position_gap", "hub_key": HUB_KEY_ID}
        )
        other.monotonic.now += 600
        log_lines.clear()
        other.runtime.periodic()
        assert record_part(log_lines) == ["ERROR record verify failed 3 position_gap"]
        assert other.signed(ALICE, HEAD, 503).body == RECORD_UNVERIFIED
    finally:
        other.close()


def test_the_end_replaced_by_other_valid_events_fails_record_hash_mismatch_and_a_new_hub_serves(
    hub: Hub, record_db: RecordDatabase, cfg: Config, tmp_path: Path, log_lines: list[str]  # noqa: F811
) -> None:
    other = start_hub(record_db, cfg, tmp_path, name="other")
    try:
        written = [hub.write(ALICE, "task", new_id(), task_create(f"Task {index}")) for index in range(4)]
        hub.runtime.checkpoint()
        assert other.signed(ALICE, HEAD, 200).json()["position"] == 4
        tamper(record_db, "record.event", "DELETE FROM record.event WHERE position >= 3")
        tamper(record_db, "record.checkpoint", "DELETE FROM record.checkpoint WHERE position >= 3")
        # Two other events, validly signed and chained, take positions 3 and 4.
        third_text, third_signature = hub.event(BOB, "task", new_id(), task_create("Other three"))
        fourth_text, fourth_signature = hub.event(
            BOB, "task", new_id(), task_create("Other four"), prev_hash=event_hash(third_text)
        )
        third = stored_event(3, NOW, third_text, third_signature, written[1]["record_hash"])
        fourth = stored_event(4, NOW, fourth_text, fourth_signature, third.record_hash)
        insert_as_superuser(record_db, third)
        insert_as_superuser(record_db, fourth)
        assert fourth.record_hash != written[3]["record_hash"]
        assert hub.signed(ALICE, VERIFY, 200).body == body(
            {"verified": False, "position": 4, "record_hash": None, "failure": "record_hash_mismatch", "hub_key": HUB_KEY_ID}
        )
        other.monotonic.now += 600
        log_lines.clear()
        other.runtime.periodic()
        assert record_part(log_lines) == ["ERROR record verify failed 4 record_hash_mismatch"]
    finally:
        other.close()
    log_lines.clear()
    fresh = start_hub(record_db, cfg, tmp_path, name="fresh")
    try:
        assert record_part(log_lines) == [f"INFO record verified 4 {fourth.record_hash}"]
        assert fresh.signed(BOB, HEAD, 200).json()["position"] == 4
    finally:
        fresh.close()


# --- checkpoints: a pause across three full hours, byte order in `en_US` (sections 7.10 and 12.2) -----


def test_after_a_pause_across_three_full_hours_one_attempt_writes_one_checkpoint_at_its_time(
    hub: Hub, log_lines: list[str]  # noqa: F811
) -> None:
    hub.write(ALICE, "task", new_id(), task_create())
    hub.runtime.checkpoint()
    second = hub.write(ALICE, "task", new_id(), task_create())
    # Paused across three full hours, and resumed 30 minutes after the last of them.
    hub.clock.now += 3 * 3600 + 1800
    taken = hub.runtime.pool.taken
    log_lines.clear()
    hub.runtime.checkpoint()
    assert hub.runtime.pool.taken == taken + 1
    assert record_part(log_lines) == [f"INFO record checkpoint 2 {second['record_hash']}"]
    hub.runtime.checkpoint()
    assert hub.runtime.pool.taken == taken + 1
    page = hub.signed(ALICE, CHECKPOINTS, 200).json()
    assert [(entry["position"], entry["published_at"]) for entry in page["checkpoints"]] == [
        (2, fmt(hub.clock.now)),
        (1, fmt(NOW)),
    ]


def test_the_checkpoint_list_pages_in_byte_order_in_a_database_with_the_collation_en_us(
    record_db_en_us: RecordDatabase, cfg: Config, tmp_path: Path
) -> None:
    db = record_db_en_us
    migrated(db)
    with db.admin() as admin:
        # The collation of the database orders `hub1` before `hub-2`; byte order does the opposite.
        assert admin.execute("SELECT 'hub1' < 'hub-2', 'hub-2' < 'hub1' COLLATE \"C\"").fetchone() == (True, True)
    made = start_hub(db, cfg, tmp_path)
    try:
        written = [made.write(ALICE, "task", new_id(), task_create()) for _ in range(2)]
        for position, item in enumerate(written, start=1):
            for hub_key_id in ("hub-2", "hub1"):
                insert_checkpoint(db, Checkpoint(hub_key_id, position, item["record_hash"], NOW, bytes(64)))
        seen = []
        target = f"{CHECKPOINTS}?limit=1"
        while True:
            page = made.signed(ALICE, target, 200).json()
            seen += [(entry["position"], entry["signed_by"]) for entry in page["checkpoints"]]
            if page["next_before"] is None:
                break
            target = f"{CHECKPOINTS}?limit=1&before={page['next_before']}&before_key={page['next_before_key']}"
    finally:
        made.close()
    assert seen == [(2, "hub1"), (2, "hub-2"), (1, "hub1"), (1, "hub-2")]


# --- the largest valid `limit` (sections 7.4, 7.6, 7.7 and 7.10) ----------------------------------------


def test_the_largest_limit_of_each_list_is_valid(hub: Hub) -> None:
    work = new_id()
    hub.write(ALICE, "task", work, task_create())
    hub.write(ALICE, "comment", new_id(), [("body", None, "Seen"), ("task", None, work)])
    hub.runtime.checkpoint()
    for target, member, cursor in (
        (f"{EVENTS}?limit=100", "events", "next_after"),
        (f"{TASKS}?limit=200", "tasks", "next_before"),
        (f"{COMMENTS}?task={work}&limit=200", "comments", "next_before"),
        (f"{CHECKPOINTS}?limit=200", "checkpoints", "next_before"),
    ):
        page = hub.signed(ALICE, target, 200).json()
        assert len(page[member]) >= 1 and page[cursor] is None, target


# --- the periodic verification of a sound record (section 12.1) --------------------------------------


def test_a_sound_periodic_run_logs_nothing_and_a_run_longer_than_10_minutes_is_followed_at_once(
    hub: Hub, log_lines: list[str]  # noqa: F811
) -> None:
    hub.write(ALICE, "task", new_id(), task_create())
    runs: list[float] = []

    def long_run() -> None:
        runs.append(hub.monotonic.now)
        hub.monotonic.now += 700

    hub.points.snapshot_between_reads = long_run
    hub.monotonic.now += 600
    log_lines.clear()
    hub.runtime.periodic()
    assert len(runs) == 1
    # The run lasted 700 seconds: the next one follows at once, and that one is quick.
    hub.points.snapshot_between_reads = lambda: runs.append(hub.monotonic.now)
    hub.runtime.periodic()
    assert len(runs) == 2
    hub.monotonic.now += 599
    hub.runtime.periodic()
    assert len(runs) == 2
    hub.monotonic.now += 1
    hub.runtime.periodic()
    assert len(runs) == 3
    assert record_part(log_lines) == []
    assert not hub.runtime.unverified
    # Periodic runs do not count for the limit of the verification on request (section 7.9).
    assert hub.signed(ALICE, VERIFY, 200).json()["verified"] is True
