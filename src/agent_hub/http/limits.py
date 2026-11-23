"""Rate limits of the hub API (03-api.md section 1).

Anonymous requests: 30 per minute per source address; signed requests: 600 per minute per browser session key, 6000
per minute per device key (the platform root and agents share the browser limit, ADR-056). Each bucket is a token
bucket with the capacity of one minute and a steady refill, so a client may burst up to the per-minute limit. A refused
request gets `429 rate_limited` with `Retry-After` in whole seconds until the next token. The limiter is per hub process
and keeps at most `max_buckets` buckets (least recently used are dropped first).
"""

from __future__ import annotations

import math
import threading
from collections import OrderedDict
from dataclasses import dataclass

from agent_hub.clock import Clock

ANONYMOUS_PER_MINUTE = 30
SESSION_PER_MINUTE = 600
DEVICE_PER_MINUTE = 6000
ROOT_PER_MINUTE = 600
# Failed signature checks per source address and minute; beyond, signed requests of that source are refused before
# the gate reads their body or looks up their key.
FAILED_PER_MINUTE = 30
MAX_BUCKETS = 100_000
SECONDS_PER_MINUTE = 60.0

PER_MINUTE = {
    "anonymous": ANONYMOUS_PER_MINUTE,
    "session": SESSION_PER_MINUTE,
    "device": DEVICE_PER_MINUTE,
    "agent": SESSION_PER_MINUTE,
    "root": ROOT_PER_MINUTE,
    "failed": FAILED_PER_MINUTE,
}


@dataclass
class _Bucket:
    tokens: float
    at: float


class RateLimiter:
    """Token buckets keyed by `(class, identity)`; the time comes from the injected clock."""

    def __init__(self, clock: Clock, *, max_buckets: int = MAX_BUCKETS) -> None:
        self._clock = clock
        self._max_buckets = max_buckets
        self._buckets: OrderedDict[tuple[str, str], _Bucket] = OrderedDict()
        self._lock = threading.Lock()

    def _bucket(self, kind: str, identity: str) -> tuple[_Bucket, float]:
        """The refilled bucket (moved to the most recently used end) and its refill rate; holds no lock."""
        limit = PER_MINUTE[kind]
        rate = limit / SECONDS_PER_MINUTE
        now = self._clock().timestamp()
        key = (kind, identity)
        bucket = self._buckets.pop(key, None)
        if bucket is None:
            bucket = _Bucket(tokens=float(limit), at=now)
        else:
            bucket.tokens = min(float(limit), bucket.tokens + max(0.0, now - bucket.at) * rate)
            bucket.at = now
        self._buckets[key] = bucket
        while len(self._buckets) > self._max_buckets:
            self._buckets.popitem(last=False)
        return bucket, rate

    def hit(self, kind: str, identity: str) -> int | None:
        """Take one token of the bucket; None when allowed, else the `Retry-After` seconds."""
        with self._lock:
            bucket, rate = self._bucket(kind, identity)
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return None
            return max(1, math.ceil((1.0 - bucket.tokens) / rate))

    def blocked(self, kind: str, identity: str) -> int | None:
        """Like `hit` without taking a token: the `Retry-After` seconds of an exhausted bucket, else None."""
        with self._lock:
            bucket, rate = self._bucket(kind, identity)
            return None if bucket.tokens >= 1.0 else max(1, math.ceil((1.0 - bucket.tokens) / rate))

    def __len__(self) -> int:
        return len(self._buckets)
