"""U-RUN-crypto / catch-up wrapping (05-runner.md section 2.5, 04-crypto.md section 5.11): planning against the log
vectors (`contracts/vectors/log-wraps.json`), the upload limit of 200, refusals and the step in a reconcile run."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from agent_runner.catchup import plan
from agent_runner.catchup import reconcile as catchup_step
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.log import vectors as v
from agent_runner.crypto.log.platform import verify_platform_log
from agent_runner.crypto.log.verify import verify_log
from agent_runner.crypto.log.wraps import WrapSlot, check_cred_wrap, check_wrap
from agent_runner.crypto.wrap import unwrap_key
from agent_runner.identity import DeviceKeys
from agent_runner.orgs.book import OrgBook
from agent_runner.orgs.db import session
from agent_runner.orgs.keys import store_scope_key
from agent_runner.reconcile.engine import Engine, Run
from agent_runner.reconcile.reconcile import STEPS as BASE_STEPS
from agent_runner.sync.http import HubRefusal

from ..reconcile.rig import DEVICE, Env, Rig, device_keys, pid, standard_world

VECTORS = json.loads((Path(__file__).resolve().parents[4] / "contracts" / "vectors" / "log-wraps.json").read_text())
Json = dict[str, Any]


def slot(item: Json) -> WrapSlot:
    return WrapSlot(item["scope"], item["scope_id"], item["epoch"], item["recipient_id"])


def label_of(principal: str) -> str:
    return str(next(name for name, ident in VECTORS["identities"].items() if ident["id"] == principal))


def vector_book(wrapper: str) -> OrgBook:
    ident = VECTORS["identities"][label_of(wrapper)]
    kind = "platform" if label_of(wrapper) == "pdev" else "org"
    book = OrgBook(
        device_id=wrapper, kem_public=ident["kem"], sig_public=ident["sig"], root=v.sig_pk("root"), kind=kind
    )
    book.platform = verify_platform_log(VECTORS["logs"]["platform"], v.sig_pk("root"))
    log = verify_log(VECTORS["logs"]["main"], VECTORS["founder"], platform=book.platform.state)
    book.logs = {log.state.org: log}
    return book


DEVICE_PLANS = [
    p for p in VECTORS["catch_up"]["plans"] if label_of(p["wrapper"]) in ("device", "pdev") and not p["excluded"]
]


@pytest.mark.parametrize("vector", DEVICE_PLANS, ids=lambda p: label_of(p["wrapper"]))
def test_plans_match_the_catch_up_vectors(vector: Json) -> None:
    book = vector_book(vector["wrapper"])
    assert book.acting_orgs() == [VECTORS["ids"]["org"]]
    expected = sorted(slot(item) for item in vector["missing"])
    held = {(s.scope, s.scope_id, s.epoch) for s in expected}
    memory = plan.Memory(done={slot(item) for item in VECTORS["catch_up"]["existing"]})
    assert plan.plan(book, held, memory) == expected
    # without the keys nothing is planned; with half of them only their slots
    assert plan.plan(book, set(), memory) == []
    some = {(s.scope, s.scope_id, s.epoch) for s in expected[:1]}
    assert plan.plan(book, some, memory) == [s for s in expected if (s.scope, s.scope_id, s.epoch) in some]


def test_cred_plans_match_the_vectors() -> None:
    [vector] = [p for p in VECTORS["cred_catch_up"]["plans"] if label_of(p["wrapper"]) == "device"]
    book = vector_book(vector["wrapper"])
    memory = plan.Memory(done={slot(item) for item in VECTORS["cred_catch_up"]["existing"]})
    held = {("cred", vector["owner"], 1), ("cred", vector["owner"], vector["current_epoch"])}
    planned = [s for s in plan.plan(book, held, memory) if s.scope == "cred"]
    assert planned == sorted(slot(item) for item in vector["missing"])
    # a cred key of somebody who is no user of an acting org is never planned
    stranger = {("cred", pid("stranger"), 1)}
    assert [s for s in plan.plan(book, stranger, memory) if s.scope == "cred"] == []


def test_the_plan_is_limited_and_remembers() -> None:
    [vector] = [p for p in DEVICE_PLANS if label_of(p["wrapper"]) == "device"]
    book = vector_book(vector["wrapper"])
    expected = sorted(slot(item) for item in vector["missing"])
    held = {(s.scope, s.scope_id, s.epoch) for s in expected}
    memory = plan.Memory(done={slot(item) for item in VECTORS["catch_up"]["existing"]})
    first = plan.plan(book, held, memory, limit=2)
    assert first == expected[:2]
    plan.finish(memory, first)
    assert plan.plan(book, held, memory) == expected[2:]
    plan.refuse(memory, expected[2:], plan.heads(book))
    assert plan.plan(book, held, memory) == []
    plan.refuse(memory, expected[2:], ())
    assert plan.plan(book, held, memory) == expected[2:]
    assert plan.MAX_WRAPS == 200


def test_recipient_keys_come_from_the_logs() -> None:
    [vector] = [p for p in DEVICE_PLANS if label_of(p["wrapper"]) == "device"]
    book = vector_book(vector["wrapper"])
    for item in vector["missing"]:
        assert plan.recipient_kem(book, slot(item)) == VECTORS["identities"][label_of(item["recipient_id"])]["kem"]
    alice = VECTORS["identities"]["alice"]
    assert plan.recipient_kem(book, WrapSlot("cred", alice["id"], 2, alice["id"])) == alice["kem"]
    with pytest.raises(KeyError):
        plan.recipient_kem(book, WrapSlot("wg", VECTORS["ids"]["wg_one"], 1, pid("nobody")))


def test_memory_is_kept_on_the_context() -> None:
    context = type("Ctx", (), {})()
    made = plan.memory_for(context)
    assert plan.memory_for(context) is made


# --- the step in a reconcile run (platform device `rdev` of the RUN-03 rig) ----------------------------------------


class Uploads:
    """`POST /v1/device/wraps` of the fake hub: W3 against the world's logs, insert-only slots."""

    def __init__(self, env: Env, refuse: set[str] | None = None, code: str = "wrap_unauthorized") -> None:
        self.env = env
        self.stored: dict[tuple[str, str, int, str], Json] = {}
        self.requests: list[list[Json]] = []
        self.refuse = refuse or set()
        self.code = code
        original = env.hub.route

        def route(method: str, target: str, body: Any) -> tuple[int, Json]:
            if (method, target) == ("POST", "/v1/device/wraps"):
                return self.post(body)
            return original(method, target, body)

        env.hub.route = route  # type: ignore[method-assign]

    def post(self, body: Json) -> tuple[int, Json]:
        self.env.hub.check("api/device-wraps-post.request.json", body)
        self.requests.append(body["wraps"])
        world = self.env.world
        platform = verify_platform_log(world.platform.entries, v.sig_pk("root")).state
        logs = [verify_log(spec.log.entries, v.sig_pk(spec.founder), platform=platform) for spec in world.orgs.values()]
        for wrap in body["wraps"]:
            if wrap["recipient_id"] in self.refuse:
                return 400, {"error": self.code}
            try:
                if wrap["scope"] == "cred":
                    check_cred_wrap(wrap, pid(DEVICE), logs, wrap["epoch"], require_active=True)
                else:
                    [log] = [log for log in logs if wrap["scope_id"] in (log.state.org, *log.state.workgroups)]
                    check_wrap(wrap, pid(DEVICE), log, platform.excluded(log.state), require_active=True)
            except CryptoError as refused:
                return 400, {"error": refused.code}
        stored = 0
        for wrap in body["wraps"]:
            key = (wrap["scope"], wrap["scope_id"], wrap["epoch"], wrap["recipient_id"])
            if key not in self.stored:
                self.stored[key] = wrap
                stored += 1
        answer = {"stored": stored, "ignored": len(body["wraps"]) - stored}
        self.env.hub.check("api/device-wraps-post.response.json", answer)
        return 200, answer


