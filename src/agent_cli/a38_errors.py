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
        # Set when the run had already failed before the limit stopped it: an
        # earlier pull request in a sweep or multi-PR event failed, or the
        # current pull request had a failing verdict the limit kept from being
        # published. The run then exits 1 although it stopped at a limit.
        self.failed_before = False


def is_guard_error(exc: BaseException) -> bool:
    return isinstance(exc, GuardError) or (
        isinstance(exc, RuntimeError) and type(exc).__name__ == "GuardError"
    )


def is_rate_limit_error(exc: BaseException) -> bool:
    return isinstance(exc, RateLimitError)
