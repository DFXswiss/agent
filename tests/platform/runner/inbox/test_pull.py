"""U-RUN-inbox: pull loop, cursors, gaps and paging, inbound verification before acting, consumers, L10 refresh."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent_runner.crypto.log import vectors as v
from agent_runner.inbox import connect, hints, pull, tasks
from agent_runner.inbox.dispatch import Consumer, VerifiedEvent
from agent_runner.inbox.pull import PERIOD_S, Inbox, inbox_for, parse_page
from agent_runner.reconcile import wire
from agent_runner.reconcile.engine import Engine
from agent_runner.reconcile.reconcile import STEPS

from ..reconcile.rig import DEVICE, Env, OrgSpec, Rig, World, pid, prompt, seal, standard_world, wrap_for


class Recorder:
    def __init__(self, kinds: frozenset[str] = frozenset({"prompt"}), fail: int = 0) -> None:
        self.seen: list[VerifiedEvent] = []
        self.fail = fail
        self.consumer = Consumer("test.recorder", kinds, self.handle)

    async def handle(self, _context: Any, conn: Any, event: VerifiedEvent) -> None:
        if self.fail and event.kind == "prompt":
            self.fail -= 1
            raise RuntimeError("consumer down")
        await conn.execute("SELECT 1")
        self.seen.append(event)


async def ready(env: Env, recorder: Recorder) -> Inbox:
    """Reconcile once (verified logs and keys), then an inbox with the recorder as the only consumer."""
    engine = Engine(env.context, STEPS)
    env.context.reconcile_engine = engine
    run = await engine.run_once()
    assert run is not None
    assert run.halted is None
    inbox = Inbox(env.context, [recorder.consumer])
    env.context.inbox = inbox
    return inbox


async def cursors(env: Env) -> dict[str, int]:
    return {str(s): int(q) for s, q in await env.rows("SELECT scope_id::text, scope_seq FROM runner.inbox_cursor")}


async def rejections(env: Env) -> list[tuple[str, str]]:
    return [
        (a["target_id"], a["detail"]["code"]) for a in await env.audits() if a["action"] == "runner.inbound_rejected"
    ]


def one_org() -> tuple[World, OrgSpec]:
    world = standard_world("a")
    return world, next(iter(world.orgs.values()))


def test_verified_events_reach_the_consumer_with_their_body(rig: Rig) -> None:
    async def body() -> None:
        world, spec = one_org()
        wg = spec.default_wg
        world.add_event(wg, 1, prompt(spec, label="p1"))
        world.add_event(wg, 4, prompt(spec, "bob", label="p2"))  # gap: 2 and 3 never reach devices
        world.add_event(wg, 5, seal(spec, "alice", "settings.workgroup", label="s1", body={"repositories": []}))
        world.add_event(spec.org, 2, seal(spec, "alice", "org.profile", scope="org", label="o1", body={"name": "Ä"}))
        async with rig.env(world) as env:
            recorder = Recorder()
            inbox = await ready(env, recorder)
            assert await inbox.pull_all() == 4
            assert [(e.scope_seq, e.author, e.body["text"]) for e in recorder.seen] == [
                (1, pid("alice"), "Grüße p1"),
                (4, pid("bob"), "Grüße p2"),
            ]
            first = recorder.seen[0]
            assert (first.kind, first.org_id, first.scope, first.scope_id) == ("prompt", spec.org, "wg", wg)
            assert first.event_id == pid("event/a/p1")
            assert first.sealed["header"] == dict(first.header)
            assert first.received_at == "2026-11-21T12:31:00.000Z"
            assert await cursors(env) == {wg: 5, spec.org: 2}
            assert await rejections(env) == []
            assert await inbox.pull_all() == 0
            assert len(recorder.seen) == 2
            assert inbox.pulls == 2
            query = env.hub.targets("/v1/device/pull")[-1]
            assert (
                query
                == "/v1/device/pull?" + "&".join(sorted([f"cursor={spec.org}:2", f"cursor={wg}:5"])) + "&limit=200"
            )

    asyncio.run(body())


def test_pull_follows_more_pages_and_skips_repeats(rig: Rig) -> None:
    async def body() -> None:
        world, spec = one_org()
        for seq in range(1, 6):
            world.add_event(spec.default_wg, seq, prompt(spec, label=f"p{seq}"))
        world.pull_page = 2
        async with rig.env(world) as env:
            recorder = Recorder()
            inbox = await ready(env, recorder)
            assert await inbox.pull_all() == 5
            assert [e.scope_seq for e in recorder.seen] == [1, 2, 3, 4, 5]
            assert len(env.hub.targets("/v1/device/pull")) == 3
            # a hub repeating old events changes nothing
            env.hub.scripted = []
            async with env.pool.connection() as conn:
                await conn.execute("UPDATE runner.inbox_cursor SET scope_seq = 3")
            world.events[spec.default_wg] = world.events[spec.default_wg][:2] + world.events[spec.default_wg]
            items, more = parse_page({"events": [], "more": False})
            assert (items, more) == ([], False)
            assert await inbox.pull_all() == 2
            assert [e.scope_seq for e in recorder.seen][-2:] == [4, 5]

    asyncio.run(body())


def test_events_failing_verification_are_audited_and_skipped(rig: Rig) -> None:
    async def body() -> None:
        world, spec = one_org()
        wg, one = spec.default_wg, pid("a/wg/one")
        forged = prompt(spec, label="forged")
        forged["sig"] = prompt(spec, label="other")["sig"]
        not_writer = prompt(spec, "bob", label="early", lh=2)  # bob was no member at lh 2
        device_kind = seal(spec, "alice", "session.output", label="o", session=pid("s"), task=pid("t"))
        wrong_scope = prompt(spec, label="w", scope_id=one, key=b"\x01" * 32)
        world.add_event(wg, 1, forged)
        world.add_event(wg, 2, not_writer)
        world.add_event(wg, 3, device_kind)
        world.add_event(wg, 4, wrong_scope)
        world.add_event(wg, 5, {"header": "nope"})
        world.add_event(wg, 6, prompt(spec, label="ok"))
        async with rig.env(world) as env:
            env.hub.check = lambda *_args: None  # type: ignore[method-assign]
            recorder = Recorder()
            inbox = await ready(env, recorder)
            assert await inbox.pull_all() == 6
            assert [e.event_id for e in recorder.seen] == [pid("event/a/ok")]
            assert await rejections(env) == [
                (f"{wg}:1", "signature_invalid"),
                (f"{wg}:2", "author_not_writer"),
                (f"{wg}:3", "kind_not_allowed"),
                (f"{wg}:4", "header_invalid"),
                (f"{wg}:5", "header_invalid"),
            ]
            assert await cursors(env) == {wg: 6}
            assert "runner.inbound rejected source=event code=signature_invalid" in env.log()

    asyncio.run(body())


def test_bodies_that_do_not_open_are_refused(rig: Rig) -> None:
    async def body() -> None:
        world, spec = one_org()
        world.add_event(spec.default_wg, 1, prompt(spec, label="k", key=b"\x02" * 32))
        world.add_event(spec.default_wg, 2, prompt(spec, label="list", body=["not", "an", "object"]))
        async with rig.env(world) as env:
            recorder = Recorder()
            inbox = await ready(env, recorder)
            assert await inbox.pull_all() == 2
            assert recorder.seen == []
            assert await rejections(env) == [
                (f"{spec.default_wg}:1", "decrypt_failed"),
                (f"{spec.default_wg}:2", "header_invalid"),
            ]

    asyncio.run(body())


def test_events_without_a_consumer_only_move_the_cursor(rig: Rig) -> None:
    async def body() -> None:
        world, spec = one_org()
        world.add_event(spec.default_wg, 1, prompt(spec, label="p", epoch=7))  # no key for epoch 7 is needed
        async with rig.env(world) as env:
            inbox = await ready(env, Recorder(frozenset({"settings.workgroup"})))
            assert await inbox.pull_all() == 1
            assert await cursors(env) == {spec.default_wg: 1}
            assert env.hub.targets("/v1/device/wraps") == ["/v1/device/wraps"]

    asyncio.run(body())


def test_a_missing_key_refreshes_the_wraps_once_then_skips(rig: Rig) -> None:
    async def body() -> None:
        world, spec = one_org()
        spec.log.add("rotate", "alice", scope="wg", scope_id=spec.default_wg, epoch=2)
        world.add_event(spec.default_wg, 1, prompt(spec, label="new", epoch=2))
        world.add_event(spec.default_wg, 2, prompt(spec, label="newer", epoch=3))
        async with rig.env(world) as env:
            recorder = Recorder()
            inbox = await ready(env, recorder)
            world.wraps.append(wrap_for("alice", "wg", spec.default_wg, 2))  # arrives after the reconcile
            assert await inbox.pull_all() == 2
            assert [e.event_id for e in recorder.seen] == [pid("event/a/new")]
            assert len(env.hub.targets("/v1/device/wraps")) == 2
            assert f"runner.inbox key_missing scope={spec.default_wg} seq=2 epoch=3" in env.log()

    asyncio.run(body())


def test_an_lh_beyond_the_verified_head_refreshes_the_log(rig: Rig) -> None:
    async def body() -> None:
        world, spec = one_org()
        async with rig.env(world) as env:
            recorder = Recorder()
            inbox = await ready(env, recorder)
            spec.log.add("grant", "alice", subject=pid("bob"), wg=pid("a/wg/one"), access="write")  # 5
            world.add_event(spec.default_wg, 1, prompt(spec, label="fresh", lh=5))
            assert await inbox.pull_all() == 1
            assert len(recorder.seen) == 1
            assert env.context.org_book.head(spec.org).seq == 5

    asyncio.run(body())


def test_an_lh_the_refreshed_log_does_not_reach_is_a_fork(rig: Rig) -> None:
    async def body() -> None:
        world, spec = one_org()
        world.add_event(spec.default_wg, 1, prompt(spec, label="ahead", lh=9))
        world.add_event(spec.default_wg, 2, prompt(spec, label="after"))
        async with rig.env(world) as env:
            recorder = Recorder()
            inbox = await ready(env, recorder)
            assert await inbox.pull_all() == 0
            assert recorder.seen == []
            book = env.context.org_book
            assert book.stopped == {spec.org: "log_fork"}
            assert book.pull_scopes() == {}
            fork = [a for a in await env.audits() if a["action"] == "runner.log_fork"]
            assert [a["detail"] for a in fork] == [{"verified_seq": 4, "offered_seq": 9}]
            assert await cursors(env) == {}

    asyncio.run(body())


def test_an_lh_refresh_that_finds_a_rollback_stops_the_org(rig: Rig) -> None:
    async def body() -> None:
        world, spec = one_org()
        world.add_event(spec.default_wg, 1, prompt(spec, label="ahead", lh=9))
        async with rig.env(world) as env:
            inbox = await ready(env, Recorder())
            world.served[spec.org] = spec.log.entries[:2]
            assert await inbox.pull_all() == 0
            assert env.context.org_book.stopped == {spec.org: "log_rollback"}

    asyncio.run(body())


def test_a_failing_consumer_holds_its_scope_and_is_retried(rig: Rig) -> None:
    async def body() -> None:
        world, spec = one_org()
        world.add_event(spec.default_wg, 1, prompt(spec, label="p1"))
        world.add_event(spec.default_wg, 2, prompt(spec, label="p2"))
        world.add_event(spec.org, 1, seal(spec, "alice", "org.profile", scope="org", label="o", body={"name": "n"}))
        async with rig.env(world) as env:
            recorder = Recorder(frozenset({"prompt", "org.profile"}), fail=1)
            inbox = await ready(env, recorder)
            assert await inbox.pull_all() == 1
            assert [e.kind for e in recorder.seen] == ["org.profile"]
            assert await cursors(env) == {spec.org: 1}
            assert "runner.inbox consumer_failed scope=" in env.log()
            assert "error=RuntimeError" in env.log()
            assert "consumer down" not in env.log()
            assert await inbox.pull_all() == 2
            assert [e.scope_seq for e in recorder.seen if e.kind == "prompt"] == [1, 2]

    asyncio.run(body())


def test_unexpected_scopes_and_invalid_pages(rig: Rig) -> None:
    async def body() -> None:
        world, spec = one_org()
        async with rig.env(world) as env:
            inbox = await ready(env, Recorder())
            stray = prompt(spec, label="x", scope_id=pid("a/wg/one"))
            env.hub.scripted = [
                (
                    200,
                    {
                        "events": [
                            {
                                "scope_id": pid("a/wg/one"),
                                "scope_seq": 1,
                                "received_at": "2026-11-21T12:31:00.000Z",
                                "event": stray,
                            }
                        ],
                        "more": True,
                    },
                    {},
                )
            ]
            assert await inbox.pull_all() == 0
            assert f"runner.inbox unexpected_scope scope={pid('a/wg/one')}" in env.log()
            for answer in (
                {"events": [], "more": "no"},
                {"events": [{"scope_id": spec.default_wg}], "more": False},
                {"events": "none", "more": False},
            ):
                with pytest.raises(wire.ResponseInvalid):
                    parse_page(answer)

    asyncio.run(body())


def test_decertified_authors_are_refused(rig: Rig) -> None:
    async def body() -> None:
        world, spec = one_org()
        found = v.secrets("pdev")
        world.platform.add("device_invite", invite_id=found.invite_id, pk_inv=found.pk_inv, expires=v.EXPIRES)  # 3
        world.platform.certify("pdev")  # 4
        world.platform.add("device_decertify", device=pid("pdev"))  # 5
        spec.log.add("device_assign", "alice", device=pid("pdev"), wg=spec.default_wg, platform_seq=4)  # 5
        world.add_event(
            spec.default_wg, 1, seal(spec, "pdev", "operation.params", label="op", ref=pid("op"), body={"x": 1})
        )
        async with rig.env(world) as env:
            inbox = await ready(env, Recorder(frozenset({"operation.params"})))
            assert await inbox.pull_all() == 1
            assert await rejections(env) == [(f"{spec.default_wg}:1", "author_not_writer")]

    asyncio.run(body())


def test_nothing_is_pulled_without_acting_scopes_or_enrollment(rig: Rig) -> None:
    async def body() -> None:
        async with rig.env(World()) as env:
            inbox = Inbox(env.context, [])
            assert await inbox.pull_all() == 0
            assert env.hub.targets("/v1/device/pull") == []
        async with rig.env(enrolled=False) as env:
            assert inbox_for(env.context) is None
            assert await Inbox(env.context, []).pull_all() == 0
            await tasks.run_inbox(env.context)
            await hints.events(env.context, {"type": "events"})
            await connect.pull_now(env.context)
            assert "runner.inbox not_enrolled" in env.log()

    asyncio.run(body())


def test_task_hint_and_connect_hook(rig: Rig) -> None:
    async def body() -> None:
        world, spec = one_org()
        world.add_event(spec.default_wg, 1, prompt(spec, label="p1"))
        async with rig.env(world) as env:
            recorder = Recorder()
            inbox = await ready(env, recorder)
            assert inbox_for(env.context) is inbox
            await hints.HANDLERS["events"](env.context, {"type": "events"})
            assert inbox.trigger.is_set()
            await connect.HOOKS[0].run(env.context)
            assert (connect.HOOKS[0].phase, connect.HOOKS[0].name) == ("pull", "inbox.pull")
            assert len(recorder.seen) == 1
            env.hub.scripted = [(500, {"error": "unavailable"}, {})]

            def tick(_seconds: float) -> None:
                if len(env.clock.sleeps) == 1:
                    inbox.request()
                if len(env.clock.sleeps) == 2:
                    env.context.stopping.set()

            env.clock.on_sleep = tick
            await tasks.run_inbox(env.context)
            assert env.clock.sleeps == [PERIOD_S, PERIOD_S]
            assert "runner.inbox pull_failed error=HubRefusal code=unavailable" in env.log()
            assert [spec.name for spec in tasks.TASKS] == ["inbox"]

    asyncio.run(body())


def test_inbox_for_discovers_consumers(rig: Rig) -> None:
    async def body() -> None:
        async with rig.env() as env:
            inbox = inbox_for(env.context)
            assert inbox is not None
            assert inbox is inbox_for(env.context)
            assert all(isinstance(c, Consumer) for c in inbox.consumers)
            assert DEVICE == "rdev"

    asyncio.run(body())


def test_a_pull_ends_after_the_round_limit(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    async def body() -> None:
        world, spec = one_org()
        for seq in range(1, 4):
            world.add_event(spec.default_wg, seq, prompt(spec, label=f"p{seq}"))
        world.pull_page = 1
        async with rig.env(world) as env:
            inbox = await ready(env, Recorder())
            monkeypatch.setattr(pull, "MAX_ROUNDS", 2)
            assert await inbox.pull_all() == 2
            assert await cursors(env) == {spec.default_wg: 2}

    asyncio.run(body())


def test_l10_refreshes_the_platform_log_first(rig: Rig) -> None:
    async def body() -> None:
        world, spec = one_org()
        async with rig.env(world) as env:
            recorder = Recorder()
            inbox = await ready(env, recorder)
            found = v.secrets("pdev")
            world.platform.add("device_invite", invite_id=found.invite_id, pk_inv=found.pk_inv, expires=v.EXPIRES)
            world.platform.certify("pdev")  # 4, unknown to the device's copy so far
            spec.log.add("device_assign", "alice", device=pid("pdev"), wg=spec.default_wg, platform_seq=4)  # 5
            world.add_event(spec.default_wg, 1, prompt(spec, label="after-assign", lh=5))
            assert await inbox.pull_all() == 1
            assert len(recorder.seen) == 1
            book = env.context.org_book
            assert book.stopped == {}
            assert book.head(spec.org).seq == 5
            assert await env.audits() == []

    asyncio.run(body())


def test_an_lh_reached_only_by_refused_entries_refuses_the_event(rig: Rig) -> None:
    async def body() -> None:
        world, spec = one_org()
        async with rig.env(world) as env:
            inbox = await ready(env, Recorder())
            spec.log.add("wg_create", "alice", signer="bob", wg=pid("a/wg/x"), epoch=1)  # 5, bad signature
            world.add_event(spec.default_wg, 1, prompt(spec, label="on-forged", lh=5))
            assert await inbox.pull_all() == 1
            assert env.context.org_book.stopped == {}
            assert await rejections(env) == [(spec.org, "log_signature"), (f"{spec.default_wg}:1", "log_chain")]

    asyncio.run(body())


def test_a_malformed_header_never_refreshes_or_stops(rig: Rig) -> None:
    async def body() -> None:
        world, spec = one_org()
        bad = prompt(spec, label="m", lh=9)
        bad["header"] = {**bad["header"], "salt": "x"}
        world.add_event(spec.default_wg, 1, bad)
        async with rig.env(world) as env:
            env.hub.check = lambda *_args: None  # type: ignore[method-assign]
            inbox = await ready(env, Recorder())
            logs_before = len(env.hub.targets("/v1/device/logs"))
            assert await inbox.pull_all() == 1
            assert len(env.hub.targets("/v1/device/logs")) == logs_before
            assert env.context.org_book.stopped == {}
            assert await rejections(env) == [(f"{spec.default_wg}:1", "header_invalid")]

    asyncio.run(body())


def test_a_replayed_event_is_refused(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    async def body() -> None:
        world, spec = one_org()
        sealed = prompt(spec, label="once")
        world.add_event(spec.default_wg, 1, sealed)
        world.add_event(spec.default_wg, 2, sealed)
        async with rig.env(world) as env:
            recorder = Recorder()
            inbox = await ready(env, recorder)
            assert await inbox.pull_all() == 2
            assert len(recorder.seen) == 1
            assert await rejections(env) == [(f"{spec.default_wg}:2", "event_conflict")]
            monkeypatch.setattr(pull, "MAX_SEEN", 1)
            inbox.remember("x")
            assert inbox.seen == {"x"}

    asyncio.run(body())


def test_an_entitled_epoch_key_is_waited_for(rig: Rig) -> None:
    async def body() -> None:
        world, spec = one_org()
        spec.log.add("rotate", "alice", scope="wg", scope_id=spec.default_wg, epoch=2)
        world.add_event(spec.default_wg, 1, prompt(spec, label="new", epoch=2))
        world.add_event(spec.default_wg, 2, prompt(spec, label="newer", epoch=2))
        world.add_event(spec.org, 1, seal(spec, "alice", "org.profile", scope="org", label="o", body={"name": "n"}))
        async with rig.env(world) as env:
            recorder = Recorder(frozenset({"prompt", "org.profile"}))
            inbox = await ready(env, recorder)
            assert await inbox.pull_all() == 1
            assert [e.kind for e in recorder.seen] == ["org.profile"]
            assert f"runner.inbox key_waiting scope={spec.default_wg} seq=1 epoch=2" in env.log()
            assert await cursors(env) == {spec.org: 1}
            world.wraps.append(wrap_for("alice", "wg", spec.default_wg, 2))
            assert await inbox.pull_all() == 2
            assert [e.scope_seq for e in recorder.seen if e.kind == "prompt"] == [1, 2]

    asyncio.run(body())


def test_a_blocked_scope_does_not_starve_the_others(rig: Rig) -> None:
    async def body() -> None:
        world, spec = one_org()
        for seq in range(1, 6):
            world.add_event(spec.default_wg, seq, prompt(spec, label=f"p{seq}"))
        for seq in range(1, 4):
            world.add_event(
                spec.org, seq, seal(spec, "alice", "org.profile", scope="org", label=f"o{seq}", body={"name": "n"})
            )
        world.pull_page = 2
        async with rig.env(world) as env:
            recorder = Recorder(frozenset({"prompt", "org.profile"}), fail=100)
            inbox = await ready(env, recorder)
            await inbox.pull_all()
            assert [e.scope_seq for e in recorder.seen] == [1, 2, 3]
            assert await cursors(env) == {spec.org: 3}

    asyncio.run(body())


def test_a_support_author_unknown_to_the_platform_copy_refreshes_it_once(rig: Rig) -> None:
    """ADR-096: a prompt of a support person the device's platform log does not know yet is checked again after one
    refresh of the platform log; an author that is neither a member nor support stays refused."""

    async def body() -> None:
        world, spec = one_org()
        spec.log.add("support_grant", "alice", wg=spec.default_wg, access="write")  # 5
        async with rig.env(world) as env:
            recorder = Recorder()
            inbox = await ready(env, recorder)
            world.platform.add("user_certify", user=pid("sam"), pub=v.pub("sam"))  # 3
            world.platform.add("role_grant", subject=pid("sam"), role="support")  # 4
            world.add_event(spec.default_wg, 1, prompt(spec, "sam", label="support", lh=5))
            world.add_event(spec.default_wg, 2, prompt(spec, "erin", label="stranger", lh=5))
            assert await inbox.pull_all() == 2
            assert [(event.author, event.body["text"]) for event in recorder.seen] == [(pid("sam"), "Grüße support")]
            assert await rejections(env) == [(f"{spec.default_wg}:2", "author_not_writer")]
            assert env.context.org_book.support_roster() == env.context.org_book.support_authors()

    asyncio.run(body())
