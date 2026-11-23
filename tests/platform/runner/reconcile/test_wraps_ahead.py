"""HOT-04: a key wrap that arrives before the log entry entitling it is judged only after a log refresh.

The browser stores the `device_assign` of a workgroup and the device's wrap in one request. A reconcile run that
asked for the state (step 1) before that request and fetches the wraps (step 3) after it holds a wrap whose
entitlement its verified log does not show yet. The wrap step refreshes the logs from the hub before it judges such a
wrap: a rightful wrap is stored without any audit, a wrap the hub's current logs do not entitle is audited exactly
once, and a hub that cannot serve the logs leads to a later try, not to an audit."""

from __future__ import annotations

import asyncio
from typing import Any

from agent_runner.orgs.book import ATTRIBUTE as BOOK_ATTRIBUTE
from agent_runner.reconcile import wraps
from agent_runner.reconcile.engine import Engine, Run
from agent_runner.reconcile.reconcile import STEPS

from .rig import CERT_SEQ, DEVICE, Env, OrgSpec, Rig, pid, standard_world, wrap_for


async def once(env: Env) -> Run:
    built = getattr(env.context, "reconcile_engine", None)
    if built is None:
        built = Engine(env.context, STEPS)
        env.context.reconcile_engine = built
    run = await built.run_once()
    assert run is not None
    assert run.halted is None
    return run


async def held(env: Env, scope_id: str) -> list[int]:
    rows = await env.rows("SELECT epoch FROM runner.scope_key WHERE scope_id = %s ORDER BY epoch", scope_id)
    return [int(row[0]) for row in rows]


async def alarms(env: Env) -> list[str]:
    """Every `runner.*` audit item of the device (E2E-14 expects none in normal operation)."""
    return [a["action"] for a in await env.audits() if str(a["action"]).startswith("runner.")]


def refreshes(env: Env) -> int:
    return len(env.hub.targets("/v1/device/logs")) + len(env.hub.targets("/v1/device/platform-log"))


async def rejections(env: Env) -> list[tuple[str, Any]]:
    return [
        (a["target_id"], a["detail"]["code"]) for a in await env.audits() if a["action"] == "runner.inbound_rejected"
    ]


def assign_with_wrap(env: Env, spec: OrgSpec, wg: str) -> None:
    """What the admin's browser stores in one request: the `device_assign` entry and the device's wrap."""
    spec.log.add("device_assign", "alice", device=pid(DEVICE), wg=wg, platform_seq=CERT_SEQ)
    env.world.wraps.append(wrap_for("alice", "wg", wg, 1))


def stored_after_the_state(env: Env, spec: OrgSpec, wg: str) -> None:
    """Let the browser's request land right after the hub answered the run's `GET /v1/device/state`."""
    answer = env.hub.state_answer

    def racing() -> tuple[int, dict[str, Any]]:
        result = answer()
        env.hub.state_answer = answer  # type: ignore[method-assign]
        assign_with_wrap(env, spec, wg)
        return result

    env.hub.state_answer = racing  # type: ignore[method-assign]


def logs_unavailable(env: Env, org: str | None = None) -> list[str]:
    """`GET /v1/device/logs` (of `org` only, if given) answers 503 while the returned list holds an item."""
    down = ["down"]
    serve = env.hub.logs

    def logs(query: dict[str, list[str]]) -> tuple[int, dict[str, Any]]:
        hit = down and org in (None, query["org"][0])
        return (503, {"error": "unavailable"}) if hit else serve(query)

    env.hub.logs = logs  # type: ignore[method-assign]
    return down


def test_a_wrap_ahead_of_its_log_entry_is_stored_after_a_refresh_without_audit(rig: Rig) -> None:
    async def body() -> None:
        world = standard_world("a")
        spec = next(iter(world.orgs.values()))
        one = pid("a/wg/one")
        async with rig.env(world) as env:
            await once(env)
            stored_after_the_state(env, spec, one)
            run = await once(env)
            # judged in the same run, against the refreshed log: stored, nothing audited
            assert await held(env, one) == [1]
            head = run.book.head(spec.org)
            assert head is not None
            assert head.seq == len(spec.log.entries) - 1
            assert await alarms(env) == []
            await once(env)
            assert await alarms(env) == []
            assert spec.org in run.book.acting_orgs()

    asyncio.run(body())


