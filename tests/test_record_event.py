"""The event of the team record (RECORD-PROTOCOL.md sections 4 and 14, "The event").

Pure functions: these tests need no database. The hashes of the examples of section 4.2 are
compared with fixed values and, as a second assertion, with a recomputation in this file that uses
`hashlib` instead of the library's helper.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timezone
from typing import Callable

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_core.keys import KeyEntry, KeyRing
from agent_core.record.event import (
    MAX_EVENT_BYTES,
    MAX_TIME,
    MIN_TIME,
    Change,
    ChainHead,
    Event,
    RecordRefusal,
    build_event_text,
    chain_head,
    check_chain,
    check_signed_at,
    decode_value_token,
    encode_value_token,
    event_hash,
    next_received_at,
    parse_event,
    record_hash,
    signature_valid,
    time_text,
    valid_hash,
    valid_uuid,
    verify_event_signature,
)

EVENT_ID = "0b6f3c1e-8d2a-4c1b-9f3e-2a7d5c9e1f40"
OBJECT_ID = "5d1c2b3a-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
ACTOR = "example-staff-laptop-1"

# Fixed values of the examples of section 4.2: the event hashes (section 4.4) of the two example
# events, and the record hashes (section 4.6) of the two events at positions 1 and 2 with the
# `received_at` of the story of section 4.2 (10:00:01 and 10:05:01).
EXAMPLE_1_EVENT_HASH = "ee3e09fbb4e47ff826cc7623039e964908379d62d0e8cf5881356c4a618e48bf"
EXAMPLE_2_EVENT_HASH = "634af2c5c36e9191ac78ccc1ef1ff285c71b102853d16772806f17a83e80f1e4"
RECORD_HASH_1 = "6c73dcb14650019ae5c8769d33123a169253418a62ec13fc0e775a7933fb447f"
RECORD_HASH_2 = "f380dba999261e59b4fbc1b0adc7c3fbd0e3b2cffbc10f6a99a8ef20a04c4f9f"
RECEIVED_AT_1 = "2026-11-17T10:00:01Z"
RECEIVED_AT_2 = "2026-11-17T10:05:01Z"

# The two example events of section 4.2, written out as the specification shows them. The second
# carries the fixed event hash of the first in place of `<event hash of the first event>`.
EXAMPLE_1_LINES = (
    "agent-event-v1",
    EVENT_ID,
    "task",
    OBJECT_ID,
    ACTOR,
    "2026-11-17T10:00:00Z",
    "-",
    "-",
    "3",
    "kind",
    "-",
    "=d29yaw",
    "team",
    "-",
    "=ZXhhbXBsZQ",
    "title",
    "-",
    "=Rml4IGxvZ2lu",
)
EXAMPLE_1 = "\n".join(EXAMPLE_1_LINES).encode("ascii")
EXAMPLE_1_HASH = hashlib.sha256(EXAMPLE_1).hexdigest()
EXAMPLE_2 = "\n".join(
    [
        "agent-event-v1",
        "6a0e9d4c-2b7f-4e1a-8c3d-5f2b1a0e9d8c",
        "task",
        OBJECT_ID,
        ACTOR,
        "2026-11-17T10:05:00Z",
        EXAMPLE_1_EVENT_HASH,
        "-",
        "2",
        "assignee",
        "-",
        "=ZXhhbXBsZS1zdGFmZg",
        "status",
        "=b3Blbg",
        "=aW5fcHJvZ3Jlc3M",
    ]
).encode("ascii")


def seconds(text: str) -> int:
    return int(datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def replaced(index: int, value: str) -> bytes:
    lines = list(EXAMPLE_1_LINES)
    lines[index] = value
    return "\n".join(lines).encode("utf-8")


def joined(lines: list[str] | tuple[str, ...], separator: str = "\n") -> bytes:
    return separator.join(lines).encode("utf-8")


def swapped_blocks() -> bytes:
    lines = list(EXAMPLE_1_LINES)
    lines[9:12], lines[12:15] = lines[12:15], lines[9:12]
    return joined(lines)


def independent_record_hash(position: int, received_at: str, event_hash_hex: str, previous: str) -> str:
    text = f"agent-record-v1\n{position}\n{received_at}\n{event_hash_hex}\n{previous}"
    return hashlib.sha256(text.encode("ascii")).hexdigest()


# --- accepted events and their hashes ---------------------------------------------------------


def test_the_first_example_of_section_4_2_is_accepted() -> None:
    event = parse_event(EXAMPLE_1)
    assert event.text == EXAMPLE_1
    assert event.event_hash == EXAMPLE_1_HASH
    assert event.event_id == EVENT_ID
    assert event.object_type == "task"
    assert event.object_id == OBJECT_ID
    assert event.actor_key_id == ACTOR
    assert event.signed_at == seconds("2026-11-17T10:00:00Z")
    assert event.prev_hash == "-"
    assert event.key_certificate is None
    assert event.changes == (
        Change("kind", None, b"work"),
        Change("team", None, b"example"),
        Change("title", None, b"Fix login"),
    )


def test_the_second_example_chains_to_the_first() -> None:
    first = parse_event(EXAMPLE_1)
    second = parse_event(EXAMPLE_2)
    assert second.prev_hash == first.event_hash
    assert second.changes == (
        Change("assignee", None, b"example-staff"),
        Change("status", b"open", b"in_progress"),
    )
    check_chain(chain_head(first), second)
    check_signed_at(chain_head(first), second)


def test_the_event_hash_is_the_sha256_of_the_text() -> None:
    # Fixed vectors of SHA-256 (FIPS 180-2), so that the helper is not only compared with itself.
    assert event_hash(b"abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert event_hash(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert event_hash(EXAMPLE_1) == EXAMPLE_1_HASH
    assert event_hash(EXAMPLE_2) == hashlib.sha256(EXAMPLE_2).hexdigest()
    assert parse_event(EXAMPLE_2).event_hash == hashlib.sha256(EXAMPLE_2).hexdigest()


def test_the_examples_of_section_4_2_have_their_fixed_event_hashes() -> None:
    assert len(EXAMPLE_1) == 199
    assert len(EXAMPLE_2) == 269
    assert event_hash(EXAMPLE_1) == EXAMPLE_1_EVENT_HASH
    assert parse_event(EXAMPLE_1).event_hash == EXAMPLE_1_EVENT_HASH
    assert event_hash(EXAMPLE_2) == EXAMPLE_2_EVENT_HASH
    assert parse_event(EXAMPLE_2).event_hash == EXAMPLE_2_EVENT_HASH
    # Second assertion: the same values through `hashlib`, not through the library.
    assert hashlib.sha256(EXAMPLE_1).hexdigest() == EXAMPLE_1_EVENT_HASH
    assert EXAMPLE_1_HASH == EXAMPLE_1_EVENT_HASH
    assert hashlib.sha256(EXAMPLE_2).hexdigest() == EXAMPLE_2_EVENT_HASH


def test_the_examples_of_section_4_2_have_their_fixed_record_hashes() -> None:
    position_1 = record_hash(1, seconds(RECEIVED_AT_1), EXAMPLE_1_EVENT_HASH, None)
    assert position_1 == RECORD_HASH_1
    position_2 = record_hash(2, seconds(RECEIVED_AT_2), EXAMPLE_2_EVENT_HASH, RECORD_HASH_1)
    assert position_2 == RECORD_HASH_2
    # Second assertion: the same values through `hashlib`, not through the library.
    assert independent_record_hash(1, RECEIVED_AT_1, EXAMPLE_1_EVENT_HASH, "-") == RECORD_HASH_1
    assert independent_record_hash(2, RECEIVED_AT_2, EXAMPLE_2_EVENT_HASH, RECORD_HASH_1) == RECORD_HASH_2


def test_the_record_hash_chains_over_positions() -> None:
    first_hash = event_hash(EXAMPLE_1)
    second_hash = event_hash(EXAMPLE_2)
    position_1 = record_hash(1, seconds("2026-11-17T10:00:01Z"), first_hash, None)
    assert position_1 == independent_record_hash(1, "2026-11-17T10:00:01Z", first_hash, "-")
    position_2 = record_hash(2, seconds("2026-11-17T10:05:01Z"), second_hash, position_1)
    assert position_2 == independent_record_hash(2, "2026-11-17T10:05:01Z", second_hash, position_1)
    assert position_2 != position_1
    # Every input is covered: another time or another previous hash gives another record hash.
    assert record_hash(2, seconds("2026-11-17T10:05:02Z"), second_hash, position_1) != position_2
    assert record_hash(2, seconds("2026-11-17T10:05:01Z"), second_hash, first_hash) != position_2


def test_the_largest_position_has_a_record_hash() -> None:
    largest = 9223372036854775807
    value = record_hash(largest, 0, EXAMPLE_1_HASH, EXAMPLE_1_HASH)
    assert value == independent_record_hash(largest, "1970-01-01T00:00:00Z", EXAMPLE_1_HASH, EXAMPLE_1_HASH)


@pytest.mark.parametrize(
    ("position", "event_hash_hex", "previous"),
    [
        (0, EXAMPLE_1_HASH, None),
        (9223372036854775808, EXAMPLE_1_HASH, EXAMPLE_1_HASH),
        (1, EXAMPLE_1_HASH, EXAMPLE_1_HASH),
        (2, EXAMPLE_1_HASH, None),
        (1, EXAMPLE_1_HASH.upper(), None),
        (1, "-", None),
        (2, EXAMPLE_1_HASH, EXAMPLE_1_HASH[:63]),
        (True, EXAMPLE_1_HASH, None),
        (1.0, EXAMPLE_1_HASH, None),
        ("1", EXAMPLE_1_HASH, None),
    ],
    ids=[
        "position 0",
        "position above the largest",
        "previous at position 1",
        "no previous at position 2",
        "event hash in uppercase",
        "event hash -",
        "previous of 63 characters",
        "position True",
        "position as float",
        "position as text",
    ],
)
def test_record_hash_refuses_inputs_outside_section_4_6(
    position: object, event_hash_hex: str, previous: str | None
) -> None:
    with pytest.raises(ValueError):
        record_hash(position, 0, event_hash_hex, previous)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "received_at",
    [MIN_TIME - 1, MAX_TIME + 1, 10**30, -(10**30), True, 0.0, "0", None],
    ids=["before year 1", "after year 9999", "huge", "huge negative", "True", "float", "text", "None"],
)
def test_record_hash_refuses_a_received_at_that_is_not_a_time(received_at: object) -> None:
    with pytest.raises(ValueError):
        record_hash(1, received_at, EXAMPLE_1_HASH, None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (0, "1970-01-01T00:00:00Z"),
        (-1, "1969-12-31T23:59:59Z"),
        (MIN_TIME, "0001-01-01T00:00:00Z"),
        (MAX_TIME, "9999-12-31T23:59:59Z"),
        (-31000000000, "0987-08-25T16:53:20Z"),
        (seconds("2026-11-17T10:00:01Z"), "2026-11-17T10:00:01Z"),
    ],
)
def test_time_text_has_a_year_of_four_digits(value: int, text: str) -> None:
    assert time_text(value) == text
    # The same text that `datetime` gives for the same moment, which section 2 parses back.
    assert seconds(text) == value


def test_a_received_at_before_the_year_1000_is_hashed_with_four_digits() -> None:
    received_at = seconds("0987-08-25T16:53:20Z")
    assert record_hash(1, received_at, EXAMPLE_1_HASH, None) == independent_record_hash(
        1, "0987-08-25T16:53:20Z", EXAMPLE_1_HASH, "-"
    )
    assert record_hash(1, MIN_TIME, EXAMPLE_1_HASH, None) == independent_record_hash(
        1, "0001-01-01T00:00:00Z", EXAMPLE_1_HASH, "-"
    )
    assert record_hash(1, MAX_TIME, EXAMPLE_1_HASH, None) == independent_record_hash(
        1, "9999-12-31T23:59:59Z", EXAMPLE_1_HASH, "-"
    )


def test_received_at_never_decreases() -> None:
    assert next_received_at(100, None) == 100
    assert next_received_at(100, 99) == 100
    assert next_received_at(100, 100) == 100
    assert next_received_at(100, 160) == 160


# --- the form of section 4.2 ------------------------------------------------------------------


def sized_event(total: int) -> bytes:
    """An event of the form of section 4.2 with exactly `total` bytes, through two long values."""
    head = list(EXAMPLE_1_LINES[:8]) + ["4"]
    for extra in range(1, 4):
        for size in range(max(1, (total * 3) // 4 - 400), (total * 3) // 4 + 1):
            lines = head + [
                "description", "-", "=" + b64(b"d" * extra),
                "kind", "-", "=d29yaw",
                "team", "-", "=ZXhhbXBsZQ",
                "title", "-", "=" + b64(b"t" * size),
            ]  # fmt: skip
            text = joined(lines)
            if len(text) == total:
                return text
    raise AssertionError(f"no event of {total} bytes")


def test_an_event_text_of_exactly_65536_bytes_has_the_form() -> None:
    text = sized_event(MAX_EVENT_BYTES)
    assert len(text) == 65536
    event = parse_event(text)
    assert event.event_hash == hashlib.sha256(text).hexdigest()


def test_an_event_text_of_65537_bytes_is_refused() -> None:
    text = sized_event(MAX_EVENT_BYTES + 1)
    assert len(text) == 65537
    with pytest.raises(RecordRefusal) as refused:
        parse_event(text)
    assert refused.value.error == "event_invalid"


INVALID: list[tuple[str, Callable[[], object]]] = [
    ("another label", lambda: replaced(0, "agent-event-v2")),
    ("label in uppercase", lambda: replaced(0, "AGENT-EVENT-V1")),
    ("carriage return in a line", lambda: replaced(4, ACTOR + "\r")),
    ("lines joined by CRLF", lambda: joined(EXAMPLE_1_LINES, "\r\n")),
    ("trailing line feed", lambda: EXAMPLE_1 + b"\n"),
    ("leading line feed", lambda: b"\n" + EXAMPLE_1),
    ("empty line", lambda: joined(EXAMPLE_1_LINES[:9] + ("",) + EXAMPLE_1_LINES[9:])),
    ("empty text", lambda: b""),
    ("space", lambda: replaced(17, "=Rml4 IGxvZ2lu")),
    ("tab", lambda: replaced(17, "=Rml4\tIGxvZ2lu")),
    ("byte 0x7f", lambda: EXAMPLE_1.replace(b"Rml4", b"Rml4\x7f")),
    ("byte 0x00", lambda: EXAMPLE_1.replace(b"Rml4", b"Rml4\x00")),
    ("byte above 0x7f", lambda: EXAMPLE_1.replace(b"Rml4", b"Rml4\xc3\xa9")),
    ("n of 0", lambda: joined(EXAMPLE_1_LINES[:8] + ("0",))),
    ("n of 16", lambda: replaced(8, "16")),
    ("n with a leading zero", lambda: replaced(8, "03")),
    ("n with a sign", lambda: replaced(8, "+3")),
    ("n not a number", lambda: replaced(8, "x")),
    ("one line missing", lambda: joined(EXAMPLE_1_LINES[:-1])),
    ("three lines too many", lambda: joined(EXAMPLE_1_LINES + ("zone", "-", "=eg"))),
    ("n smaller than the changes", lambda: replaced(8, "2")),
    ("header only", lambda: joined(EXAMPLE_1_LINES[:8])),
    ("fields out of order", swapped_blocks),
    ("field twice", lambda: replaced(12, "kind")),
    ("old equal to new", lambda: replaced(10, "=d29yaw")),
    ("old and new both -", lambda: replaced(11, "-")),
    ("= without value", lambda: replaced(11, "=")),
    ("padded base64url", lambda: replaced(11, "=d29yaw==")),
    ("non-canonical base64url", lambda: replaced(11, "=d29yax")),
    ("base64url of impossible length", lambda: replaced(11, "=d29ya")),
    ("base64 alphabet instead of base64url", lambda: replaced(17, "=Rml4+Gxvz2lu")),
    ("value token without =", lambda: replaced(11, "d29yaw")),
    ("old not a value token", lambda: replaced(10, "x")),
    ("unknown object_type", lambda: replaced(2, "discussion")),
    ("object_type in uppercase", lambda: replaced(2, "Task")),
    ("event_id in uppercase", lambda: replaced(1, EVENT_ID.upper())),
    ("event_id without dashes", lambda: replaced(1, EVENT_ID.replace("-", ""))),
    ("event_id too short", lambda: replaced(1, EVENT_ID[:-1])),
    ("event_id in braces", lambda: replaced(1, "{" + EVENT_ID + "}")),
    ("nil event_id", lambda: replaced(1, "00000000-0000-0000-0000-000000000000")),
    ("object_id in uppercase", lambda: replaced(3, OBJECT_ID.upper())),
    ("object_id with groups of the wrong size", lambda: replaced(3, "5d1c2b3a4-e5f-4a6b-8c7d-9e0f1a2b3c4d")),
    ("nil object_id", lambda: replaced(3, "00000000-0000-0000-0000-000000000000")),
    ("event_id of version 1", lambda: replaced(1, "0b6f3c1e-8d2a-1c1b-9f3e-2a7d5c9e1f40")),
    ("event_id of version 5", lambda: replaced(1, "0b6f3c1e-8d2a-5c1b-9f3e-2a7d5c9e1f40")),
    ("event_id of variant c", lambda: replaced(1, "0b6f3c1e-8d2a-4c1b-cf3e-2a7d5c9e1f40")),
    ("event_id of variant 7", lambda: replaced(1, "0b6f3c1e-8d2a-4c1b-7f3e-2a7d5c9e1f40")),
    ("object_id of version 1", lambda: replaced(3, "5d1c2b3a-4e5f-1a6b-8c7d-9e0f1a2b3c4d")),
    ("object_id of version 5", lambda: replaced(3, "5d1c2b3a-4e5f-5a6b-8c7d-9e0f1a2b3c4d")),
    ("object_id of variant 0", lambda: replaced(3, "5d1c2b3a-4e5f-4a6b-0c7d-9e0f1a2b3c4d")),
    ("actor_key_id too short", lambda: replaced(4, "ex")),
    ("actor_key_id in uppercase", lambda: replaced(4, "Example-staff-laptop-1")),
    ("actor_key_id starting with -", lambda: replaced(4, "-example")),
    ("actor_key_id ending with -", lambda: replaced(4, "example-")),
    ("actor_key_id of 64 characters", lambda: replaced(4, "a" * 64)),
    ("signed_at without Z", lambda: replaced(5, "2026-11-17T10:00:00")),
    ("signed_at with a fraction", lambda: replaced(5, "2026-11-17T10:00:00.5Z")),
    ("signed_at on a day that does not exist", lambda: replaced(5, "2026-02-30T10:00:00Z")),
    ("prev_hash of 63 characters", lambda: replaced(6, "a" * 63)),
    ("prev_hash of 65 characters", lambda: replaced(6, "a" * 65)),
    ("prev_hash in uppercase", lambda: replaced(6, EXAMPLE_1_HASH.upper())),
    ("prev_hash not hex", lambda: replaced(6, "g" * 64)),
    ("prev_hash --", lambda: replaced(6, "--")),
    ("key_certificate not a value token", lambda: replaced(7, "x")),
    ("key_certificate = without value", lambda: replaced(7, "=")),
    ("key_certificate padded", lambda: replaced(7, "=Zg==")),
    ("not bytes", lambda: EXAMPLE_1.decode("ascii")),
]


@pytest.mark.parametrize("make", [case[1] for case in INVALID], ids=[case[0] for case in INVALID])
def test_event_invalid(make: Callable[[], object]) -> None:
    with pytest.raises(RecordRefusal) as refused:
        parse_event(make())
    assert refused.value.error == "event_invalid"
    assert str(refused.value) == "event_invalid"


def test_fifteen_changes_are_accepted() -> None:
    names = [f"f{index:02d}" for index in range(15)]
    lines = list(EXAMPLE_1_LINES[:8]) + ["15"]
    for name in names:
        lines += [name, "-", "=" + b64(name.encode("ascii"))]
    event = parse_event(joined(lines))
    assert [change.field for change in event.changes] == names


def test_a_key_certificate_value_token_has_the_form() -> None:
    # Section 4.1: any value token has the form; check 5 of section 6.7 refuses it later.
    event = parse_event(replaced(7, "=Y2VydA"))
    assert event.key_certificate == b"cert"


def test_a_change_that_removes_a_value_has_the_form() -> None:
    event = parse_event(EXAMPLE_2.replace(b"=b3Blbg\n=aW5fcHJvZ3Jlc3M", b"=b3Blbg\n-"))
    assert event.changes[1] == Change("status", b"open", None)


# --- value tokens and building ----------------------------------------------------------------


def test_value_tokens() -> None:
    assert encode_value_token(None) == "-"
    assert encode_value_token(b"work") == "=d29yaw"
    assert decode_value_token("-") is None
    assert decode_value_token("=d29yaw") == b"work"
    with pytest.raises(ValueError):
        encode_value_token(b"")
    for token in ("", "=", "x", "=d29yaw=", "=d29yax", "--"):
        with pytest.raises(RecordRefusal) as refused:
            decode_value_token(token)
        assert refused.value.error == "event_invalid"


def test_build_event_text_writes_the_example_with_the_changes_in_field_order() -> None:
    text = build_event_text(
        event_id=EVENT_ID,
        object_type="task",
        object_id=OBJECT_ID,
        actor_key_id=ACTOR,
        signed_at=seconds("2026-11-17T10:00:00Z"),
        prev_hash="-",
        changes=[
            Change("title", None, b"Fix login"),
            Change("kind", None, b"work"),
            Change("team", None, b"example"),
        ],
    )
    assert text == EXAMPLE_1


def test_build_event_text_with_a_key_certificate() -> None:
    text = build_event_text(
        event_id=EVENT_ID,
        object_type="comment",
        object_id=OBJECT_ID,
        actor_key_id=ACTOR,
        signed_at=0,
        prev_hash=EXAMPLE_1_HASH,
        changes=[Change("body", None, b"text")],
        key_certificate=b"cert",
    )
    assert parse_event(text).key_certificate == b"cert"


@pytest.mark.parametrize(
    "changes",
    [
        [],
        [Change("title", None, b"")],
        [Change("title", None, None)],
        [Change("title", None, b"a"), Change("title", None, b"b")],
        [Change("tïtle", None, b"a")],
    ],
    ids=["no change", "empty value", "nothing changed", "field twice", "field outside ASCII"],
)
def test_build_event_text_refuses_what_parse_refuses(changes: list[Change]) -> None:
    with pytest.raises(RecordRefusal) as refused:
        build_event_text(
            event_id=EVENT_ID,
            object_type="task",
            object_id=OBJECT_ID,
            actor_key_id=ACTOR,
            signed_at=0,
            prev_hash="-",
            changes=changes,
        )
    assert refused.value.error == "event_invalid"


BUILD_ARGUMENTS = {
    "event_id": EVENT_ID,
    "object_type": "task",
    "object_id": OBJECT_ID,
    "actor_key_id": ACTOR,
    "signed_at": 0,
    "prev_hash": "-",
    "changes": [Change("title", None, b"Fix login")],
}


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("signed_at", MIN_TIME - 1),
        ("signed_at", MAX_TIME + 1),
        ("signed_at", 10**30),
        ("signed_at", -(10**30)),
        ("signed_at", True),
        ("signed_at", 1.5),
        ("signed_at", "0"),
        ("signed_at", None),
        ("event_id", None),
        ("event_id", EVENT_ID.encode("ascii")),
        ("event_id", "\ud800"),
        ("object_type", 1),
        ("actor_key_id", None),
        ("prev_hash", None),
        ("changes", ["title"]),
        ("changes", None),
        ("changes", 5),
        ("changes", [Change(1, None, b"a")]),  # type: ignore[arg-type]
        ("changes", [Change("title", None, "Fix login")]),  # type: ignore[arg-type]
        ("changes", [Change("title", "-", b"a")]),  # type: ignore[arg-type]
    ],
    ids=[
        "signed_at before year 1",
        "signed_at after year 9999",
        "signed_at huge",
        "signed_at huge negative",
        "signed_at True",
        "signed_at float",
        "signed_at text",
        "signed_at None",
        "event_id None",
        "event_id bytes",
        "event_id with a lone surrogate",
        "object_type int",
        "actor_key_id None",
        "prev_hash None",
        "a change that is text",
        "changes None",
        "changes int",
        "a field that is an int",
        "a new value that is text",
        "an old value that is text",
    ],
)
def test_build_event_text_refuses_an_argument_of_the_wrong_type_or_range(name: str, value: object) -> None:
    with pytest.raises(RecordRefusal) as refused:
        build_event_text(**{**BUILD_ARGUMENTS, name: value})  # type: ignore[arg-type]
    assert refused.value.error == "event_invalid"


@pytest.mark.parametrize("signed_at", [MIN_TIME, MAX_TIME, seconds("0987-08-25T16:53:20Z")])
def test_build_event_text_writes_a_signed_at_with_four_year_digits(signed_at: int) -> None:
    text = build_event_text(**{**BUILD_ARGUMENTS, "signed_at": signed_at})  # type: ignore[arg-type]
    event = parse_event(text)
    assert event.signed_at == signed_at
    assert text.split(b"\n")[5] == time_text(signed_at).encode("ascii")  # type: ignore[union-attr]
    assert len(text.split(b"\n")[5]) == 20


def test_uuid_and_hash_forms() -> None:
    assert valid_uuid(EVENT_ID)
    for variant in "89ab":
        assert valid_uuid(f"0b6f3c1e-8d2a-4c1b-{variant}f3e-2a7d5c9e1f40")
    for version in "01235678f":
        assert not valid_uuid(f"0b6f3c1e-8d2a-{version}c1b-9f3e-2a7d5c9e1f40")
    for variant in "0123456789abcdef":
        if variant not in "89ab":
            assert not valid_uuid(f"0b6f3c1e-8d2a-4c1b-{variant}f3e-2a7d5c9e1f40")
    assert not valid_uuid("00000000-0000-0000-0000-000000000000")
    assert not valid_uuid(EVENT_ID.upper())
    assert not valid_uuid(None)
    assert valid_hash(EXAMPLE_1_HASH)
    assert not valid_hash(EXAMPLE_1_HASH.upper())
    assert not valid_hash(b"0" * 64)


def test_no_record_content_in_a_repr_or_a_refusal() -> None:
    event = parse_event(EXAMPLE_1)
    shown = repr(event) + repr(event.changes)
    assert "Rml4" not in shown
    assert "Fix login" not in shown
    assert "work" not in shown
    # Section 13: no event id and no object id.
    assert EVENT_ID not in shown
    assert OBJECT_ID not in shown
    # Section 13: no field name, and of the parts of an event only the key id.
    for name in ("kind", "team", "title"):
        assert name not in shown
    assert repr(event) == f"Event(actor_key_id={ACTOR!r})"
    assert repr(event.changes[0]) == "Change()"
    head = chain_head(event)
    assert repr(head) == "ChainHead()"
    for hidden in (EXAMPLE_1_HASH, "2026", str(event.signed_at), "task"):
        assert hidden not in repr(event) + repr(head)
    assert str(RecordRefusal("event_invalid")) == "event_invalid"


# --- the signature (section 4.3) ----------------------------------------------------------------


def private(seed: int) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes([seed]) * 32)


def public(seed: int) -> bytes:
    return private(seed).public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def entry(key_id: str, seed: int, kind: str = "ed25519") -> KeyEntry:
    return KeyEntry(
        id=key_id,
        kind=kind,
        principal="agent" if kind == "ed25519" else "staff",
        handle="example-staff",
        team="example",
        public_key=public(seed) if kind == "ed25519" else b"\x04" + bytes(64),
        scopes=("record:read", "record:write"),
        credential_id=None if kind == "ed25519" else bytes(16),
        device="laptop-1" if kind == "ed25519" else None,
        program=None,
        subjects=None,
        expires=None,
        revoked=None,
    )


def ring(*entries: KeyEntry) -> KeyRing:
    return KeyRing({item.id: item for item in entries})


def test_the_event_signature_verifies_with_the_actor_key() -> None:
    event = parse_event(EXAMPLE_1)
    verify_event_signature(ring(entry(ACTOR, 1)), event, private(1).sign(EXAMPLE_1))


@pytest.mark.parametrize(
    "signature",
    [
        private(2).sign(EXAMPLE_1),
        private(1).sign(EXAMPLE_2),
        private(1).sign(EXAMPLE_1)[:63],
        bytes(64),
    ],
    ids=["another key", "another text", "63 bytes", "zero bytes"],
)
def test_event_signature_invalid(signature: bytes) -> None:
    with pytest.raises(RecordRefusal) as refused:
        verify_event_signature(ring(entry(ACTOR, 1)), parse_event(EXAMPLE_1), signature)
    assert refused.value.error == "event_signature_invalid"


@pytest.mark.parametrize(
    "keys",
    [ring(), ring(entry("example-other-1", 1)), ring(entry(ACTOR, 1, kind="webauthn"))],
    ids=["no entry", "another id", "a webauthn entry"],
)
def test_key_unknown(keys: KeyRing) -> None:
    with pytest.raises(RecordRefusal) as refused:
        verify_event_signature(keys, parse_event(EXAMPLE_1), private(1).sign(EXAMPLE_1))
    assert refused.value.error == "key_unknown"


def test_signature_valid_refuses_a_public_key_of_the_wrong_length() -> None:
    assert signature_valid(public(1), b"text", private(1).sign(b"text"))
    assert not signature_valid(public(1)[:31], b"text", private(1).sign(b"text"))


# --- the chain of a key (section 4.5) -----------------------------------------------------------


def test_the_first_event_of_a_key_has_prev_hash_dash() -> None:
    check_chain(None, parse_event(EXAMPLE_1))
    with pytest.raises(RecordRefusal) as refused:
        check_chain(None, parse_event(EXAMPLE_2))
    assert refused.value.error == "chain_broken"


@pytest.mark.parametrize(
    "head",
    [ChainHead("0" * 64, 0), ChainHead(EXAMPLE_1_HASH, 0)],
    ids=["a wrong prev_hash", "a second first event"],
)
def test_chain_broken_after_an_accepted_event(head: ChainHead) -> None:
    event = parse_event(EXAMPLE_1 if head.event_hash == EXAMPLE_1_HASH else EXAMPLE_2)
    with pytest.raises(RecordRefusal) as refused:
        check_chain(head, event)
    assert refused.value.error == "chain_broken"


def test_chain_head_is_the_hash_and_time_of_the_event() -> None:
    event = parse_event(EXAMPLE_1)
    assert chain_head(event) == ChainHead(EXAMPLE_1_HASH, seconds("2026-11-17T10:00:00Z"))


def test_the_first_event_of_a_key_has_no_earlier_bound_on_signed_at() -> None:
    # Section 4.5 bounds `signed_at` only by the key's previous event; the bound against now of
    # check 10 of section 6.7 is not part of this library (change 3 of section 16).
    check_signed_at(None, parse_event(EXAMPLE_1))
    check_signed_at(None, parse_event(replaced(5, "9999-12-31T23:59:59Z")))


def test_signed_at_is_not_earlier_than_the_previous_event_of_the_key() -> None:
    event = parse_event(EXAMPLE_2)
    check_signed_at(ChainHead(EXAMPLE_1_HASH, event.signed_at), event)
    check_signed_at(ChainHead(EXAMPLE_1_HASH, event.signed_at - 1), event)
    with pytest.raises(RecordRefusal) as refused:
        check_signed_at(ChainHead(EXAMPLE_1_HASH, event.signed_at + 1), event)
    assert refused.value.error == "event_time_invalid"


def test_event_is_frozen() -> None:
    event = parse_event(EXAMPLE_1)
    assert isinstance(event, Event)
    with pytest.raises(AttributeError):
        event.prev_hash = "x"  # type: ignore[misc]
