"""Failed session-token checks: counted per connecting UID, limited and journaled (ATK-36, decisions ATS-02).

The control process resolves every bearer token of ``/mcp``, ``/hooks/<Event>`` and ``/model/…`` (op
``session.resolve``); the API process sends the token's SHA-256 and the UID owning the client socket (``peer.py``,
``None`` when binding is off). A check fails when the token is missing or unknown, belongs to an inactive session, or
belongs to a session of another UID (a foreign token). Token comparison itself is constant-time
(``State.resolve``); a successful check never touches the guard, so a misbehaving session cannot lock others out
and its own valid requests keep working.

Per connecting UID at most ``LIMIT`` failures within ``WINDOW_SECONDS`` are answered ``401`` and journaled as
``guest.error {code:"auth_failed"}``; the next failure inside the window starts the limit: it is journaled once as
``auth_rate_limited`` and every failure of that UID is answered ``429`` (``Retry-After`` = seconds until the oldest
counted failure leaves the window) without a record until the window has room again. The record carries the session
of the connecting UID when one is active, else none (VM level). At most ``MAX_KEYS`` UIDs are tracked (the oldest is
dropped), so the guard's memory is bounded.
"""

from __future__ import annotations

import collections
import math
import threading
from collections.abc import Callable
from dataclasses import dataclass

LIMIT = 10
WINDOW_SECONDS = 60.0
MAX_KEYS = 1024


@dataclass(frozen=True)
class Failure:
    """Outcome of one failed check: ``record`` is the journal code to write (or ``None``), ``retry_after`` is set
    while the limit holds."""

    record: str | None
    retry_after: int | None


class AuthGuard:
    def __init__(
        self,
        clock: Callable[[], float],
        *,
        limit: int = LIMIT,
        window: float = WINDOW_SECONDS,
        max_keys: int = MAX_KEYS,
    ) -> None:
        self._clock = clock
        self._limit = limit
        self._window = window
        self._max_keys = max_keys
        self._lock = threading.Lock()
        self._failures: collections.OrderedDict[int | None, collections.deque[float]] = collections.OrderedDict()
        self._limited: set[int | None] = set()

    def fail(self, key: int | None) -> Failure:
        """Count one failed check of `key` (the connecting UID) and say what to journal and answer."""
        now = self._clock()
        with self._lock:
            times = self._failures.pop(key, None)
            if times is None:
                times = collections.deque()
                while len(self._failures) >= self._max_keys:
                    dropped, _ = self._failures.popitem(last=False)
                    self._limited.discard(dropped)
            self._failures[key] = times
            while times and times[0] <= now - self._window:
                times.popleft()
            if len(times) < self._limit:
                times.append(now)
                self._limited.discard(key)
                return Failure("auth_failed", None)
            retry = max(1, math.ceil(times[0] + self._window - now))
            if key in self._limited:
                return Failure(None, retry)
            self._limited.add(key)
            return Failure("auth_rate_limited", retry)