def test_a_stale_offer_does_not_turn_a_rightful_wrap_into_an_audit(rig: Rig) -> None:
    """The state offers the head before the `device_assign` (an older snapshot): the hub's log decides. (A hub that
    keeps offering that head after the device verified more is a rollback, L9: not part of this case.)"""

    async def body() -> None:
        world = standard_world("a")
        spec = next(iter(world.orgs.values()))
        one = pid("a/wg/one")
        async with rig.env(world) as env:
            await once(env)
            world.offered[spec.org] = world.head(spec.log.entries)
            assign_with_wrap(env, spec, one)
            run = await once(env)
            assert await held(env, one) == [1]
            assert await alarms(env) == []
            assert spec.org in run.book.acting_orgs()

    asyncio.run(body())


def test_a_wrap_after_its_log_entry_needs_no_refresh(rig: Rig) -> None:
    async def body() -> None:
        world = standard_world("a")
        spec = next(iter(world.orgs.values()))
        one = pid("a/wg/one")
        async with rig.env(world) as env:
            await once(env)
            spec.log.add("device_assign", "alice", device=pid(DEVICE), wg=one, platform_seq=CERT_SEQ)
            await once(env)  # the entry is verified before the wrap exists
            fetched = len(env.hub.targets("/v1/device/logs"))
            world.wraps.append(wrap_for("alice", "wg", one, 1))
            await once(env)
            assert await held(env, one) == [1]
            assert len(env.hub.targets("/v1/device/logs")) == fetched  # the offered head equals the copy
            assert await rejections(env) == []

    asyncio.run(body())


def test_a_wrap_without_any_entitlement_is_audited_once(rig: Rig) -> None:
    async def body() -> None:
        world = standard_world("a")
        spec = next(iter(world.orgs.values()))
        one = pid("a/wg/one")
        world.wraps.append(wrap_for("alice", "wg", one, 1))  # the device is never assigned to wg/one
        async with rig.env(world) as env:
            await once(env)
            assert await rejections(env) == [(f"wg:{one}:1", "wrap_unauthorized")]
            asked = refreshes(env)
            await once(env)  # nothing changed: the refused wrap is not judged again, the logs are not refreshed
            assert refreshes(env) == asked + 1  # step 2's platform log request only
            spec.log.add("rotate", "alice", scope="wg", scope_id=spec.default_wg, epoch=2)
            await once(env)  # checked again at the new log state, still refused: no second audit
            assert await held(env, one) == []
            assert await rejections(env) == [(f"wg:{one}:1", "wrap_unauthorized")]
            spec.log.add("device_assign", "alice", device=pid(DEVICE), wg=one, platform_seq=CERT_SEQ)
            await once(env)  # the log entitles it later: stored (a refusal holds only for its log state)
            assert await held(env, one) == [1]

    asyncio.run(body())


def test_a_wrap_for_a_device_removed_from_the_workgroup_is_audited(rig: Rig) -> None:
    async def body() -> None:
        world = standard_world("a")
        spec = next(iter(world.orgs.values()))
        one = pid("a/wg/one")
        first_wrap = wrap_for("alice", "wg", one, 1)
        spec.log.add("device_assign", "alice", device=pid(DEVICE), wg=one, platform_seq=CERT_SEQ)
        world.wraps.append(first_wrap)
        async with rig.env(world) as env:
            await once(env)
            assert await held(env, one) == [1]
            spec.log.add("device_unassign", "alice", device=pid(DEVICE), wg=one)
            spec.log.add("rotate", "alice", scope="wg", scope_id=one, epoch=2)
            world.wraps.append(wrap_for("alice", "wg", one, 2))
            await once(env)
            await once(env)
            assert await held(env, one) == [1]
            assert await rejections(env) == [(f"wg:{one}:2", "wrap_unauthorized")]

    asyncio.run(body())


