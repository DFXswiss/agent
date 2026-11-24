"""U-RUN-contexts: a follow-up restores the final checkpoint of the session it continues (review KOR-33 no. 1), a
refused upload's local copy is used (no. 6), a damaged cache falls back to the hub copy (no. 18), the restored audit
names workspace and staleness (no. 16) and a resume on another VM restores again after a failed create (no. 4)."""

from __future__ import annotations

from typing import Any

from agent_runner.contexts import store
from agent_runner.contexts.checkpoint import Checkpointer
from agent_runner.contexts.restore import HUB_STEP_S, LOCAL_STEP_S, RESTORED, STALE_TEXT
from agent_runner.guestlink.errors import GuestUnreachable
from agent_runner.prompts.resume import Resumer
from agent_runner.sync.http import HubRefusal

from ..prompts.kit import CLAUDE_ID, run
from ..reconcile.rig import Rig
from ..sessions.kit import desired, session_id, wire
from .kit import CKit, archive
from .test_checkpoint import due_now, objects, observe
from .test_restore import TS, checkpointed, fallback_reasons, forge, start


def follow_up(ck: CKit, n: int, source: str, ref: str | None = None, ref_session: str | None = None) -> None:
    """The desired entry of session `n`, continuing `source`, with the hub's reference `ref` (if any)."""
    item: dict[str, Any] = {**wire(desired(n)), "resume_from": source}
    if ref is not None:
        item["context"] = {"object_id": ref, "session_id": ref_session or source, "created_at": TS, "size": 1}
    ck.pk.kit.session_env.desired = {"sessions": [item]}


async def stopped_with_final_checkpoint_due(env: Any) -> tuple[CKit, str, str]:
    """Session 1 has a committed checkpoint, was stopped, and its final checkpoint is due but not taken yet."""
    ck, older = await checkpointed(env)
    sid = session_id(1)
    await ck.pk.set_state(sid, "stopped")
    ck.guest.archives[sid] = archive(work={"notes.txt": b"the final state"})
    await due_now(ck, sid)
    return ck, sid, older


