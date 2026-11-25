"""U-RUN-subagents: one-shot end, final text, runtime limit and the `subagent.finished` ping of child sessions on a VM
whose guest is older than ADR-088 (05-runner.md section 13, ADR-038, ADR-020), driven by guest journal records through
the real handlers. The full children of a current guest: `test_turns.py`."""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

from agent_runner.crypto.ids import uuid7
from agent_runner.guestlink.errors import GuestUnreachable
from agent_runner.subagents import controller as controller_module
from agent_runner.subagents.controller import GRACE_S, SETTLE_MAX_S, SETTLE_S, Subagents, ping_id

from ..reconcile.rig import Rig
from .kit import SKit, older_guest, run
from .kit import setup as kit_setup


async def setup(env: Any, **options: Any) -> SKit:
    """The kit on a VM with a guest before ADR-088: its children keep the one-shot end."""
    found = await kit_setup(env, **options)
    await older_guest(found)
    return found


async def working_child(found: SKit, parent: str | None = None) -> tuple[str, str]:
    parent = parent or await found.parent()
    child = uuid7()
    assert (await found.subagent(parent, child=child)).state == "succeeded"
    await found.state(child, "prompt_delivering")
    await found.state(child, "working")
    return parent, child


async def first_turn_ends(found: SKit, child: str, event: str = "Stop") -> None:
    await found.hook(child, event)
    await found.state(child, "idle")


async def settle(found: SKit) -> None:
    """Grace, end, settle and ping: the ticks of the controller's loop with the clock moved in between."""
    await found.tick()
    found.env.clock.now += GRACE_S
    await found.tick()
    await found.tick()
    found.env.clock.now += SETTLE_S
    await found.tick()


def controls(found: SKit) -> list[tuple[str, str]]:
    return [value for name, value in found.guest.calls if name == "control"]


