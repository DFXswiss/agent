"""The frame of the drivers of working modes (ADR-098 section 4; runner migration `0040_task_modes`).

A **driver** is the deterministic part of a mode: platform code in the runner of the device that serves the task. It
watches, advances a step machine and requests the same operations a session would; it never runs a model in place of
the session and never runs repository code (everything that executes repository code is a `test.run` in a fresh VM;
a structural test keeps this package free of process and shell calls). `DRIVERS` maps a mode to its driver; catalogue
v1 has only the driver of the test mode (`TestDriver`).

`run_once(context)` (reconcile step `mode_drivers`) works every mode of the stored section (`state.listed`) that has a
driver and a live main session of its task on this device, one step each when it is due:

1. **State** (`runner.mode_driver`): the driver's step name, counters and the time of its next step. A device that has
   no row for a mode **takes it over** from the state the hub reported last (`driver` of the section, ADR-087: the
   task moved here) or starts at `START`. A mode that is no longer in the section is off: its row is deleted - the
   driver stops at this step boundary (operations it already requested stay, waiting ones run out as the session's).
2. **Rate budget**: a step that reads GitHub (`Step.github` of the driver's declaration `reads_github`) takes one unit
   of the budget of the GitHub identity it acts for (`Budget`: `BUDGET_PER_HOUR` per identity and hour, shared by
   every driver of the device); without a unit the step waits for the next window.
3. **Step**: `driver.step(current)` returns the next `Step`: the new step name and counters, the delay until its next
   step (None: none until the mode changes), the operations to request and whether to wake the session.
4. **Operations**: every request is recorded as an operation of the task's main session in state `received` with the
   chooser of the mode as acting user (A15: drivers act with the rights of the person who chose the mode) and goes
   through the ordinary pipeline - formal checks, pre-checks, registration at the hub, audit, execution, result.
5. **Wake-up**: when the step has work for the model, the session is resumed if it is paused and gets a content-free
   platform sentence naming the operation (`wake`; the path of the continuation after a model or credit pause,
   ADR-081 section 5, with a stable prompt ID per operation). Models never poll and never wait.
6. **Report**: the new state goes to the hub (`PUT /v1/device/tasks/{task}/modes/{mode}/driver`, metadata only); an
   answer `{on: false}` (switched off meanwhile) deletes the row at once. An unreachable hub is reported at the next
   run.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from agent_runner.crypto.ids import uuid7
from agent_runner.errors import RunnerError
from agent_runner.guestlink.errors import GuestError
from agent_runner.modes import state
from agent_runner.modes.state import TaskMode
from agent_runner.operations import records as operations
from agent_runner.operations.journal import CHANNEL
from agent_runner.orgs.db import session as store_session
from agent_runner.pings.send import stable_ping_id
from agent_runner.prompts import speaker
from agent_runner.prompts.deliver import deliverer_for
from agent_runner.prompts.resume import RESUMED, vm_running
from agent_runner.sessions import records
from agent_runner.sessions.records import LocalSession, time_text
from agent_runner.sync.http import HubRefusal, HubTransportError
from agent_runner.sync.runtime import runtime

START = "start"
BUDGET_PER_HOUR = 600
"""Requests per GitHub identity and hour for all drivers of the device (GitHub allows 5000; sessions keep the rest)."""
WINDOW = timedelta(hours=1)
RESUMABLE = frozenset({"suspended", "resuming"})
WAKE = (
    "A working mode of this task has new work for you: operation {ref} finished. Read its result with get_operation"
    " and continue as the section of the mode in your rules says."
)
WAKE_LINE = speaker.check(
    speaker.PREFIX + "the platform (not a person): a driver of a working mode of this task has work for the session."
)


@dataclass(frozen=True)
class Current:
    """What a driver step sees: the task, the mode, its chooser and the driver's state."""

    task_id: str
    mode: str
    chosen_by: str
    step: str
    counters: Mapping[str, int]


@dataclass(frozen=True)
class Step:
    """The outcome of one step (module docstring, step 3)."""

    step: str
    counters: Mapping[str, int]
    next_in: float | None
    requests: Sequence[tuple[str, Mapping[str, Any]]] = ()
    wake: bool = False