async def once(env: Env) -> Run:
    built = getattr(env.context, "reconcile_engine", None)
    if built is None:
        built = Engine(env.context, [*BASE_STEPS, *catchup_step.STEPS])
        env.context.reconcile_engine = built
    run = await built.run_once()
    assert run is not None
    return run


def keys_of(label: str) -> DeviceKeys:
    return device_keys(label)


def test_the_step_uploads_signed_wraps_once_per_daemon(rig: Rig) -> None:
    async def body() -> None:
        world = standard_world("a")
        spec = next(iter(world.orgs.values()))
        async with rig.env(world) as env:
            uploads = Uploads(env)
            run = await once(env)
            assert run.halted is None, env.log()
            members = sorted([pid("alice"), pid("bob"), pid(DEVICE)])
            assert sorted(uploads.stored) == sorted(
                [("org", spec.org, 1, m) for m in members] + [("wg", spec.default_wg, 1, m) for m in members]
            )
            bob = uploads.stored[("wg", spec.default_wg, 1, pid("bob"))]
            key = unwrap_key(
                v.identity("bob").kem,
                scope="wg",
                scope_id=spec.default_wg,
                epoch=1,
                recipient=pid("bob"),
                wrap=bob["wrap"],
            )
            assert key == v.scope_key("wg", spec.default_wg, 1)
            assert "runner.catchup planned=6 done=6 refused=0" in env.log()
            await once(env)
            assert len(uploads.requests) == 1
            # a new member of the org gets the keys at the next run
            spec.log.invite_user("alice", "carol")
            spec.log.accept("carol")
            await once(env)
            assert len(uploads.requests) == 2
            assert sorted(w["recipient_id"] for w in uploads.requests[1]) == [pid("carol"), pid("carol")]

    asyncio.run(body())


