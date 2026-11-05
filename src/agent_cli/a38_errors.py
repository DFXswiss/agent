"""Shared A38 guard errors.

This module keeps exception identity stable when ``a38_guard`` is executed as
``__main__`` while helper modules import it through its package name.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


class GuardError(RuntimeError):
    """Loud failure of the guard (API, config, bounds). Not a soft report miss."""


class RateLimitError(GuardError):
    """GitHub REST rate limit. Not an authorisation 401 or 403.

    ``retry_at`` is when the limit lifts. ``results`` is set when a sweep stops
    after some pull requests have already been reconciled. ``completed`` records
    legitimate writes and later steps that finished before the limit was hit.
    """

    def __init__(
        self,
        message: str,
        *,
        retry_at: datetime | None,
        retry_epoch: int | None = None,
    ) -> None:
        super().__init__(message)
        self.retry_at = retry_at
        self.retry_epoch = retry_epoch
        self.results: list[dict[str, Any]] | None = None
        self.completed: dict[str, Any] | None = None


def _has_class_name(exc: BaseException, name: str) -> bool:
    """Recognize equivalent errors loaded through another module identity."""
    return any(base.__name__ == name for base in type(exc).__mro__)


def is_guard_error(exc: BaseException) -> bool:
    return isinstance(exc, GuardError) or _has_class_name(exc, "GuardError")


def is_rate_limit_error(exc: BaseException) -> bool:
    return isinstance(exc, RateLimitError) or _has_class_name(exc, "RateLimitError")
