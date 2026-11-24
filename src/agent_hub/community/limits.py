"""The per-user post limit of the community (14-community.md section 4.3, ADR-069 decision 7).

A token bucket per user with `burst` tokens, refilled with one token every `refill_seconds`; `acquire` checks and takes
a token in one step (concurrent attempts of one user cannot exceed the burst). The same class limits new key requests
(`REQUEST_BURST`, `REQUEST_REFILL_SECONDS`). Held per hub process like the other limits (one hub
process per database, 03-api.md section 2); at most `MAX_USERS` buckets, least recently used dropped first.
"""

from __future__ import annotations

import math
import threading
from collections import OrderedDict

from agent_hub.clock import Clock

MAX_USERS = 100_000
REQUEST_BURST = 3
REQUEST_REFILL_SECONDS = 20.0


class PostLimiter:
    """Token buckets of posts keyed by user ID."""

    def __init__(self, clock: Clock, *, burst: int, refill_seconds: float, max_users: int = MAX_USERS) -> None:
        self._clock = clock
        self._burst = float(burst)
        self._refill = refill_seconds
        self._max_users = max_users
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._lock = threading.Lock()

    def _tokens(self, user: str) -> float:
        now = self._clock().timestamp()
        tokens, at = self._buckets.pop(user, (self._burst, now))
        tokens = min(self._burst, tokens + max(0.0, now - at) / self._refill)
        self._buckets[user] = (tokens, now)
        while len(self._buckets) > self._max_users:
            self._buckets.popitem(last=False)
        return tokens

    def acquire(self, user: str) -> int | None:
        """Take a token and return None, or return the `Retry-After` seconds while the user has none."""
        with self._lock:
            tokens = self._tokens(user)
            if tokens < 1.0:
                return max(1, math.ceil((1.0 - tokens) * self._refill))
            self._buckets[user] = (tokens - 1.0, self._buckets[user][1])
            return None

    def __len__(self) -> int:
        return len(self._buckets)
