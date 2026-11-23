"""Time and identifiers of the guest agent: an injectable clock, ISO-8601 texts and UUIDv7 (03-api.md section 1).

* ``Clock`` - protocol with ``now() -> float`` (seconds since the epoch); ``SystemClock`` reads ``time.time``,
  tests inject a fake clock
* ``iso_ms(seconds) -> str`` - ``YYYY-MM-DDTHH:MM:SS.mmmZ``
* ``uuid7(seconds, random_bytes=os.urandom) -> str`` - UUIDv7 text (RFC 9562) with the millisecond timestamp
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float: ...


class SystemClock:
    def now(self) -> float:
        return time.time()


def iso_ms(seconds: float) -> str:
    moment = datetime.fromtimestamp(int(seconds * 1000) / 1000, tz=UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def uuid7(seconds: float, random_bytes: Callable[[int], bytes] = os.urandom) -> str:
    millis = int(seconds * 1000) & ((1 << 48) - 1)
    rand = int.from_bytes(random_bytes(10), "big")
    rand_a = rand >> 68 & 0xFFF
    rand_b = rand & ((1 << 62) - 1)
    value = millis << 80 | 0x7 << 76 | rand_a << 64 | 0b10 << 62 | rand_b
    return str(uuid.UUID(int=value))
