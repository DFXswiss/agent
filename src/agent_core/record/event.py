"""The event of the team record (RECORD-PROTOCOL.md section 4).

Pure functions: no database, no HTTP and no clock. A refusal is a `RecordRefusal` whose message
is only the error value of the specification, never a value of the event (section 13).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import TYPE_CHECKING, Iterable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ..signing import (
    SIGNATURE_BYTES,
    b64url_decode,
    b64url_encode,
    format_time,
    parse_time,
    sha256_hex,
    valid_key_id,
)

if TYPE_CHECKING:
    from ..keys import KeyRing

EVENT_LABEL = "agent-event-v1"
RECORD_LABEL = "agent-record-v1"
MAX_EVENT_BYTES = 65536
MAX_CHANGES = 15
HEADER_LINES = 9
LINES_PER_CHANGE = 3
OBJECT_TYPES = ("task", "comment")
NO_VALUE = "-"
VALUE_PREFIX = "="
FIRST_PREV_HASH = "-"
NIL_UUID = "00000000-0000-0000-0000-000000000000"
MAX_POSITION = 9223372036854775807
# Section 4.5 and check 10 of section 6.7: `signed_at` is at most this far after now (at write
# time) or after `received_at` (in a fold, section 12.1).
SIGNED_AT_AHEAD_SECONDS = 120

ERROR_EVENT_INVALID = "event_invalid"
ERROR_KEY_UNKNOWN = "key_unknown"
ERROR_EVENT_SIGNATURE_INVALID = "event_signature_invalid"
ERROR_CHAIN_BROKEN = "chain_broken"
ERROR_EVENT_TIME_INVALID = "event_time_invalid"

_LINE_RE = re.compile(rb"[\x21-\x7e]+")
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_COUNT_RE = re.compile(r"[1-9][0-9]?")


class RecordRefusal(Exception):
    """A refusal with one error or failure value of RECORD-PROTOCOL.md; the message is that value."""

    def __init__(self, error: str) -> None:
        super().__init__(error)
        self.error = error


@dataclass(frozen=True)
class Change:
    """One change of an event: the field and its old and new value (None is `-`)."""

    field: str
    old: bytes | None = dataclass_field(repr=False)
    new: bytes | None = dataclass_field(repr=False)


@dataclass(frozen=True)
class Event:
    """A parsed event text. `text` is the signed bytes; `event_hash` is section 4.4."""

    text: bytes = dataclass_field(repr=False)
    event_hash: str
    event_id: str
    object_type: str
    object_id: str
    actor_key_id: str
    signed_at: int
    prev_hash: str
    key_certificate: bytes | None = dataclass_field(repr=False)
    changes: tuple[Change, ...] = dataclass_field(repr=False)


@dataclass(frozen=True)
class ChainHead:
    """The last accepted event of one key (section 4.5)."""

    event_hash: str
    signed_at: int


def valid_uuid(value: object) -> bool:
    """Section 2: lowercase 8-4-4-4-12 hex, any version, not the nil UUID."""
    return isinstance(value, str) and _UUID_RE.fullmatch(value) is not None and value != NIL_UUID


def valid_hash(value: object) -> bool:
    """SHA-256 as 64 lowercase hex characters."""
    return isinstance(value, str) and _HASH_RE.fullmatch(value) is not None


def encode_value_token(value: bytes | None) -> str:
    """`-` for no value, otherwise `=` and the base64url of the bytes. A value is never empty."""
    if value is None:
        return NO_VALUE
    if not value:
        raise ValueError("a value is never empty")
    return VALUE_PREFIX + b64url_encode(value)


def decode_value_token(token: str) -> bytes | None:
    """The value of a value token, None for `-`; `event_invalid` for any other form."""
    if token == NO_VALUE:
        return None
    if not token.startswith(VALUE_PREFIX):
        raise RecordRefusal(ERROR_EVENT_INVALID)
    value = b64url_decode(token[len(VALUE_PREFIX) :])
    if not value:
        raise RecordRefusal(ERROR_EVENT_INVALID)
    return value


def event_hash(text: bytes) -> str:
    """Section 4.4: the SHA-256 of the event text bytes."""
    return sha256_hex(text)


def parse_event(text: object) -> Event:
    """Check the form of section 4.2 and return the event; `event_invalid` for any break."""
    invalid = RecordRefusal(ERROR_EVENT_INVALID)
    if not isinstance(text, bytes) or len(text) > MAX_EVENT_BYTES:
        raise invalid
    raw_lines = text.split(b"\n")
    if not all(_LINE_RE.fullmatch(line) for line in raw_lines):
        raise invalid
    lines = [line.decode("ascii") for line in raw_lines]
    if len(lines) < HEADER_LINES or lines[0] != EVENT_LABEL:
        raise invalid
    event_id, object_type, object_id, actor_key_id, signed_at_text, prev_hash = lines[1:7]
    certificate, count = lines[7:9]
    if _COUNT_RE.fullmatch(count) is None or int(count) > MAX_CHANGES:
        raise invalid
    if len(lines) != HEADER_LINES + LINES_PER_CHANGE * int(count):
        raise invalid
    if not valid_uuid(event_id) or not valid_uuid(object_id):
        raise invalid
    if object_type not in OBJECT_TYPES or not valid_key_id(actor_key_id):
        raise invalid
    signed_at = parse_time(signed_at_text)
    if signed_at is None:
        raise invalid
    if prev_hash != FIRST_PREV_HASH and not valid_hash(prev_hash):
        raise invalid
    key_certificate = decode_value_token(certificate)
    changes: list[Change] = []
    for start in range(HEADER_LINES, len(lines), LINES_PER_CHANGE):
        name, old, new = lines[start : start + LINES_PER_CHANGE]
        if changes and name <= changes[-1].field:
            raise invalid
        if old == new:
            raise invalid
        changes.append(Change(name, decode_value_token(old), decode_value_token(new)))
    return Event(
        text=text,
        event_hash=event_hash(text),
        event_id=event_id,
        object_type=object_type,
        object_id=object_id,
        actor_key_id=actor_key_id,
        signed_at=signed_at,
        prev_hash=prev_hash,
        key_certificate=key_certificate,
        changes=tuple(changes),
    )


def build_event_text(
    *,
    event_id: str,
    object_type: str,
    object_id: str,
    actor_key_id: str,
    signed_at: int,
    prev_hash: str,
    changes: Iterable[Change],
    key_certificate: bytes | None = None,
) -> bytes:
    """The event text of section 4.2 with the changes in field order; `event_invalid` if it breaks a rule."""
    ordered = sorted(changes, key=lambda change: change.field)
    lines = [
        EVENT_LABEL,
        event_id,
        object_type,
        object_id,
        actor_key_id,
        format_time(signed_at),
        prev_hash,
        _token(key_certificate),
        str(len(ordered)),
    ]
    for change in ordered:
        lines += [change.field, _token(change.old), _token(change.new)]
    text = "\n".join(lines).encode("utf-8")
    parse_event(text)
    return text


def _token(value: bytes | None) -> str:
    try:
        return encode_value_token(value)
    except ValueError:
        raise RecordRefusal(ERROR_EVENT_INVALID) from None


def signature_valid(public_key: bytes, text: bytes, signature: bytes) -> bool:
    """Plain Ed25519 over the text (section 4.3)."""
    if len(signature) != SIGNATURE_BYTES:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, text)
    except (InvalidSignature, ValueError):
        return False
    return True


def verify_event_signature(keys: "KeyRing", event: Event, signature: bytes) -> None:
    """Section 4.3 and the rows `key_unknown` and `event_signature_invalid` of section 12.1."""
    entry = keys.get(event.actor_key_id)
    if entry is None or entry.kind != "ed25519":
        raise RecordRefusal(ERROR_KEY_UNKNOWN)
    if not signature_valid(entry.public_key, event.text, signature):
        raise RecordRefusal(ERROR_EVENT_SIGNATURE_INVALID)


def chain_head(event: Event) -> ChainHead:
    """The head of the key's chain after `event` was accepted."""
    return ChainHead(event_hash=event.event_hash, signed_at=event.signed_at)


