"""Periodic background jobs of the hub (ADR-027 section 4: `<area>/jobs.py:JOBS`).

A `Job` names a function that takes the `HubContext` and runs in a worker thread every `interval_seconds` (first run
after `initial_delay_seconds`). A job marked `at_start` also runs once in `JobRunner.prepare`, which the app awaits
before it serves the first request (also when the periodic jobs are off); its failure stops the start (KOR-06: the
configuration binding of the budget hook must never be missing while requests are served). A failing periodic run
is logged (no exception text that could carry values is returned anywhere) and the job continues with its next
interval. `JobRunner.stop` ends the loops and waits for running jobs
up to the grace period, so the process shuts down cleanly.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

LOG = logging.getLogger("agent_hub.jobs")
STOP_GRACE_SECONDS = 10.0
_NAME_RE = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*")


@dataclass(frozen=True)
class Job:
    """One periodic job; `run(context)` does one pass."""

    name: str
    interval_seconds: float
    run: Callable[[Any], object]
    initial_delay_seconds: float = 0.0
    at_start: bool = False

    def __post_init__(self) -> None:
        if _NAME_RE.fullmatch(self.name) is None:
            raise ValueError("a job name is lower-case dotted words")
        if self.interval_seconds <= 0 or self.initial_delay_seconds < 0:
            raise ValueError(f"job {self.name}: the interval must be positive and the delay not negative")


def check_jobs(jobs: Iterable[object]) -> list[Job]:
    """The discovered jobs; refuses a non-job item and a name used twice."""
    found: list[Job] = []
    for job in jobs:
        if not isinstance(job, Job):
            raise TypeError("JOBS must contain Job values")
        if any(other.name == job.name for other in found):
            raise ValueError(f"job {job.name} is registered twice")
        found.append(job)
    return found


class JobRunner:
    """Runs every job in its own loop on the running event loop."""

    def __init__(self, jobs: Iterable[Job], context: Any) -> None:
        self.jobs = list(jobs)
        self._context = context
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    async def run_once(self, job: Job) -> bool:
        """One pass of `job` in a worker thread; False when it failed (logged)."""
        try:
            await asyncio.to_thread(job.run, self._context)
        except Exception:
            LOG.error("job %s failed", job.name, exc_info=True)
            return False
        return True

    async def _wait(self, seconds: float) -> bool:
        """Sleep up to `seconds`; True when the runner was stopped meanwhile."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        return self._stop.is_set()

    async def _loop(self, job: Job) -> None:
        if await self._wait(job.initial_delay_seconds):
            return
        while True:
            await self.run_once(job)
            if await self._wait(job.interval_seconds):
                return

    async def prepare(self) -> None:
        """Run every `at_start` job once, before the process serves requests; a failed one stops the start."""
        for job in self.jobs:
            if job.at_start and not await self.run_once(job):
                raise RuntimeError(f"job {job.name} failed at start")

    def start(self) -> None:
        """Start one loop per job (on the running loop)."""
        self._stop.clear()
        self._tasks = [asyncio.create_task(self._loop(job), name=f"agent-hub-job-{job.name}") for job in self.jobs]

    async def stop(self, grace_seconds: float = STOP_GRACE_SECONDS) -> None:
        """Stop the loops; a job still running after the grace period is cancelled (its thread finishes alone)."""
        self._stop.set()
        tasks, self._tasks = self._tasks, []
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=grace_seconds)
        for task in pending:
            LOG.error("job loop %s did not stop in time", task.get_name())
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
