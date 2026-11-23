"""U-RUN-credentials / requests: the device-request frame (05-runner.md sections 2.4 and 11.1, 04-crypto.md section
4.5, 03-api.md sections 4.10, 6.1 and 6.2)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest

from agent_runner.crypto.ids import uuid7
from agent_runner.orgs import audit
from agent_runner.reconcile.wire import ResponseInvalid
from agent_runner.requests.hints import HANDLERS as HINTS
from agent_runner.requests.reconcile import STEPS
from agent_runner.requests.service import Flow, RequestService, service_for
from agent_runner.requests.tasks import TASKS

from ..credentials.kit import WALL, Env, Stored, kit_env, pid, seal_request
from ..reconcile.rig import CERT_SEQ, DEVICE, World, org_log
from ..settle import settle as settle_tasks
from ..settle import until

Handler = Callable[[Flow], Awaitable[None]]


def run(dsn: str, body: Callable[[Env], Awaitable[None]], **kwargs: Any) -> None:
    async def main() -> None:
        async with kit_env(dsn, **kwargs) as env:
            await body(env)

    asyncio.run(main())


def make_service(env: Env, handlers: dict[str, Handler]) -> RequestService:
    built = RequestService(env.context, env.sync, handlers)
    env.context.request_service = built
    return built


async def settle(service: RequestService) -> None:
    await settle_tasks(lambda: [flow.task for flow in service.flows.values()], "the request flows")


async def state(env: Env, request_id: str) -> tuple[str, int] | None:
    rows = await env.rows("SELECT state, last_seq FROM runner.device_request WHERE id = %s", request_id)
    return (str(rows[0][0]), int(rows[0][1])) if rows else None


async def is_state(env: Env, request_id: str, expected: tuple[str, int]) -> bool:
    return await state(env, request_id) == expected


async def two_steps(flow: Flow) -> None:
    await flow.respond({"step": "progress", "echo": flow.body})
    await flow.respond({"step": "done"}, final=True)


def test_a_verified_request_is_handled_and_answered_with_gapless_signed_encrypted_responses(store_dsn: str) -> None:
    seen: list[dict[str, Any]] = []

    async def handler(flow: Flow) -> None:
        seen.append(flow.body)
        assert flow.user == pid("alice")
        assert 899 < flow.remaining() <= 900
        await two_steps(flow)

    async def body(env: Env) -> None:
        service = make_service(env, {"github.connect": handler})
        request_id = env.hub.add_request(seal_request("github.connect", {"x": "ü"}))
        await service.process(request_id)
        await settle(service)
        responses = env.hub.responses(request_id)
        assert [(h["seq"], h["final"], h["in_reply_to"], h["kind"]) for h, _b in responses] == [
            (1, False, request_id, "github.connect"),
            (2, True, request_id, "github.connect"),
        ]
        assert [b for _h, b in responses] == [{"step": "progress", "echo": {"x": "ü"}}, {"step": "done"}]
        assert seen == [{"x": "ü"}]
        assert await state(env, request_id) == ("answered", 2)
        # a request already answered is not handled again
        await service.process(request_id)
        assert len(seen) == 1
        assert await env.audits() == []

    run(store_dsn, body)


def refused_cases() -> list[tuple[str, Callable[[], dict[str, Any]], str]]:
    return [
        ("forged signature", lambda: seal_request("github.connect", {}, signer="bob"), "signature_invalid"),
        ("other device", lambda: seal_request("github.connect", {}, device="other"), "device_not_assigned"),
        ("expired", lambda: seal_request("github.connect", {}, ts=WALL - 901), "time_skew"),
        ("body not an object", lambda: seal_request("github.connect", None, raw=b"[1]"), "request_invalid"),
        ("body not JSON", lambda: seal_request("github.connect", None, raw=b"{"), "request_invalid"),
        ("other key", lambda: seal_request("github.connect", {}, encrypt_to="other"), "decrypt_failed"),
    ]


@pytest.mark.parametrize(("name", "make", "code"), refused_cases(), ids=[c[0] for c in refused_cases()])
def test_a_request_failing_verification_is_ignored_audited_counted_and_remembered(
    store_dsn: str, name: str, make: Callable[[], dict[str, Any]], code: str
) -> None:
    handled: list[str] = []

    async def handler(flow: Flow) -> None:
        handled.append(flow.id)

    async def body(env: Env) -> None:
        service = make_service(env, {"github.connect": handler})
        before = audit.counters(env.context)[audit.VERIFY_FAILURES]
        request_id = env.hub.add_request(make())
        await service.process(request_id)
        await service.process(request_id)
        assert handled == []
        assert env.hub.stored_requests[request_id].responses == []
        assert await state(env, request_id) == ("rejected", 0)
        assert audit.counters(env.context)[audit.VERIFY_FAILURES] == before + 1
        assert await env.audits() == [
            {
                "action": "runner.inbound_rejected",
                "result": "rejected",
                "target_kind": "device_request",
                "target_id": request_id,
                "detail": {"source": "request", "code": code},
            }
        ]
        assert f"runner.inbound rejected source=request code={code}" in env.log()

    run(store_dsn, body)


def test_a_user_the_device_does_not_serve_is_refused(store_dsn: str) -> None:
    world = World()
    spec = world.add_org(org_log("a", device=None))
    spec.log.invite_user(spec.founder, "carol", wgs={})
    spec.log.accept("carol")
    wg_one = pid("a/wg/one")
    spec.log.add("device_assign", spec.founder, device=pid(DEVICE), wg=wg_one, platform_seq=CERT_SEQ)

    async def body(env: Env) -> None:
        service = make_service(env, {"github.connect": two_steps})
        request_id = env.hub.add_request(seal_request("github.connect", {}, user="carol"))
        await service.process(request_id)
        assert [a["detail"]["code"] for a in await env.audits()] == ["device_not_assigned"]
        served = env.hub.add_request(seal_request("github.connect", {}, user="bob"))
        await service.process(served)
        await settle(service)
        assert await state(env, served) == ("answered", 2)

    run(store_dsn, body, world=world)


def test_an_envelope_served_under_another_id_or_malformed_is_refused(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        service = make_service(env, {})
        envelope = seal_request("github.connect", {})
        other = uuid7()
        env.hub.stored_requests[other] = Stored(envelope)
        await service.process(other)
        assert await state(env, other) == ("rejected", 0)
        malformed = uuid7()
        env.hub.scripted.append((200, {"envelope": {"header": "x"}}, {}))
        await service.process(malformed)
        nothing = uuid7()
        env.hub.scripted.append((200, {"envelope": None}, {}))
        await service.process(nothing)
        bad_user = uuid7()
        header = {**envelope["header"], "id": bad_user, "user": "x", "kind": "K" * 70}
        env.hub.scripted.append((200, {"envelope": {**envelope, "header": header}}, {}))
        await service.process(bad_user)
        codes = [(a["target_id"], a["detail"]["code"]) for a in await env.audits()]
        assert codes == [
            (other, "header_invalid"),
            (malformed, "header_invalid"),
            (nothing, "header_invalid"),
            (bad_user, "header_invalid"),
        ]
        rows = await env.rows("SELECT kind, user_id::text FROM runner.device_request WHERE id = %s", bad_user)
        assert rows == [("unknown", "00000000-0000-0000-0000-000000000000")]

    run(store_dsn, body)


def test_a_request_the_hub_does_not_serve_is_logged_and_tried_again_later(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        service = make_service(env, {"github.connect": two_steps})
        missing = uuid7()
        await service.process(missing)
        assert f"fetch_failed request_id={missing} code=not_found" in env.log()
        request_id = env.hub.add_request(seal_request("github.connect", {}))
        env.hub.tamper.append("bad_signature")
        await service.process(request_id)
        assert f"fetch_failed request_id={request_id} code=response_signature_invalid" in env.log()
        assert await state(env, request_id) is None
        await service.process(request_id)
        await settle(service)
        assert await state(env, request_id) == ("answered", 2)

    run(store_dsn, body)


def test_follow_ups_reach_the_running_flow_of_their_root_and_others_are_refused(store_dsn: str) -> None:
    got: list[dict[str, Any]] = []
    started = asyncio.Event()

    async def handler(flow: Flow) -> None:
        await flow.respond({"step": "authorize"})
        started.set()
        follow = await flow.wait_follow_up(5)
        assert follow is not None
        got.append(follow.request.body)
        await flow.respond({"step": "done"}, final=True, to=follow)

    async def body(env: Env) -> None:
        service = make_service(env, {"claude.login": handler})
        root = env.hub.add_request(seal_request("claude.login", {"mode": "manual"}))
        await service.process(root)
        await started.wait()
        stranger = env.hub.add_request(seal_request("claude.login", {"code": "x"}, follows=root, user="bob"))
        other_kind = env.hub.add_request(seal_request("github.connect", {}, follows=root))
        orphan = env.hub.add_request(seal_request("claude.login", {"code": "x"}, follows=uuid7()))
        for request_id in (stranger, other_kind, orphan):
            await service.process(request_id)
        follow = env.hub.add_request(seal_request("claude.login", {"code": "abc"}, follows=root))
        await service.process(follow)
        await service.process(follow)  # routed already: not handed over twice
        await settle(service)
        assert got == [{"code": "abc"}]
        assert env.hub.bodies(root) == [{"step": "authorize"}]
        assert env.hub.bodies(follow) == [{"step": "done"}]
        assert env.hub.stored_requests[root].closed
        assert await state(env, root) == ("answered", 1)
        assert await state(env, follow) == ("answered", 1)
        codes = {a["target_id"]: a["detail"]["code"] for a in await env.audits()}
        assert codes == {stranger: "forbidden", other_kind: "forbidden", orphan: "forbidden"}

    run(store_dsn, body)


def test_wait_follow_up_gives_none_when_nothing_arrives(store_dsn: str) -> None:
    async def handler(flow: Flow) -> None:
        assert await flow.wait_follow_up(0) is None
        await flow.respond({"step": "failed", "code": "timeout"}, final=True)

    async def body(env: Env) -> None:
        service = make_service(env, {"claude.login": handler})
        request_id = env.hub.add_request(seal_request("claude.login", {"mode": "manual"}))
        await service.process(request_id)
        await settle(service)
        assert env.hub.bodies(request_id) == [{"step": "failed", "code": "timeout"}]

    run(store_dsn, body)


def test_a_newer_root_of_the_same_user_and_kind_replaces_the_running_flow_and_an_older_one_is_busy(
    store_dsn: str,
) -> None:
    hold = asyncio.Event()

    async def handler(flow: Flow) -> None:
        await flow.respond({"step": "device_code"})
        await hold.wait()
        await flow.respond({"step": "done"}, final=True)

    async def body(env: Env) -> None:
        service = make_service(env, {"github.connect": handler})
        older = env.hub.add_request(seal_request("github.connect", {}, ts=WALL - 10))
        first = env.hub.add_request(seal_request("github.connect", {}, ts=WALL - 5))
        newer = env.hub.add_request(seal_request("github.connect", {}))
        await service.process(first)
        await until(lambda: env.hub.bodies(first) == [{"step": "device_code"}], "first flow started")
        await service.process(older)
        assert env.hub.bodies(older) == [{"step": "failed", "code": "busy"}]
        assert await state(env, older) == ("answered", 1)
        await service.process(newer)
        await until(lambda: is_state(env, first, ("abandoned", 1)), "first flow abandoned")
        assert list(service.flows) == [newer]
        assert f"abandoned request_id={first} reason=replaced" in env.log()
        hold.set()
        await settle(service)
        assert env.hub.bodies(newer) == [{"step": "device_code"}, {"step": "done"}]

    run(store_dsn, body)


def test_a_kind_without_handler_and_failing_handlers_are_answered_failed(store_dsn: str) -> None:
    async def raises(flow: Flow) -> None:
        raise RuntimeError("boom")

    async def silent(flow: Flow) -> None:
        await flow.respond({"step": "progress"})

    async def final_then_raises(flow: Flow) -> None:
        await flow.respond({"step": "done"}, final=True)
        raise RuntimeError("late")

    async def body(env: Env) -> None:
        service = make_service(
            env, {"credential.check": raises, "mailbox.check": silent, "github.connect": final_then_raises}
        )
        unknown = env.hub.add_request(seal_request("claude.login", {}))
        failing = env.hub.add_request(seal_request("credential.check", {}))
        quiet = env.hub.add_request(seal_request("mailbox.check", {}))
        late = env.hub.add_request(seal_request("github.connect", {}))
        for request_id in (unknown, failing, quiet, late):
            await service.process(request_id)
        await settle(service)
        failed = {"step": "failed", "code": "upstream_error"}
        assert env.hub.bodies(unknown) == [failed]
        assert env.hub.bodies(failing) == [failed]
        assert env.hub.bodies(quiet) == [{"step": "progress"}, failed]
        assert env.hub.bodies(late) == [{"step": "done"}]
        assert f"no_handler request_id={unknown} kind=claude.login" in env.log()
        assert f"handler_failed request_id={failing} error=RuntimeError" in env.log()
        assert await state(env, late) == ("answered", 1)

    run(store_dsn, body)


def test_a_flow_ends_with_timeout_when_its_request_expires(store_dsn: str) -> None:
    async def forever(flow: Flow) -> None:
        await asyncio.Event().wait()

    async def body(env: Env) -> None:
        service = make_service(env, {"github.connect": forever})
        request_id = env.hub.add_request(seal_request("github.connect", {}, ts=WALL - 899.9))
        await service.process(request_id)
        await settle(service)
        assert env.hub.bodies(request_id) == [{"step": "failed", "code": "timeout"}]
        assert await state(env, request_id) == ("answered", 1)

    run(store_dsn, body)


def test_a_hub_that_closed_the_request_ends_the_flow_failed(store_dsn: str) -> None:
    async def handler(flow: Flow) -> None:
        env_hub.stored_requests[flow.id].closed = True
        await flow.respond({"step": "done"}, final=True)

    async def raises_after_close(flow: Flow) -> None:
        env_hub.stored_requests[flow.id].closed = True
        raise RuntimeError("after close")

    env_hub: Any = None

    async def body(env: Env) -> None:
        nonlocal env_hub
        env_hub = env.hub
        service = make_service(env, {"github.connect": handler, "claude.login": raises_after_close})
        closed = env.hub.add_request(seal_request("github.connect", {}))
        await service.process(closed)
        await settle(service)
        assert await state(env, closed) == ("failed", 1)
        assert f"closed request_id={closed} code=forbidden" in env.log()
        late = env.hub.add_request(seal_request("claude.login", {}))
        await service.process(late)
        await settle(service)
        assert await state(env, late) == ("failed", 1)
        assert f"respond_failed request_id={late} code=forbidden" in env.log()
        # a request without handler whose answer the hub refuses
        refused = env.hub.add_request(seal_request("mailbox.check", {}))
        env.hub.stored_requests[refused].closed = True
        await service.process(refused)
        assert await state(env, refused) == ("failed", 1)

    run(store_dsn, body)


def test_responses_are_resent_unchanged_after_transport_errors(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        service = make_service(env, {"github.connect": two_steps})
        request_id = env.hub.add_request(seal_request("github.connect", {}))
        env.hub.tamper.extend(["skip", "unsigned"])  # the fetch passes, the first response answer is unsigned
        await service.process(request_id)
        await settle(service)
        assert [h["seq"] for h, _b in env.hub.responses(request_id)] == [1, 2]
        posts = [r for r in env.hub.requests if r.method == "POST"]
        assert len(posts) == 3
        assert posts[0].raw == posts[1].raw  # the identical envelope again
        assert f"respond_retry request_id={request_id} reason=response_unsigned" in env.log()
        assert env.clock.sleeps == [1.0]

    run(store_dsn, body)


def test_a_hub_that_stays_unreachable_ends_the_responses(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        service = make_service(env, {"github.connect": two_steps})
        request_id = env.hub.add_request(seal_request("github.connect", {}))
        await service.process(request_id)
        await settle(service)
        other = env.hub.add_request(seal_request("github.connect", {}))
        env.hub.tamper.extend(["skip"] + ["unsigned"] * 4)
        await service.process(other)
        await settle(service)
        assert f"closed request_id={other} code=hub_unreachable" in env.log()
        assert env.clock.sleeps == [1.0, 2.0, 4.0]
        assert await state(env, other) == ("failed", 1)

    run(store_dsn, body)


def test_a_request_active_before_a_restart_is_answered_device_shutdown(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        service = make_service(env, {"github.connect": two_steps})
        request_id = env.hub.add_request(seal_request("github.connect", {}))
        async with env.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO runner.device_request (id, kind, user_id, state, last_seq) "
                "VALUES (%s, 'github.connect', %s, 'active', 0)",
                (request_id, pid("alice")),
            )
        await service.process(request_id)
        assert env.hub.bodies(request_id) == [{"step": "failed", "code": "device_shutdown"}]
        assert await state(env, request_id) == ("answered", 1)

    run(store_dsn, body)


def test_flows_whose_root_is_no_longer_listed_twice_are_abandoned(store_dsn: str) -> None:
    hold = asyncio.Event()

    async def handler(flow: Flow) -> None:
        await hold.wait()

    async def body(env: Env) -> None:
        service = make_service(env, {"github.connect": handler})
        request_id = env.hub.add_request(seal_request("github.connect", {}))
        await service.process(request_id)
        await asyncio.sleep(0.01)
        (step,) = STEPS
        assert (step.order, step.name) == (70, "requests")
        env.hub.listed = []
        await step.run(await env.desired())
        assert request_id in service.flows
        env.hub.listed = [request_id]
        await step.run(await env.desired())
        env.hub.listed = []
        await step.run(await env.desired())
        assert request_id in service.flows
        await step.run(await env.desired())
        assert request_id not in service.flows
        await until(lambda: is_state(env, request_id, ("abandoned", 0)), "flow abandoned")
        assert f"abandoned request_id={request_id} reason=not_listed" in env.log()
        assert env.hub.stored_requests[request_id].responses == []

    run(store_dsn, body)


def test_the_reconcile_step_offers_listed_requests_and_checks_their_form(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        service = make_service(env, {})
        request_id = env.hub.add_request(seal_request("github.connect", {}))
        (step,) = STEPS
        await step.run(await env.desired())
        assert service.queue.get_nowait() == request_id
        empty = await env.desired()
        empty.desired = None
        await step.run(empty)
        assert service.queue.empty()
        broken = await env.desired()
        broken.desired = {"requests": [{"request_id": "x"}]}
        with pytest.raises(ResponseInvalid):
            await step.run(broken)

    run(store_dsn, body)


def test_the_request_hint_offers_the_request_and_drops_a_malformed_one(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        service = make_service(env, {})
        await HINTS["request"](env.context, {"type": "request", "request_id": "nope"})
        assert "runner.requests hint_invalid" in env.log()
        request_id = uuid7()
        await HINTS["request"](env.context, {"type": "request", "request_id": request_id})
        assert service.queue.get_nowait() == request_id

    run(store_dsn, body)


def test_the_loop_processes_offers_and_ends_running_flows_on_shutdown(store_dsn: str) -> None:
    hold = asyncio.Event()

    async def handler(flow: Flow) -> None:
        await flow.respond({"step": "device_code"})
        await hold.wait()

    async def finished(flow: Flow) -> None:
        await flow.respond({"step": "done"}, final=True)
        await hold.wait()

    async def body(env: Env) -> None:
        service = make_service(env, {"github.connect": handler, "claude.login": finished})
        running = env.hub.add_request(seal_request("github.connect", {}))
        done = env.hub.add_request(seal_request("claude.login", {}))
        loop = asyncio.create_task(service.loop(env.context.stopping))
        service.offer(running)
        service.offer(done)
        await until(
            lambda: len(env.hub.stored_requests[running].responses) == 1 and env.hub.stored_requests[done].closed,
            "both offers answered",
        )
        env.context.stopping.set()
        await loop
        assert env.hub.bodies(running) == [{"step": "device_code"}, {"step": "failed", "code": "device_shutdown"}]
        assert env.hub.bodies(done) == [{"step": "done"}]
        assert await state(env, running) == ("answered", 2)

    run(store_dsn, body)


def test_shutdown_logs_an_answer_the_hub_refuses(store_dsn: str) -> None:
    hold = asyncio.Event()

    async def handler(flow: Flow) -> None:
        await hold.wait()

    async def body(env: Env) -> None:
        service = make_service(env, {"github.connect": handler})
        request_id = env.hub.add_request(seal_request("github.connect", {}))
        await service.process(request_id)
        await asyncio.sleep(0.01)
        env.hub.stored_requests[request_id].closed = True
        await service.shutdown()
        assert f"shutdown_respond_failed request_id={request_id} error=RequestClosed" in env.log()
        # not closed locally: a later start answers it `device_shutdown` (if the hub still lists it)
        assert await state(env, request_id) == ("active", 1)

    run(store_dsn, body)


def test_the_daemon_task_and_the_service_of_a_device_that_is_not_enrolled(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        (task,) = TASKS
        assert task.name == "requests"
        built = service_for(env.context)
        assert built is not None
        assert service_for(env.context) is built
        assert set(built.handlers) == {"github.connect", "claude.login", "credential.check", "mailbox.check"}
        env.context.stopping.set()
        await task.run(env.context)
        env.context.identity = None
        del env.context.request_service
        env.context.sync_runtime = None
        await task.run(env.context)
        assert "runner.requests not_enrolled" in env.log()
        await HINTS["request"](env.context, {"type": "request", "request_id": uuid7()})
        (step,) = STEPS
        await step.run(SimpleNamespace(desired={"requests": []}, context=env.context))

    run(store_dsn, body)


def test_cancelling_a_flow_whose_task_has_not_been_created_only_drops_it(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        service = make_service(env, {})
        request = await service.verify(*fetched(env, env.hub.add_request(seal_request("github.connect", {}))))
        flow = Flow(service=service, request=request, deadline=0.0)
        service.flows[flow.id] = flow
        service.cancel(flow)
        assert service.flows == {}
        assert flow.finished

    run(store_dsn, body)


def fetched(env: Env, request_id: str) -> tuple[str, dict[str, Any]]:
    return request_id, env.hub.stored_requests[request_id].envelope


@pytest.mark.parametrize(
    ("make", "code"),
    [
        (lambda: seal_request("github.connect", {}, ts=WALL + 121), "time_skew"),
        (lambda: seal_request("github.connect", {}, user="zed"), "key_unknown"),
    ],
    ids=["future", "unknown user"],
)
def test_a_transient_refusal_is_audited_once_and_verified_again_later(
    store_dsn: str, make: Callable[[], dict[str, Any]], code: str
) -> None:
    async def body(env: Env) -> None:
        service = make_service(env, {"github.connect": two_steps})
        request_id = env.hub.add_request(make())
        await service.process(request_id)
        await service.process(request_id)
        assert await state(env, request_id) is None  # not remembered as rejected
        assert [a["detail"]["code"] for a in await env.audits()] == [code]
        fetches = [r for r in env.hub.requests if r.target == f"/v1/device/requests/{request_id}"]
        assert len(fetches) == 2
        assert env.context.reconcile_engine.trigger.is_set()  # the logs are synced again soon

    run(store_dsn, body)


def test_a_follow_up_before_its_root_is_verified_again_once_the_root_runs(store_dsn: str) -> None:
    got: list[dict[str, Any]] = []

    async def handler(flow: Flow) -> None:
        follow = await flow.wait_follow_up(5)
        assert follow is not None
        got.append(follow.request.body)
        await flow.respond({"step": "done"}, final=True, to=follow)

    async def body(env: Env) -> None:
        service = make_service(env, {"claude.login": handler})
        root = env.hub.add_request(seal_request("claude.login", {"mode": "manual"}))
        follow = env.hub.add_request(seal_request("claude.login", {"code": "abc"}, follows=root))
        await service.process(follow)
        assert await state(env, follow) is None
        await service.process(root)
        await service.process(follow)
        await settle(service)
        assert got == [{"code": "abc"}]
        assert [a["detail"]["code"] for a in await env.audits()] == ["forbidden"]
        # a follow-up of a root that is closed here is refused for good
        late = env.hub.add_request(seal_request("claude.login", {"code": "x"}, follows=root))
        await service.process(late)
        assert await state(env, late) == ("rejected", 0)

    run(store_dsn, body)


def test_a_response_number_lost_before_the_hub_is_healed_from_expected_seq(store_dsn: str) -> None:
    async def body(env: Env) -> None:
        service = make_service(env, {"github.connect": two_steps})
        request_id = env.hub.add_request(seal_request("github.connect", {}))
        async with env.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO runner.device_request (id, kind, user_id, state, last_seq) "
                "VALUES (%s, 'github.connect', %s, 'active', 1)",
                (request_id, pid("alice")),
            )
        await service.process(request_id)  # seq 2 is refused (`expected_seq` 1), then sent as 1
        assert [h["seq"] for h, _b in env.hub.responses(request_id)] == [1]
        assert env.hub.bodies(request_id) == [{"step": "failed", "code": "device_shutdown"}]
        assert await state(env, request_id) == ("answered", 1)
        assert f"respond_gap request_id={request_id} seq=2 expected=1" in env.log()

    run(store_dsn, body)


def test_a_seq_conflict_that_cannot_be_healed_closes_the_flow(store_dsn: str) -> None:
    async def handler(flow: Flow) -> None:
        env_hub.scripted.append((409, {"error": "seq_conflict", "detail": {"expected_seq": 5}}, {}))
        await flow.respond({"step": "progress"})

    env_hub: Any = None

    async def body(env: Env) -> None:
        nonlocal env_hub
        env_hub = env.hub
        service = make_service(env, {"github.connect": handler})
        request_id = env.hub.add_request(seal_request("github.connect", {}))
        await service.process(request_id)
        await settle(service)
        assert f"closed request_id={request_id} code=seq_conflict" in env.log()

    run(store_dsn, body)


def test_a_finished_flow_is_not_replaced_by_a_newer_root(store_dsn: str) -> None:
    hold = asyncio.Event()

    async def handler(flow: Flow) -> None:
        await flow.respond({"step": "done"}, final=True)
        await hold.wait()

    async def body(env: Env) -> None:
        service = make_service(env, {"github.connect": handler})
        first = env.hub.add_request(seal_request("github.connect", {}, ts=WALL - 5))
        await service.process(first)
        await until(lambda: is_state(env, first, ("answered", 1)), "first flow answered")
        newer = env.hub.add_request(seal_request("github.connect", {}))
        await service.process(newer)
        await until(lambda: is_state(env, newer, ("answered", 1)), "newer flow answered")
        assert "reason=replaced" not in env.log()
        hold.set()
        await settle(service)
        assert await state(env, first) == ("answered", 1)
        assert await state(env, newer) == ("answered", 1)

    run(store_dsn, body)