def test_at_most_200_wraps_per_run(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plan, "MAX_WRAPS", 4)

    async def body() -> None:
        world = standard_world("a")
        async with rig.env(world) as env:
            uploads = Uploads(env)
            await once(env)
            await once(env)
            await once(env)
            assert [len(r) for r in uploads.requests] == [4, 2]

    asyncio.run(body())


def test_refused_batches_are_split_and_refused_slots_wait_for_a_new_head(rig: Rig) -> None:
    async def body() -> None:
        world = standard_world("a")
        spec = next(iter(world.orgs.values()))
        async with rig.env(world) as env:
            uploads = Uploads(env, refuse={pid("bob")})
            await once(env)
            # bisection: each half that holds a refused slot is halved again
            assert [len(r) for r in uploads.requests] == [6, 3, 1, 2, 1, 1, 3, 1, 2, 1, 1]
            assert len(uploads.stored) == 4
            assert "runner.catchup planned=6 done=4 refused=2" in env.log()
            await once(env)
            assert len(uploads.requests) == 11
            uploads.refuse = set()
            spec.log.invite_user("alice", "carol")
            await once(env)
            assert sorted(w["recipient_id"] for w in uploads.requests[-1]) == [pid("bob"), pid("bob")]

    asyncio.run(body())


def test_other_refusals_fail_the_step(rig: Rig) -> None:
    async def body() -> None:
        async with rig.env(standard_world("a")) as env:
            Uploads(env, refuse={pid("alice")}, code="rate_limited")
            run = await once(env)
            assert run.halted == "failed"
            assert "step_failed step=catchup error=HubRefusal code=rate_limited" in env.log()

    asyncio.run(body())


def test_cred_keys_are_wrapped_for_eligible_devices(rig: Rig) -> None:
    async def body() -> None:
        world = standard_world("a")
        async with rig.env(world) as env:
            uploads = Uploads(env)
            await once(env)
            async with session(env.pool) as conn, conn.transaction():
                await store_scope_key(
                    conn,
                    env.sync.storage_key,
                    ("cred", pid("bob"), 1),
                    v.scope_key("cred", pid("bob"), 1),
                    source="wrap",
                )
            await once(env)
            assert sorted((w["scope"], w["recipient_id"]) for w in uploads.requests[-1]) == sorted(
                [("cred", pid("bob")), ("cred", pid(DEVICE))]
            )

    asyncio.run(body())


def test_without_a_desired_state_nothing_happens(rig: Rig) -> None:
    async def body() -> None:
        async with rig.env(standard_world("a")) as env:
            run = Run(
                context=env.context,
                sync=env.sync,
                book=OrgBook(device_id=pid(DEVICE), kem_public="", sig_public="", root=v.sig_pk("root")),
            )
            assert await catchup_step.catch_up(run) == 0

    asyncio.run(body())


def test_a_single_refused_wrap_that_is_not_a_rule_error_propagates(rig: Rig) -> None:
    async def body() -> None:
        async with rig.env(standard_world("a")) as env:
            run = await once(env)
            calls: list[int] = []

            async def post(_run: Run, wraps: list[Json]) -> None:
                calls.append(len(wraps))
                raise HubRefusal(503, "unavailable")

            original = catchup_step._post
            catchup_step._post = post  # type: ignore[assignment]
            try:
                with pytest.raises(HubRefusal):
                    await catchup_step.upload(run, [WrapSlot("org", pid("x"), 1, pid("y"))], [{}])
            finally:
                catchup_step._post = original
            assert calls == [1]

    asyncio.run(body())
