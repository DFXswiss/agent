"""Script-owned issue-to-ready-PR coordinator: one bounded tick per call.

The outer CLI/daemon polls and invokes configured workers. This module never
loops, waits for CI, or starts a monitor. Models never own GitHub, Git, tests,
lane starts, or merge. See DESIGN.md §§19.1, 19.7 and docs/issue-coordinator.md.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
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
from .github_request_share import (
    authentication_failure,
    cached_account_login,
    note_authentication_failure,
)
from .store import Store, StoreError

Runner = Callable[[list[str]], Completed]
LaneRunner = Callable[[list[str], str | None], Any]

LOCK_PREFIX = "coordinator-worker:"
_LOGIN_ARGV_TAIL = ["gh", "api", "user", "--jq", ".login"]
# Reuse of one successful gh api user check on this device. This is not
# CLAUDE_USAGE_POLL_SECONDS, which only spaces Claude subscription usage reads.
LOGIN_REUSE_SECONDS = 15 * 60


def login_cached(runner: Runner, *, max_age_seconds: int, cache_dir: Path) -> Runner:
    """Reuse one successful ``gh api user`` verification per GitHub account.

    The caller chooses the bound. Workers and the knock child pass
    ``LOGIN_REUSE_SECONDS``. An authentication error drops the saved
    verification so the next check calls GitHub. A hit in this process returns
    the same ``Completed``; other processes read ``cache_dir``.
    """
    memory: dict[str, tuple[float, Completed]] = {}

    def cached(argv: list[str]) -> Completed:
        if argv[-5:] != _LOGIN_ARGV_TAIL:
            result = runner(argv)
            if authentication_failure(result):
                note_authentication_failure(cache_dir, argv, memory)
            return result
        return cached_account_login(
            runner,
            argv,
            home=cache_dir,
            max_age_seconds=max_age_seconds,
            memory=memory,
        )

    return cached


def shared_github_runner(runner: Runner, *, home: Path) -> Runner:
    """Share ``gh api user`` across the workers and the knock child on one device."""
    return login_cached(runner, max_age_seconds=LOGIN_REUSE_SECONDS, cache_dir=home)


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
    runner = shared_github_runner(runner, home=store.home)
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
    "LOGIN_REUSE_SECONDS",
    "login_cached",
    "shared_github_runner",
    "tick",
]
