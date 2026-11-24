"""Copied from src/agent_hub/crypto/encoding.py.

Encodings of 04-crypto.md section 1: strict base64url, canonical JSON, body JSON, times and the domain prefix.

The format checks are written after Spike D `e2ee.py` (`b64u`, `canonical`), made strict: the base64url decoder
refuses padding and non-canonical trailing bits, and canonical JSON checks every string instead of the serialised
text (a literal backslash followed by `u` is printable ASCII and allowed).
"""

from __future__ import annotations

import base64
import json
import re
from datetime import UTC, datetime

from agent_runner.crypto.errors import fail

P = b"agent.dfx.swiss/v1/"
"""The protocol prefix of every info, AAD and signature label (a protocol constant, not a host name)."""

MAX_SAFE_INTEGER = 2**53 - 1
"""Integers in canonical JSON stay within the exact range of a JavaScript number (the TypeScript twin)."""

_B64U_RE = re.compile(r"[A-Za-z0-9_-]*")
_TIME_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z")
_PRINTABLE_RE = re.compile(r"[\x20-\x7e]*")

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


def b64u_encode(raw: bytes) -> str:
    """base64url without padding."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def is_b64u(text: str) -> bool:
    """True for strict base64url text: url-safe alphabet, no padding, canonical trailing bits."""
    if len(text) % 4 == 1 or not _B64U_RE.fullmatch(text):
        return False
    return b64u_encode(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))) == text


def b64u_decode(text: object, length: int | None = None) -> bytes:
    """Decode strict base64url: no padding, only the url-safe alphabet, canonical trailing bits.

    `length` additionally requires the exact decoded size. Every violation is `header_invalid` (a malformed
    field of a structure).
    """
    if not isinstance(text, str) or not is_b64u(text):
        fail("header_invalid")
    raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if length is not None and len(raw) != length:
        fail("header_invalid")
    return raw


def is_printable_ascii(text: str) -> bool:
    """Only the characters 0x20-0x7E."""
    return _PRINTABLE_RE.fullmatch(text) is not None


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for key, item in value.items() for text in (*_strings(key), *_strings(item))]
    if isinstance(value, list | tuple):
        return [text for item in value for text in _strings(item)]
    return []


def _types_valid(value: object) -> bool:
    if isinstance(value, str | bool) or value is None:
        return True
    if isinstance(value, int):
        return -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER
    if isinstance(value, dict):
        return all(isinstance(key, str) and _types_valid(item) for key, item in value.items())
    if isinstance(value, list | tuple):
        return all(_types_valid(item) for item in value)
    # floats and every other type
    return False


def canonical(value: object) -> bytes:
    """Canonical JSON (section 1): sorted keys, `,` and `:`, printable ASCII only, integers only.

    A string or key outside 0x20-0x7E is `non_ascii` (checked first, over the whole value); then a float, a
    non-string key, an integer outside the JavaScript safe range or any other type is `header_invalid`.
    """
    if not all(is_printable_ascii(text) for text in _strings(value)):
        fail("non_ascii")
    if not _types_valid(value):
        fail("header_invalid")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def body_json(value: object) -> bytes:
    """Body JSON (section 1): UTF-8, sorted keys, no whitespace, non-ASCII kept; the plaintext of ciphertexts.

    NaN/Infinity and values that are not JSON (including lone surrogates) are `header_invalid`.
    """
    raw: bytes | None
    try:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        raw = text.encode("utf-8")
    except (TypeError, ValueError):
        raw = None
    if raw is None:
        fail("header_invalid")
    return raw


def _refuse_constant(_name: str) -> object:
    fail("header_invalid")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, item in pairs:
        if key in result:
            fail("header_invalid")
        result[key] = item
    return result


def parse_json(raw: bytes) -> object:
    """Parse UTF-8 JSON strictly: no repeated keys, no NaN/Infinity; anything else is `header_invalid`."""
    parsed: list[object] = []
    try:
        parsed.append(
            json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_refuse_constant)
        )
    except ValueError:
        # the decode error holds the input (decrypted plaintext); it must not become the context of the refusal
        parsed.clear()
    if not parsed:
        fail("header_invalid")
    return parsed[0]


def format_time(moment: datetime) -> str:
    """`YYYY-MM-DDTHH:MM:SS.mmmZ` in UTC with milliseconds (header times, section 1)."""
    utc = moment.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def parse_time(text: object) -> datetime:
    """The inverse of `format_time`; any other form is `header_invalid`."""
    if not isinstance(text, str) or not _TIME_RE.fullmatch(text):
        fail("header_invalid")
    moment: datetime | None
    try:
        moment = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError:
        moment = None
    if moment is None:
        fail("header_invalid")
    return moment


def require_bytes(raw: object, length: int, code: str = "header_invalid") -> bytes:
    """`raw` as bytes of exactly `length` bytes, else `code`."""
    if not isinstance(raw, bytes) or len(raw) != length:
        fail(code)
    return raw


def require_keys(value: object, keys: frozenset[str]) -> dict[str, object]:
    """`value` as a dict with exactly the key set `keys` (missing or extra keys are `header_invalid`)."""
    if not isinstance(value, dict) or set(value) != keys:
        fail("header_invalid")
    return value


def require_int(value: object, minimum: int, maximum: int = MAX_SAFE_INTEGER) -> int:
    """An integer (not a boolean) within `minimum..maximum`, else `header_invalid`."""
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        fail("header_invalid")
    return value
