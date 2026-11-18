"""The export file format and offline verification (RECORD-PROTOCOL.md sections 12.1 and 12.3).

`verify_export` is the library function behind the command `verify`; every `file_invalid` case of
section 14 is checked here with its position.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from agent_core.keys import KeyRing
from agent_core.record.checkpoint import Checkpoint
from agent_core.record.event import time_text
from agent_core.record.export import ExportInvalid, parse_export, verify_export, write_export
from tests.test_record_reducer import BOB, T0, public
from tests.test_record_verify import HUB, HUB_KEYS, HUB_SEED, checkpoint, story

EXPORTED_AT = T0 + 86400


def lines_of(data: bytes) -> list[bytes]:
    assert data.endswith(b"\n")
    return data[:-1].split(b"\n")


def joined(lines: list[bytes]) -> bytes:
    return b"".join(line + b"\n" for line in lines)


def dumped(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


def story_export() -> tuple[bytes, Any]:
    world, _ = story()
    last = world.stored[-1]
    checkpoints = [
        checkpoint(3, last.record_hash, T0 + 3600, HUB, HUB_SEED),
        checkpoint(2, world.stored[1].record_hash, T0 + 1800, HUB, HUB_SEED),
    ]
    return write_export(world.stored, checkpoints, EXPORTED_AT), world


def invalid_position(data: bytes) -> int:
    with pytest.raises(ExportInvalid) as invalid:
        parse_export(data)
    assert str(invalid.value) == "file_invalid"
    return invalid.value.position


# --- writing and reading ------------------------------------------------------------------------------


def test_an_export_of_the_story_verifies_offline() -> None:
    data, world = story_export()
    lines = lines_of(data)
    last = world.stored[-1]
    assert lines[0] == (
        b'{"format":"agent-record-export-v1","position":3,"record_hash":"' + last.record_hash.encode()
        + b'","exported_at":"' + time_text(EXPORTED_AT).encode() + b'"}'  # type: ignore[union-attr]
    )
    assert json.loads(lines[1])["type"] == "event"
    assert list(json.loads(lines[1])) == ["type", "position", "received_at", "event_hash", "record_hash", "event", "signature"]
    assert list(json.loads(lines[4])) == ["type", "position", "record_hash", "published_at", "signed_by", "signature"]
    assert [json.loads(line)["position"] for line in lines[4:]] == [2, 3]
    assert b" " not in data
    export = parse_export(data)
    assert export.header.position == 3
    assert export.header.record_hash == last.record_hash
    assert export.header.exported_at == EXPORTED_AT
    assert [stored.position for stored in export.events] == [1, 2, 3]
    assert export.events[0].text == world.stored[0].text
    assert export.events[0].copies is None
    assert [item.position for item in export.checkpoints] == [2, 3]
    result = verify_export(data, world.keys, HUB_KEYS)
    assert result.failure is None
    assert result.state == world.state


def test_an_empty_export() -> None:
    data = write_export([], [], EXPORTED_AT)
    assert data == b'{"format":"agent-record-export-v1","position":0,"record_hash":null,"exported_at":"' + \
        time_text(EXPORTED_AT).encode() + b'"}\n'  # type: ignore[union-attr]
    export = parse_export(data)
    assert (export.header.position, export.header.record_hash, export.events, export.checkpoints) == (0, None, (), ())
    result = verify_export(data, KeyRing({}), {})
    assert result.failure is None
    assert result.state.head is None


def test_checkpoint_lines_are_written_in_byte_order_of_the_hub_key_id() -> None:
    world, _ = story()
    value = world.stored[-1].record_hash
    first = checkpoint(3, value, T0, "hub1", HUB_SEED)
    second = checkpoint(3, value, T0, "hub-2", 31)
    data = write_export(world.stored, [first, second], EXPORTED_AT)
    assert [json.loads(line)["signed_by"] for line in lines_of(data)[4:]] == ["hub-2", "hub1"]
    assert verify_export(data, world.keys, {"hub1": public(HUB_SEED), "hub-2": public(31)}).failure is None


def test_the_writer_refuses_a_time_outside_its_form() -> None:
    world, _ = story()
    with pytest.raises(ValueError):
        write_export(world.stored, [], 10**12)


def test_offline_verify_of_a_well_formed_export_that_fails() -> None:
    world, _ = story()
    changed = [world.stored[0], replace(world.stored[1], signature=bytes(64)), world.stored[2]]
    result = verify_export(write_export(changed, [], EXPORTED_AT), world.keys, {})
    assert result.failure is not None
    assert (result.failure.failure, result.failure.position) == ("event_signature_invalid", 2)
    without_bob = KeyRing({entry_id: entry for entry_id, entry in world.keys.entries.items() if entry_id != BOB.id})
    result = verify_export(write_export(world.stored, [], EXPORTED_AT), without_bob, {})
    assert result.failure is not None
    assert (result.failure.failure, result.failure.position) == ("key_unknown", 2)
    bad = checkpoint(3, "0" * 64, T0, HUB, HUB_SEED)
    data = write_export(world.stored, [bad], EXPORTED_AT)
    result = verify_export(data, world.keys, HUB_KEYS)
    assert result.failure is not None
    assert (result.failure.failure, result.failure.position) == ("checkpoint_invalid", 3)
    assert verify_export(data, world.keys, {}).failure is None


def test_verify_export_reports_file_invalid_with_its_position() -> None:
    result = verify_export(b"", KeyRing({}), {})
    assert result.failure is not None
    assert (result.failure.failure, result.failure.position, result.failure.event) == ("file_invalid", 0, None)


# --- file_invalid and its position ---------------------------------------------------------------------


def edited(index: int, change: dict[str, Any]) -> bytes:
    data, _ = story_export()
    lines = lines_of(data)
    value = json.loads(lines[index])
    for name, new in change.items():
        if new is DROP:
            del value[name]
        else:
            value[name] = new
    lines[index] = dumped(value)
    return joined(lines)


DROP = object()


def raw_member(index: int, name: str, literal: bytes) -> bytes:
    data, _ = story_export()
    lines = lines_of(data)
    value = json.loads(lines[index])
    value[name] = "@@"
    lines[index] = dumped(value).replace(b'"@@"', literal)
    return joined(lines)


def test_faults_of_the_whole_file_have_position_0() -> None:
    data, _ = story_export()
    assert invalid_position(b"") == 0
    assert invalid_position(data[:-1]) == 0
    assert invalid_position(data[:-1] + b"\xff\n") == 0
    # Not UTF-8 decides before an empty line.
    assert invalid_position(b"\xff\n\n") == 0


def test_an_empty_line() -> None:
    data, _ = story_export()
    lines = lines_of(data)
    assert invalid_position(joined([*lines[:3], b"", *lines[3:]])) == 3
    assert invalid_position(joined([b"", *lines])) == 0
    # The first item of the list decides: an empty line after a line that is not JSON.
    assert invalid_position(joined([lines[0], b"{", *lines[1:], b""])) == 4


@pytest.mark.parametrize(
    "bad",
    [b"{", b"[]", b'"x"', b"{}{}", b'{"type":"event","type":"event"}', b'{"a":{"b":1,"b":1}}', b'{"a":NaN}',
     b"[" * 100000 + b"]" * 100000, b'{"type":"ev\tent"}', b"\xef\xbb\xbf{}"],
    ids=["not JSON", "an array", "a string", "two objects", "a member twice", "a nested member twice", "NaN",
         "deep nesting", "a raw tab in a string", "a byte order mark"],
)
def test_a_line_that_is_not_exactly_one_json_object(bad: bytes) -> None:
    data, _ = story_export()
    lines = lines_of(data)
    assert invalid_position(joined([*lines[:2], bad, *lines[2:]])) == 2
    assert invalid_position(joined([bad, *lines[1:]])) == 0


@pytest.mark.parametrize(
    "change",
    [
        {"format": "agent-record-export-v2"},
        {"format": DROP},
        {"extra": 1},
        {"record_hash": "A" * 64},
        {"exported_at": "2026-11-18T02:00:00"},
        {"position": "3"},
    ],
    ids=["unknown format", "missing member", "unknown member", "record_hash", "exported_at", "position as a string"],
)
def test_a_header_outside_its_form(change: dict[str, Any]) -> None:
    assert invalid_position(edited(0, change)) == 0


@pytest.mark.parametrize("literal", [b"3.0", b"3e0", b"-0", b"03", b"-3", b"9223372036854775808",
                                     b"99999999999999999999"])
def test_a_header_position_outside_its_form(literal: bytes) -> None:
    assert invalid_position(raw_member(0, "position", literal)) == 0


@pytest.mark.parametrize("literal", [b"1.0", b"1e0", b"-0", b"01", b"0", b'"1"', b"true", b"null"])
def test_an_event_position_outside_its_form(literal: bytes) -> None:
    assert invalid_position(raw_member(2, "position", literal)) == 2


@pytest.mark.parametrize(
    ("index", "change", "position"),
    [
        (2, {"type": "discussion"}, 2),
        (2, {"type": 1}, 2),
        (2, {"type": DROP}, 2),
        (2, {"signature": DROP}, 2),
        (2, {"extra": 1}, 2),
        (2, {"received_at": "2026-11-17"}, 2),
        (2, {"event_hash": "x"}, 2),
        (2, {"record_hash": "A" * 64}, 2),
        (2, {"event": "a=="}, 2),
        (2, {"signature": "AAAA"}, 2),
        (4, {"signed_by": "Hub"}, 4),
        (4, {"published_at": 1}, 4),
        (4, {"record_hash": None}, 4),
        (4, {"signature": "x"}, 4),
        (4, {"position": 0}, 4),
        (4, {"extra": 1}, 4),
    ],
)
def test_a_later_line_outside_its_form(index: int, change: dict[str, Any], position: int) -> None:
    assert invalid_position(edited(index, change)) == position


def test_the_order_of_event_and_checkpoint_lines() -> None:
    data, _ = story_export()
    lines = lines_of(data)
    header, events, checkpoints = lines[0], lines[1:4], lines[4:]
    assert invalid_position(joined([header, events[0], checkpoints[0], *events[1:], checkpoints[1]])) == 2
    assert invalid_position(joined([header, *events, checkpoints[1], checkpoints[0]])) == 4
    assert invalid_position(joined([header, *events, checkpoints[0], checkpoints[0]])) == 4


def test_the_header_agrees_with_the_last_event_line() -> None:
    assert invalid_position(edited(0, {"position": 2})) == 0
    assert invalid_position(edited(0, {"record_hash": "a" * 64})) == 0
    data, _ = story_export()
    lines = lines_of(data)
    assert invalid_position(joined([lines[0], *lines[4:]])) == 0
    empty = lines_of(write_export([], [], EXPORTED_AT))
    assert invalid_position(joined([dumped({**json.loads(empty[0]), "position": 1})])) == 0
    assert invalid_position(joined([dumped({**json.loads(empty[0]), "record_hash": "a" * 64})])) == 0


def test_a_checkpoint_line_in_a_file_without_event_lines() -> None:
    data, _ = story_export()
    checkpoint_line = lines_of(data)[4]
    empty = lines_of(write_export([], [], EXPORTED_AT))
    assert invalid_position(joined([empty[0], checkpoint_line])) == 0
    unknown = dumped({**json.loads(checkpoint_line), "signed_by": "hub-unknown"})
    assert invalid_position(joined([empty[0], unknown])) == 0


def story_lines() -> list[bytes]:
    """The lines of the story export: the header, three event lines, two checkpoint lines."""
    data, _ = story_export()
    return lines_of(data)


def replaced(lines: list[bytes], index: int, line: bytes) -> bytes:
    changed = list(lines)
    changed[index] = line
    return joined(changed)


def spaced(line: bytes) -> bytes:
    """The line with a space inside the string of `event_hash` (item 5, never item 3 or 6)."""
    assert b'"event_hash":"' in line
    return line.replace(b'"event_hash":"', b'"event_hash":" ', 1)


def escaped(line: bytes, name: str) -> bytes:
    """The line with the first character of the string member `name` written as an escape."""
    value = json.loads(line)[name]
    escape = "\\u%04x" % ord(value[0])
    member = f'"{name}":"'.encode()
    return line.replace(member + value.encode(), member + escape.encode() + value[1:].encode(), 1)


def test_the_writer_form_of_the_header() -> None:
    """Item 6 for the first line: position 0."""
    lines = story_lines()
    header = lines[0]
    assert invalid_position(replaced(lines, 0, header.replace(b'","', b'", "', 1))) == 0
    assert invalid_position(replaced(lines, 0, b" " + header)) == 0
    assert invalid_position(replaced(lines, 0, header.replace(b'"position":', b'"position":\t', 1))) == 0
    assert invalid_position(replaced(lines, 0, escaped(header, "format"))) == 0


def test_the_writer_form_of_an_event_line() -> None:
    """Item 6 at the position of that line: the third line is the second event line."""
    lines = story_lines()
    event = lines[2]
    reordered = dumped(dict(reversed(list(json.loads(event).items()))))
    for line in (
        event.replace(b'":', b'": ', 1),  # a space between two tokens
        event.replace(b",", b"\t,", 1),  # a tab between two tokens
        b" " + event,  # a space before the object
        event + b" ",  # a space after the object
        event + b"\r",  # a carriage return after the object
        reordered,  # the members in another order
        escaped(event, "event_hash"),  # a character written as an escape
    ):
        assert invalid_position(replaced(lines, 2, line)) == 2
    # The same faults in a checkpoint line: the number of event lines before it plus 1.
    assert invalid_position(replaced(lines, 5, lines[5].replace(b'":', b'": ', 1))) == 4
    assert invalid_position(replaced(lines, 5, escaped(lines[5], "signed_by"))) == 4


def test_whitespace_inside_a_string_is_judged_by_the_form_of_its_member() -> None:
    lines = story_lines()
    # Item 5 at the position of that line.
    assert invalid_position(replaced(lines, 2, spaced(lines[2]))) == 2
    # A header with another `format` and a later line with a space inside a string: item 4 first.
    header = lines[0].replace(b"agent-record-export-v1", b"agent-record-export-v2")
    assert invalid_position(joined([header, spaced(lines[1]), *lines[2:]])) == 0
    # Line 2 with a space inside a string (item 5) and line 5 not JSON (item 3): item 3 first, at
    # the number of event lines among lines 2 to 4 plus 1. Line 2 is an event line.
    assert invalid_position(joined([lines[0], spaced(lines[1]), lines[2], lines[3], b"{", lines[5]])) == 4
    # Line 2 with a space inside a string (item 5) and line 3 with a space between tokens (item 6).
    assert invalid_position(joined([lines[0], spaced(lines[1]), lines[2].replace(b'":', b'": ', 1),
                                    *lines[3:]])) == 1


def test_the_items_decide_in_the_order_of_the_list() -> None:
    lines = story_lines()
    header, events, checkpoints = lines[0], lines[1:4], lines[4:]
    # Item 3 before item 4: a header with another `format` and a later line that is not JSON.
    other_format = header.replace(b"agent-record-export-v1", b"agent-record-export-v2")
    assert invalid_position(joined([other_format, events[0], b"{", *events[1:], *checkpoints])) == 2
    # Item 6 before item 7: an event line after a checkpoint line (item 7, position 2) and a space
    # between two tokens of the last checkpoint line (item 6, after three event lines).
    late = checkpoints[1].replace(b'":', b'": ', 1)
    assert invalid_position(joined([header, events[0], checkpoints[0], *events[1:], late])) == 4
    # Item 7 before item 8: an event line after a checkpoint line (item 7, position 2) and a header
    # that disagrees with the last event line (item 8, position 0).
    disagreeing = dumped({**json.loads(header), "position": 2})
    assert invalid_position(joined([disagreeing, events[0], checkpoints[0], *events[1:], checkpoints[1]])) == 2


@pytest.mark.parametrize(
    ("line", "position"),
    [
        (b'{"type":"event","x":1,"x":1}', 2),
        (b'{ "type" : "event" }', 2),
        (b'{"type":"event","position":"1"}', 2),
        (b'{"type":"event","type":"event"}', 1),
        (b'{"type":"x"}', 1),
        (b'{"type":1}', 1),
        (b'{"kind":"event"}', 1),
        (b"[]", 1),
        (b"{", 1),
    ],
    ids=["another member twice", "whitespace", "another member outside its form", "a repeated type",
         "another type", "a type that is no string", "no type", "no object", "not JSON"],
)
def test_which_line_counts_as_an_event_line(line: bytes, position: int) -> None:
    """Line 2 as given and an empty line 3 (item 2): the position tells whether line 2 counted."""
    lines = story_lines()
    assert invalid_position(joined([lines[0], line, b"", *lines[1:]])) == position


def test_the_positions_of_section_14_for_empty_lines() -> None:
    lines = story_lines()
    # Line 2 not JSON, line 3 an event line, line 4 empty: 2, because line 2 is no event line.
    assert invalid_position(joined([lines[0], b"{", lines[1], b"", *lines[2:]])) == 2
    # Line 2 an event line, line 3 an object with `type` `x`, line 4 empty: 2.
    assert invalid_position(joined([lines[0], lines[1], b'{"type":"x"}', b"", *lines[2:]])) == 2
    # An empty first line, and a first line that is not JSON: 0.
    assert invalid_position(joined([b"", *lines[1:]])) == 0
    assert invalid_position(joined([b"{", *lines[1:]])) == 0
    # The first line is never an event line, also when it is an event line's object: 1, not 2.
    assert invalid_position(joined([lines[1], b"", *lines[1:]])) == 1


def test_no_record_content_in_a_repr() -> None:
    data, _ = story_export()
    export = parse_export(data)
    shown = repr(export)
    assert "agent-event" not in shown
    assert shown.startswith("ExportFile(header=ExportHeader(position=3, record_hash=")
    assert isinstance(export.checkpoints[0], Checkpoint)