class Driver(Protocol):
    @property
    def reads_github(self) -> bool: ...

    async def step(self, current: Current) -> Step: ...


@dataclass(frozen=True)
class TestDriver:
    """The driver of the test mode: one step that requests a read without side effects (the GitHub identities the
    session may use) and wakes the session with it; then it rests until the mode changes."""

    reads_github: bool = False

    async def step(self, current: Current) -> Step:
        if current.step != START:
            return Step(current.step, current.counters, None)
        runs = int(current.counters.get("runs", 0)) + 1
        return Step("woken", {"runs": runs}, None, (("github.list_identities", {}),), wake=True)


DRIVERS: Mapping[str, Driver] = {"test": TestDriver()}


@dataclass
class Budget:
    """The rate budget per GitHub identity (module docstring, step 2), kept in `runner.mode_rate`."""

    per_hour: int = BUDGET_PER_HOUR
    window: timedelta = field(default=WINDOW)

    async def take(self, conn: AsyncConnection[Any], identity: str, now: datetime) -> datetime | None:
        """None when a unit was taken, else the start of the next window."""
        cursor = await conn.execute(
            "SELECT window_start, used FROM runner.mode_rate WHERE identity = %s FOR UPDATE", (identity,)
        )
        row = await cursor.fetchone()
        if row is None or row[0] + self.window <= now:
            start, used = now, 0
        else:
            start, used = row[0], int(row[1])
        if used >= self.per_hour:
            return start + self.window
        await conn.execute(
            "INSERT INTO runner.mode_rate (identity, window_start, used) VALUES (%s, %s, %s) ON CONFLICT (identity)"
            " DO UPDATE SET window_start = EXCLUDED.window_start, used = EXCLUDED.used",
            (identity, start, used + 1),
        )
        return None


async def _main_session(conn: AsyncConnection[Any], task_id: str) -> LocalSession | None:
    live = [
        found
        for found in await records.listed(conn)
        if found.task_id == task_id and found.depth == 0 and found.vm_id is not None
    ]
    return live[-1] if live else None


async def _current(conn: AsyncConnection[Any], mode: TaskMode) -> tuple[str, dict[str, int], datetime | None]:
    cursor = await conn.execute(
        "SELECT step, counters, next_at FROM runner.mode_driver WHERE task_id = %s AND mode = %s",
        (mode.task_id, mode.mode),
    )
    row = await cursor.fetchone()
    if row is not None:
        return str(row[0]), dict(row[1]), row[2]
    reported = mode.driver or {}
    step = reported.get("step")
    counters = reported.get("counters")
    next_at = reported.get("next_at")
    return (
        step if isinstance(step, str) else START,
        dict(counters) if isinstance(counters, Mapping) else {},
        datetime.fromisoformat(next_at) if isinstance(next_at, str) else None,
    )


async def request(conn: AsyncConnection[Any], session: LocalSession, chooser: str, op_type: str, params: Any) -> str:
    """Record one operation of the driver as an operation of the task's main session (module docstring, step 4)."""
    operation_id = uuid7()
    await operations.insert(
        conn,
        operation_id=operation_id,
        session_id=session.id,
        task_id=session.task_id,
        wg_id=session.wg_id,
        org_id=session.org_id,
        vm_id=str(session.vm_id),
        pending_id=uuid7(),
        op_type=op_type,
        params=dict(params),
        requested_at=time_text(datetime.now(UTC).timestamp()),
        idempotency_key=None,
        local_error=None,
        actor_id=chooser,
    )
    await conn.execute("SELECT pg_notify(%s, '')", (CHANNEL,))
    return operation_id


async def wake(context: Any, session_id: str, ref: str) -> bool:
    """Resume a paused session and hand it the platform sentence of the operation (module docstring, step 5)."""
    deliverer = deliverer_for(context)
    if deliverer is None:
        return False
    env = deliverer.env
    async with deliverer.lock(session_id):
        async with env.transaction() as conn:
            found = await records.get(conn, session_id)
        if found is None or found.state in records.FINAL:
            return False
        if found.state in RESUMABLE and await deliverer.resumer.resume(found) != RESUMED:
            return False
        async with env.transaction() as conn:
            current = await records.get(conn, session_id)
        row = None if current is None else await vm_running(env, current)
        if row is None:
            return False
        try:
            async with await env.client(row) as guest:
                await speaker.register(
                    env,
                    guest,
                    vm_id=row.id,
                    session_id=session_id,
                    prompt_id=stable_ping_id("mode.wake", ref),
                    text=WAKE.format(ref=ref),
                    line=WAKE_LINE,
                )
        except (GuestError, RunnerError):
            return False
    return True