def test_a_follow_up_right_after_a_stop_waits_for_the_final_checkpoint(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck, sid, older = await stopped_with_final_checkpoint_due(env)
        taken: list[str] = []

        async def sleep(seconds: float) -> None:
            await env.clock.sleep(seconds)
            if not taken:
                [due] = [item for item in await store_due(ck) if item.session_id == sid]
                taken.append(await Checkpointer(ck.contexts).take(due))  # sealed locally is enough

        ck.contexts.sleep = sleep
        follow_up(ck, 2, sid, older)  # the hub still names the older checkpoint
        request = await start(ck)
        assert taken == ["taken"]
        final = next(obj for obj in await objects(ck) if obj.object_id != older)
        assert final.state == "sealed"
        assert request["restore"]["object_id"] == final.object_id
        assert env.clock.sleeps.count(LOCAL_STEP_S) == 1
        assert STALE_TEXT not in request["restore"]["note"]
        await observe(ck, session_id(2), state="agent_starting", at=TS)
        [audit] = await ck.audits(RESTORED)
        assert audit["detail"] == {"object_id": final.object_id, "workspace": "full", "stale": False}

    run(rig, body)


def test_the_wait_ends_after_its_limit_and_says_the_checkpoint_may_be_stale(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck, sid, older = await stopped_with_final_checkpoint_due(env)
        ck.contexts.wait_s = 10.0
        follow_up(ck, 2, sid, older)
        request = await start(ck)
        assert request["restore"]["object_id"] == older
        assert request["restore"]["note"].endswith(STALE_TEXT)
        assert sum(env.clock.sleeps) >= 10.0
        assert any("context_wait_timeout" in line for line in ck.lines)
        await observe(ck, session_id(2), state="agent_starting", at=TS)
        [audit] = await ck.audits(RESTORED)
        assert audit["detail"]["stale"] is True

    run(rig, body)


def test_no_wait_when_the_source_cannot_checkpoint_or_has_settled(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck, sid, older = await stopped_with_final_checkpoint_due(env)
        # still working, but without a VM: nothing can be taken, so nothing is waited for
        await ck.pk.set_state(sid, "working")
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.session SET vm_id = NULL WHERE id = %s", (sid,))
        follow_up(ck, 2, sid, older)
        sleeps = len(env.clock.sleeps)
        assert (await start(ck))["restore"]["object_id"] == older
        assert len(env.clock.sleeps) == sleeps
        # a due checkpoint of a source whose VM is gone: no wait either
        await ck.pk.set_state(sid, "stopped")
        follow_up(ck, 3, sid, older)
        assert (await start(ck, 3))["restore"]["object_id"] == older
        assert len(env.clock.sleeps) == sleeps

    run(rig, body)


def test_a_follow_up_of_a_session_of_another_device_reads_the_hub_until_it_names_that_session(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck = await ckit_with_forge(env)
        earlier = forge(ck)  # of session 1
        source = session_id(30)  # ran on another device
        final = forge(ck, data=archive(session_id=source), session=source)
        follow_up(ck, 2, source, earlier, ref_session=session_id(1))
        entry = {**wire(desired(2)), "resume_from": source}
        named = {**entry, "context": {"object_id": final, "session_id": source, "created_at": TS, "size": 1}}
        ck.hub.answers["/v1/device/state"] = [
            {"sessions": [{**wire(desired(5)), "resume_from": source}]},
            {"sessions": [{**entry, "context": {"object_id": "x"}}]},
            {"sessions": "x"},
            {"sessions": [named]},
        ]
        ck.hub.errors = []
        request = await start(ck)
        assert request["restore"]["object_id"] == final
        assert STALE_TEXT not in request["restore"]["note"]
        assert "on a VM of another device" not in request["restore"]["note"]  # same author device here
        assert env.clock.sleeps.count(HUB_STEP_S) == 4
        assert any("context_wait_read_failed" in line for line in ck.lines)

    run(rig, body)


def test_a_hub_read_failure_while_waiting_is_logged_and_the_wait_goes_on(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck = await ckit_with_forge(env)
        source = session_id(31)
        final = forge(ck, data=archive(session_id=source), session=source)
        follow_up(ck, 2, source)
        named = {**wire(desired(2)), "context": {"object_id": final, "session_id": source, "created_at": TS, "size": 1}}
        ck.hub.answers["/v1/device/state"] = [{"sessions": [named]}]
        ck.hub.errors = [HubRefusal(503, "unavailable")]
        assert (await start(ck))["restore"]["object_id"] == final
        assert sum("context_wait_read_failed" in line and "HubRefusal" in line for line in ck.lines) == 1

    run(rig, body)


async def ckit_with_forge(env: Any) -> CKit:
    from .kit import ckit  # noqa: PLC0415

    return await ckit(env)


async def store_due(ck: CKit) -> list[store.DueSession]:
    async with ck.pk.env.pool.connection() as conn:
        return await store.due(conn, ck.contexts.now())


def test_the_newest_local_copy_is_restored_after_the_hub_refused_it(rig: Rig) -> None:
    """Review no. 6: a quota refusal must not make a restore on this device fall back to the hub's older object."""

    async def body(env: Any) -> None:
        ck, older = await checkpointed(env)
        sid = session_id(1)
        ck.store.refuse["put"] = (413, "quota_exceeded")
        ck.guest.archives[sid] = archive(work={"notes.txt": b"newer"})
        assert await Checkpointer(ck.contexts).take(await due_now(ck, sid)) == "taken"
        assert await Checkpointer(ck.contexts).upload_round() == 1
        [refused] = await objects(ck)
        assert refused.state == "refused"
        follow_up(ck, 2, sid, older)
        assert (await start(ck))["restore"]["object_id"] == refused.object_id
        assert await fallback_reasons(ck) == []

    run(rig, body)


def test_a_damaged_cached_copy_is_read_again_from_the_hub(rig: Rig) -> None:
    """Review no. 18."""

    async def body(env: Any) -> None:
        ck, object_id = await checkpointed(env)
        good = ck.contexts.cache.read_chunk(object_id, 0)
        assert good is not None
        damaged = bytearray(good)
        damaged[20] ^= 1
        ck.contexts.cache.write_chunk(object_id, 0, bytes(damaged))
        ck.pk.kit.session_env.desired = {"sessions": [wire(desired(2))]}
        assert (await start(ck))["restore"]["object_id"] == object_id
        assert any("cache_unusable" in line for line in ck.lines)
        assert ck.contexts.cache.read_chunk(object_id, 0) == good  # repaired from the hub
        # damaged here and not readable at the hub either: the emergency path, the cached row stays
        ck.contexts.cache.write_chunk(object_id, 0, bytes(damaged))
        ck.store.refuse["chunk"] = (404, "not_found")
        ck.pk.kit.session_env.desired = {"sessions": [wire(desired(4))]}
        assert "restore" not in await start(ck, 4)
        assert await fallback_reasons(ck) == ["context_unavailable"]
        assert object_id in [obj.object_id for obj in await objects(ck)]
        del ck.store.refuse["chunk"]
        ck.contexts.cache.write_chunk(object_id, 0, good)
        # a copy only this device holds cannot be read again: the emergency path
        sid = session_id(1)
        ck.store.refuse["put"] = (503, "unavailable")
        ck.guest.archives[sid] = archive(work={"notes.txt": b"local only"})
        assert await Checkpointer(ck.contexts).take(await due_now(ck, sid)) == "taken"
        sealed = next(obj for obj in await objects(ck) if obj.state == "sealed")
        raw = bytearray(ck.contexts.cache.read_chunk(sealed.object_id, 0) or b"")
        raw[20] ^= 1
        ck.contexts.cache.write_chunk(sealed.object_id, 0, bytes(raw))
        ck.pk.kit.session_env.desired = {"sessions": [wire(desired(3))]}
        assert "restore" not in await start(ck, 3)
        assert await fallback_reasons(ck) == ["context_unavailable", "context_invalid"]

    run(rig, body)


def test_a_resume_on_another_vm_restores_again_after_a_failed_create(rig: Rig) -> None:
    """Review no. 4: the session keeps its old VM until the guest took the restore."""

    async def body(env: Any) -> None:
        ck, object_id = await checkpointed(env)
        sid = session_id(1)
        await ck.pk.set_state(sid, "suspended")
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.session SET vm_id = 'vm-0000ffff' WHERE id = %s", (sid,))
        ck.guest.fail["create_session"] = GuestUnreachable("sessions")
        resumer = Resumer(ck.pk.kit.session_env)
        assert await resumer.resume(await ck.pk.session(sid)) == "retry"
        assert (await ck.pk.session(sid)).vm_id == "vm-0000ffff"
        del ck.guest.fail["create_session"]
        assert await resumer.resume(await ck.pk.session(sid)) == "resumed"
        assert (await ck.pk.session(sid)).vm_id == (await ck.pk.vm()).id
        creates = [value for name, value in ck.guest.calls if name == "create_session"]
        assert [{**request["restore"], "note": ""} for request in creates] == [
            {"claude_session_id": CLAUDE_ID, "object_id": object_id, "note": ""}
        ] * 2
        assert all("resume_claude_session_id" not in request for request in creates)
        assert "on another VM of this device" in creates[-1]["restore"]["note"]
        assert [name for name, _ in ck.guest.calls].count("stage_context") == 2

    run(rig, body)