def check_chain(head: ChainHead | None, event: Event) -> None:
    """Section 4.5: `prev_hash` is the hash of the key's last accepted event, `-` for its first."""
    expected = FIRST_PREV_HASH if head is None else head.event_hash
    if event.prev_hash != expected:
        raise RecordRefusal(ERROR_CHAIN_BROKEN)


def check_signed_at(head: ChainHead | None, event: Event, latest: int) -> None:
    """`signed_at` is at most 120 seconds after `latest` and not earlier than the key's last event.

    `latest` is now at write time (check 10 of section 6.7) and `received_at` in a fold (section
    12.1).
    """
    if event.signed_at > latest + SIGNED_AT_AHEAD_SECONDS:
        raise RecordRefusal(ERROR_EVENT_TIME_INVALID)
    if head is not None and event.signed_at < head.signed_at:
        raise RecordRefusal(ERROR_EVENT_TIME_INVALID)


def next_received_at(now: int, previous: int | None) -> int:
    """Section 4.6: the later of now and the `received_at` of the previous position."""
    return now if previous is None else max(now, previous)


def record_hash(position: int, received_at: int, event_hash_hex: str, previous: str | None) -> str:
    """Section 4.6: the record hash of one position; `previous` is None exactly at position 1."""
    if not 1 <= position <= MAX_POSITION:
        raise ValueError("a position is 1 to 9223372036854775807")
    if (previous is None) != (position == 1):
        raise ValueError("only position 1 has no previous record hash")
    if not valid_hash(event_hash_hex) or (previous is not None and not valid_hash(previous)):
        raise ValueError("a hash is 64 lowercase hex characters")
    lines = [
        RECORD_LABEL,
        str(position),
        format_time(received_at),
        event_hash_hex,
        FIRST_PREV_HASH if previous is None else previous,
    ]
    return sha256_hex("\n".join(lines).encode("ascii"))
