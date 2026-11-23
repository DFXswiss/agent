"""U-RUN-sessions: a session that stays `agent_starting` fails `agent_start_failed` after the deadline (05-runner.md
section 6.1; AUD-01 A-05, decisions/KOR-02.md). Store, start, outbox and the fake guest of the session kit are real."""

from __future__ import annotations

import asyncio
from typing import Any

from agent_runner.orgs.book import book_for
from agent_runner.reconcile.engine import Run
from agent_runner.sessions import records
from agent_runner.sessions.service import ATTRIBUTE, SessionService
from agent_runner.sessions.start import Starter
from agent_runner.startguard import reconcile
from agent_runner.startguard.reconcile import AGENT_START_TIMEOUT_S, STEPS

from ..reconcile.rig import Rig, standard_world
from .kit import Kit, desired, kit, session_id


def run(rig: Rig, body: Any) -> None:
    async def main() -> None:
        async with rig.env(standard_world()) as env:
            await body(env)

    asyncio.run(main())


async def started(env: Any) -> tuple[Kit, SessionService, list[float]]:
    found = await kit(env)
    now = [1000.0]
    found.session_env.clock = lambda: now[0]
    assert (await Starter(found.session_env).start(desired())).started
    found.guest.calls.clear()
    service = SessionService(found.session_env)
    setattr(env.context, ATTRIBUTE, service)
    return found, service, now


async def step(env: Any) -> None:
    await STEPS[0].run(Run(context=env.context, sync=env.sync, book=book_for(env.context)))
    await asyncio.gather(*getattr(env.context, reconcile.STOPS, set()))


def test_a_session_left_agent_starting_fails_after_the_deadline(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found, _service, now = await started(env)
        sid = session_id()
        await step(env)
        now[0] += AGENT_START_TIMEOUT_S - 1
        await step(env)
        assert (await found.stored(sid)).state == "agent_starting"
        now[0] += 1
        await step(env)
        assert (await found.transitions())[-1] == ("failed", "agent_start_failed")
        assert ("control", (sid, "stop")) in found.guest.calls
        assert f"agent_start_timeout session_id={sid}" in "\n".join(found.lines)
        assert reconcile._seen(env.context) == {}
        await step(env)  # an ended session is forgotten
        for _ in range(2):  # stops in the background share one task set
            reconcile._stop_later(env.context, found.session_env, sid)
        await step(env)
        assert getattr(env.context, reconcile.STOPS) == set()

    run(rig, body)


def test_progress_a_running_task_or_no_service_reset_the_count(rig: Rig) -> None:
    async def body(env: Any) -> None:
        await step(env)  # no session service yet
        found, service, now = await started(env)
        sid = session_id()
        await step(env)
        assert sid in reconcile._seen(env.context)
        busy: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        service.tasks[sid] = asyncio.ensure_future(busy)  # its start or stop runs
        await step(env)
        assert reconcile._seen(env.context) == {}
        del service.tasks[sid]
        busy.cancel()
        now[0] += 10 * AGENT_START_TIMEOUT_S
        await step(env)  # seen again: the count starts now
        assert (await found.stored(sid)).state == "agent_starting"
        async with env.pool.connection() as conn:  # ended meanwhile by the journal: nothing more to do
            await conn.execute("UPDATE runner.session SET state = 'failed' WHERE id = %s", (sid,))
        reconcile._seen(env.context)[sid] = 0.0
        await step(env)
        assert reconcile._seen(env.context) == {}
        setattr(env.context, reconcile.SEEN, {})
        assert reconcile._seen(env.context) == {}

    run(rig, body)


def test_a_session_the_journal_moved_on_between_the_reads_is_not_failed(rig: Rig, monkeypatch: Any) -> None:
    async def body(env: Any) -> None:
        found, _service, now = await started(env)
        sid = session_id()
        await step(env)
        now[0] += AGENT_START_TIMEOUT_S
        listed = records.listed

        snapshot: list[Any] = []

        async def stale(conn: Any) -> Any:
            if not snapshot:
                snapshot.extend(await listed(conn))  # read as `agent_starting` ...
                async with env.pool.connection() as other:  # ... then the journal makes it `working`
                    await other.execute("UPDATE runner.session SET state = 'working' WHERE id = %s", (sid,))
            return snapshot

        monkeypatch.setattr(records, "listed", stale)
        await step(env)
        assert (await found.stored(sid)).state == "working"
        assert "agent_start_timeout" not in "\n".join(found.lines)
        assert ("control", (sid, "stop")) not in found.guest.calls
        async with env.pool.connection() as conn:
            await conn.execute("DELETE FROM runner.session WHERE id = %s", (sid,))
        reconcile._seen(env.context)[sid] = 0.0
        await step(env)  # a session that vanished is not failed either
        assert "agent_start_timeout" not in "\n".join(found.lines)

    run(rig, body)
