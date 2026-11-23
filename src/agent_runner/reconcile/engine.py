"""Reconcile engine of the runner (05-runner.md section 2.2, 03-api.md section 6.2, ADR-013, ADR-028).

Triggers: every 30 s (the `reconcile` daemon task), every WebSocket hint that asks for it (`reconcile`, `log`,
`wraps`, `decertified`; `request()` wakes the task) and after every (re)connect (the `reconcile` connect hook runs
`run_once()` directly, before the outbox push and the pull, ADR-028).

One run executes the discovered steps (`<area>/reconcile.py:STEPS`, `registry.reconcile_steps`: order number, then
name) in order on one `Run`. Every step is idempotent: running the same desired state again changes nothing. A step
may halt the run (`Run.halt`), for example while the device is `pending_certification` or not certified by the
verified platform log: the later steps then do nothing, because nothing is acted upon before it is verified. A step
that raises halts the run as well; the failure is logged by exception type (and closed code) only, and the next
trigger starts again from step 1. Runs never overlap (`lock`).

Step order numbers are `10 * n` for step n of section 2.2 (state 10, logs 20, wraps 30, assignments 40, sessions
50, prompts 60, requests/pings/credentials 70, catch-up 80), leaving room in between.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from agent_runner import registry
from agent_runner.crypto.log.entries import Head
from agent_runner.orgs.book import OrgBook, book_for
from agent_runner.sync.http import HubRefusal, HubTransportError
from agent_runner.sync.runtime import SyncRuntime, runtime

PERIOD_S = 30.0
ATTRIBUTE = "reconcile_engine"


@dataclass
class Run:
    """The state of one reconcile run, passed to every step."""

    context: Any
    sync: SyncRuntime
    book: OrgBook
    desired: dict[str, Any] | None = None
    """The answer of `GET /v1/device/state` (step 1) of an `active` device."""
    device_state: str | None = None
    """`active` or `pending_certification` as answered by the hub."""
    halted: str | None = None
    asked_heads: dict[str, Head] | None = None
    """The verified org heads when step 1 requested the state (None without that step). Step 2 judges an offered head
    against them: the inbox may verify newer entries in between (L10), and an offer below only those is older than
    the copy, not a rollback."""

    def halt(self, reason: str) -> None:
        if self.halted is None:
            self.halted = reason

    @property
    def pool(self) -> Any:
        return self.context.pool


def describe(exc: BaseException) -> str:
    """Exception type and, for hub answers, the closed code or reason (never a message)."""
    if isinstance(exc, HubRefusal):
        return f"HubRefusal code={exc.code}"
    if isinstance(exc, HubTransportError):
        return f"HubTransportError reason={exc.reason}"
    return type(exc).__name__


class Engine:
    def __init__(self, context: Any, steps: Sequence[registry.ReconcileStep]) -> None:
        self.context = context
        self.steps = list(steps)
        self.trigger = asyncio.Event()
        self.lock = asyncio.Lock()
        self.last: Run | None = None
        self.runs = 0
        self._reported: str | None = None

    def line(self, text: str) -> None:
        self.context.log.write(f"runner.reconcile {text}\n")
        self.context.log.flush()

    def request(self) -> None:
        """Ask for a run as soon as possible (hint handlers)."""
        self.trigger.set()

    def _report(self, run: Run) -> None:
        """Log the device's situation when it changes (`pending_certification`, `active`, `uncertified`, ...)."""
        situation = run.halted if run.halted not in (None, "failed") else run.device_state
        if situation is not None and situation != self._reported:
            self._reported = situation
            self.line(f"state={situation}")

    async def run_once(self) -> Run | None:
        """One run of every step; None when the device is not enrolled."""
        sync = runtime(self.context)
        if sync is None:
            return None
        async with self.lock:
            run = Run(context=self.context, sync=sync, book=book_for(self.context))
            for step in self.steps:
                if run.halted is not None:
                    break
                try:
                    await step.run(run)
                except Exception as exc:
                    self.line(f"step_failed step={step.name} error={describe(exc)}")
                    run.halt("failed")
            self.runs += 1
            self.last = run
            self._report(run)
            return run

    async def wait(self, seconds: float, stopping: asyncio.Event) -> None:
        """Until `request()`, `stopping` or `seconds` on the host clock."""
        waiters = {
            asyncio.ensure_future(self.trigger.wait()),
            asyncio.ensure_future(stopping.wait()),
            asyncio.ensure_future(self.context.host.sleep(seconds)),
        }
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)

    async def loop(self, stopping: asyncio.Event) -> None:
        """Run every `PERIOD_S` and on request until `stopping`."""
        while not stopping.is_set():
            # cleared before the run, so a hint arriving during the run starts the next one at once
            self.trigger.clear()
            await self.run_once()
            await self.wait(PERIOD_S, stopping)


def engine_for(context: Any) -> Engine | None:
    """The engine of this daemon with the discovered steps; None while the device is not enrolled."""
    found = getattr(context, ATTRIBUTE, None)
    if isinstance(found, Engine):
        return found
    if context.identity is None:
        return None
    engine = Engine(context, registry.reconcile_steps(context.host.package))
    setattr(context, ATTRIBUTE, engine)
    return engine
