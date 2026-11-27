"""Repeat ingest, dispatch, supervise and prune in separate child processes.

Each subcommand ends through die()/SystemExit and opens its own store. A child
keeps one failing step from ending the loop, and a fresh process per step gives
each step the isolation the old schedule-per-command model provided.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import threading
from collections.abc import Callable

STEP_NAMES: tuple[str, ...] = ("ingest", "dispatch", "supervise", "prune")


def step_argvs(prefix: list[str], session_id: str) -> list[tuple[str, list[str]]]:
    """Build each scheduled command in pass order."""
    return [
        (name, [*prefix, "runner", name, *(
            ["--session", session_id] if name == "ingest" else []
        )])
        for name in STEP_NAMES
    ]


def _run(argv: list[str]) -> int:
    """Run one step to its end, inheriting the terminal streams."""
    # Ctrl-C reaches the entire foreground process group. Isolate the step so
    # dispatch cannot die between worktree creation and its row update: that
    # half-built state is exactly what the runner lock prevents (docs/runner.md).
    proc = subprocess.Popen(argv, start_new_session=True)
    return proc.wait()


def run_loop(
    *,
    steps: list[tuple[str, list[str]]],
    interval: float,
    run: Callable[[list[str]], int] | None = None,
    wait: Callable[[float], bool] | None = None,
    install_signals: bool = True,
    stop: threading.Event | None = None,
) -> int:
    """Repeat passes until stopped, letting an active step finish first.

    Always return 0 on a requested stop; other failures raise exceptions.
    """
    stopping = threading.Event() if stop is None else stop
    run_step = _run if run is None else run
    wait_pass = stopping.wait if wait is None else wait

    def on_signal(_signum: int, _frame: object) -> None:
        stopping.set()

    previous = {}
    try:
        if install_signals:
            for signum in (signal.SIGTERM, signal.SIGINT):
                previous[signum] = signal.signal(signum, on_signal)
        while True:
            for name, argv in steps:
                if stopping.is_set():
                    return 0
                try:
                    status = run_step(argv)
                except OSError as exc:
                    reason = exc.strerror or type(exc).__name__
                    print(f"runner loop {name} could not start: {reason}", file=sys.stderr)
                    continue
                if status != 0:
                    print(f"runner loop {name} exited {status}", file=sys.stderr)
            if wait_pass(interval) or stopping.is_set():
                return 0
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
