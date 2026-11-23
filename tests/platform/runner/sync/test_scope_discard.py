"""KOR-08 (AUD-01 A-01, ADR-047): the cases in which a scope key is gone for good, with verified logs.

The RUN-03 rig builds real signed membership logs; the reconcile engine runs the real steps (state, logs, wraps,
assignments and the outbox step of this area), the pusher the real outbox logic against the rig's hub:

- `device_unassign` with its rotation, and `remove` of this device: the assignment step deletes the keys; events the
  device wrote for that scope and had not sealed yet can never be sealed. They wait in the scope's queue (other scopes
  go on) and the outbox step discards them with an audit `runner.alert outbox_discarded`, as the hub would refuse them
  (`author_not_writer`, E5 at receipt) anyway.
- a stopped org (rollback, fork), a scope of no verified org, or a scope the device may still read keep their queue:
  nothing is discarded without verified evidence.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agent_runner import registry
from agent_runner.assignments import reconcile as assignment_step
from agent_runner.orgs.book import book_for
from agent_runner.reconcile.engine import Engine, Run
from agent_runner.reconcile.reconcile import STEPS as BASE_STEPS
from agent_runner.store.migrate import migrate
from agent_runner.sync import outbox
from agent_runner.sync import reconcile as outbox_step

from ..reconcile.rig import DEVICE, Env, Rig, pid, standard_world
from .test_push import METRICS

ORG_A, ORG_B = pid("a/org"), pid("b/org")
WG_A, WG_B = pid("a/wg/default"), pid("b/wg/default")


def rig_for(runner_dsn: str) -> Rig:
    assert migrate(runner_dsn).status == "applied"
    return Rig(runner_dsn)


async def reconcile(env: Env) -> Run:
    engine = getattr(env.context, "reconcile_engine", None)
    if engine is None:
        steps = [*BASE_STEPS, *assignment_step.STEPS, *outbox_step.STEPS]
        engine = Engine(env.context, sorted(steps, key=lambda s: s.order))
        env.context.reconcile_engine = engine
    run = await engine.run_once()
    assert run is not None
    assert run.halted is None, env.log()
    return run


def event(org: str, scope: str, scope_id: str, label: str) -> dict[str, Any]:
    wg = scope_id if scope == "wg" else ""
    kind = "session.output" if scope == "wg" else "device.profile"
    body: dict[str, Any] = (
        {"message_id": label, "role": "assistant", "text": label, "turn": 1} if scope == "wg" else {"label": label}
    )
    return outbox.event_draft(
        id=pid(f"event/{label}"),
        org=org,
        scope=scope,
        scope_id=scope_id,
        wg=wg,
        session=pid(f"session/{label}") if scope == "wg" else "",
        task=pid(f"task/{label}") if scope == "wg" else "",
        kind=kind,
        ts="2026-11-21T12:00:00.000Z",
        body=body,
    )


async def enqueue(env: Env, kind: str, body: dict[str, Any]) -> None:
    async with env.pool.connection() as conn, conn.transaction():
        await env.sync.enqueue(conn, kind, body)


def stored(env: Env) -> list[tuple[str, str]]:
    """`(item_kind, label)` of the hub's items in order: the scope ID of an event, the alert of an audit."""
    found = []
    for item in env.hub.items():
        body = item["body"]
        if item["item_kind"] == "event":
            found.append(("event", str(body["event"]["header"]["id"])))
        elif item["item_kind"] == "audit":
            alert = body["detail"].get("alert", body["action"])
            found.append(("audit", f"{alert}:{body['target_kind']}:{body['target_id']}"))
        else:
            found.append((item["item_kind"], ""))
    assert sorted(env.hub.stored) == list(range(1, len(env.hub.stored) + 1))
    return found


async def held(env: Env) -> list[tuple[str, str]]:
    rows = await env.rows("SELECT scope, scope_id::text FROM runner.outbox_held ORDER BY id")
    return [(str(a), str(b)) for a, b in rows]


def test_events_of_an_unassigned_workgroup_are_discarded_with_an_audit(runner_dsn: str) -> None:
    async def body() -> None:
        async with rig_for(runner_dsn).env(standard_world("a", "b")) as env:
            await reconcile(env)
            # the session of workgroup a writes its last output while the device is being unassigned
            await enqueue(env, "event", event(ORG_A, "wg", WG_A, "a1"))
            spec = env.world.orgs[ORG_A]
            spec.log.add("device_unassign", "alice", device=pid(DEVICE), wg=WG_A)
            spec.log.add("rotate", "alice", scope="wg", scope_id=WG_A, epoch=2)
            await reconcile(env)
            assert ("wg", WG_A) not in {
                (str(a), str(b)) for a, b in await env.rows("SELECT scope, scope_id::text FROM runner.scope_key")
            }
            await enqueue(env, "event", event(ORG_B, "wg", WG_B, "b1"))
            await enqueue(env, "metrics", METRICS)
            await env.sync.pusher.drain()
            # before KOR-08: nothing after a1 ever reached the hub (a1 waited for a key that was deleted for good)
            assert stored(env) == [("event", pid("event/b1")), ("metrics", "")]
            assert await held(env) == [("wg", WG_A)]
            await reconcile(env)
            assert await held(env) == []
            assert f"runner.sync outbox_discarded scope=wg scope_id={WG_A} items=1\n" in env.log()
            await env.sync.pusher.drain()
            assert stored(env)[-1] == ("audit", f"outbox_discarded:workgroup:{WG_A}")
            audit = env.hub.items()[-1]["body"]
            assert (audit["action"], audit["result"]) == ("runner.alert", "failed")
            assert audit["detail"] == {"alert": "outbox_discarded", "value": 1}
            async with env.pool.connection() as conn:
                assert await outbox.backlog(conn) == 0

    asyncio.run(body())


