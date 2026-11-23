"""Copied from agent-core src/agent_hub/crypto/fields.py at 99578e6bf451.

Field checks shared by the header formats of 04-crypto.md section 4 (all failures are `header_invalid`)."""

from __future__ import annotations

import re
from typing import cast

from agent_runner.crypto.encoding import b64u_decode, canonical, parse_time, require_keys
from agent_runner.crypto.errors import fail
from agent_runner.crypto.ids import is_uuid7

KIND_RE = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*")
KIND_MAX = 64


def header_dict(value: object, keys: frozenset[str]) -> dict[str, object]:
    """Exact key set (`header_invalid`), then printable ASCII and integer-only values (`canonical`)."""
    header = require_keys(value, keys)
    canonical(header)
    return header


def uuid(value: object) -> str:
    """A UUIDv7 in lowercase text form."""
    if not is_uuid7(value):
        fail("header_invalid")
    return cast(str, value)


def uuid_or_empty(value: object) -> str:
    """A UUIDv7 or `""`."""
    return "" if value == "" else uuid(value)


def time(value: object) -> str:
    """A header time `YYYY-MM-DDTHH:MM:SS.mmmZ`."""
    parse_time(value)
    return cast(str, value)


def b64u_bytes(value: object, length: int) -> str:
    """b64u text of exactly `length` bytes."""
    b64u_decode(value, length)
    return cast(str, value)


def kind(value: object) -> str:
    """A dotted lowercase kind name such as `session.output` (the closed lists live in `contracts/enums/`)."""
    if not isinstance(value, str) or len(value) > KIND_MAX or not KIND_RE.fullmatch(value):
        fail("header_invalid")
    return value


def literal(value: object, expected: object) -> None:
    """Exactly `expected` (of the same type; `True` is not `1`)."""
    if type(value) is not type(expected) or value != expected:
        fail("header_invalid")
