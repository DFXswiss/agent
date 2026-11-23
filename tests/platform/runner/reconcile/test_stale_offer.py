"""U-RUN-inbox / reconcile step 2: an offered head that is only older than the copy is not a rollback (L9, L10).

The state of a reconcile run is fetched before its log step runs. Meanwhile the inbox may verify newer entries of the
same org (L10 refresh for an event whose `lh` is beyond the verified head). The offered head of the state is then
below the copy although the hub never served less than the device had verified when it asked; before the
correction this stopped the org as `log_rollback` (decisions/HOT-03.md). A head below what was verified when the
state was requested stays a rollback, and an offered head that is not part of the verified history stays refused.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agent_runner.crypto.log.entries import Head, entry_hash
from agent_runner.inbox.pull import inbox_for
from agent_runner.orgs.book import book_for
from agent_runner.reconcile import logs, state, wraps
from agent_runner.reconcile.engine import Run

from .rig import Env, Rig, pid, prompt, standard_world
from .test_logs import alerts, once


def new_run(env: Env) -> Run:
    return Run(context=env.context, sync=env.sync, book=book_for(env.context))


async def concurrent_l10(env: Env, org: str, label: str) -> None:
    """The inbox pulls an event whose `lh` names the hub's newest entry: its L10 refresh verifies that entry."""
    spec = env.world.orgs[org]
    seq = len(env.world.events.get(spec.default_wg, [])) + 1
    env.world.add_event(spec.default_wg, seq, prompt(spec, label=label))
    inbox = inbox_for(env.context)
    assert inbox is not None
    await inbox.pull_all()


def test_an_offered_head_overtaken_by_the_inbox_refresh_is_no_rollback(rig: Rig) -> None:
    async def body() -> None:
        world = standard_world("a")
        a = next(iter(world.orgs))
        async with rig.env(world) as env:
            await once(env)
            run = new_run(env)
            await state.fetch_state(run)
            assert run.asked_heads == {a: Head(4, entry_hash(world.orgs[a].log.entries[4]))}
            # after the state answer (head 4) the hub appends entry 5; the inbox verifies it before step 2 runs
            world.orgs[a].log.add("wg_create", "alice", wg=pid("a/wg/new"), epoch=1)
            await concurrent_l10(env, a, "race")
            assert run.book.head(a) == Head(5, entry_hash(world.orgs[a].log.entries[5]))
            requests = len(env.hub.targets("/v1/device/logs"))
            await logs.sync_logs(run)
            await wraps.step(run)
            assert run.halted is None
            assert run.book.stopped == {}
            assert run.book.acting_orgs() == [a]
            assert await alerts(env) == []
            assert "log_rollback" not in env.log()
            # the hub's current log decides, as in the L10 refresh: nothing newer, nothing to store
            assert env.hub.targets("/v1/device/logs")[requests:] == [f"/v1/device/logs?org={a}&limit=1000&after=5"]
            assert run.book.head(a) == Head(5, entry_hash(world.orgs[a].log.entries[5]))
            # the next run sees the current head and goes on
            world.orgs[a].log.add("wg_create", "alice", wg=pid("a/wg/later"), epoch=1)
            assert (await once(env)).book.head(a) == Head(6, entry_hash(world.orgs[a].log.entries[6]))
            assert await alerts(env) == []

    asyncio.run(body())


def test_a_hub_that_serves_less_after_the_refresh_is_still_a_rollback(rig: Rig) -> None:
    async def body() -> None:
        world = standard_world("a")
        a = next(iter(world.orgs))
        async with rig.env(world) as env:
            await once(env)
            run = new_run(env)
            await state.fetch_state(run)
            world.orgs[a].log.add("wg_create", "alice", wg=pid("a/wg/new"), epoch=1)
            await concurrent_l10(env, a, "race")
            # the stale offer is not judged alone, but the log the hub serves now ends below the verified head
            world.served[a] = world.orgs[a].log.entries[:4]
            await logs.sync_logs(run)
            assert run.book.stopped == {a: "log_rollback"}
            assert (await alerts(env))[1] == ("runner.log_rollback", a, {"verified_seq": 5, "offered_seq": 3})

    asyncio.run(body())


def test_an_offer_below_the_head_verified_when_asking_is_a_rollback(rig: Rig) -> None:
    async def body() -> None:
        world = standard_world("a")
        a = next(iter(world.orgs))
        async with rig.env(world) as env:
            await once(env)
            entries = world.orgs[a].log.entries
            world.offered[a] = {"seq": 3, "hash": entry_hash(entries[3])}
            run = new_run(env)
            await state.fetch_state(run)
            # a concurrent refresh does not excuse an answer below what the device had verified before it asked
            world.orgs[a].log.add("wg_create", "alice", wg=pid("a/wg/new"), epoch=1)
            await concurrent_l10(env, a, "race")
            await logs.sync_logs(run)
            assert run.book.stopped == {a: "log_rollback"}
            assert (await alerts(env))[1] == ("runner.log_rollback", a, {"verified_seq": 5, "offered_seq": 3})

    asyncio.run(body())


def test_a_stale_offer_outside_the_verified_history_is_refused(rig: Rig) -> None:
    async def body() -> None:
        world = standard_world("a")
        a = next(iter(world.orgs))
        async with rig.env(world) as env:
            await once(env)
            world.orgs[a].log.add("wg_create", "alice", wg=pid("a/wg/new"), epoch=1)
            world.orgs[a].log.add("wg_create", "alice", wg=pid("a/wg/next"), epoch=1)
            # offered seq 5 lies between the head verified when asking (4) and the copy (6), with another hash
            world.offered[a] = {"seq": 5, "hash": entry_hash(world.orgs[a].log.entries[4])}
            run = new_run(env)
            await state.fetch_state(run)
            await concurrent_l10(env, a, "race")
            assert run.book.head(a) == Head(6, entry_hash(world.orgs[a].log.entries[6]))
            await logs.sync_logs(run)
            assert run.book.stopped == {a: "log_rollback"}

    asyncio.run(body())


def test_a_run_without_a_state_snapshot_judges_the_offer_alone(rig: Rig) -> None:
    async def body(env: Any) -> None:
        a = next(iter(env.world.orgs))
        await once(env)
        entries = env.world.orgs[a].log.entries
        run = new_run(env)
        run.desired = {"orgs": [{"org_id": a, "log_head": {"seq": 2, "hash": entry_hash(entries[2])}}]}
        await logs.sync_logs(run)
        assert run.book.stopped == {a: "log_rollback"}

    async def main() -> None:
        async with rig.env(standard_world("a")) as env:
            await body(env)

    asyncio.run(main())
