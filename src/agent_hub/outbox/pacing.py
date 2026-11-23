"""The push rate of a device (03-api.md section 6.1, KOR-13, ADR-048).

Beside the general device rate limit of the pipeline (6000 requests per minute and device key), the outbox paces the
pushes of each device with a token bucket: the rate is one push per `MIN_INTERVAL_SECONDS` (the runner's start-to-start
interval) and the reserve `BURST` pushes. Arrival times jitter by the way (a wireless link, a slow previous answer), so
a runner that starts its pushes at least `MIN_INTERVAL_SECONDS` apart may see two arrive closer together; the reserve
absorbs a spread of travel times up to `(BURST - 1) * MIN_INTERVAL_SECONDS`, so a conforming runner never gets `429`.
A runner also sends a push whose answer is late once more (ADR-050), at most one such copy every 2 s; a copy spends
a push like any other, and since a copied push lasts longer than the interval the starts and copies together stay
below the rate.
A device that pushes faster for good is held to the rate: once the reserve is spent a push is `429 rate_limited` with
`Retry-After: 1` (whole seconds, the smallest value the header can carry) and is not processed; a refused push spends
nothing. At the full rate a device sends at most `60 / MIN_INTERVAL_SECONDS + BURST` pushes per minute, far inside
the general limit. Like the pipeline's limiter the pacer is per hub process, keyed by the hub context's limiter (one
per running app), and keeps at most `MAX_DEVICES` devices (least recently seen dropped).
"""

from __future__ import annotations

import threading
import weakref
from collections import OrderedDict
from dataclasses import dataclass
from uuid import UUID

from agent_hub.clock import Clock
from agent_hub.http.limits import RateLimiter

MIN_INTERVAL_SECONDS = 0.1
BURST = 10
RETRY_AFTER_SECONDS = 1
MAX_DEVICES = 100_000


@dataclass
class _Bucket:
    tokens: float
    at: float


class Pacer:
    """The push reserve of every device."""

    def __init__(self, clock: Clock, *, max_devices: int = MAX_DEVICES) -> None:
        self._clock = clock
        self._max_devices = max_devices
        self._buckets: OrderedDict[UUID, _Bucket] = OrderedDict()
        self._lock = threading.Lock()

    def admit(self, device: UUID) -> int | None:
        """None when the push may run (and spend one push of the reserve), else the `Retry-After` seconds."""
        now = self._clock().timestamp()
        with self._lock:
            bucket = self._buckets.pop(device, None)
            if bucket is None:
                bucket = _Bucket(tokens=float(BURST), at=now)
            else:
                refill = max(0.0, now - bucket.at) / MIN_INTERVAL_SECONDS
                bucket.tokens = min(float(BURST), bucket.tokens + refill)
                bucket.at = now
            self._buckets[device] = bucket
            while len(self._buckets) > self._max_devices:
                self._buckets.popitem(last=False)
            if bucket.tokens < 1.0:
                return RETRY_AFTER_SECONDS
            bucket.tokens -= 1.0
            return None

    def __len__(self) -> int:
        return len(self._buckets)


_PACERS: weakref.WeakKeyDictionary[RateLimiter, Pacer] = weakref.WeakKeyDictionary()
_PACERS_LOCK = threading.Lock()


def pacer_of(limiter: RateLimiter, clock: Clock) -> Pacer:
    """The pacer of one running app (identified by its rate limiter)."""
    with _PACERS_LOCK:
        found = _PACERS.get(limiter)
        if found is None:
            found = _PACERS[limiter] = Pacer(clock)
        return found
