"""U-RUN-model retry (FIX-02): a turn Claude Code ended with a retryable `StopFailure` (`rate_limit`, `overloaded`,
`server_error`) is continued by the platform after a backoff, at most three times, with the platform's sender line and
the person of the interrupted turn; a person's prompt, a normal turn end, another error, a pause, an ended session
or a subagent leave it alone. Real session starts against the fake guest over a real store; the deliverer of the
prompt area holds the session locks."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agent_runner.billing import pause as credit
from agent_runner.credentials import acting
from agent_runner.guestlink.errors import GuestUnreachable
from agent_runner.model import retry
from agent_runner.model import session_state as model_state
from agent_runner.prompts import speaker
from agent_runner.prompts.resume import RESUMED, RETRY
from agent_runner.registry import ROOT_PACKAGE
from agent_runner.sessions import records
from agent_runner.sessions.extensions import state_observers

from ..sessions.kit import session_id
from .fallback_kit import T0, FKit, fallback_kit
from .kit import ALICE, model_env


def run(dsn: str, body: Any) -> None:
    async def main() -> None:
        async with model_env(dsn) as env, fallback_kit(env, None) as found:
            await body(found)

    asyncio.run(main())


async def started(found: FKit, n: int = 1, state_: str = "idle", actor: str | None = ALICE) -> str:
    assert (await found.start(n)).started, found.log()
    sid = session_id(n)
    await found.state(sid, state_)
    async with found.env.pool.connection() as conn:
        await conn.execute("UPDATE runner.session SET turn_actor = %s, turn_open = false WHERE id = %s", (actor, sid))
    found.guest.calls.clear()
    return sid


async def observe(found: FKit, sid: str, body: dict[str, Any]) -> None:
    journal = SimpleNamespace(env=found.kit.session_env)
    session = await found.stored(sid)
    async with found.env.pool.connection() as conn:
        await retry.observe(journal, conn, session, body)


async def failed(found: FKit, sid: str, error: str | None = "rate_limit") -> None:
    body: dict[str, Any] = {"state": "idle", "at": "2026-11-27T17:02:29.000Z"}
    if error is not None:
        body["stop_error"] = error
    await observe(found, sid, body)


async def row(found: FKit, sid: str) -> tuple[Any, ...] | None:
    rows = await found.env.rows(
        "SELECT stop_error, attempts, due_at, continued_at FROM runner.turn_retry WHERE session_id = %s", sid
    )
    return rows[0] if rows else None


def at(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


def test_the_observer_is_registered() -> None:
    names = [observer.name for observer in state_observers(ROOT_PACKAGE)]
    assert "model.retry" in names
    assert model_state.OBSERVERS[0].handle is retry.observe


def test_a_rate_limited_turn_is_continued_for_its_person_after_the_backoff(store_dsn: str) -> None:
    async def body(found: FKit) -> None:
        sid = await started(found)
        await failed(found, sid)
        assert await row(found, sid) == ("rate_limit", 0, at(T0 + 60), None)
        assert f"runner.model retry_scheduled session_id={sid} stop_error=rate_limit attempt=1" in found.log()
        await retry.run_once(found.deliverer)  # not due yet
        assert found.guest.texts() == []
        found.wall.now = T0 + 60
        await retry.run_once(found.deliverer)
        continued = retry.prompt_id(sid, at(T0))
        assert found.guest.sent() == [continued]
        assert found.guest.texts() == [retry.CONTINUATION]
        assert found.guest.speakers() == [speaker.PLATFORM_RETRY]
        assert await row(found, sid) == ("rate_limit", 1, at(T0 + 60), at(T0 + 60))
        assert f"runner.model retry_continued session_id={sid} attempt=1 actor=yes" in found.log()
        # the continuation acts for alice once the guest accepts it
        recorded = await found.env.rows(
            "SELECT actor_id::text FROM runner.platform_prompt WHERE prompt_id = %s", continued
        )
        assert recorded == [(ALICE,)]
        await observe(found, sid, {"state": "prompt_accepted", "prompt_id": continued, "at": "x"})
        assert await row(found, sid) is not None  # its own continuation keeps the row
        await retry.run_once(found.deliverer)  # handed over already: nothing twice
        assert len(found.guest.sent()) == 1

    run(store_dsn, body)


def test_the_backoff_grows_and_ends_after_three_continuations(store_dsn: str) -> None:
    async def body(found: FKit) -> None:
        sid = await started(found, actor=None)
        expected = []
        for attempt, delay in enumerate(retry.BACKOFF_S):
            await failed(found, sid, "overloaded" if attempt == 1 else "rate_limit")
            found_row = await row(found, sid)
            assert found_row is not None
            assert found_row[1:3] == (attempt, at(found.wall.now + delay))
            found.wall.now += delay
            await retry.run_once(found.deliverer)
            expected.append(retry.CONTINUATION)
            assert found.guest.texts() == expected
        assert f"runner.model retry_continued session_id={sid} attempt=3 actor=none" in found.log()
        await failed(found, sid, "server_error")
        assert await row(found, sid) is None
        assert f"runner.model retry_exhausted session_id={sid} stop_error=server_error attempts=3" in found.log()

    run(store_dsn, body)


@pytest.mark.parametrize(
    "event",
    [
        {"state": "idle", "at": "x"},  # a normal end of the turn
        {"state": "idle", "stop_error": "invalid_request", "at": "x"},  # a retry cannot help
        {"state": "idle", "stop_error": "unknown", "at": "x"},
        {"state": "prompt_accepted", "prompt_id": "01a21df5-0000-7000-8000-000000000001", "at": "x"},  # a person
    ],
)
def test_a_normal_end_another_error_or_a_person_drops_the_retry(store_dsn: str, event: dict[str, Any]) -> None:
    async def body(found: FKit) -> None:
        sid = await started(found)
        await failed(found, sid)
        await observe(found, sid, event)
        assert await row(found, sid) is None
        await observe(found, sid, {"state": "prompt_accepted", "prompt_id": "x", "at": "x"})  # no row: nothing
        assert await row(found, sid) is None

    run(store_dsn, body)


def test_other_states_and_subagents_are_left_alone(store_dsn: str) -> None:
    async def body(found: FKit) -> None:
        sid = await started(found)
        await failed(found, sid)
        await observe(found, sid, {"state": "working", "at": "x"})
        assert await row(found, sid) is not None
        child = await started(found, 2)
        async with found.env.pool.connection() as conn:
            await conn.execute("UPDATE runner.session SET depth = 1 WHERE id = %s", (child,))
        await failed(found, child)
        assert await row(found, child) is None

    run(store_dsn, body)


@pytest.mark.parametrize("case", ["working", "stopped", "person", "paused"])
def test_a_due_retry_is_dropped_when_the_session_moved_on(store_dsn: str, case: str, monkeypatch: Any) -> None:
    async def body(found: FKit) -> None:
        sid = await started(found)
        await failed(found, sid)
        found.wall.now = T0 + 60
        if case in ("working", "stopped"):
            await found.state(sid, case)
        elif case == "person":
            async with found.env.pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO runner.prompt_delivery (event_id, session_id, state) VALUES (%s, %s, 'pending')",
                    ("01a21df5-0000-7000-8000-0000000000aa", sid),
                )
        else:
            monkeypatch.setattr(credit, "deferred", lambda _env, _session: True)
        await retry.run_once(found.deliverer)
        assert found.guest.texts() == []
        assert await row(found, sid) is None

    run(store_dsn, body)


def test_a_suspended_session_is_resumed_first(store_dsn: str, monkeypatch: Any) -> None:
    async def body(found: FKit) -> None:
        sid = await started(found, state_="suspended")
        await failed(found, sid)
        found.wall.now = T0 + 60
        resume = AsyncMock(return_value=RETRY)
        monkeypatch.setattr(found.deliverer.resumer, "resume", resume)
        await retry.run_once(found.deliverer)
        assert found.guest.texts() == []
        assert (await row(found, sid) or ())[3] is None  # still due
        resume.return_value = RESUMED
        await retry.run_once(found.deliverer)
        assert found.guest.texts() == [retry.CONTINUATION]
        assert resume.await_count == 2

    run(store_dsn, body)


def test_an_unreachable_guest_or_vm_leaves_the_retry_due(store_dsn: str, monkeypatch: Any) -> None:
    async def body(found: FKit) -> None:
        sid = await started(found)
        await failed(found, sid)
        found.wall.now = T0 + 60
        found.guest.prompt_errors.append(GuestUnreachable("prompts"))
        await retry.run_once(found.deliverer)
        assert f"runner.model retry_failed session_id={sid} code=guest_unreachable" in found.log()
        assert (await row(found, sid) or ())[1:] == (0, at(T0 + 60), None)

        async def no_vm(_env: Any, _session: Any) -> None:
            return None

        monkeypatch.setattr(retry, "vm_running", no_vm)
        await retry.run_once(found.deliverer)
        assert (await row(found, sid) or ())[3] is None
        monkeypatch.undo()
        await retry.run_once(found.deliverer)
        assert (await row(found, sid) or ())[1] == 1
        await retry._continue(found.deliverer, "01a21df5-0000-7000-8000-0000000000bb")  # no row: nothing

    run(store_dsn, body)


def test_continuation_ids_are_stable_per_failure() -> None:
    first = retry.prompt_id(session_id(1), at(T0))
    assert first == retry.prompt_id(session_id(1), at(T0))
    assert first != retry.prompt_id(session_id(1), at(T0 + 1))
    assert first != retry.prompt_id(session_id(2), at(T0))
    assert speaker.check(speaker.PLATFORM_RETRY) == retry.LINE


def test_a_vanished_session_drops_its_retry(store_dsn: str, monkeypatch: Any) -> None:
    async def body(found: FKit) -> None:
        sid = await started(found)
        await failed(found, sid)
        found.wall.now = T0 + 60

        async def gone(_conn: Any, _sid: str, **_kw: Any) -> None:
            return None

        monkeypatch.setattr(records, "get", gone)
        await retry.run_once(found.deliverer)
        monkeypatch.undo()
        assert await row(found, sid) is None
        assert found.guest.texts() == []
        async with found.env.pool.connection() as conn:
            assert await acting.continuation(conn, sid, retry.prompt_id(sid, at(T0))) == ALICE

    run(store_dsn, body)
