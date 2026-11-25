"""U-RUN-model (KOR-50, ADR-081 sections 3 to 5): the hub learns every wait of a session for model quota - its start
(also of a new session held in the start chain), each change of `wait_until` or `dfx_choice` (an earlier release, the
recheck of an own credential that cannot be used, the organization's credit) and its end (`wait_until: null`) - as
the outbox item `session_wait`, never twice the same, only towards a hub with `model_fallback.api`; and the runs of
the step never overlap, so a wait cannot be released by a concurrent run."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from agent_runner.billing import pause as credit
from agent_runner.model import fallback, limits, pauses
from agent_runner.model.limits import EXHAUSTED, Limit

from ..credentials.kit import make_record
from ..sessions.kit import desired, session_id
from .fallback_kit import AT, HOUR, T0, FKit, entry, fallback_kit, state
from .kit import ALICE, TOKEN, model_env
from .test_operator import pool_dir

OWN = limits.own_key(ALICE)
Body = Callable[[FKit], Awaitable[None]]


def run(dsn: str, tmp_path: Path | None, body: Body, **options: Any) -> None:
    async def main() -> None:
        async with model_env(dsn) as env:
            directory = None if tmp_path is None else pool_dir(tmp_path)
            async with fallback_kit(env, directory, **options) as found:
                await body(found)

    asyncio.run(main())


async def mark(found: FKit, until: float = AT + HOUR) -> None:
    await limits.mark(found.context, Limit(OWN, EXHAUSTED, until, True, T0))


async def waits(found: FKit) -> list[tuple[str | None, bool]]:
    return [(item["wait_until"], item["dfx_choice"]) for item in await found.kit.of_kind("session_wait")]


def test_a_running_wait_is_reported_when_it_starts_changes_and_ends(store_dsn: str) -> None:
    async def body(found: FKit) -> None:
        assert (await found.start()).started
        await found.state(session_id(), "working")
        await mark(found)
        await fallback.run_once(found.deliverer)
        await fallback.run_once(found.deliverer)  # unchanged: not sent again
        assert await waits(found) == [("2026-11-18T12:07:00.000Z", True)]
        await mark(found, AT + 600)
        await fallback.run_once(found.deliverer)
        found.wall.now = AT + 600
        await fallback.run_once(found.deliverer)
        assert await waits(found) == [
            ("2026-11-18T12:07:00.000Z", True),
            ("2026-11-18T11:17:00.000Z", True),
            (None, False),
        ]
        item = (await found.kit.of_kind("session_wait"))[0]
        assert (item["session_id"], item["at"]) == (session_id(), "2026-11-18T11:06:40.000Z")
        await found.env.sync.pusher.drain()  # the contract-checking hub takes the items

    run(store_dsn, None, body, desired_state=state(entry(1)))


def test_a_waiting_start_in_the_start_chain_and_the_credit_flipping_the_choice(store_dsn: str) -> None:
    async def body(found: FKit) -> None:
        await mark(found)
        assert (await found.start()).started  # held in the start chain: reported at its start
        assert await waits(found) == [("2026-11-18T12:07:00.000Z", True)]
        exhausted = {"api": 1, "active": True, "orgs": {desired().org_id: {"exhausted": True}}}
        found.set_desired(state(entry(1), billing=exhausted))
        await fallback.run_once(found.deliverer)  # no DFX choice while the credit is used up
        found.set_desired(state(entry(1), billing={**exhausted, "orgs": {}}))
        await fallback.run_once(found.deliverer)
        assert [choice for _until, choice in await waits(found)] == [True, False, True]
        session = await found.stored(session_id())
        assert not await credit.credit_holds(found.kit.session_env, session)

    run(store_dsn, None, body, desired_state=state(entry(1)))


def test_the_recheck_of_an_unusable_own_credential_moves_once_per_period(store_dsn: str) -> None:
    async def body(found: FKit) -> None:
        found.env.hub.records[(ALICE, "claude_subscription")] = make_record(
            {"token": TOKEN, "email": None, "kind": "setup_token"}, kind="claude_subscription", epoch=2
        )
        assert (await found.start(model_access_pref="subscription")).started
        await found.state(session_id(), "working")
        await mark(found)  # the own subscription, whose record no longer opens
        await fallback.run_once(found.deliverer)
        for _ in range(3):
            found.wall.now += 5
            await fallback.run_once(found.deliverer)
        assert await waits(found) == [("2026-11-18T11:11:40.000Z", False)]
        found.wall.now = T0 + limits.RECHECK_S
        await fallback.run_once(found.deliverer)
        assert (await waits(found))[-1] == ("2026-11-18T11:16:40.000Z", False)

    run(store_dsn, None, body, desired_state=state(entry(1, model_access_pref="subscription")))


def test_an_older_hub_gets_no_wait_items(store_dsn: str) -> None:
    async def body(found: FKit) -> None:
        await mark(found)
        assert (await found.start()).started
        assert await waits(found) == []

    run(store_dsn, None, body, desired_state=state(api=False))


def test_runs_never_overlap_and_a_wait_recorded_meanwhile_stays_held(store_dsn: str, monkeypatch: Any) -> None:
    async def body(found: FKit) -> None:
        sid = await_session = session_id()
        assert (await found.start()).started
        await found.state(sid, "working")
        await mark(found)
        inside: list[int] = []
        overlapped: list[int] = []
        real = fallback.check

        async def check(deliverer: Any, session: Any) -> bool:
            inside.append(1)
            if len(inside) > 1:
                overlapped.append(1)
            await asyncio.sleep(0.01)
            pauses.pend(found.context, "01930005-0000-7000-8000-000000000099")  # a start records its wait meanwhile
            try:
                return await real(deliverer, session)
            finally:
                inside.pop()

        monkeypatch.setattr(fallback, "check", check)
        await asyncio.gather(fallback.run_once(found.deliverer), fallback.run_once(found.deliverer))
        assert overlapped == []
        assert pauses.held(found.context, await_session)
        assert pauses.held(found.context, "01930005-0000-7000-8000-000000000099")  # not seen by a run yet
        assert fallback.run_lock(found.context) is fallback.run_lock(found.context)
        assert not pauses.held(None, sid)

    run(store_dsn, None, body)
