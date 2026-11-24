"""The hub clock: every module that needs the time takes a `Clock` (07-testing.md section 1: tests inject a fixed
clock)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

Clock = Callable[[], datetime]


def system_clock() -> datetime:
    """The current time in UTC."""
    return datetime.now(UTC)


def epoch_seconds(clock: Clock) -> int:
    """The clock's time in whole seconds since the epoch (the unit of `agent_core.signing`)."""
    return int(clock().timestamp())