def test_events_of_an_org_that_removed_the_device_are_discarded(runner_dsn: str) -> None:
    async def body() -> None:
        async with rig_for(runner_dsn).env(standard_world("a", "b")) as env:
            await reconcile(env)
            await enqueue(env, "event", event(ORG_A, "wg", WG_A, "a1"))
            await enqueue(env, "event", event(ORG_A, "org", ORG_A, "a2"))
            await enqueue(env, "event", event(ORG_A, "wg", WG_A, "a3"))
            env.world.orgs[ORG_A].log.add("remove", "alice", subject=pid(DEVICE))
            await reconcile(env)
            await enqueue(env, "event", event(ORG_B, "wg", WG_B, "b1"))
            await env.sync.pusher.drain()
            assert stored(env) == [("event", pid("event/b1"))]
            assert sorted(await held(env)) == sorted([("wg", WG_A), ("org", ORG_A), ("wg", WG_A)])
            await reconcile(env)
            await env.sync.pusher.drain()
            assert await held(env) == []
            assert stored(env)[1:] == [
                ("audit", f"outbox_discarded:workgroup:{WG_A}"),
                ("audit", f"outbox_discarded:org:{ORG_A}"),
            ]
            assert [i["body"]["detail"]["value"] for i in env.hub.items()[1:]] == [2, 1]

    asyncio.run(body())


def test_queues_without_verified_evidence_are_kept(runner_dsn: str) -> None:
    """A stopped org (rollback or fork), a scope of no verified org and a scope the device may still read keep their
    queues: their items are neither sent nor discarded, and the other scopes go on."""

    async def body() -> None:
        async with rig_for(runner_dsn).env(standard_world("a", "b")) as env:
            await reconcile(env)
            stranger = pid("x/wg")
            await env.rows("DELETE FROM runner.scope_key WHERE scope_id IN (%s, %s) RETURNING 1", WG_A, WG_B)
            await enqueue(env, "event", event(ORG_A, "wg", WG_A, "a1"))
            await enqueue(env, "event", event(ORG_B, "wg", WG_B, "b1"))
            await enqueue(env, "event", event(pid("x/org"), "wg", stranger, "x1"))
            await enqueue(env, "metrics", METRICS)
            await env.sync.pusher.drain()
            assert stored(env) == [("metrics", "")]
            book_for(env.context).stopped[ORG_A] = "log_fork"
            env.world.orgs[ORG_A].log.add("remove", "alice", subject=pid(DEVICE))
            run = await reconcile(env)
            assert run.halted is None
            # org a is stopped, x is in no verified log, b may still be read (its wrap is merely missing locally)
            assert sorted(await held(env)) == sorted([("wg", WG_A), ("wg", WG_B), ("wg", stranger)])
            assert "outbox_discarded" not in env.log()
            book = book_for(env.context)
            book.stopped.clear()
            book.platform_stopped = "log_fork"
            assert await outbox_step.discardable(env.context, book) == []

    asyncio.run(body())


def test_a_stopped_org_keeps_sending_with_its_keys(runner_dsn: str) -> None:
    """A stopped org keeps its keys and log head (`assignments.teardown`): its items are sealed and pushed like any
    other; the hub, whose copy of the log is authoritative, decides on them."""

    async def body() -> None:
        async with rig_for(runner_dsn).env(standard_world("a", "b")) as env:
            await reconcile(env)
            book_for(env.context).stopped[ORG_A] = "log_rollback"
            await enqueue(env, "event", event(ORG_A, "wg", WG_A, "a1"))
            await enqueue(env, "event", event(ORG_B, "wg", WG_B, "b1"))
            await env.sync.pusher.drain()
            assert stored(env) == [("event", pid("event/a1")), ("event", pid("event/b1"))]

    asyncio.run(body())


def test_the_outbox_step_is_discovered_after_the_assignments() -> None:
    steps = {step.name: step.order for step in registry.reconcile_steps()}
    assert steps["assignments"] < steps["outbox"] < steps["sessions"]


def test_discarding_an_empty_queue_writes_no_audit(runner_dsn: str) -> None:
    async def body() -> None:
        async with rig_for(runner_dsn).env(standard_world("a")) as env:
            assert await outbox_step.discard(env.context, env.sync, "wg", WG_A) == 0
            assert await env.audits() == []
            assert "outbox_discarded" not in env.log()

    asyncio.run(body())
