"""The export file of the record and its offline verification (RECORD-PROTOCOL.md section 12.3).

`write_export` writes the JSON Lines file, or refuses a stored value outside the form of its
member with `ValueUnwritable` (`refused value_unwritable`); `parse_export` checks the file format
in the order of the list of section 12.3 and refuses it with `file_invalid` and the position that
section defines; `verify_export` is offline `verify` as a library function. The commands, the
snapshot read and the file handling come with the storage part.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any, Mapping, Sequence

from ..keys import KeyRing
from ..signing import SIGNATURE_BYTES, b64url_decode, b64url_encode, parse_time, valid_key_id
from .checkpoint import Checkpoint
from .event import MAX_POSITION, time_text, valid_hash
from .failures import FILE_INVALID
from .state import EMPTY_STATE
from .verify import Failure, FoldResult, StoredEvent, verify_record

EXPORT_FORMAT = "agent-record-export-v1"
# The members of each kind of line, in the order of the writer's form (section 12.3).
HEADER_ORDER = ("format", "position", "record_hash", "exported_at")
EVENT_ORDER = ("type", "position", "received_at", "event_hash", "record_hash", "event", "signature")
CHECKPOINT_ORDER = ("type", "position", "record_hash", "published_at", "signed_by", "signature")
HEADER_MEMBERS = frozenset(HEADER_ORDER)
EVENT_MEMBERS = frozenset(EVENT_ORDER)
CHECKPOINT_MEMBERS = frozenset(CHECKPOINT_ORDER)
TYPE_EVENT = "event"
TYPE_CHECKPOINT = "checkpoint"

_POSITION_RE = re.compile(r"[1-9][0-9]{0,18}")


class ExportInvalid(Exception):
    """The file breaks the file format of section 12.3; `position` is the position of the failure."""

    def __init__(self, position: int) -> None:
        super().__init__(FILE_INVALID)
        self.position = position


@dataclass(frozen=True)
class ExportHeader:
    position: int
    record_hash: str | None
    exported_at: int = dataclass_field(repr=False)


@dataclass(frozen=True)
class ExportFile:
    header: ExportHeader
    events: tuple[StoredEvent, ...] = dataclass_field(repr=False)
    checkpoints: tuple[Checkpoint, ...] = dataclass_field(repr=False)


# --- writing -------------------------------------------------------------------------------------


def _line(value: dict[str, Any]) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("ascii") + b"\n"


REFUSED_VALUE_UNWRITABLE = "value_unwritable"


class ValueUnwritable(Exception):
    """A stored value of the snapshot has no form of its member (section 12.3, decision 57).

    `export` stops at it, removes `FILE` and prints `refused value_unwritable`. The message is only
    that value, never the stored value (section 13).
    """

    def __init__(self) -> None:
        super().__init__(REFUSED_VALUE_UNWRITABLE)


def _stored_time(seconds: int) -> str:
    """A stored `received_at` or `published_at` as a time of section 2, or `ValueUnwritable`."""
    text = time_text(seconds)
    if text is None:
        raise ValueUnwritable()
    return text


def _stored_key_id(hub_key_id: str) -> str:
    """A stored `hub_key_id`, written as `signed_by`, when it follows the `id` rule."""
    if not valid_key_id(hub_key_id):
        raise ValueUnwritable()
    return hub_key_id


def write_export(
    events: Sequence[StoredEvent], checkpoints: Sequence[Checkpoint], exported_at: int
) -> bytes:
    """The export file of section 12.3: the header with the head (the last event), every event in
    the given order, and the checkpoints by hub key id in byte order, then by position.

    It never writes a stored value outside the form of its member. Of the stored values it checks
    the three whose form no column type or `CHECK` of section 11.3 guarantees: `received_at`, and
    a checkpoint's `published_at` and `hub_key_id`. At the first that fails, in the order of the
    file, it raises `ValueUnwritable` and returns nothing, so no partial file comes from here.
    `exported_at` is the caller's clock, no stored value: one outside the form of section 2 is a
    fault of the caller, `ValueError`.
    """
    exported = time_text(exported_at)
    if exported is None:
        raise ValueError("exported_at is outside the form of section 2")
    last = events[-1] if events else None
    out = [
        _line(
            {
                "format": EXPORT_FORMAT,
                "position": 0 if last is None else last.position,
                "record_hash": None if last is None else last.record_hash,
                "exported_at": exported,
            }
        )
    ]
    for stored in events:
        out.append(
            _line(
                {
                    "type": TYPE_EVENT,
                    "position": stored.position,
                    "received_at": _stored_time(stored.received_at),
                    "event_hash": stored.event_hash,
                    "record_hash": stored.record_hash,
                    "event": b64url_encode(stored.text),
                    "signature": b64url_encode(stored.signature),
                }
            )
        )
    # Byte order of the stored text (`COLLATE "C"`, section 12.1); a stored hub key id outside the
    # `id` rule can hold any character, so it is ordered by its UTF-8 bytes, which for a valid id
    # are its ASCII bytes.
    for checkpoint in sorted(checkpoints, key=lambda item: (item.hub_key_id.encode("utf-8"), item.position)):
        out.append(
            _line(
                {
                    "type": TYPE_CHECKPOINT,
                    "position": checkpoint.position,
                    "record_hash": checkpoint.record_hash,
                    "published_at": _stored_time(checkpoint.published_at),
                    "signed_by": _stored_key_id(checkpoint.hub_key_id),
                    "signature": b64url_encode(checkpoint.signature),
                }
            )
        )
    return b"".join(out)


# --- parsing -------------------------------------------------------------------------------------


class _Number:
    """A JSON number literal, kept as its text, so that `1.0`, `1e0` and `-0` stay distinguishable."""

    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        self.text = text


class _Members(list):  # type: ignore[type-arg]
    """The members of one JSON object in the order of the line, a member written twice included."""


@dataclass(frozen=True)
class _Line:
    """One line of the file read as JSON.

    `members` holds the members of the object when the line is one JSON text (RFC 8259) whose value
    is an object, whitespace around and between its tokens included; None otherwise. `twice` tells
    whether an object of the line, at any depth, has a member twice.
    """

    members: tuple[tuple[str, Any], ...] | None = dataclass_field(repr=False)
    twice: bool

    @property
    def valid(self) -> bool:
        """Item 3 of the list of section 12.3: one JSON object, and no object with a member twice."""
        return self.members is not None and not self.twice

    @property
    def counts_as_event_line(self) -> bool:
        """For counting positions (section 12.3): an object with exactly one member `type`, whose
        value is the string `event`. Whitespace and the other members of the line do not matter."""
        if self.members is None:
            return False
        types = [value for name, value in self.members if name == "type"]
        return len(types) == 1 and isinstance(types[0], str) and types[0] == TYPE_EVENT


def _refuse_constant(name: str) -> Any:
    """`NaN`, `Infinity` and `-Infinity` are no JSON (RFC 8259)."""
    raise ValueError("not JSON")


def _read_line(line: bytes) -> _Line:
    twice = False

    def members(pairs: list[tuple[str, Any]]) -> _Members:
        nonlocal twice
        names = [name for name, _ in pairs]
        if len(set(names)) != len(names):
            twice = True
        return _Members(pairs)

    try:
        value = json.loads(
            line.decode("utf-8"),
            object_pairs_hook=members,
            parse_int=_Number,
            parse_float=_Number,
            parse_constant=_refuse_constant,
        )
    except (ValueError, RecursionError):
        return _Line(members=None, twice=False)
    if not isinstance(value, _Members):
        return _Line(members=None, twice=False)
    return _Line(members=tuple(value), twice=twice)


def _render(value: object) -> str:
    """A value of a line in the writer's form. Items 4 and 5 left only `null`, a JSON integer
    literal in its form, and strings of characters from 0x21 to 0x7E other than `"` and `\\`."""
    if value is None:
        return "null"
    if isinstance(value, _Number):
        return value.text
    return '"' + str(value) + '"'


def _writer_form(value: Mapping[str, Any], order: Sequence[str]) -> bytes:
    """The bytes that `export` writes for this object (section 12.3, "writer's form")."""
    members = ",".join('"' + name + '":' + _render(value[name]) for name in order)
    return ("{" + members + "}").encode("utf-8")


def _position(value: object) -> int | None:
    if not isinstance(value, _Number) or _POSITION_RE.fullmatch(value.text) is None:
        return None
    number = int(value.text)
    return number if number <= MAX_POSITION else None


def _header(value: dict[str, Any]) -> ExportHeader | None:
    if value.keys() != HEADER_MEMBERS or value["format"] != EXPORT_FORMAT:
        return None
    raw_position = value["position"]
    position = 0 if isinstance(raw_position, _Number) and raw_position.text == "0" else _position(raw_position)
    record_hash = value["record_hash"]
    exported_at = parse_time(value["exported_at"])
    if position is None or exported_at is None:
        return None
    if record_hash is not None and not valid_hash(record_hash):
        return None
    return ExportHeader(position=position, record_hash=record_hash, exported_at=exported_at)


def _item(value: dict[str, Any]) -> StoredEvent | Checkpoint | None:
    kind = value.get("type")
    if kind == TYPE_EVENT:
        return _event_item(value)
    if kind == TYPE_CHECKPOINT:
        return _checkpoint_item(value)
    return None


def _signature(value: object) -> bytes | None:
    raw = b64url_decode(value)
    return raw if raw is not None and len(raw) == SIGNATURE_BYTES else None


def _event_item(value: dict[str, Any]) -> StoredEvent | None:
    if value.keys() != EVENT_MEMBERS:
        return None
    position = _position(value["position"])
    received_at = parse_time(value["received_at"])
    text = b64url_decode(value["event"])
    signature = _signature(value["signature"])
    if position is None or received_at is None or text is None or signature is None:
        return None
    if not valid_hash(value["event_hash"]) or not valid_hash(value["record_hash"]):
        return None
    return StoredEvent(
        position=position,
        received_at=received_at,
        event_hash=value["event_hash"],
        record_hash=value["record_hash"],
        text=text,
        signature=signature,
        copies=None,
    )


def _checkpoint_item(value: dict[str, Any]) -> Checkpoint | None:
    if value.keys() != CHECKPOINT_MEMBERS:
        return None
    position = _position(value["position"])
    published_at = parse_time(value["published_at"])
    signature = _signature(value["signature"])
    if position is None or published_at is None or signature is None:
        return None
    if not valid_hash(value["record_hash"]) or not valid_key_id(value["signed_by"]):
        return None
    return Checkpoint(
        hub_key_id=value["signed_by"],
        position=position,
        record_hash=value["record_hash"],
        published_at=published_at,
        signature=signature,
    )


def parse_export(data: bytes) -> ExportFile:
    """Check the file format of section 12.3, item by item of its list, and return the file.

    `ExportInvalid` with the position of section 12.3: the first item of the list that the file
    breaks decides, and within that item the first faulty line.
    """
    # Item 1, a fault of the whole file: position 0.
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        raise ExportInvalid(0) from None
    if not data.endswith(b"\n"):
        raise ExportInvalid(0)  # also the empty file
    lines = data[:-1].split(b"\n")
    read = [_read_line(line) for line in lines]
    # The number of event lines before each line; the first line is never an event line.
    before: list[int] = []
    count = 0
    for index, line in enumerate(read):
        before.append(count)
        if index > 0 and line.counts_as_event_line:
            count += 1

    def position_of(index: int) -> int:
        """Items 2, 3 and 6: 0 for the first line (the header line), otherwise the number of event
        lines before the faulty line plus 1. Items 5 and 7 never concern the first line."""
        return 0 if index == 0 else before[index] + 1

    # Item 2, an empty line.
    for index, raw in enumerate(lines):
        if not raw:
            raise ExportInvalid(position_of(index))
    # Item 3, a line that is not one JSON object, or an object with a member twice.
    for index, line in enumerate(read):
        if not line.valid:
            raise ExportInvalid(position_of(index))
    objects = [dict(line.members or ()) for line in read]
    # Item 4, the header: position 0.
    header = _header(objects[0])
    if header is None:
        raise ExportInvalid(0)
    # Item 5, the type and the members of every later line.
    items: list[StoredEvent | Checkpoint] = []
    for index in range(1, len(objects)):
        item = _item(objects[index])
        if item is None:
            raise ExportInvalid(position_of(index))
        items.append(item)
    # Item 6, the writer's form of every line.
    orders = [HEADER_ORDER] + [EVENT_ORDER if isinstance(item, StoredEvent) else CHECKPOINT_ORDER for item in items]
    for index, order in enumerate(orders):
        if lines[index] != _writer_form(objects[index], order):
            raise ExportInvalid(position_of(index))
    # Item 7, the order of event and checkpoint lines.
    previous: tuple[bytes, int] | None = None
    for index, item in enumerate(items, start=1):
        if isinstance(item, StoredEvent):
            if previous is not None:
                raise ExportInvalid(position_of(index))
            continue
        key = (item.hub_key_id.encode("ascii"), item.position)
        if previous is not None and key <= previous:
            raise ExportInvalid(position_of(index))
        previous = key
    events = tuple(item for item in items if isinstance(item, StoredEvent))
    checkpoints = tuple(item for item in items if isinstance(item, Checkpoint))
    # Item 8, the header agrees with the last event line: position 0.
    if events:
        if (header.position, header.record_hash) != (events[-1].position, events[-1].record_hash):
            raise ExportInvalid(0)
    elif header.position != 0 or header.record_hash is not None:
        raise ExportInvalid(0)
    # Item 9, a checkpoint line in a file without event lines: position 0.
    if not events and checkpoints:
        raise ExportInvalid(0)
    return ExportFile(header=header, events=events, checkpoints=checkpoints)


def verify_export(data: bytes, keys: KeyRing, hub_keys: Mapping[str, bytes]) -> FoldResult:
    """Offline `verify` of section 12.3 as a library function: the file format first, then section
    12.1 against the keys file, checking the checkpoints of the hub key ids in `hub_keys`."""
    try:
        export = parse_export(data)
    except ExportInvalid as invalid:
        return FoldResult(state=EMPTY_STATE, failure=Failure(FILE_INVALID, invalid.position))
    return verify_record(export.events, export.checkpoints, keys, hub_keys, None)