def test_the_first_stop_ends_the_child_with_its_last_assistant_message(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent, child = await working_child(found)
        await found.output(child, "Looking at the change.", "msg_a")
        await found.output(child, "The change looks good.", "msg_b")
        await first_turn_ends(found, child)
        assert child in found.controller.idle_since
        # within the grace time nothing happens, then stopping, the guest's stop and stopped
        await found.tick()
        assert controls(found) == []
        found.env.clock.now += GRACE_S
        await found.tick()
        assert controls(found) == [(child, "stop")]
        assert (await found.stored(child)).state == "stopped"
        assert [state for state, _ in await found.ops.kit.transitions(child)][-2:] == ["stopping", "stopped"]
        # a last transcript record after the end still counts
        await found.output(child, "Second block.", "msg_b-2")
        early = await found.read(parent, {"session_id": child})
        assert early == {"status": "ok", "result": {"session_id": child, "state": "stopping"}}
        assert found.pinged == []
        found.env.clock.now += SETTLE_S
        await found.tick()
        assert [(p.parent_session_id, p.child_session_id, p.ping_id) for p in found.pinged] == [
            (parent, child, ping_id(child))
        ]
        answer = await found.read(parent, {"session_id": child})
        result = answer["result"]
        assert result["state"] == "stopped"
        assert result["final_text"] == "The change looks good.\n\nSecond block."
        assert result["finished_at"].endswith("Z")
        assert (await found.read(parent, {"session_id": child}))["result"] == result  # the cached text
        # the ping is sent once
        await found.tick()
        assert len(found.pinged) == 1
        assert any(f"completed child_session_id={child} stopped=1" in line for line in found.lines)

    run(rig, body)


def test_a_stop_failure_ends_the_child_failed_without_text(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent, child = await working_child(found)
        await found.output(child, "Partial answer", "msg_a")
        await first_turn_ends(found, child, "StopFailure")
        await settle(found)
        stored = await found.stored(child)
        assert stored.state == "failed"
        assert ("failed", "agent_exited") in await found.ops.kit.transitions(child)
        assert controls(found) == [(child, "stop")]
        assert [p.child_session_id for p in found.pinged] == [child]
        answer = await found.read(parent, {"session_id": child})
        assert answer["result"] == {
            "session_id": child,
            "state": "failed",
            "finished_at": answer["result"]["finished_at"],
        }

    run(rig, body)


def test_a_stop_failure_after_the_child_ended_changes_nothing(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        _parent, child = await working_child(found)
        await first_turn_ends(found, child, "StopFailure")
        await found.tick()
        found.env.clock.now += GRACE_S
        # the guest reports the end first: the controller's `failed` is no transition and nothing is stopped
        child_row = await found.stored(child)
        await found.state(child, "failed", reason="agent_exited")
        await found.controller._finish(child_row)
        assert controls(found) == []

    run(rig, body)


def test_a_child_without_output_has_an_empty_final_text(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent, child = await working_child(found)
        await first_turn_ends(found, child)
        await settle(found)
        answer = await found.read(parent, {"session_id": child})
        assert answer["result"]["final_text"] == ""

    run(rig, body)


def test_the_final_text_is_cut_at_64_kib(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent, child = await working_child(found)
        await found.output(child, "ä" * 30000, "msg_a")
        await found.output(child, "b" * 10000, "msg_a-2")
        await first_turn_ends(found, child)
        await settle(found)
        text = (await found.read(parent, {"session_id": child}))["result"]["final_text"]
        assert len(text.encode()) <= 64 * 1024
        assert text.startswith("ä" * 30000 + "\n\n" + "b")
        assert len(text.encode()) >= 64 * 1024 - 1

    run(rig, body)


def test_the_runtime_limit_fails_the_child(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        _parent, child = await working_child(found)
        # without a runtime setting (older hub, or 0 = no limit) a child runs on
        found.advance(365 * 86400)
        await found.tick()
        settings = {"subagent_max_depth": 5, "subagent_max_running": 50, "subagent_max_total": 0}
        found.service.env.desired = {"limits": {"api": 1, **settings, "subagent_max_runtime_seconds": 0}}
        await found.tick()
        assert (await found.stored(child)).state == "working"
        found.now[0] -= 365 * 86400
        found.service.env.desired = {"limits": {"api": 1, **settings, "subagent_max_runtime_seconds": 1800}}
        found.advance(30 * 60 - 1)
        await found.tick()
        assert (await found.stored(child)).state == "working"
        found.advance(1)
        await found.tick()
        assert (await found.stored(child)).state == "failed"
        assert ("failed", "runtime_exceeded") in await found.ops.kit.transitions(child)
        assert controls(found) == [(child, "stop")]
        assert any(f"runtime_exceeded child_session_id={child}" in line for line in found.lines)
        # already failed: a later pass does not stop it again
        await found.tick()
        assert controls(found) == [(child, "stop")]

    run(rig, body)


def test_the_runtime_limit_of_a_child_that_ended_meanwhile_changes_nothing(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        _parent, child = await working_child(found)
        row = await found.stored(child)
        await found.state(child, "failed", reason="oom")
        await found.controller._runtime_exceeded(row)
        assert controls(found) == []

    run(rig, body)


def test_a_restarted_daemon_pings_recently_ended_children_again_with_the_same_ping_id(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent, child = await working_child(found)
        await found.state(child, "failed", reason="oom")
        await found.tick()
        await found.tick()
        assert [p.ping_id for p in found.pinged] == [ping_id(child)]
        # a new controller (daemon restart) pings again; the guest recognises the stable ping ID
        restarted = Subagents(
            found.ops.kit.session_env, found.service, notify=found.controller.notify, wall=found.controller.wall
        )
        await restarted.tick()
        assert [(p.parent_session_id, p.ping_id) for p in found.pinged] == [(parent, ping_id(child))] * 2

    run(rig, body)


def test_without_a_notifier_the_end_is_only_logged(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env, notify=False)
        parent, child = await working_child(found)
        await found.tick()
        await found.state(child, "failed", reason="oom")
        await found.tick()
        assert any(
            f"finished child_session_id={child} parent_session_id={parent} state=failed" in line for line in found.lines
        )

    run(rig, body)


def test_no_ping_for_an_ended_parent_and_a_failing_notifier_is_logged(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent, child = await working_child(found)
        other_parent, other = await working_child(found, await found.parent(2))
        await found.tick()
        await found.state(parent, "failed", reason="oom")
        await found.state(child, "failed", reason="oom")
        await found.tick()
        assert found.pinged == []

        async def broken(_context: Any, _ping: Any) -> None:
            raise RuntimeError("down")

        found.controller.notify = broken
        await found.state(other, "failed", reason="oom")
        await found.tick()
        assert any(f"notify_failed child_session_id={other} error=RuntimeError" in line for line in found.lines)
        assert other_parent != parent

    run(rig, body)


def test_a_child_without_parent_is_only_logged(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        _parent, child = await working_child(found)
        await found.tick()
        async with found.env.pool.connection() as conn:
            await conn.execute("UPDATE runner.session SET parent_id = NULL WHERE id = %s", (child,))
        await found.state(child, "failed", reason="oom")
        await found.tick()
        assert found.pinged == []

    run(rig, body)


def test_the_loop_ticks_logs_failures_and_cancels_its_work_at_the_end(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        controller = found.controller
        calls: list[int] = []
        original = controller.tick

        async def tick() -> None:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("store down")
            await original()
            if len(calls) == 2:
                controller.wake()
            if len(calls) == 3:
                env.context.stopping.set()

        controller.tick = tick  # type: ignore[method-assign]
        blocker = asyncio.Event()

        async def hold() -> None:
            await blocker.wait()

        controller._spawn_end("x", hold())
        await controller.loop(env.context.stopping)
        assert len(calls) == 3
        assert "runner.subagents tick_failed error=RuntimeError" in found.lines
        assert controller.ending == {}

    run(rig, body)


def test_a_failing_end_is_logged(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)

        async def broken() -> None:
            raise RuntimeError("x")

        found.controller._spawn_end("c1", broken())
        await found.settle_ends()
        await asyncio.sleep(0)
        assert "runner.subagents end_failed child_session_id=c1 error=RuntimeError" in found.lines

    run(rig, body)


def test_the_observer_wakes_the_loop_for_children_only(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent, child = await working_child(found)
        found.controller.trigger.clear()
        await found.state(parent, "idle")
        assert not found.controller.trigger.is_set()
        assert parent not in found.controller.idle_since
        await found.state(child, "idle")
        assert found.controller.trigger.is_set()
        assert child in found.controller.idle_since
        # without a controller the observer does nothing
        delattr(env.context, controller_module.ATTRIBUTE)
        await found.state(child, "working")

    run(rig, body)


def test_an_interrupted_turn_without_stop_hook_ends_the_child_stopped(rig: Rig, monkeypatch: Any) -> None:
    async def body(env: Any) -> None:
        from agent_runner.subagents import results  # noqa: PLC0415

        monkeypatch.setattr(results, "BATCH", 2)
        found = await setup(env)
        parent, child = await working_child(found)
        await found.output(child, "First.", "msg_a")
        await found.output(child, "Last.", "msg_b")
        for _ in range(5):
            await found.emit(
                child, "session.terminal", {"offset_ms_start": 0, "frames": [[1, "o", "x"]], "cols": 160, "rows": 48}
            )
        await found.state(child, "idle")
        await settle(found)
        assert (await found.stored(child)).state == "stopped"
        assert (await found.read(parent, {"session_id": child}))["result"]["final_text"] == "Last."

    run(rig, body)


def test_an_orphan_recorded_twice_is_written_once(rig: Rig) -> None:
    async def body(env: Any) -> None:
        from ..sessions.kit import desired  # noqa: PLC0415

        found = await setup(env)
        parent = await found.parent()
        item = desired(8, parent_session_id=parent, depth=1)
        await found.controller._orphan(item)
        await found.controller._orphan(item)
        assert f"runner.subagents orphan child_session_id={item.session_id} recorded=0" in found.lines
        assert [s for s, _ in await found.ops.kit.transitions(item.session_id)] == ["failed"]

    run(rig, body)


def test_the_final_text_waits_for_a_lagging_journal_reader(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        parent, child = await working_child(found)
        await first_turn_ends(found, child)
        await settle(found)
        # the guest's journal is ahead of what the runner handled: not final yet
        found.controller.final_seen.pop(child, None)
        found.controller.texts.pop(child, None)
        found.controller.heads.pop(child, None)
        found.guest.emit(child, "guest.error", {"code": "x"})
        found.guest.records[-1] = dataclasses.replace(found.guest.records[-1], seq=5000)
        assert (await found.read(parent, {"session_id": child}))["result"]["state"] == "stopping"
        found.env.clock.now += SETTLE_S
        assert (await found.read(parent, {"session_id": child}))["result"]["state"] == "stopping"
        assert found.controller.heads[child] == 5000
        # the reader caught up with the head
        await found.output(child, "Late.", "msg_late")
        row = await found.ops.vm()
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.guest_cursor SET journal_seq = 5000 WHERE vm_id = %s", (row.id,))
        answer = await found.read(parent, {"session_id": child})
        assert (answer["result"]["state"], answer["result"]["final_text"]) == ("stopped", "Late.")

    run(rig, body)


def test_settling_gives_up_on_the_journal_after_the_maximum_and_without_a_vm(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        _parent, child = await working_child(found)
        await first_turn_ends(found, child)
        await settle(found)
        row = await found.stored(child)
        controller = found.controller
        controller.final_seen[child] = found.env.clock() - SETTLE_S
        controller.heads[child] = 10**9
        assert not await controller.settled(row)
        controller.final_seen[child] = found.env.clock() - SETTLE_MAX_S
        assert await controller.settled(row)
        controller.final_seen[child] = found.env.clock() - SETTLE_S
        assert await controller.settled(dataclasses.replace(row, vm_id=None))
        # the head of a VM that does not run or cannot be reached is 0
        assert await controller._journal_head("vm-unknown") == 0
        found.guest.fail["journal"] = GuestUnreachable("x")
        assert await controller._journal_head(row.vm_id or "") == 0

    run(rig, body)


def test_idle_times_are_reset_stopping_children_wait_and_old_children_are_forgotten(rig: Rig) -> None:
    async def body(env: Any) -> None:
        found = await setup(env)
        _parent, child = await working_child(found)
        controller = found.controller
        controller.idle_since[child] = 0.0
        await found.tick()
        assert child not in controller.idle_since
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.session SET state = 'stopping' WHERE id = %s", (child,))
        found.advance(3600)
        await found.tick()
        assert (await found.stored(child)).state == "stopping"
        controller.texts["gone"] = "x"
        controller.notified.add("gone")
        await found.tick()
        assert "gone" not in controller.texts
        assert "gone" not in controller.notified

    run(rig, body)
