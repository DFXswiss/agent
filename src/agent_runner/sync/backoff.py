"""Capped exponential backoff with jitter (03-api.md section 6.2; legacy `agent_cli.main.cmd_sync` rule).

1 s doubling to 30 s, each delay multiplied by a factor drawn from [0.8, 1.2] (±20 %); `reset()` after a stable
period starts again at 1 s. The random source is injected for tests.
"""

from __future__ import annotations

import random as _random
from collections.abc import Callable

BASE_S = 1.0
CAP_S = 30.0
JITTER = 0.2
MAX_DOUBLINGS = 64


class Backoff:
    def __init__(
        self,
        *,
        base: float = BASE_S,
        cap: float = CAP_S,
        jitter: float = JITTER,
        random: Callable[[], float] = _random.random,
    ) -> None:
        self.base = base
        self.cap = cap
        self.jitter = jitter
        self._random = random
        self.failures = 0

    def next(self) -> float:
        """The delay before the next attempt; every call doubles the nominal delay up to the cap."""
        nominal = min(self.cap, self.base * 2 ** min(self.failures, MAX_DOUBLINGS))
        self.failures = min(self.failures + 1, MAX_DOUBLINGS)
        return float(nominal * (1.0 + self.jitter * (2.0 * self._random() - 1.0)))

    def reset(self) -> None:
        self.failures = 0