def test_logs_unavailable_for_the_refresh_lead_to_a_later_try_not_to_an_audit(rig: Rig) -> None:
    async def body() -> None:
        world = standard_world("a")
        spec = next(iter(world.orgs.values()))
        one = pid("a/wg/one")
        async with rig.env(world) as env:
            await once(env)
            world.offered[spec.org] = world.head(spec.log.entries)  # step 2 has nothing to fetch
            assign_with_wrap(env, spec, one)
            down = logs_unavailable(env)
            await once(env)
            assert await held(env, one) == []
            assert await rejections(env) == []
            down.clear()
            await once(env)
            assert await held(env, one) == [1]
            assert await rejections(env) == []

    asyncio.run(body())


def test_a_wrap_is_judged_on_the_verified_copy_after_the_refresh_failed_repeatedly(rig: Rig) -> None:
    """A hub that serves wraps but never the logs cannot keep an unentitled wrap out of the audit for good."""

    async def body() -> None:
        world = standard_world("a")
        spec = next(iter(world.orgs.values()))
        one = pid("a/wg/one")
        async with rig.env(world) as env:
            await once(env)
            world.offered[spec.org] = world.head(spec.log.entries)
            world.wraps.append(wrap_for("alice", "wg", one, 1))
            logs_unavailable(env)
            for _ in range(wraps.REFRESH_TRIES - 1):
                await once(env)
                assert await rejections(env) == []
            await once(env)
            assert await rejections(env) == [(f"wg:{one}:1", "wrap_unauthorized")]
            await once(env)
            assert await rejections(env) == [(f"wg:{one}:1", "wrap_unauthorized")]
            assert await held(env, one) == []

    asyncio.run(body())


def test_a_runner_restart_between_wrap_and_refresh_keeps_the_wrap_unaudited(rig: Rig) -> None:
    async def body() -> None:
        world = standard_world("a")
        spec = next(iter(world.orgs.values()))
        one = pid("a/wg/one")
        async with rig.env(world) as env:
            await once(env)
            world.offered[spec.org] = world.head(spec.log.entries)
            assign_with_wrap(env, spec, one)
            down = logs_unavailable(env)
            await once(env)
            assert await held(env, one) == []
            # restart: a new book (loaded again from the store) and engine, nothing kept in memory
            for attribute in (BOOK_ATTRIBUTE, "reconcile_engine"):
                delattr(env.context, attribute)
            down.clear()
            await once(env)
            assert await held(env, one) == [1]
            assert await rejections(env) == []

    asyncio.run(body())


def test_a_wrap_ahead_is_stored_although_another_orgs_log_cannot_be_refreshed(rig: Rig) -> None:
    async def body() -> None:
        world = standard_world("a", "b")
        a, b = world.orgs.values()
        one = pid("a/wg/one")
        async with rig.env(world) as env:
            await once(env)
            world.offered[a.org] = world.head(a.log.entries)
            assign_with_wrap(env, a, one)
            logs_unavailable(env, b.org)
            await once(env)
            assert await held(env, one) == [1]
            assert await rejections(env) == []

    asyncio.run(body())


def test_the_inbox_never_judges_a_wrap_whose_logs_cannot_be_refreshed(rig: Rig) -> None:
    """Only reconcile runs count towards `REFRESH_TRIES`: the inbox's calls of the wrap step leave the wrap alone."""

    async def body() -> None:
        world = standard_world("a")
        spec = next(iter(world.orgs.values()))
        one = pid("a/wg/one")
        async with rig.env(world) as env:
            run = await once(env)
            world.wraps.append(wrap_for("alice", "wg", one, 1))
            down = logs_unavailable(env)
            for _ in range(wraps.REFRESH_TRIES + 1):
                assert await wraps.sync_wraps(run) == 0
            assert await rejections(env) == []
            spec.log.add("device_assign", "alice", device=pid(DEVICE), wg=one, platform_seq=CERT_SEQ)
            down.clear()
            assert await wraps.sync_wraps(run) == 1
            assert await rejections(env) == []

    asyncio.run(body())
