"""Controlled stop when an organization's credit is used up, and the continuation after a credit (KOR-31, ADR-062
section 6, 12-billing.md section 7; AUD-04 W12: suspend, never stop, so the session stays resumable).

Only a **platform device** (DFX hardware) acts: every session there costs runtime. An org device (own hardware) never
stops, defers or refuses anything for credit (idea 45).

`enforce(deliverer, desired)` (reconcile step `billing`, after the sessions step, on every desired state - the hub
sends `reconcile {}` when an org's `exhausted` changes). Each session is paused under the deliverer's lock of that
session, so no prompt is handed over in between:

1. **Exhausted org**: every main session in `working` is interrupted (guest control `interrupt`, the running turn ends
   and the session is `idle`; the guest refuses a `suspend` until the turn has ended, so it is asked again up to
   `SUSPEND_TRIES` times `SUSPEND_WAIT_S` apart), every `idle` main session is suspended (guest control `suspend`,
   transition `suspended` with the reason `credit_exhausted`); the transcript stays, so the session is resumable as
   after an idle suspension. The pause is recorded in `runner.credit_pause` (`interrupted` = a turn was running) before
   the guest is asked, so a crash in between suspends it on the next run. A subagent session (one turn, at most 30 min)
   is stopped with `credit_exhausted`; its operation fails and the parent learns it like any ended subagent. Sessions
   still in the start chain are handled on the next run.
2. **Credit back**: a recorded pause whose org is no longer exhausted ends. An interrupted session is resumed
   (`Resumer.resume`, the same path a follow-up prompt takes) and gets the fixed continuation prompt
   `CONTINUATION` (no user text; a stable prompt ID derived from the pause, so a repeated hand-over is the same prompt
   for the guest); a session that was idle stays suspended until its next prompt. `resumed_at` marks the pause done.

`deferred(env, session)`: while its org is exhausted no prompt is handed to a session of a platform device - neither
to a suspended one (no resume) nor to one that is still idle before the pause; the prompt stays pending and is
delivered once credit is back (`deliver.Deliverer`).
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any

from agent_runner.billing import state
from agent_runner.crypto.ids import uuid7
from agent_runner.errors import RunnerError
from agent_runner.guestlink.errors import GuestError, GuestRefused
from agent_runner.prompts.resume import RESUMED, vm_running
from agent_runner.sessions import records
from agent_runner.sessions.env import SessionEnv
from agent_runner.sessions.records import LocalSession
from agent_runner.sessions.stop import stop_session

REASON = "credit_exhausted"
CONTINUATION = (
    "The platform paused this session because the organization's credit was used up. Credit is available again:"
    " continue the interrupted work where you stopped."
)
STOPPED_STATES = frozenset({"working", "idle"})
SUSPEND_TRIES = 10
SUSPEND_WAIT_S = 0.5


def platform(env: SessionEnv) -> bool:
    return env.device_kind == "platform"


def deferred(env: SessionEnv, session: LocalSession) -> bool:
    """A follow-up must not resume the session now (module docstring)."""
    return platform(env) and state.exhausted(env.desired, session.org_id)


def prompt_id(session_id: str, paused_at: datetime) -> str:
    """The stable ID of the continuation prompt of one pause."""
    seed = hashlib.sha256(f"credit:{session_id}:{paused_at.timestamp()}".encode()).digest()[:10]
    return uuid7(int(paused_at.timestamp() * 1000), seed)


async def _guest(env: SessionEnv, session: LocalSession, action: str, tries: int = 1) -> bool:
    row = await vm_running(env, session)
    if row is None:
        return False
    attempt = 0
    while True:
        attempt += 1
        try:
            async with await env.client(row) as guest:
                await guest.control(session.id, action)
            return True
        except GuestRefused as exc:
            if attempt < tries and exc.error == "state_conflict":
                await asyncio.sleep(SUSPEND_WAIT_S)  # the interrupted turn is still ending
                continue
            code = exc.code
        except (GuestError, RunnerError) as exc:
            code = exc.code
        env.line(f"credit_{action}_failed session_id={session.id} code={code}")
        return False


async def _pause(env: SessionEnv, session: LocalSession) -> None:
    async with env.transaction() as conn:
        await conn.execute(
            "INSERT INTO runner.credit_pause (session_id, org_id, interrupted, paused_at) VALUES (%s, %s, %s, %s)"
            " ON CONFLICT (session_id) DO UPDATE SET interrupted = CASE WHEN runner.credit_pause.resumed_at IS NULL"
            " THEN runner.credit_pause.interrupted OR EXCLUDED.interrupted ELSE EXCLUDED.interrupted END,"
            " paused_at = CASE WHEN runner.credit_pause.resumed_at IS NULL"
            " THEN runner.credit_pause.paused_at ELSE EXCLUDED.paused_at END, resumed_at = NULL",
            (session.id, session.org_id, session.state == "working", datetime.fromtimestamp(env.wall(), UTC)),
        )
    interrupted = session.state == "working"
    if interrupted and not await _guest(env, session, "interrupt"):
        return
    if not await _guest(env, session, "suspend", SUSPEND_TRIES if interrupted else 1):
        return
    async with env.transaction() as conn:
        await records.advance(conn, env.outbox, session.id, "suspended", at=env.at(), reason=REASON)
    env.line(f"credit_suspended session_id={session.id}")


async def stop_for_credit(
    env: SessionEnv, lock: Callable[[str], AbstractAsyncContextManager[Any]], desired: Mapping[str, Any] | None
) -> None:
    """Step 1 of the module docstring."""
    async with env.transaction() as conn:
        local = await records.listed(conn)
    for found in local:
        if found.state not in STOPPED_STATES or not state.exhausted(desired, found.org_id):
            continue
        if found.depth > 0:
            await stop_session(env, found.id, reason=REASON)
            env.line(f"credit_stopped session_id={found.id}")
            continue
        async with lock(found.id):
            async with env.transaction() as conn:
                session = await records.get(conn, found.id)
            if session is not None and session.state in STOPPED_STATES:
                await _pause(env, session)


async def continue_after_credit(
    env: SessionEnv,
    lock: Callable[[str], AbstractAsyncContextManager[Any]],
    resumer: Any,
    desired: Mapping[str, Any] | None,
) -> None:
    """Step 2 of the module docstring."""
    async with env.transaction() as conn:
        cursor = await conn.execute(
            "SELECT session_id::text, org_id::text, interrupted, paused_at FROM runner.credit_pause"
            " WHERE resumed_at IS NULL ORDER BY paused_at, session_id"
        )
        pauses = await cursor.fetchall()
    for session_id, org_id, interrupted, paused_at in pauses:
        if state.exhausted(desired, org_id):
            continue
        async with lock(session_id):
            await _continue(env, resumer, session_id, interrupted, paused_at)


async def _continue(env: SessionEnv, resumer: Any, session_id: str, interrupted: bool, paused_at: datetime) -> None:
    """One recorded pause whose org has credit again (step 2)."""
    async with env.transaction() as conn:
        session = await records.get(conn, session_id)
    if interrupted and session is not None and session.state not in records.FINAL:
        if session.state in {"suspended", "resuming"} and await resumer.resume(session) != RESUMED:
            return
        async with env.transaction() as conn:
            current = await records.get(conn, session_id)
        row = None if current is None else await vm_running(env, current)
        if row is None:
            return
        try:
            async with await env.client(row) as guest:
                await guest.register_prompt(session_id, prompt_id(session_id, paused_at), CONTINUATION)
        except (GuestError, RunnerError) as exc:
            env.line(f"credit_continue_failed session_id={session_id} code={exc.code}")
            return
        env.line(f"credit_continued session_id={session_id}")
    async with env.transaction() as conn:
        await conn.execute(
            "UPDATE runner.credit_pause SET resumed_at = %s WHERE session_id = %s",
            (datetime.fromtimestamp(env.wall(), UTC), session_id),
        )


async def enforce(deliverer: Any, desired: Mapping[str, Any] | None) -> None:
    """Both steps on a platform device (module docstring); `deliverer`: the daemon's `deliver.Deliverer`."""
    env: SessionEnv = deliverer.env
    if not platform(env) or not state.active(desired):
        return
    await stop_for_credit(env, deliverer.lock, desired)
    await continue_after_credit(env, deliverer.lock, deliverer.resumer, desired)
