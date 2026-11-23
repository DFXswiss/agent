"""KOR-17: a device request that arrives before the device's copy of the verified log knows its user is judged only
after a log refresh (decisions/KOR-17.md, the same rule as decisions/HOT-04.md for key wraps).

A new user's first request ("connect GitHub" right after joining) can reach the device before its log copy shows the
user. The service refreshes the platform log and the acting orgs' logs from the hub before it judges a refusal that
depends only on the log state (`key_unknown`, `device_not_assigned` of the user check): a rightful request is handled
without any audit, a request the hub's current logs do not entitle is audited exactly once, a hub that cannot serve
the logs leads to a later try (judged against the copy after `REFRESH_TRIES`), and a flood of forged requests cannot
make the device fetch the logs more often than once per `REFRESH_INTERVAL_S`."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from agent_runner.orgs.book import ATTRIBUTE as BOOK_ATTRIBUTE
from agent_runner.requests import service as service_module
from agent_runner.requests.service import Flow, RequestService

from ..credentials.kit import Env, kit_env, pid, seal_request
from ..reconcile.rig import CERT_SEQ, DEVICE, World, org_log
from ..settle import settle as settle_tasks


def run(dsn: str, body: Callable[[Env], Awaitable[None]], world: World | None = None) -> None:
    async def main() -> None:
        async with kit_env(dsn, world) as env:
            await body(env)

    asyncio.run(main())


async def done(flow: Flow) -> None:
    await flow.respond({"step": "done"}, final=True)


def make_service(env: Env) -> RequestService:
    built = RequestService(env.context, env.sync, {"github.connect": done})
    env.context.request_service = built
    return built


async def pump(service: RequestService) -> None:
    """Let the deferred requests come back (the release timer runs on the test clock) and process the queue."""
    while True:
        timer = getattr(service, "timer", None)
        if timer is not None:
            await timer
        if service.queue.empty():
            break
        while not service.queue.empty():
            await service.process(service.queue.get_nowait())
    await settle_tasks(lambda: [flow.task for flow in service.flows.values()], "the request flows")


def hold_timer(env: Env, service: RequestService) -> asyncio.Event:
    """The release timer sleeps until the returned event is set (the test clock otherwise ends a sleep at once, while
    the device would go on processing other requests in the meantime)."""
    gate = asyncio.Event()

    async def sleep(seconds: float) -> None:
        await gate.wait()
        await env.clock.sleep(seconds)

    service.sleep = sleep
    return gate


def join(env: Env, user: str, wgs: dict[str, str]) -> None:
    """The user joins org `a` on the hub (the device's verified copy does not show it yet); every user is in the
    default workgroup."""
    spec = env.world.orgs[pid("a/org")]
    spec.log.invite_user(spec.founder, user, wgs=wgs)
    spec.log.accept(user)


def refreshes(env: Env) -> int:
    return len(env.hub.targets("/v1/device/logs")) + len(env.hub.targets("/v1/device/platform-log"))


async def rejections(env: Env) -> list[tuple[str, str]]:
    return [
        (a["target_id"], a["detail"]["code"]) for a in await env.audits() if a["action"] == "runner.inbound_rejected"
    ]


async def state(env: Env, request_id: str) -> tuple[str, int] | None:
    rows = await env.rows("SELECT state, last_seq FROM runner.device_request WHERE id = %s", request_id)
    return (str(rows[0][0]), int(rows[0][1])) if rows else None


def logs_unavailable(env: Env) -> list[str]:
    """`GET /v1/device/logs` answers 503 while the returned list holds an item."""
    down = ["down"]
    serve = env.hub.logs

    def logs(query: dict[str, list[str]]) -> tuple[int, dict[str, Any]]:
        return (503, {"error": "unavailable"}) if down else serve(query)

    env.hub.logs = logs  # type: ignore[method-assign]
    return down


def test_a_request_ahead_of_its_log_entry_is_handled_after_a_refresh_without_audit(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        service = make_service(env)
        join(env, "carol", {})
        request_id = env.hub.add_request(seal_request("github.connect", {}, user="carol"))
        await service.process(request_id)
        await pump(service)
        assert await state(env, request_id) == ("answered", 1)
        assert env.hub.bodies(request_id, user="carol") == [{"step": "done"}]
        assert await env.audits() == []
        assert refreshes(env) > 0
        assert service.waiting == {}

    run(store_dsn, body)


def test_a_request_after_its_log_entry_needs_no_refresh(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        service = make_service(env)
        join(env, "carol", {})
        await env.verify_logs()
        before = refreshes(env)
        request_id = env.hub.add_request(seal_request("github.connect", {}, user="carol"))
        await service.process(request_id)
        await pump(service)
        assert await state(env, request_id) == ("answered", 1)
        assert refreshes(env) == before
        assert await env.audits() == []

    run(store_dsn, body)


def test_a_request_of_an_unknown_user_is_audited_once_after_a_refresh(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        service = make_service(env)
        request_id = env.hub.add_request(seal_request("github.connect", {}, user="zed"))
        await service.process(request_id)
        await pump(service)
        after_first = refreshes(env)
        assert after_first > 0
        assert await rejections(env) == [(request_id, "key_unknown")]
        # offered again (hint, desired state): no second audit, no second refresh, still not handled
        await service.process(request_id)
        await pump(service)
        assert await rejections(env) == [(request_id, "key_unknown")]
        assert refreshes(env) == after_first
        assert env.hub.stored_requests[request_id].responses == []
        assert service.waiting == {}

    run(store_dsn, body)


def test_a_known_user_the_device_does_not_serve_is_audited_once(store_dsn: str) -> None:
    world = World()
    spec = world.add_org(org_log("a", device=None))
    spec.log.add("device_assign", spec.founder, device=pid(DEVICE), wg=pid("a/wg/one"), platform_seq=CERT_SEQ)

    async def body(env: Env) -> None:
        service = make_service(env)
        join(env, "carol", {})  # the default workgroup only: the device does not serve carol
        request_id = env.hub.add_request(seal_request("github.connect", {}, user="carol"))
        await service.process(request_id)
        await pump(service)
        await service.process(request_id)
        await pump(service)
        assert await rejections(env) == [(request_id, "device_not_assigned")]
        assert env.hub.stored_requests[request_id].responses == []
        assert await state(env, request_id) is None  # may still pass once the user is assigned

    run(store_dsn, body, world)


def test_a_request_waits_while_the_logs_cannot_be_refreshed(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        service = make_service(env)
        gate = hold_timer(env, service)
        down = logs_unavailable(env)
        join(env, "carol", {})
        request_id = env.hub.add_request(seal_request("github.connect", {}, user="carol"))
        await service.process(request_id)
        # neither handled nor audited, and offered again later
        assert await state(env, request_id) is None
        assert await env.audits() == []
        assert request_id in service.deferred
        down.clear()
        gate.set()
        await pump(service)
        assert await state(env, request_id) == ("answered", 1)
        assert await env.audits() == []

    run(store_dsn, body)


def test_a_request_is_judged_against_the_copy_after_the_refresh_tries(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        service = make_service(env)
        before = len(env.hub.targets("/v1/device/logs"))
        logs_unavailable(env)
        join(env, "carol", {})
        request_id = env.hub.add_request(seal_request("github.connect", {}, user="carol"))
        await service.process(request_id)
        await pump(service)
        # REFRESH_TRIES failed refreshes, then judged against the copy: audited once, never handled
        assert len(env.hub.targets("/v1/device/logs")) - before == service_module.REFRESH_TRIES
        assert await rejections(env) == [(request_id, "key_unknown")]
        assert env.hub.stored_requests[request_id].responses == []
        assert "runner.requests logs_refreshed complete=false" in env.log()

    run(store_dsn, body)


def test_a_restart_between_request_and_refresh_handles_it_without_audit(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        first = make_service(env)
        down = logs_unavailable(env)
        join(env, "carol", {})
        request_id = env.hub.add_request(seal_request("github.connect", {}, user="carol"))
        await first.process(request_id)
        assert first.timer is not None
        first.timer.cancel()
        await asyncio.gather(first.timer, return_exceptions=True)
        # the daemon restarts: a new service, the book loaded again from the store
        delattr(env.context, BOOK_ATTRIBUTE)
        down.clear()
        second = make_service(env)
        await second.process(request_id)
        await pump(second)
        assert await state(env, request_id) == ("answered", 1)
        assert await env.audits() == []

    run(store_dsn, body)


def test_forged_requests_of_unknown_users_cannot_force_more_than_one_refresh_per_interval(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        service = make_service(env)
        gate = hold_timer(env, service)
        before = refreshes(env)
        states = len(env.hub.targets("/v1/device/state"))
        forged = [
            env.hub.add_request(seal_request("github.connect", {}, user=f"mallory{n}", signer="zed")) for n in range(20)
        ]
        start = env.clock.now
        for request_id in forged:
            await service.process(request_id)
        assert set(service.deferred) == set(forged[1:])
        gate.set()
        await pump(service)
        # one refresh for the first request, one after the interval for all the others
        assert env.log().count("runner.requests logs_refreshed") == 2
        assert len(env.hub.targets("/v1/device/state")) - states == 2
        assert refreshes(env) - before == 2  # the platform log twice; the org logs did not move
        assert env.clock.now - start == service_module.REFRESH_INTERVAL_S
        # every forged request audited exactly once, none handled
        assert sorted(await rejections(env)) == sorted((request_id, "key_unknown") for request_id in forged)
        assert all(env.hub.stored_requests[r].responses == [] for r in forged)
        assert service.waiting == {}
        assert service.deferred == {}

    run(store_dsn, body)


def test_a_rightful_request_behind_forged_ones_waits_at_most_one_interval(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        service = make_service(env)
        gate = hold_timer(env, service)
        forged = env.hub.add_request(seal_request("github.connect", {}, user="mallory", signer="zed"))
        await service.process(forged)
        join(env, "carol", {})
        request_id = env.hub.add_request(seal_request("github.connect", {}, user="carol"))
        await service.process(request_id)
        assert request_id in service.deferred
        gate.set()
        await pump(service)
        assert await state(env, request_id) == ("answered", 1)
        assert await rejections(env) == [(forged, "key_unknown")]

    run(store_dsn, body)


def test_the_waiting_memory_is_bounded(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        service = make_service(env)
        service_module.MAX_WAITING, limit = 2, service_module.MAX_WAITING
        try:
            env.world.state = "pending_certification"
            ids = [env.hub.add_request(seal_request("github.connect", {}, user=f"m{n}")) for n in range(3)]
            for request_id in ids:
                await service.process(request_id)
            assert list(service.waiting) == ids[1:]  # the oldest was dropped
        finally:
            service_module.MAX_WAITING = limit

    run(store_dsn, body)


def test_a_request_ahead_of_its_user_is_still_fully_verified_after_the_refresh(store_dsn: str) -> None:
    """Known after the refresh, but signed by someone else: refused for good (signature), never handled."""

    async def body(env: Env) -> None:
        service = make_service(env)
        join(env, "carol", {})
        request_id = env.hub.add_request(seal_request("github.connect", {}, user="carol", signer="zed"))
        await service.process(request_id)
        await pump(service)
        assert await rejections(env) == [(request_id, "signature_invalid")]
        assert await state(env, request_id) == ("rejected", 0)
        assert env.hub.stored_requests[request_id].responses == []

    run(store_dsn, body)


def test_shutdown_stops_the_release_timer(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        service = make_service(env)
        hold_timer(env, service)
        env.world.state = "pending_certification"
        request_id = env.hub.add_request(seal_request("github.connect", {}, user="carol"))
        await service.process(request_id)
        timer = service.timer
        assert timer is not None
        await service.shutdown()
        assert timer.cancelled()
        assert service.queue.empty()
        assert await env.audits() == []

    run(store_dsn, body)


def test_a_request_of_a_user_in_an_org_new_to_the_device_is_handled_without_audit(store_dsn: str) -> None:
    """The new user's own org (onboarding): the device is assigned there, but has never verified its log."""

    async def body(env: Env) -> None:
        service = make_service(env)
        spec = env.world.orgs[pid("z/org")]
        spec.log.add("device_assign", spec.founder, device=pid(DEVICE), wg=spec.default_wg, platform_seq=CERT_SEQ)
        assert env.world.listed is not None
        env.world.listed.append(spec.org)  # the hub now lists the org to the device
        request_id = env.hub.add_request(seal_request("github.connect", {}, user="zed"))
        await service.process(request_id)
        await pump(service)
        assert await state(env, request_id) == ("answered", 1)
        assert await env.audits() == []

    run(store_dsn, body)


def test_a_device_halted_by_the_refresh_waits_and_judges_after_the_tries(store_dsn: str) -> None:
    """The hub answers `pending_certification` for the state of the refresh: incomplete, like a failed log fetch."""

    async def body(env: Env) -> None:
        service = make_service(env)
        env.world.state = "pending_certification"
        request_id = env.hub.add_request(seal_request("github.connect", {}, user="carol"))
        await service.process(request_id)
        await pump(service)
        assert env.log().count("runner.requests logs_refreshed complete=false") == service_module.REFRESH_TRIES
        assert await rejections(env) == [(request_id, "key_unknown")]

    run(store_dsn, body)


def test_a_later_deferral_is_not_released_early(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        service = make_service(env)
        gate = hold_timer(env, service)
        service.defer("a", 1.0)
        service.defer("b", 5.0)
        gate.set()
        assert service.timer is not None
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert service.queue.get_nowait() == "a"
        assert service.queue.empty()
        await service.timer
        assert service.queue.get_nowait() == "b"
        assert env.clock.sleeps == [1.0, 4.0]

    run(store_dsn, body)
