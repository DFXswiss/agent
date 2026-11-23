"""U-RUN-inbox / reconcile step 2: platform log and membership logs (P1-P4, L1-L9, rollback and fork per org)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent_runner.crypto.log import vectors as v
from agent_runner.crypto.log.entries import Head, entry_hash
from agent_runner.orgs.book import stop_key
from agent_runner.reconcile import logs
from agent_runner.reconcile.engine import Engine, Run
from agent_runner.reconcile.reconcile import STEPS

from .rig import DEVICE, Env, Rig, org_log, pid, platform_log, standard_world, wrap_for


def engine(env: Env) -> Engine:
    built = Engine(env.context, STEPS)
    env.context.reconcile_engine = built
    return built


async def once(env: Env) -> Run:
    built = getattr(env.context, "reconcile_engine", None) or engine(env)
    run = await built.run_once()
    assert run is not None
    return run


async def counts(env: Env) -> tuple[int, int, int, int]:
    rows = await env.rows(
        "SELECT (SELECT count(*) FROM runner.log_entry), (SELECT count(*) FROM runner.platform_log_entry), "
        "(SELECT count(*) FROM runner.scope_key), (SELECT count(*) FROM runner.outbox)"
    )
    return tuple(int(x) for x in rows[0])  # type: ignore[return-value]


def test_reconcile_verifies_and_stores_everything_and_is_idempotent(rig: Rig) -> None:
    async def body() -> None:
        world = standard_world("a", "b")
        async with rig.env(world) as env:
            run = await once(env)
            assert run.halted is None
            assert run.book.acting_orgs() == sorted(world.orgs)
            first = await counts(env)
            assert first == (10, 3, 4, 0)
            requests = len(env.hub.requests)
            again = await once(env)
            assert again.halted is None
            assert await counts(env) == first
            targets = [r.target for r in env.hub.requests[requests:]]
            assert targets == [
                "/v1/device/state",
                "/v1/device/platform-log?limit=1000&after=2",
                "/v1/device/wraps",
            ]
            assert await env.audits() == []

    asyncio.run(body())


def test_logs_are_fetched_page_by_page(rig: Rig) -> None:
    async def body() -> None:
        world = standard_world("a")
        world.page_size = 2
        async with rig.env(world) as env:
            run = await once(env)
            assert run.halted is None
            assert await counts(env) == (5, 3, 2, 0)
            org = next(iter(world.orgs))
            assert env.hub.targets("/v1/device/logs") == [
                f"/v1/device/logs?org={org}&limit=1000",
                f"/v1/device/logs?org={org}&limit=1000&after=1",
                f"/v1/device/logs?org={org}&limit=1000&after=3",
            ]
            assert env.hub.targets("/v1/device/platform-log") == [
                "/v1/device/platform-log?limit=1000",
                "/v1/device/platform-log?limit=1000&after=1",
            ]
            spec = world.orgs[org]
            for n in range(3):
                spec.log.add("wg_create", "alice", wg=pid(f"a/wg/new{n}"), epoch=1)
            await once(env)
            assert await counts(env) == (8, 3, 2, 0)
            assert env.hub.targets("/v1/device/logs")[-2:] == [
                f"/v1/device/logs?org={org}&limit=1000&after=4",
                f"/v1/device/logs?org={org}&limit=1000&after=6",
            ]

    asyncio.run(body())


async def alerts(env: Env) -> list[tuple[str, str, Any]]:
    return [(a["action"], a["target_id"], a["detail"]) for a in await env.audits()]


def test_rollback_stops_only_that_org_and_is_alerted_once(rig: Rig) -> None:
    async def body() -> None:
        world = standard_world("a", "b")
        a, b = (spec.org for spec in world.orgs.values())
        async with rig.env(world) as env:
            await once(env)
            entries = world.orgs[a].log.entries
            world.offered[a] = {"seq": 2, "hash": entry_hash(entries[2])}
            run = await once(env)
            assert run.halted is None
            assert run.book.acting_orgs() == [b]
            assert run.book.stopped == {a: "log_rollback"}
            assert await alerts(env) == [
                ("runner.alert", a, {"alert": "log_rollback", "value": None}),
                ("runner.log_rollback", a, {"verified_seq": 4, "offered_seq": 2}),
            ]
            assert f"runner.alert log_rollback org={a} verified_seq=4 offered_seq=2" in env.log()
            assert await env.rows("SELECT value FROM runner.sync_state WHERE key = %s", stop_key(a)) == [
                ("log_rollback",)
            ]
            # b goes on: a new entry of b is still verified and stored
            world.orgs[b].log.add("wg_create", "alice", wg=pid("b/wg/new"), epoch=1)
            requests = len(env.hub.targets("/v1/device/logs"))
            await once(env)
            assert len(await alerts(env)) == 2
            assert env.hub.targets("/v1/device/logs")[requests:] == [f"/v1/device/logs?org={b}&limit=1000&after=4"]
            assert run.book.head(b) == Head(5, entry_hash(world.orgs[b].log.entries[5]))

    asyncio.run(body())


def test_same_seq_with_another_hash_is_a_fork(rig: Rig) -> None:
    async def body() -> None:
        world = standard_world("a")
        org = next(iter(world.orgs))
        async with rig.env(world) as env:
            await once(env)
            world.offered[org] = {"seq": 4, "hash": entry_hash(world.orgs[org].log.entries[3])}
            run = await once(env)
            assert run.book.stopped == {org: "log_fork"}
            assert run.book.acting_orgs() == []
            assert [x[0] for x in await alerts(env)] == ["runner.alert", "runner.log_fork"]

    asyncio.run(body())


def test_entries_continuing_another_history_are_a_fork(rig: Rig) -> None:
    async def body() -> None:
        world = standard_world("a")
        org = next(iter(world.orgs))
        spec = world.orgs[org]
        async with rig.env(world) as env:
            await once(env)
            other = v.LogBuilder(org, spec.log.entries[:4])
            other.add("wg_create", "alice", wg=pid("a/wg/x"), epoch=1)  # another seq 4
            other.add("wg_create", "alice", wg=pid("a/wg/y"), epoch=1)  # 5
            world.served[org] = other.entries
            run = await once(env)
            assert run.book.stopped == {org: "log_fork"}
            assert (await alerts(env))[1] == ("runner.log_fork", org, {"verified_seq": 4, "offered_seq": 5})

    asyncio.run(body())


def test_an_answer_below_the_verified_head_is_a_rollback(rig: Rig) -> None:
    async def body() -> None:
        world = standard_world("a")
        org = next(iter(world.orgs))
        async with rig.env(world) as env:
            await once(env)
            entries = world.orgs[org].log.entries
            world.served[org] = entries[:3]
            world.offered[org] = {"seq": 9, "hash": entry_hash(entries[0])}
            run = await once(env)
            assert run.book.stopped == {org: "log_rollback"}
            assert (await alerts(env))[1] == ("runner.log_rollback", org, {"verified_seq": 4, "offered_seq": 2})

    asyncio.run(body())


def test_a_refused_entry_is_audited_once_and_the_prefix_kept(rig: Rig) -> None:
    async def body() -> None:
        world = standard_world("a")
        org = next(iter(world.orgs))
        spec = world.orgs[org]
        async with rig.env(world) as env:
            await once(env)
            spec.log.add("wg_create", "alice", wg=pid("a/wg/ok"), epoch=1)  # 5
            spec.log.add("wg_create", "alice", signer="bob", wg=pid("a/wg/forged"), epoch=1)  # 6, bad signature
            run = await once(env)
            await once(env)
            assert run.book.acting_orgs() == [org]
            assert run.book.head(org) == Head(5, entry_hash(spec.log.entries[5]))
            assert await alerts(env) == [
                ("runner.inbound_rejected", org, {"source": "log", "code": "log_signature"}),
            ]

    asyncio.run(body())


def test_an_org_that_does_not_assign_the_device_is_refused(rig: Rig) -> None:
    async def body() -> None:
        world = standard_world("a")
        stranger = world.add_org(org_log("s", device=None))
        async with rig.env(world) as env:
            run = await once(env)
            await once(env)
            assert run.book.acting_orgs() == [o for o in world.orgs if o != stranger.org]
            assert await alerts(env) == [
                ("runner.inbound_rejected", stranger.org, {"source": "log", "code": "device_not_assigned"}),
            ]
            assert await env.rows("SELECT count(*) FROM runner.log_entry WHERE org_id = %s", stranger.org) == [(0,)]

    asyncio.run(body())


@pytest.mark.parametrize("case", ["rollback", "fork"])
def test_platform_rollback_and_fork_stop_every_org(rig: Rig, case: str) -> None:
    async def body() -> None:
        world = standard_world("a", "b")
        async with rig.env(world) as env:
            await once(env)
            if case == "rollback":
                world.served_platform = world.platform.entries[:2]
            else:
                other = v.PlatformBuilder(world.platform.entries[:2])
                other.add(
                    "user_invite", invite_id=v.secrets("u").invite_id, pk_inv=v.secrets("u").pk_inv, expires=v.EXPIRES
                )  # another seq 2
                other.add(
                    "user_invite", invite_id=v.secrets("w").invite_id, pk_inv=v.secrets("w").pk_inv, expires=v.EXPIRES
                )  # 3
                world.served_platform = other.entries
            run = await once(env)
            assert run.halted == "stopped"
            assert run.book.acting_orgs() == []
            assert (await alerts(env))[1] == (
                f"runner.log_{case}",
                "platform",
                {"verified_seq": 2, "offered_seq": 1 if case == "rollback" else 3},
            )
            requests = len(env.hub.requests)
            again = await once(env)
            assert again.halted == "stopped"
            assert len(await alerts(env)) == 2
            assert "/v1/device/platform-log" not in [r.target.split("?")[0] for r in env.hub.requests[requests:]]

    asyncio.run(body())


def test_a_refused_platform_entry_is_audited(rig: Rig) -> None:
    async def body() -> None:
        world = standard_world("a")
        async with rig.env(world) as env:
            await once(env)
            world.platform.add(
                "user_invite",
                invite_id=v.secrets("u").invite_id,
                pk_inv=v.secrets("u").pk_inv,
                expires=v.EXPIRES,
                signer="mallory",
            )
            run = await once(env)
            await once(env)
            assert run.halted is None
            assert run.book.platform_head == Head(2, entry_hash(world.platform.entries[2]))
            assert await alerts(env) == [
                ("runner.inbound_rejected", "platform", {"source": "platform_log", "code": "log_signature"})
            ]

    asyncio.run(body())


def test_nothing_is_acted_upon_before_the_device_is_certified(rig: Rig) -> None:
    async def body() -> None:
        world = standard_world("a")
        world.platform = v.PlatformBuilder(platform_log().entries[:2])
        async with rig.env(world) as env:
            run = await once(env)
            assert run.halted == "uncertified"
            assert env.hub.targets("/v1/device/logs") == []
            assert env.hub.targets("/v1/device/wraps") == []
            assert await counts(env) == (0, 2, 0, 0)
            assert "runner.reconcile state=uncertified" in env.log()
            world.platform.certify(DEVICE)
            assert (await once(env)).halted is None

    asyncio.run(body())


def test_a_decertified_device_stops_acting(rig: Rig) -> None:
    async def body() -> None:
        world = standard_world("a")
        async with rig.env(world) as env:
            assert (await once(env)).halted is None
            world.platform.add("device_decertify", device=pid(DEVICE))
            run = await once(env)
            assert run.halted == "decertified"
            assert run.book.acting_orgs() == []
            world.wraps.append(wrap_for("alice", "wg", pid("a/wg/one"), 1))
            assert env.hub.targets("/v1/device/wraps") == ["/v1/device/wraps"]

    asyncio.run(body())


def test_an_unknown_listed_org_fails_the_step(rig: Rig) -> None:
    async def body() -> None:
        world = standard_world("a")
        world.listed = [pid("ghost/org")]
        world.offered[pid("ghost/org")] = {"seq": 0, "hash": "A" * 43}
        async with rig.env(world) as env:
            run = await once(env)
            assert run.halted == "failed"
            assert "step_failed step=logs error=HubRefusal code=not_found" in env.log()

    asyncio.run(body())


def test_log_fetching_ends_after_the_page_limit(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    async def body() -> None:
        world = standard_world("a")
        world.page_size = 2
        async with rig.env(world) as env:
            monkeypatch.setattr(logs, "MAX_PAGES", 1)
            run = await once(env)
            # two platform entries of three: not yet certified, nothing else is fetched
            assert run.halted == "uncertified"
            assert await counts(env) == (0, 2, 0, 0)

    asyncio.run(body())
