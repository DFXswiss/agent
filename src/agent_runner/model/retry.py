"""A turn a model rate limit ended is continued by the platform (FIX-02).

Claude Code ends a turn with `StopFailure` when the model API refuses it after Claude Code's own retries; the guest
journals `session.state idle` with `stop_error` (`agent_guest.hook_addons.idle`). Before this, a main session then sat
idle until a person wrote again, although the person's instruction was not done.

- `observe` (the `session.state` observer of this area, `model/session_state.py`), main sessions only:
  * `idle` with a retryable `stop_error` (`RETRYABLE`: `rate_limit`, `overloaded`, `server_error`) records the turn in
    `runner.turn_retry`: due `BACKOFF_S[attempts]` seconds later; a session that already got `MAX_ATTEMPTS`
    continuations since its last normal turn end is left to the person (row removed, logged `retry_exhausted`);
  * `idle` without `stop_error` or with another one (a normal end, an error a retry cannot help) removes the row;
  * `prompt_accepted` of any other prompt than this row's continuation removes the row: a person wrote, so the
    platform does not continue on its own.
  A subagent session is not handled here: its `StopFailure` fails it (`subagents.turns`), and its parent decides.
- `run_once(deliverer)` (from the daemon task `model_fallback`, every `fallback.TICK_S`): every due row whose session
  is still idle (or suspended by the idle timer: it is resumed first) gets the continuation `CONTINUATION` with the
  platform's sender line `speaker.PLATFORM_RETRY` and a stable prompt ID per failure (`prompt_id`), recorded first with
  the person of the interrupted turn (`credentials.acting.continuation`), so the continued turn keeps that person's
  GitHub connection. Under the deliverer's lock of the session, like every other prompt. A row is dropped when the
  session ended, when a person's prompt waits for it (it starts the next turn anyway) or while a credit or model pause
  holds the session (`billing.pause.deferred`: that pause continues the turn itself). A guest that cannot be reached
  leaves the row due for the next run.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg import AsyncConnection

from agent_runner.billing import pause as credit
from agent_runner.credentials import acting
from agent_runner.crypto.ids import uuid7
from agent_runner.errors import RunnerError
from agent_runner.guestlink.errors import GuestError
from agent_runner.prompts import queue, speaker
from agent_runner.prompts.resume import RESUMED, vm_running
from agent_runner.sessions import records
from agent_runner.sessions.env import SessionEnv
from agent_runner.sessions.records import LocalSession

RETRYABLE = frozenset({"rate_limit", "overloaded", "server_error"})
BACKOFF_S = (60.0, 180.0, 600.0)
MAX_ATTEMPTS = len(BACKOFF_S)
CONTINUATION = (
    "The model provider refused the last request for a while (rate limit or overload), so your last turn ended"
    " early. Continue the interrupted work where you stopped."
)
LINE = speaker.check(speaker.PLATFORM_RETRY)
WAITING = frozenset({"idle", "suspended"})


def prompt_id(session_id: str, failed_at: datetime) -> str:
    """The stable ID of the continuation of one failed turn."""
    seed = hashlib.sha256(f"retry:{session_id}:{failed_at.timestamp()}".encode()).digest()[:10]
    return uuid7(int(failed_at.timestamp() * 1000), seed)


def _line(env: Any, text: str) -> None:
    env.log(f"runner.model {text}")


async def _row(conn: AsyncConnection[Any], session_id: str) -> tuple[int, datetime, datetime | None] | None:
    cursor = await conn.execute(
        "SELECT attempts, failed_at, continued_at FROM runner.turn_retry WHERE session_id = %s FOR UPDATE",
        (session_id,),
    )
    found = await cursor.fetchone()
    return None if found is None else (int(found[0]), found[1], found[2])


async def _drop(conn: AsyncConnection[Any], session_id: str) -> None:
    await conn.execute("DELETE FROM runner.turn_retry WHERE session_id = %s", (session_id,))


async def observe(journal: Any, conn: AsyncConnection[Any], session: LocalSession, body: Mapping[str, Any]) -> None:
    """The `session.state` observer (module docstring)."""
    if session.depth > 0:
        return
    state = body.get("state")
    if state == "prompt_accepted":
        found = await _row(conn, session.id)
        if found is not None and body.get("prompt_id") != prompt_id(session.id, found[1]):
            await _drop(conn, session.id)
        return
    if state != "idle":
        return
    error = body.get("stop_error")
    if error not in RETRYABLE:
        await _drop(conn, session.id)
        return
    env = journal.env
    found = await _row(conn, session.id)
    attempts = 0 if found is None else found[0]
    if attempts >= MAX_ATTEMPTS:
        await _drop(conn, session.id)
        _line(env, f"retry_exhausted session_id={session.id} stop_error={error} attempts={attempts}")
        return
    now = datetime.fromtimestamp(env.wall(), UTC)
    await conn.execute(
        "INSERT INTO runner.turn_retry (session_id, stop_error, failed_at, attempts, due_at)"
        " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (session_id) DO UPDATE SET stop_error = EXCLUDED.stop_error,"
        " failed_at = EXCLUDED.failed_at, due_at = EXCLUDED.due_at, continued_at = NULL",
        (session.id, error, now, attempts, now + timedelta(seconds=BACKOFF_S[attempts])),
    )
    _line(env, f"retry_scheduled session_id={session.id} stop_error={error} attempt={attempts + 1}")


async def _person_waits(conn: AsyncConnection[Any], session_id: str) -> bool:
    cursor = await conn.execute(
        "SELECT 1 FROM runner.prompt_delivery WHERE session_id = %s AND state = ANY(%s) LIMIT 1",
        (session_id, list(queue.OPEN)),
    )
    return await cursor.fetchone() is not None


async def run_once(deliverer: Any) -> None:
    """Hand over every due continuation (module docstring)."""
    env: SessionEnv = deliverer.env
    now = datetime.fromtimestamp(env.wall(), UTC)
    async with env.transaction() as conn:
        cursor = await conn.execute(
            "SELECT session_id::text FROM runner.turn_retry WHERE continued_at IS NULL AND due_at <= %s"
            " ORDER BY due_at, session_id",
            (now,),
        )
        due = [row[0] for row in await cursor.fetchall()]
    for session_id in due:
        async with deliverer.lock(session_id):
            await _continue(deliverer, session_id)


async def _continue(deliverer: Any, session_id: str) -> None:
    env: SessionEnv = deliverer.env
    async with env.transaction() as conn:
        session = await records.get(conn, session_id)
        found = await _row(conn, session_id)
        if found is None:
            return
        if session is None or session.state not in WAITING or await _person_waits(conn, session_id):
            await _drop(conn, session_id)
            return
    if credit.deferred(env, session):
        async with env.transaction() as conn:
            await _drop(conn, session_id)
        return
    if session.state == "suspended" and await deliverer.resumer.resume(session) != RESUMED:
        return
    async with env.transaction() as conn:
        current = await records.get(conn, session_id)
    row = None if current is None else await vm_running(env, current)
    if row is None:
        return
    attempts, failed_at, _continued = found
    continued = prompt_id(session_id, failed_at)
    async with env.transaction() as conn:
        actor = await acting.continuation(conn, session_id, continued)
    try:
        async with await env.client(row) as guest:
            await speaker.register(
                env, guest, vm_id=row.id, session_id=session_id, prompt_id=continued, text=CONTINUATION, line=LINE
            )
    except (GuestError, RunnerError) as exc:
        _line(env, f"retry_failed session_id={session_id} code={exc.code}")
        return
    async with env.transaction() as conn:
        await conn.execute(
            "UPDATE runner.turn_retry SET attempts = %s, continued_at = %s WHERE session_id = %s",
            (attempts + 1, datetime.fromtimestamp(env.wall(), UTC), session_id),
        )
    _line(env, f"retry_continued session_id={session_id} attempt={attempts + 1} actor={'yes' if actor else 'none'}")
