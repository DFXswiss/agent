"""Copied from agent-core src/agent_hub/crypto/ids.py at 56db2db59b0e.

UUIDv7 identifiers (04-crypto.md section 1, RFC 9562): lowercase text with dashes, made by the object creator."""

from __future__ import annotations

import os
import re
import time

from agent_runner.crypto.errors import fail

UUID7_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_RANDOM_BYTES = 10
_MAX_MS = 2**48 - 1


def uuid7(unix_ms: int | None = None, random: bytes | None = None) -> str:
    """A UUIDv7: 48-bit Unix milliseconds, version 7, 12 + 62 random bits, RFC 4122 variant.

    `unix_ms` and `random` (10 bytes; the top 4 bits of byte 0 and the top 2 bits of byte 2 are replaced by
    version and variant) are parameters only for deterministic test vectors.
    """
    ms = time.time_ns() // 1_000_000 if unix_ms is None else unix_ms
    rand = os.urandom(_RANDOM_BYTES) if random is None else random
    if not 0 <= ms <= _MAX_MS or len(rand) != _RANDOM_BYTES:
        fail("header_invalid")
    raw = bytearray(ms.to_bytes(6, "big") + rand)
    raw[6] = 0x70 | (raw[6] & 0x0F)
    raw[8] = 0x80 | (raw[8] & 0x3F)
    text = raw.hex()
    return f"{text[:8]}-{text[8:12]}-{text[12:16]}-{text[16:20]}-{text[20:]}"


def is_uuid7(value: object) -> bool:
    """True for the lowercase text form of a version-7 UUID with the RFC 4122 variant."""
    return isinstance(value, str) and UUID7_RE.fullmatch(value) is not None


def uuid7_ms(value: str) -> int:
    """The Unix milliseconds of a UUIDv7 (`header_invalid` for anything else)."""
    if not is_uuid7(value):
        fail("header_invalid")
    return int(value[:8] + value[9:13], 16)