async def _report(context: Any, mode: TaskMode, step: Step, at: datetime) -> bool:
    """The state to the hub (module docstring, step 6); whether the mode is still on (True when unreachable)."""
    sync = runtime(context)
    if sync is None:
        return True
    next_at = None if step.next_in is None else time_text((at + timedelta(seconds=step.next_in)).timestamp())
    driver = {"step": step.step, "counters": dict(step.counters), "next_at": next_at, "at": time_text(at.timestamp())}
    body = {"driver": driver}
    try:
        answer = await sync.client.request("PUT", f"/v1/device/tasks/{mode.task_id}/modes/{mode.mode}/driver", body)
    except (HubRefusal, HubTransportError):
        return True
    return answer.get("on") is not False


async def _stop_others(conn: AsyncConnection[Any], modes: Sequence[TaskMode]) -> None:
    keep = [f"{mode.task_id}:{mode.mode}" for mode in modes]
    await conn.execute(
        "DELETE FROM runner.mode_driver WHERE NOT (task_id::text || ':' || mode = ANY(%s::text[]))", (keep,)
    )


async def run_once(context: Any, *, now: datetime | None = None, budget: Budget | None = None) -> int:
    """One pass over the modes (module docstring); the number of steps taken."""
    at = now or datetime.now(UTC)
    limits = budget or Budget()
    async with store_session(context.pool) as conn:
        modes = await state.listed(conn)
        await _stop_others(conn, modes)
    taken = 0
    for mode in modes:
        driver = DRIVERS.get(mode.mode)
        if driver is not None and await _one(context, mode, driver, at, limits):
            taken += 1
    return taken


async def _one(context: Any, mode: TaskMode, driver: Driver, at: datetime, budget: Budget) -> bool:
    async with store_session(context.pool) as conn, conn.transaction():
        step_name, counters, next_at = await _current(conn, mode)
        session = await _main_session(conn, mode.task_id)
        if session is None or (next_at is not None and next_at > at):
            return False
        if driver.reads_github:
            waiting = await budget.take(conn, f"github:{mode.chosen_by}", at)
            if waiting is not None:
                await _save(conn, mode, step=step_name, counters=counters, next_at=waiting, at=at)
                return False
        outcome = await driver.step(Current(mode.task_id, mode.mode, mode.chosen_by, step_name, counters))
        if outcome.step == step_name and dict(outcome.counters) == counters and not outcome.requests:
            return False
        refs = [await request(conn, session, mode.chosen_by, op_type, params) for op_type, params in outcome.requests]
        later = None if outcome.next_in is None else at + timedelta(seconds=outcome.next_in)
        await _save(conn, mode, step=outcome.step, counters=dict(outcome.counters), next_at=later, at=at)
    if outcome.wake and refs:
        await wake(context, session.id, refs[-1])
    if not await _report(context, mode, outcome, at):
        async with store_session(context.pool) as conn:
            await conn.execute(
                "DELETE FROM runner.mode_driver WHERE task_id = %s AND mode = %s", (mode.task_id, mode.mode)
            )
    return True


async def _save(
    conn: AsyncConnection[Any],
    mode: TaskMode,
    *,
    step: str,
    counters: dict[str, int],
    next_at: datetime | None,
    at: datetime,
) -> None:
    await conn.execute(
        "INSERT INTO runner.mode_driver (task_id, mode, step, counters, next_at, updated_at) VALUES (%s, %s, %s, %s,"
        " %s, %s) ON CONFLICT (task_id, mode) DO UPDATE SET step = EXCLUDED.step, counters = EXCLUDED.counters,"
        " next_at = EXCLUDED.next_at, updated_at = EXCLUDED.updated_at",
        (mode.task_id, mode.mode, step, Jsonb(counters), next_at, at),
    )
