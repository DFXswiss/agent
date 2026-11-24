"""Session keys on the record endpoints (PASSKEY-PROTOCOL.md sections 7.1 and 8.3; RECORD-PROTOCOL.md
sections 3, 6.7, 7, 10 and 13).

Every request goes through the real application with a real session key, signed in with the software
authenticator of PASSKEY-PROTOCOL.md section 13.1, against the real PostgreSQL of the record tests
(AGENT_CORE_TEST_POSTGRES), with a fixed clock. A session key has the scopes, team and principal of
its passkey entry (check 9 of section 7.1, RECORD-PROTOCOL.md section 10), and it never writes the
record in this build step: its `Agent-Key` has the session key id form, which no `actor_key_id`
passes (checks 3 and 4 of RECORD-PROTOCOL.md section 6.7).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric import ec

from agent_core.config import Config
from agent_core.record.event import Change, build_event_text
from tests.conftest import HUB_KEY_ID, RecordDatabase
from tests.passkey_hub import (
    BEGIN,
    FINISH,
    ISSUE_SECONDS,
    ORIGIN,
    SESSION_END,
    Session,
    anonymous_wire,
    b64,
    new_passkey,
    session_signature,
    signed_wire,
    staff_entry,
)
from tests.record_helpers import counts, migrated
from tests.record_hub_helpers import (
    ALICE,
    DAVE,
    JSON,
    OTHER_TEAM,
    Hub,
    append_body,
    encode,
    error,
    key_entries,
    new_id,
    security_create,
    start_hub,
    task_create,
)
from tests.test_signed_requests import NOW, Answer, Wire, assert_refused, assert_signed, fmt
from tests.test_signed_requests import log_lines  # noqa: F401 - a fixture, used by name
from tests.webauthn_authenticator import SoftAuthenticator, point_of

EVENTS = "/api/key/record/events"
WATCH = "/api/key/record/watch"
OBJECT = "/api/key/record/object"
TASKS = "/api/key/record/tasks"
COMMENTS = "/api/key/record/comments"
HEAD = "/api/key/record/head"
VERIFY = "/api/key/record/verify"
CHECKPOINTS = "/api/key/record/checkpoints"

# The passkeys of the keys file, each with the seed of its authenticator's randomness.
ERIN = "erin-passkey-1"  # team example, record:read and record:write
FRANK = "frank-passkey-1"  # team example, also record:security
GINA = "gina-passkey-1"  # team example, keys:invite only: no record scope
OLGA = "olga-passkey-1"  # team ops, record:read
HANK = "hank-passkey-1"  # team example, record:read, revoked one hour after the start
SEEDS = {ERIN: 61, FRANK: 62, GINA: 63, OLGA: 64, HANK: 65}
REVOKED_AT = NOW + 3600


def body_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def passkey_entries(passkeys: dict[str, SoftAuthenticator]) -> list[dict[str, Any]]:
    return [
        staff_entry(ERIN, "erin", passkeys[ERIN], ["record:read", "record:write"]),
        staff_entry(FRANK, "frank", passkeys[FRANK], ["record:read", "record:write", "record:security"]),
        staff_entry(GINA, "gina", passkeys[GINA], ["keys:invite"]),
        staff_entry(OLGA, "olga", passkeys[OLGA], ["record:read"], team=OTHER_TEAM),
        staff_entry(HANK, "hank", passkeys[HANK], ["record:read"], revoked=fmt(REVOKED_AT)),
    ]


def write_files(directory: Path, passkeys: dict[str, SoftAuthenticator]) -> tuple[Path, Path]:
    """The keys file of the record hub tests with the passkeys above, and a teams file in which every
    staff handle is a member of its team."""
    teams = directory / "passkey-record-teams.yaml"
    roster = {
        "teams": {
            "example": {"members": ["alice", "bob", "carol", "erin", "frank", "gina", "hank"]},
            OTHER_TEAM: {"members": ["dave", "olga"]},
        }
    }
    teams.write_text(yaml.safe_dump(roster), encoding="utf-8")
    keys = directory / "passkey-record-keys.yaml"
    entries = [*key_entries(), *passkey_entries(passkeys)]
    keys.write_text(yaml.safe_dump({"keys": entries}, sort_keys=False), encoding="utf-8")
    return keys, teams


@pytest.fixture
def passkeys() -> dict[str, SoftAuthenticator]:
    return {key_id: new_passkey(seed) for key_id, seed in SEEDS.items()}


@pytest.fixture
def record_hub(
    record_db: RecordDatabase, cfg: Config, tmp_path: Path, passkeys: dict[str, SoftAuthenticator]
) -> Iterator[Hub]:
    migrated(record_db)
    made = start_hub(record_db, cfg, tmp_path, keys_and_teams=write_files(tmp_path, passkeys))
    try:
        yield made
    finally:
        made.close()


def sign_in(hub: Hub, passkey: SoftAuthenticator) -> Session:
    """Begin and finish (PASSKEY-PROTOCOL.md sections 6.1 and 6.3) through the application."""
    private = ec.generate_private_key(ec.SECP256R1())
    begin_body = json.dumps({"session_public_key": b64(point_of(private))}).encode("utf-8")
    begun = hub.send(anonymous_wire(BEGIN, begin_body))
    assert begun.status == 200, begun.body
    options = begun.json()
    asserted = passkey.get({"challenge": options["challenge"], "rp_id": options["rp_id"]}, ORIGIN)
    finish = hub.send(anonymous_wire(FINISH, json.dumps(asserted.body(options["sign_in"])).encode("utf-8")))
    assert finish.status == 200, finish.body
    answer = finish.json()
    return Session(id=answer["session_key"], private=private, public=point_of(private), answer=answer)


def session_wire(hub: Hub, session: Session, target: str, *, method: str = "GET", body: bytes = b"") -> Wire:
    """A request signed by the session key at the hub clock; a `POST` carries JSON."""
    return signed_wire(
        session.id,
        lambda text: session_signature(session.private, text),
        method=method,
        target=target,
        body=body,
        time_text=fmt(hub.clock.now),
        extra_headers=JSON if method == "POST" else (),
    )


def signed(hub: Hub, session: Session, target: str, status: int, **options: Any) -> Answer:
    """A request of the session key whose answer carries the hub signature and the given status."""
    wire = session_wire(hub, session, target, **options)
    answer = hub.send(wire)
    assert_signed(wire, answer, status, hub.clock.now)
    return answer


def event_text(actor_key_id: str, *, certificate: bytes | None = None) -> bytes:
    """A task create of the form of RECORD-PROTOCOL.md section 4.2 in the name of `actor_key_id`."""
    return build_event_text(
        event_id=new_id(),
        object_type="task",
        object_id=new_id(),
        actor_key_id=actor_key_id,
        signed_at=NOW,
        prev_hash="-",
        changes=[Change(name, encode(old), encode(new)) for name, old, new in task_create()],
        key_certificate=certificate,
    )


def post_event(hub: Hub, session: Session, text: bytes) -> tuple[Wire, Answer]:
    """An append through `POST /api/key/record/events`, the event signed by the session key."""
    raw = append_body(text, session_signature(session.private, text))
    wire = session_wire(hub, session, EVENTS, method="POST", body=raw)
    return wire, hub.send(wire)


def lines_of(lines: list[str], session: Session) -> list[str]:
    """Every log line that names the session key, as `logger LEVEL text`."""
    return [line for line in lines if session.id in line]


# --- reading, with and without the scope the route needs ------------------------------------------


def test_a_session_key_with_record_read_reads_every_record_endpoint(
    record_hub: Hub, passkeys: dict[str, SoftAuthenticator]
) -> None:
    work = new_id()
    written = record_hub.write(ALICE, "task", work, task_create())
    session = sign_in(record_hub, passkeys[ERIN])

    # RECORD-PROTOCOL.md section 7.8: the caller's own chain is the chain of the session key
    # (section 10), which has no event; `key` is the request's `Agent-Key`.
    head = signed(record_hub, session, HEAD, 200)
    assert head.body == body_bytes(
        {
            "position": 1,
            "record_hash": written["record_hash"],
            "key": session.id,
            "key_position": None,
            "key_event_hash": None,
            "key_signed_at": None,
            "hub_key": HUB_KEY_ID,
        }
    )
    shown = signed(record_hub, session, f"{OBJECT}?id={work}", 200).json()["object"]
    assert (shown["id"], shown["created"]["key"], shown["created"]["handle"]) == (work, ALICE, "alice")
    assert [task["id"] for task in signed(record_hub, session, TASKS, 200).json()["tasks"]] == [work]
    events = signed(record_hub, session, EVENTS, 200).json()
    assert [(item["position"], item["event"] is not None) for item in events["events"]] == [(1, True)]
    comments = signed(record_hub, session, f"{COMMENTS}?task={work}", 200)
    assert comments.body == body_bytes({"comments": [], "next_before": None, "hub_key": HUB_KEY_ID})
    verified = signed(record_hub, session, VERIFY, 200)
    assert verified.body == body_bytes(
        {"verified": True, "position": 1, "record_hash": written["record_hash"], "failure": None, "hub_key": HUB_KEY_ID}
    )
    checkpoints = signed(record_hub, session, CHECKPOINTS, 200).json()
    assert checkpoints["hub_key"] == HUB_KEY_ID and isinstance(checkpoints["checkpoints"], list)


READ_TARGETS = [
    EVENTS,
    f"{OBJECT}?id=5d1c2b3a-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
    TASKS,
    f"{COMMENTS}?task=5d1c2b3a-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
    HEAD,
    VERIFY,
    CHECKPOINTS,
]


@pytest.mark.parametrize("target", READ_TARGETS)
def test_a_session_key_whose_passkey_lacks_record_read_gets_scope_missing(
    record_hub: Hub, passkeys: dict[str, SoftAuthenticator], target: str
) -> None:
    session = sign_in(record_hub, passkeys[GINA])
    assert signed(record_hub, session, target, 403).body == error("scope_missing")


@pytest.mark.parametrize(("passkey_id", "path"), [(GINA, EVENTS), (ERIN, WATCH)])
def test_a_session_key_whose_passkey_lacks_the_append_scope_gets_scope_missing(
    record_hub: Hub, record_db: RecordDatabase, passkeys: dict[str, SoftAuthenticator], passkey_id: str, path: str
) -> None:
    # `record:write` for the events endpoint, `record:watch` (only a `service` key can hold it) for
    # the watch endpoint; check 9 runs before the body is read.
    session = sign_in(record_hub, passkeys[passkey_id])
    answer = signed(record_hub, session, path, 403, method="POST", body=append_body(event_text(ERIN), bytes(64)))
    assert answer.body == error("scope_missing")
    assert counts(record_db)[0] == 0


# --- what the visibility rules of RECORD-PROTOCOL.md section 3 show to the passkey's handle -------


def _story(hub: Hub) -> tuple[str, str, str]:
    """A work task and a security issue of team example by alice, then a work task of team ops by
    dave: positions 1, 2 and 3."""
    work, issue, other = new_id(), new_id(), new_id()
    hub.write(ALICE, "task", work, task_create())
    hub.write(ALICE, "task", issue, security_create("login-bypass"))
    hub.write(DAVE, "task", other, task_create("Ops work", team=OTHER_TEAM))
    return work, issue, other


def _visible(hub: Hub, session: Session) -> tuple[list[str], list[tuple[int, bool]]]:
    tasks = [task["id"] for task in signed(hub, session, TASKS, 200).json()["tasks"]]
    events = signed(hub, session, EVENTS, 200).json()
    assert events["head"] == 3
    return tasks, [(item["position"], item["event"] is not None) for item in events["events"]]


def test_a_session_key_sees_the_work_tasks_of_its_passkeys_team_and_nothing_else(
    record_hub: Hub, passkeys: dict[str, SoftAuthenticator]
) -> None:
    work, issue, other = _story(record_hub)
    session = sign_in(record_hub, passkeys[ERIN])
    assert _visible(record_hub, session) == ([work], [(1, True), (2, False), (3, False)])
    for hidden in (issue, other):
        assert signed(record_hub, session, f"{OBJECT}?id={hidden}", 404).body == error("object_not_found")
        assert signed(record_hub, session, f"{COMMENTS}?task={hidden}", 404).body == error("object_not_found")
        assert signed(record_hub, session, f"{EVENTS}?object={hidden}", 404).body == error("object_not_found")
    assert signed(record_hub, session, f"{TASKS}?kind=security", 200).json()["tasks"] == []


def test_a_session_key_whose_passkey_holds_record_security_sees_the_security_issue(
    record_hub: Hub, passkeys: dict[str, SoftAuthenticator]
) -> None:
    work, issue, other = _story(record_hub)
    session = sign_in(record_hub, passkeys[FRANK])
    assert _visible(record_hub, session) == ([issue, work], [(1, True), (2, True), (3, False)])
    assert signed(record_hub, session, f"{OBJECT}?id={issue}", 200).json()["object"]["kind"] == "security"
    assert signed(record_hub, session, f"{OBJECT}?id={other}", 404).body == error("object_not_found")


def test_a_session_key_of_a_passkey_of_another_team_sees_only_that_team(
    record_hub: Hub, passkeys: dict[str, SoftAuthenticator]
) -> None:
    work, issue, other = _story(record_hub)
    session = sign_in(record_hub, passkeys[OLGA])
    assert _visible(record_hub, session) == ([other], [(1, False), (2, False), (3, True)])
    assert signed(record_hub, session, f"{OBJECT}?id={work}", 404).body == error("object_not_found")


# --- a write attempt and its exact refusal ---------------------------------------------------------


def test_an_event_in_the_name_of_the_passkey_is_actor_mismatch(
    record_hub: Hub, record_db: RecordDatabase, passkeys: dict[str, SoftAuthenticator]
) -> None:
    # Check 4 of section 6.7: `actor_key_id` equals the request's `Agent-Key`, which is the session
    # key id, not the passkey id; the signature of check 6 is never looked at.
    session = sign_in(record_hub, passkeys[ERIN])
    wire, answer = post_event(record_hub, session, event_text(ERIN))
    assert_signed(wire, answer, 403, record_hub.clock.now)
    assert answer.body == error("actor_mismatch")
    assert counts(record_db)[0] == 0


def test_an_event_in_the_name_of_another_keys_file_key_is_actor_mismatch(
    record_hub: Hub, record_db: RecordDatabase, passkeys: dict[str, SoftAuthenticator]
) -> None:
    session = sign_in(record_hub, passkeys[ERIN])
    wire, answer = post_event(record_hub, session, record_hub.event(ALICE, "task", new_id(), task_create())[0])
    assert_signed(wire, answer, 403, record_hub.clock.now)
    assert answer.body == error("actor_mismatch")
    assert counts(record_db)[0] == 0


def test_an_event_with_a_certificate_in_the_name_of_the_passkey_is_actor_mismatch_before_check_5(
    record_hub: Hub, record_db: RecordDatabase, passkeys: dict[str, SoftAuthenticator]
) -> None:
    session = sign_in(record_hub, passkeys[ERIN])
    wire, answer = post_event(record_hub, session, event_text(ERIN, certificate=b"certificate"))
    assert_signed(wire, answer, 403, record_hub.clock.now)
    assert answer.body == error("actor_mismatch")
    assert counts(record_db)[0] == 0


def test_an_event_in_the_name_of_the_session_key_is_event_invalid(
    record_hub: Hub, record_db: RecordDatabase, passkeys: dict[str, SoftAuthenticator]
) -> None:
    # Check 3 of section 6.7: the session key id contains a `.`, which the `id` rule of an
    # `actor_key_id` does not allow (PASSKEY-PROTOCOL.md section 2).
    session = sign_in(record_hub, passkeys[ERIN])
    text = event_text(ALICE).replace(ALICE.encode("ascii"), session.id.encode("ascii"))
    assert session.id.encode("ascii") in text
    wire, answer = post_event(record_hub, session, text)
    assert_signed(wire, answer, 400, record_hub.clock.now)
    assert answer.body == error("event_invalid")
    assert counts(record_db)[0] == 0


# --- the log lines ----------------------------------------------------------------------------------


def test_a_request_of_a_session_key_to_the_record_writes_the_record_line_and_the_session_line(
    record_hub: Hub, passkeys: dict[str, SoftAuthenticator], log_lines: list[str]  # noqa: F811
) -> None:
    # RECORD-PROTOCOL.md section 13 (lines 2059 to 2061): exactly one line of the record part,
    # `record request <key id> …`, with the `Agent-Key` as key id; the rule that it replaces the
    # line of KEY-PROTOCOL.md section 5 holds "for a `service` key" (lines 2084 to 2086), and the
    # passkey's principal is `staff`. PASSKEY-PROTOCOL.md section 7.1 (lines 817 to 819): exactly
    # one line `key request <session key id> <passkey id> …` for every request of a session key
    # that passed check 8. The record line has no field for the passkey id, so the request writes
    # both lines, one of each kind.
    session = sign_in(record_hub, passkeys[ERIN])
    head = session_wire(record_hub, session, HEAD)
    assert record_hub.send(head).status == 200
    text = event_text(ERIN)
    _, refused = post_event(record_hub, session, text)
    assert refused.status == 403
    assert record_hub.send(session_wire(record_hub, session, f"{TASKS}?x=1")).status == 400
    # A replay is refused at check 8 and writes no line.
    assert record_hub.send(head).status == 401
    assert lines_of(log_lines, session) == [
        f"uvicorn.error INFO record request {session.id} GET {HEAD} 200 ok",
        f"uvicorn.error INFO key request {session.id} {ERIN} GET {HEAD} 200",
        f"uvicorn.error INFO record request {session.id} POST {EVENTS} 403 actor_mismatch",
        f"uvicorn.error INFO key request {session.id} {ERIN} POST {EVENTS} 403",
        f"uvicorn.error INFO record request {session.id} GET {TASKS} 400 query_invalid",
        f"uvicorn.error INFO key request {session.id} {ERIN} GET {TASKS} 400",
    ]
    for line in log_lines:
        assert b64(text) not in line
        assert head.value(b"agent-signature").decode("ascii") not in line


# --- an ended, expired and revoked session key ------------------------------------------------------


ROUTES = [("GET", HEAD), ("POST", EVENTS)]


def _attempt(hub: Hub, session: Session, method: str, path: str) -> Answer:
    body = append_body(event_text(ERIN), bytes(64)) if method == "POST" else b""
    return hub.send(session_wire(hub, session, path, method=method, body=body))


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_an_ended_session_key_is_key_unknown_on_a_record_route(
    record_hub: Hub,
    record_db: RecordDatabase,
    passkeys: dict[str, SoftAuthenticator],
    log_lines: list[str],  # noqa: F811
    method: str,
    path: str,
) -> None:
    session = sign_in(record_hub, passkeys[ERIN])
    signed(record_hub, session, SESSION_END, 200, method="POST")
    assert_refused(_attempt(record_hub, session, method, path), 401, "key_unknown")
    assert counts(record_db)[0] == 0
    assert [line for line in lines_of(log_lines, session) if " record request " in line] == []


@pytest.mark.parametrize("offset", [0, 1])
@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_a_session_key_at_and_after_its_expiry_is_key_expired_on_a_record_route(
    record_hub: Hub, record_db: RecordDatabase, passkeys: dict[str, SoftAuthenticator], method: str, path: str, offset: int
) -> None:
    session = sign_in(record_hub, passkeys[ERIN])
    record_hub.clock.now = NOW + ISSUE_SECONDS - 1
    signed(record_hub, session, HEAD, 200)
    record_hub.clock.now = NOW + ISSUE_SECONDS + offset
    assert_refused(_attempt(record_hub, session, method, path), 401, "key_expired")
    assert counts(record_db)[0] == 0


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_a_session_key_whose_passkey_is_revoked_is_key_revoked_on_a_record_route(
    record_hub: Hub, record_db: RecordDatabase, passkeys: dict[str, SoftAuthenticator], method: str, path: str
) -> None:
    session = sign_in(record_hub, passkeys[HANK])
    signed(record_hub, session, HEAD, 200)
    record_hub.clock.now = REVOKED_AT
    assert_refused(_attempt(record_hub, session, method, path), 401, "key_revoked")
    assert counts(record_db)[0] == 0
