"""U-RUN-subagents: reconcile of child sessions against the hub's desired state (05-runner.md sections 2.2 and 13):
stops, unassigned workgroups, interrupted starts and children the hub created that this device never started."""

from __future__ import annotations

from typing import Any

from agent_runner.crypto.ids import uuid7
from agent_runner.sessions import records
from agent_runner.sessions.desired import Desired
from agent_runner.subagents.controller import SETTLE_S

from ..reconcile.rig import Rig
from ..sessions.kit import desired as main_desired
from .kit import ORG, WG, Book, SKit, run, setup
from .test_completion import controls, working_child


def child_desired(found: SKit, parent: str, child: str, state: str = "working", **changes: Any) -> Desired:
    return main_desired(1, session_id=child, parent_session_id=parent, depth=1, state=state, **changes)


async def parents(found: SKit, parent: str) -> list[Desired]:
    return [main_desired(1, session_id=parent, state="working")]


def test_children_the_hub_stops_or_no_longer_lists_are_stopped(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent, first = await working_child(found)
        _p, second = await working_child(found, parent)
        wanted = [*await parents(found, parent), child_desired(found, parent, first, "stopping")]
        # a child started within the last minute may be missing from an older desired state: kept
        await found.controller.reconcile([*await parents(found, parent)], Book())
        await found.service.idle()
        assert controls(found) == []
        found.advance(60)
        await found.controller.reconcile(wanted, Book())
        await found.service.idle()
        assert (await found.stored(first)).state == "stopped"
        assert (await found.stored(second)).state == "stopped"
        assert sorted(controls(found)) == sorted([(first, "stop"), (second, "stop")])

    run(rig, body)


def test_children_of_an_unassigned_workgroup_are_stopped_and_others_left_alone(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent, child = await working_child(found)
        wanted = [*await parents(found, parent), child_desired(found, parent, child)]
        await found.controller.reconcile(wanted, Book())
        await found.service.idle()
        assert controls(found) == []
        await found.controller.reconcile(wanted, Book(assigned=[]))
        await found.service.idle()
        assert ("stopping", "unassigned") in await found.ops.kit.transitions(child)
        assert (await found.stored(child)).state == "stopped"

    run(rig, body)


def test_a_child_left_in_a_start_state_fails_and_its_guest_session_is_stopped(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent, child = await working_child(found)
        parent_row = await found.stored(parent)
        interrupted, bare = uuid7(), uuid7()
        async with env.pool.connection() as conn, conn.transaction():
            for sid in (interrupted, bare):
                await records.insert(
                    conn,
                    session_id=sid,
                    task_id=parent_row.task_id,
                    org_id=ORG,
                    wg_id=WG,
                    parent_id=parent,
                    depth=1,
                    owner_id=parent_row.owner_id or "",
                    created_at=parent_row.created_at,
                )
            await records.update(conn, interrupted, state="sandbox_ready", vm_id=parent_row.vm_id)
        wanted = [
            *await parents(found, parent),
            child_desired(found, parent, child),
            child_desired(found, parent, interrupted, "sandbox_ready"),
            child_desired(found, parent, bare, "requested"),
        ]
        await found.controller.reconcile(wanted, Book())
        await found.settle_ends()
        assert ("failed", "device_shutdown") in await found.ops.kit.transitions(interrupted)
        assert ("failed", "device_shutdown") in await found.ops.kit.transitions(bare)
        assert controls(found) == [(interrupted, "stop")]
        assert (await found.stored(child)).state == "working"

    run(rig, body)


def test_a_child_the_hub_created_but_this_device_never_started_fails(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent = await found.parent()
        orphan = uuid7()
        wanted = [*await parents(found, parent), child_desired(found, parent, orphan, "requested")]
        await found.controller.reconcile(wanted, Book())
        stored = await found.stored(orphan)
        assert (stored.state, stored.parent_id, stored.depth) == ("failed", parent, 1)
        assert ("failed", "agent_start_failed") in await found.ops.kit.transitions(orphan)
        assert f"runner.subagents orphan child_session_id={orphan} recorded=1" in found.lines

    run(rig, body)


def test_a_requested_child_waits_while_its_request_is_worked_on_or_its_start_runs(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent = await found.parent()
        pending, starting, foreign = uuid7(), uuid7(), uuid7()
        await found.ops.request(parent, "subagent.request", {"purpose": "p", "prompt": "q"})  # received
        wanted = [*await parents(found, parent), child_desired(found, parent, pending, "requested")]
        await found.controller.reconcile(wanted, Book())
        await found.controller.reconcile([child_desired(found, parent, foreign, "requested")], Book(assigned=[]))
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.operation SET state = 'failed'")
        found.controller.specs[starting] = None  # type: ignore[assignment]
        await found.controller.reconcile([child_desired(found, parent, starting, "requested")], Book())
        assert await env.rows("SELECT id FROM runner.session WHERE depth > 0") == []

    run(rig, body)


def test_children_with_a_running_start_or_end_are_left_alone(rig: Rig) -> None:
    async def body(env: Any) -> None:
        import asyncio  # noqa: PLC0415

        found = await setup(env)
        parent, child = await working_child(found)
        blocker = asyncio.Event()

        async def hold() -> None:
            await blocker.wait()

        found.controller._spawn_end(child, hold())
        await found.controller.reconcile(await parents(found, parent), Book())
        await found.controller.tick()
        assert controls(found) == []
        blocker.set()
        await found.settle_ends()

    run(rig, body)


def test_children_the_hub_still_runs_but_a_wiped_store_does_not_know_are_ended(rig: Rig) -> None:
    """NA-06 (AUD-02, DR-01 residual risk 1): after a wipe of the runner store the hub still lists child sessions as
    running on this device; like main sessions (`SessionService.store_lost`) they are recorded and reported `failed`
    (`device_shutdown`) - `stopped` when the hub wants them stopped - and the parent gets its finished ping, so it no
    longer waits for a result that never comes. A child whose start runs right now is left to it."""

    async def body(env: Any) -> None:
        found = await setup(env)
        parent = await found.parent()
        lost, stopping, starting = uuid7(), uuid7(), uuid7()
        found.controller.specs[starting] = None  # type: ignore[assignment]
        wanted = [
            *await parents(found, parent),
            child_desired(found, parent, lost, "working"),
            child_desired(found, parent, stopping, "stopping"),
            child_desired(found, parent, starting, "agent_starting"),
        ]
        await found.controller.reconcile(wanted, Book())
        stored = await found.stored(lost)
        assert (stored.state, stored.parent_id, stored.depth) == ("failed", parent, 1)
        assert ("failed", "device_shutdown") in await found.ops.kit.transitions(lost)
        assert (await found.stored(stopping)).state == "stopped"
        assert await env.rows("SELECT id FROM runner.session WHERE id = %s", starting) == []
        await found.controller.tick()
        assert [p.child_session_id for p in found.pinged] == [lost]  # a failed child has no final text to wait for
        found.env.clock.now += SETTLE_S  # a stopped one is settled after the usual wait
        await found.controller.tick()
        assert sorted(p.child_session_id for p in found.pinged) == sorted([lost, stopping])
        assert {p.parent_session_id for p in found.pinged} == {parent}
        # a second pass changes nothing
        await found.controller.reconcile(wanted, Book())
        assert [t for t in await found.ops.kit.transitions(lost) if t[0] == "failed"] == [("failed", "device_shutdown")]

    run(rig, body)


def test_the_desired_states_without_a_local_child_are_those_of_main_sessions() -> None:
    from agent_runner.sessions import service  # noqa: PLC0415
    from agent_runner.subagents import controller  # noqa: PLC0415

    assert controller.NO_LOCAL == service.NO_LOCAL


def test_a_full_wipe_ends_parent_and_child_and_pings_nobody(rig: Rig) -> None:
    """The real wipe: parent and child are both unknown. The service ends the parent (`device_shutdown`; the next
    follow-up gets a new main session), the controller the child; no ping goes to an ended parent - nobody waits."""

    async def body(env: Any) -> None:
        found = await setup(env)
        parent, child = uuid7(), uuid7()
        wanted = [main_desired(1, session_id=parent, state="working"), child_desired(found, parent, child, "working")]
        await found.service.reconcile(wanted, Book())
        await found.controller.reconcile(wanted, Book())
        assert (await found.stored(parent)).state == "failed"
        assert ("failed", "device_shutdown") in await found.ops.kit.transitions(child)
        await found.controller.tick()
        assert found.pinged == []
        assert f"runner.subagents finished child_session_id={child} parent_session_id={parent} state=failed" in (
            found.lines
        )

    run(rig, body)


def test_a_child_recorded_meanwhile_or_with_a_pending_request_is_not_ended(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent, child = await working_child(found)
        # the store was read before the child's start recorded it: `store_lost` finds the record and leaves it
        lost = child_desired(found, parent, child, "stopping")
        await found.service.store_lost(lost)
        assert (await found.stored(child)).state == "working"
        # a child of a task whose accepted `subagent.request` still runs is the executor's
        pending = uuid7()
        await found.ops.request(parent, "subagent.request", {"purpose": "p", "prompt": "q"})
        wanted = [*await parents(found, parent), child_desired(found, parent, pending, "stopping")]
        await found.controller.reconcile(wanted, Book())
        assert await env.rows("SELECT id FROM runner.session WHERE id = %s", pending) == []

    run(rig, body)
