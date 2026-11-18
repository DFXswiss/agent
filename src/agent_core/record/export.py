"""The export file of the record and its offline verification (RECORD-PROTOCOL.md section 12.3).

`write_export` writes the JSON Lines file; `parse_export` checks the file format in the order of
the list of section 12.3 and refuses it with `file_invalid` and the position that section defines;
`verify_export` is offline `verify` as a library function. The commands come with the storage part.
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
from .state import EMPTY_STATE
from .verify import FILE_INVALID, Failure, FoldResult, StoredEvent, verify_record

EXPORT_FORMAT = "agent-record-export-v1"
HEADER_MEMBERS = frozenset({"format", "position", "record_hash", "exported_at"})
EVENT_MEMBERS = frozenset(
    {"type", "position", "received_at", "event_hash", "record_hash", "event", "signature"}
)
CHECKPOINT_MEMBERS = frozenset(
    {"type", "position", "record_hash", "published_at", "signed_by", "signature"}
)
TYPE_EVENT = "event"
TYPE_CHECKPOINT = "checkpoint"

_POSITION_RE = re.compile(r"[1-9][0-9]{0,18}")
# JSON whitespace; the file is written without whitespace (section 12.3).
_WHITESPACE = (b" ", b"\t", b"\r")


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


def _time(seconds: int) -> str:
    text = time_text(seconds)
    if text is None:
        raise ValueError("a time of the export is outside the form of section 2")
    return text


def write_export(
    events: Sequence[StoredEvent], checkpoints: Sequence[Checkpoint], exported_at: int
) -> bytes:
    """The export file of section 12.3: the header with the head (the last event), every event in
    the given order, and the checkpoints by hub key id in byte order, then by position."""
    last = events[-1] if events else None
    out = [
        _line(
            {
                "format": EXPORT_FORMAT,
                "position": 0 if last is None else last.position,
                "record_hash": None if last is None else last.record_hash,
                "exported_at": _time(exported_at),
            }
        )
    ]
    for stored in events:
        out.append(
            _line(
                {
                    "type": TYPE_EVENT,
                    "position": stored.position,
                    "received_at": _time(stored.received_at),
                    "event_hash": stored.event_hash,
                    "record_hash": stored.record_hash,
                    "event": b64url_encode(stored.text),
                    "signature": b64url_encode(stored.signature),
                }
            )
        )
    for checkpoint in sorted(checkpoints, key=lambda item: (item.hub_key_id.encode("ascii"), item.position)):
        out.append(
            _line(
                {
                    "type": TYPE_CHECKPOINT,
                    "position": checkpoint.position,
                    "record_hash": checkpoint.record_hash,
                    "published_at": _time(checkpoint.published_at),
                    "signed_by": checkpoint.hub_key_id,
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


def _unique_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, value in pairs:
        if name in out:
            raise ValueError("a member twice")
        out[name] = value
    return out


def _refuse_constant(name: str) -> Any:
    raise ValueError("not JSON")


def _json_object(line: bytes) -> dict[str, Any] | None:
    """The object of a line that is exactly one JSON object without whitespace and without a member
    twice; None otherwise."""
    if any(space in line for space in _WHITESPACE):
        return None
    try:
        value = json.loads(
            line.decode("utf-8"),
            object_pairs_hook=_unique_members,
            parse_int=_Number,
            parse_float=_Number,
            parse_constant=_refuse_constant,
        )
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


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
    # Item 1, the faults of the whole file (position 0).
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        raise ExportInvalid(0) from None
    if not data.endswith(b"\n"):
        raise ExportInvalid(0)  # also the empty file
    lines = data[:-1].split(b"\n")
    parsed = [_json_object(line) for line in lines]
    # The position of a fault of a line: 0 for the header line, otherwise the number of event lines
    # before it plus 1. An event line is a later line that is a JSON object with `type` `event`.
    before: list[int] = []
    count = 0
    for index, value in enumerate(parsed):
        before.append(count)
        if index > 0 and value is not None and value.get("type") == TYPE_EVENT:
            count += 1

    def position_of(index: int) -> int:
        return 0 if index == 0 else before[index] + 1

    # Item 1, an empty line.
    for index, line in enumerate(lines):
        if not line:
            raise ExportInvalid(position_of(index))
    # Item 2.
    for index, value in enumerate(parsed):
        if value is None:
            raise ExportInvalid(position_of(index))
    objects = [value for value in parsed if value is not None]
    # Item 3.
    header = _header(objects[0])
    if header is None:
        raise ExportInvalid(0)
    # Item 4.
    items: list[StoredEvent | Checkpoint] = []
    for index in range(1, len(objects)):
        item = _item(objects[index])
        if item is None:
            raise ExportInvalid(position_of(index))
        items.append(item)
    # Item 5.
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
    # Item 6.
    if events:
        if (header.position, header.record_hash) != (events[-1].position, events[-1].record_hash):
            raise ExportInvalid(0)
    elif header.position != 0 or header.record_hash is not None:
        raise ExportInvalid(0)
    # Item 7.
    if not events and checkpoints:
        raise ExportInvalid(0)
    return ExportFile(header=header, events=events, checkpoints=checkpoints)


def verify_export(data: bytes, keys: KeyRing, hub_keys: Mapping[str, bytes]) -> FoldResult:
    """Offline `verify` of section 12.3 as a library function: the file format first, then section
    12.1 against the keys file, checking the checkpoints of the hub key ids in `hub_keys`."""
    try:
        export = parse_export(data)
    except ExportInvalid as invalid:
        return FoldResult(state=EMPTY_STATE, failure=Failure(FILE_INVALID, invalid.position, None))
    return verify_record(export.events, export.checkpoints, keys, hub_keys, None)
