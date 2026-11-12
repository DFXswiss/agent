"""Script-owned issue-to-ready-PR coordinator: one bounded tick per call.

The outer CLI/daemon polls and invokes configured workers. This module never
loops, waits for CI, or starts a monitor. Models never own GitHub, Git, tests,
lane starts, or merge. See DESIGN.md §§19.1, 19.7 and docs/issue-coordinator.md.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from .coordinator_config import WorkerConfig
from .coordinator_runtime import (
    CoordinatorError,
    advance_one,
    discover_assignments,
    preflight_worker,
    redact,
)
from .runtime import Completed, run_argv
from .coordinator_exec import process_scope, run_bounded
from .store import Store, StoreError

Runner = Callable[[list[str]], Completed]
LaneRunner = Callable[[list[str], str | None], Any]

LOCK_PREFIX = "coordinator-worker:"
_LOGIN_ARGV_TAIL = ["gh", "api", "user", "--jq", ".login"]


def login_cached(runner: Runner, *, max_age_seconds: int) -> Runner:
    """Cache the successful login verification of each account for one tick."""
    cache: dict[tuple[str, ...], tuple[float, Completed]] = {}

    def cached(argv: list[str]) -> Completed:
        if argv[-5:] != _LOGIN_ARGV_TAIL:
            return runner(argv)
        key = tuple(argv)
        now = time.monotonic()
        hit = cache.get(key)
        if hit is not None and now - hit[0] <= max_age_seconds:
            return hit[1]
        result = runner(argv)
        if result.returncode == 0:
            cache[key] = (now, result)
        else:
            cache.pop(key, None)
        return result

    return cached


def tick(
    store: Store,
    worker: WorkerConfig,
    *,
    runner: Runner = run_argv,
    lane_runner: LaneRunner | None = None,
) -> list[str]:
    """Observe the worker's waiting tasks and advance its oldest runnable task by at most one resumable step.

    Holds a Postgres session advisory lock for the whole operation so concurrent
    same-session ticks across processes are excluded. The lock is released on
    success and on error. Returns observation lines for the invoking script.
    """
    lock_key = f"{LOCK_PREFIX}{worker.session_id}"
    observations: list[str] = []
    if runner is run_argv:
        runner = lambda argv: run_bounded(argv, timeout=worker.check_timeout)
    runner = login_cached(runner, max_age_seconds=worker.poll_seconds)
    try:
        with process_scope(), store.exclusive(lock_key):
            try:
                preflight_worker(store, worker, runner)
            except CoordinatorError as exc:
                return [f"preflight blocked: {redact(str(exc))}"]
            try:
                discovered = discover_assignments(store, worker, runner)
                observations.extend(discovered)
                lines = advance_one(
                    store,
                    worker,
                    runner=runner,
                    lane_runner=lane_runner,
                )
                observations.extend(lines)
            except CoordinatorError as exc:
                observations.append(f"blocked: {redact(str(exc))}")
            except StoreError as exc:
                observations.append(f"store error: {redact(str(exc))}")
            except Exception as exc:  # noqa: BLE001 — tick must never raise into a silent failure
                observations.append(f"error: {redact(str(exc))}")
            if not observations:
                observations.append("idle")
            return observations
    except StoreError as exc:
        return [f"lock error: {redact(str(exc))}"]


__all__ = [
    "login_cached",
    "tick",
]
