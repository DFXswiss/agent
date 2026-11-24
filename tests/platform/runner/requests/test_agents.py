"""U-RUN-credentials / requests: device requests of a user's agent (ADR-082 sections 1 and 9.12): the agent signs with
its own key, the owner is its user, the response is sealed to the agent, follow-ups and busy flows compare owners, and a
revoked agent is refused."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from agent_runner.crypto.log import vectors as v
from agent_runner.requests.service import Flow

from ..credentials.kit import Env, pid, seal_request
from ..reconcile.rig import CERT_SEQ, DEVICE, World, org_log, standard_world
from ..settle import until
from .test_service import make_service, run, settle, state


def agent_world(*steps: Callable[[v.LogBuilder], object]) -> World:
    """Org `a` (alice founds it, the device serves her) with alice's agent `bot` (generation 1), then `steps`."""
    world = standard_world("a")
    log = next(iter(world.orgs.values())).log
    log.agent_add("alice", "bot", "alice", 1)
    for step in steps:
        step(log)
    return world


def test_a_request_of_the_agent_acts_for_its_user_and_is_answered_to_the_agent(store_dsn: str) -> None:
    seen: list[tuple[str, str]] = []

    async def handler(flow: Flow) -> None:
        seen.append((flow.user, flow.requester))
        await flow.respond({"step": "done"}, final=True)

    async def body(env: Env) -> None:
        service = make_service(env, {"github.connect": handler})
        request_id = env.hub.add_request(seal_request("github.connect", {}, user="bot"))
        await service.process(request_id)
        await settle(service)
        assert seen == [(pid("alice"), pid("bot"))]
        responses = env.hub.responses(request_id, user="bot")
        assert [(h["user"], b) for h, b in responses] == [(pid("bot"), {"step": "done"})]
        rows = await env.rows("SELECT user_id::text, state FROM runner.device_request WHERE id = %s", request_id)
        assert rows == [(pid("bot"), "answered")]

    run(store_dsn, body, world=agent_world())


def test_the_agent_continues_the_flow_of_its_user_and_replaces_it_as_the_same_owner(store_dsn: str) -> None:
    got: list[dict[str, Any]] = []
    started, hold = asyncio.Event(), asyncio.Event()

    async def handler(flow: Flow) -> None:
        await flow.respond({"step": "authorize"})
        started.set()
        follow = await flow.wait_follow_up(5)
        assert follow is not None
        got.append(follow.request.body)
        await flow.respond({"step": "done"}, final=True, to=follow)

    async def held(flow: Flow) -> None:
        await flow.respond({"step": "device_code"})
        await hold.wait()
        await flow.respond({"step": "done"}, final=True)

    async def body(env: Env) -> None:
        service = make_service(env, {"claude.login": handler, "github.connect": held})
        root = env.hub.add_request(seal_request("claude.login", {"mode": "manual"}))
        await service.process(root)
        await started.wait()
        follow = env.hub.add_request(seal_request("claude.login", {"code": "abc"}, follows=root, user="bot"))
        await service.process(follow)
        await settle(service)
        assert got == [{"code": "abc"}]
        assert env.hub.bodies(follow, user="bot") == [{"step": "done"}]
        # a root of the agent replaces a running root of its user (one flow per owner and kind)
        first = env.hub.add_request(seal_request("github.connect", {}))
        await service.process(first)
        await until(lambda: env.hub.bodies(first) == [{"step": "device_code"}], "first flow started")
        newer = env.hub.add_request(seal_request("github.connect", {}, user="bot"))
        await service.process(newer)
        assert f"abandoned request_id={first} reason=replaced" in env.log()
        hold.set()
        await settle(service)
        assert env.hub.bodies(newer, user="bot") == [{"step": "device_code"}, {"step": "done"}]

    run(store_dsn, body, world=agent_world())


def refused(world: World, user: str, code: str) -> Callable[[Env], Awaitable[None]]:
    async def body(env: Env) -> None:
        service = make_service(env, {"github.connect": lambda flow: flow.respond({}, final=True)})
        request_id = env.hub.add_request(seal_request("github.connect", {}, user=user))
        await service.process(request_id)
        assert await state(env, request_id) is None
        assert [a["detail"]["code"] for a in await env.audits()] == [code]
        assert env.hub.stored_requests[request_id].responses == []

    return body


def test_a_removed_agent_is_refused_as_unknown(store_dsn: str) -> None:
    world = agent_world(lambda log: log.add("remove", "alice", subject=pid("bot")))
    run(store_dsn, refused(world, "bot", "key_unknown"), world=world)


def test_a_superseded_agent_still_in_a_log_is_refused_as_unknown(store_dsn: str) -> None:
    world = standard_world("a", "b")
    first, second = (spec.log for spec in world.orgs.values())
    first.agent_add("alice", "bot", "alice", 1)
    second.agent_add("alice", "bot6", "alice", 2)
    run(store_dsn, refused(world, "bot", "key_unknown"), world=world)


def test_an_agent_of_a_user_the_device_does_not_serve_is_refused(store_dsn: str) -> None:
    world = World()
    spec = world.add_org(org_log("a", device=None))
    spec.log.invite_user(spec.founder, "carol", wgs={})
    spec.log.accept("carol")
    spec.log.agent_add("carol", "bot3", "carol", 1)
    spec.log.add("device_assign", spec.founder, device=pid(DEVICE), wg=pid("a/wg/one"), platform_seq=CERT_SEQ)
    run(store_dsn, refused(world, "bot3", "device_not_assigned"), world=world)
