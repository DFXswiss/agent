"""U-RUN-contexts: whether the newest checkpoint of a paused session is complete, so its VM may move to a newer golden
(`store.checkpoint_holds`, `vm_retire.checkpoints`; ADR-093 section 1, KOR-70)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast

from psycopg.rows import dict_row

from agent_runner.contexts import archive as archive_format
from agent_runner.contexts import store, vm_retire
from agent_runner.contexts.checkpoint import Checkpointer
from agent_runner.contexts.restore import FALLBACK, RESTORED, STALE_TEXT, Restored, restored_line
from agent_runner.guestlink.errors import GuestRefused
from agent_runner.prompts.resume import RESTART_LINE, Resumer

from ..prompts.kit import run
from ..reconcile.rig import Rig
from ..sessions.kit import TASK, WG, session_id
from .kit import accept, archive, ckit, context_json
from .test_checkpoint import observe
from .test_restore import TS, checkpointed


def test_the_newest_checkpoint_says_whether_a_paused_session_may_move(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck = await ckit(env)
        sid = await ck.pk.add_session(state="suspended")
        ck.guest.archives[sid] = archive()
        row = await ck.pk.vm()
        worker = Checkpointer(ck.contexts)
        other = session_id(5)

        async def holds() -> Any:
            return await vm_retire.checkpoints(env.context, row, [sid])

        async def due() -> store.DueSession:
            async with env.pool.connection() as conn:
                await store.mark_due(
                    conn, session_id=sid, task_id=TASK, org_id=str(ck.pk.kit.guest.org_id), wg_id=WG,
                    at=ck.contexts.now(),
                )  # fmt: skip
                [found] = [item for item in await store.due(conn, ck.contexts.now()) if item.session_id == sid]
            return found

        async def state() -> str | None:
            async with env.pool.connection() as conn:
                cur = await conn.execute(
                    "SELECT checkpoint_state FROM runner.context_session WHERE session_id = %s", (sid,)
                )
                found = await cur.fetchone()
            return None if found is None else found[0]

        await accept(env, sid)  # it worked (HOT-24); `other` never did: nothing to save
        # never checkpointed here
        assert await vm_retire.checkpoints(env.context, row, [sid, other]) == {sid: "checkpoint_missing", other: None}
        pending = await due()
        assert await holds() == {sid: "checkpoint_due"}
        assert await worker.take(pending) == "taken"
        assert (await state(), await holds()) == ("full", {sid: None})
        assert await worker.take(await due()) == "identical"  # nothing changed: still complete
        assert (await state(), await holds()) == ("full", {sid: None})
        # a checkpoint that left part of the workspace out
        ck.guest.archives[sid] = archive(workspace_omitted=2, work={"notes.txt": b"changed"})
        assert await worker.take(await due()) == "taken"
        assert (await state(), await holds()) == ("partial", {sid: "workspace_incomplete"})
        # a request that can never be taken on this VM (HOT-24; `dropped` ones: the test above)
        ck.guest.fail["export_context"] = GuestRefused("context_export", 409, "size_exceeded")
        assert await worker.take(await due()) == "dropped"
        assert (await state(), await holds()) == ("impossible", {sid: "checkpoint_impossible"})
        del ck.guest.fail["export_context"]
        ck.guest.archives[sid] = archive(work={"notes.txt": b"complete again"})
        await accept(env, sid)  # it worked again: due again
        assert await worker.take(await due()) == "taken"
        assert await holds() == {sid: None}
        async with env.pool.connection() as conn:
            # a workspace too large to be saved
            await conn.execute(
                "UPDATE runner.context_session SET checkpoint_state = 'omitted' WHERE session_id = %s", (sid,)
            )
            assert await store.checkpoint_holds(conn, [sid]) == {sid: "workspace_incomplete"}
            # a checkpoint of before migration 0034_golden_switch: its completeness is unknown
            await conn.execute(
                "UPDATE runner.context_session SET checkpoint_state = NULL WHERE session_id = %s", (sid,)
            )
            assert await store.checkpoint_holds(conn, [sid]) == {sid: "checkpoint_missing"}
            await conn.execute(
                "UPDATE runner.context_session SET checkpoint_state = 'empty' WHERE session_id = %s", (sid,)
            )
            assert await store.checkpoint_holds(conn, [sid]) == {sid: None}
            # a restore from the newest object failed here before: it is no valid checkpoint to move with
            newest = (await store.cache_rows(conn))[0]
            await conn.execute(
                "INSERT INTO runner.context_session (session_id, task_id, org_id, wg_id, restored_object_id,"
                " restore_failed) VALUES (%s, %s, %s, %s, %s, true)",
                (other, TASK, str(ck.pk.kit.guest.org_id), WG, newest.object_id),
            )
            assert await store.checkpoint_holds(conn, [sid]) == {sid: "checkpoint_missing"}
            assert await store.checkpoint_holds(conn, []) == {}
        # a device that is not enrolled vouches for nobody
        assert await vm_retire.checkpoints(SimpleNamespace(identity=None), row, [sid]) == {}

    run(rig, body)


async def context_row(env: Any, sid: str) -> dict[str, Any] | None:
    async with env.pool.connection() as conn:
        cursor = conn.cursor(row_factory=dict_row)
        await cursor.execute(
            "SELECT due_at IS NOT NULL AS due, checkpoint_state, impossible_reason, impossible_turns"
            " FROM runner.context_session WHERE session_id = %s",
            (sid,),
        )
        return cast("dict[str, Any] | None", await cursor.fetchone())


def test_a_session_that_can_never_be_checkpointed_is_remembered_and_not_made_due_again(rig: Rig) -> None:
    """HOT-24: `not_found` (a guest without the export route answers so) is stored with its reason and the turns so
    far; no request makes the session due again until it worked again; the switch learns `checkpoint_impossible`."""

    async def body(env: Any) -> None:
        ck = await ckit(env)
        sid = await ck.pk.add_session(state="suspended")
        row = await ck.pk.vm()
        worker = Checkpointer(ck.contexts)
        await accept(env, sid, 2)
        ck.guest.fail["export_context"] = GuestRefused("context_export", 404, "not_found")
        await observe(ck, sid, state="suspended", at=TS)  # the pause makes it due
        assert await worker.flush_vm(row.id) == ["dropped"]
        assert await context_row(env, sid) == {
            "due": False,
            "checkpoint_state": "impossible",
            "impossible_reason": "export_refused:not_found",
            "impossible_turns": 2,
        }
        assert await vm_retire.checkpoints(env.context, row, [sid]) == {sid: "checkpoint_impossible"}
        exports = len([name for name, _ in ck.guest.calls if name == "export_context"])
        # neither a state of the guest, nor the stop of its VM, nor the periodic rule makes it due again
        await observe(ck, sid, state="suspended", at=TS)
        assert await worker.flush_vm(row.id) == []
        await ck.pk.set_state(sid, "working")
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.context_session SET last_at = NULL WHERE session_id = %s", (sid,))
            assert await store.mark_periodic(conn, interval_s=1.0, at=ck.contexts.now()) == 0
        assert not await worker.vm_pending(row.id)
        assert len([name for name, _ in ck.guest.calls if name == "export_context"]) == exports
        # it worked again: due again, and a checkpoint that succeeds ends `impossible`
        await accept(env, sid)
        await ck.pk.set_state(sid, "suspended")
        await observe(ck, sid, state="suspended", at=TS)
        assert await worker.vm_pending(row.id)
        del ck.guest.fail["export_context"]
        ck.guest.archives[sid] = archive()
        assert await worker.flush_vm(row.id) == ["taken"]
        found = await context_row(env, sid)
        assert found is not None
        assert (found["checkpoint_state"], found["impossible_reason"], found["impossible_turns"]) == (
            "full",
            None,
            None,
        )
        assert await vm_retire.checkpoints(env.context, row, [sid]) == {sid: None}

    run(rig, body)


def test_the_mark_of_a_never_savable_session_belongs_to_its_vm(rig: Rig) -> None:
    """HOT-24 (independent review): after a move the new guest may export the session - there it is due again and
    held as missing until a checkpoint succeeds."""

    async def body(env: Any) -> None:
        ck = await ckit(env)
        sid = await ck.pk.add_session(state="suspended")
        row = await ck.pk.vm()
        await accept(env, sid)
        ck.guest.fail["export_context"] = GuestRefused("context_export", 404, "not_found")
        await observe(ck, sid, state="suspended", at=TS)
        assert await Checkpointer(ck.contexts).flush_vm(row.id) == ["dropped"]
        assert await vm_retire.checkpoints(env.context, row, [sid]) == {sid: "checkpoint_impossible"}
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.session SET vm_id = 'vm-0000eeee' WHERE id = %s", (sid,))
            assert await store.checkpoint_holds(conn, [sid]) == {sid: "checkpoint_missing"}
            assert await store.mark_vm_due(conn, "vm-0000eeee", ck.contexts.now()) == 1

    run(rig, body)


def test_a_session_without_a_complete_turn_has_nothing_to_save(rig: Rig) -> None:
    """HOT-24: `state_conflict` or `context_incomplete` of a session that never had a checkpoint here holds nothing; of
    one that had, the request is only dropped (its newer state may be lost otherwise). A session that never worked
    holds nothing and keeps no stopped VM from being replaced."""

    async def body(env: Any) -> None:
        ck = await ckit(env)
        first = await ck.pk.add_session(1, state="suspended")
        second = await ck.pk.add_session(2, state="suspended")
        never = await ck.pk.add_session(3, state="suspended")
        row = await ck.pk.vm()
        worker = Checkpointer(ck.contexts)
        await accept(env, first)
        await accept(env, second)
        ck.guest.archives[first] = archive(transcript=b'{"type":"user"}\n')  # no complete turn
        ck.guest.fail["export_context"] = GuestRefused("context_export", 409, "state_conflict")
        for sid in (first, second, never):
            await observe(ck, sid, state="suspended", at=TS)
        assert await worker.vm_pending(row.id)
        del ck.guest.fail["export_context"]
        ck.guest.archives[second] = archive(session_id=second)
        ck.guest.archives[never] = archive(session_id=never)
        assert await worker.take_all(await due_list(env, ck, [second])) == ["taken"]
        ck.guest.fail["export_context"] = GuestRefused("context_export", 409, "state_conflict")
        assert await worker.take_all(await due_list(env, ck, [first])) == ["dropped"]
        del ck.guest.fail["export_context"]
        await observe(ck, second, state="suspended", at=TS)
        ck.guest.fail["export_context"] = GuestRefused("context_export", 409, "state_conflict")
        assert await worker.take_all(await due_list(env, ck, [second])) == ["dropped"]
        assert [(await context_row(env, sid) or {}).get("checkpoint_state") for sid in (first, second)] == [
            "nothing",
            "dropped",  # it had a checkpoint: not "nothing"
        ]
        # only the never used session is still due: it keeps no stopped VM from being replaced
        assert [due.session_id for due in await due_list(env, ck, [first, second, never])] == [never]
        assert not await worker.vm_pending(row.id)
        assert await vm_retire.checkpoints(env.context, row, [first, second, never]) == {
            first: None,
            second: "checkpoint_missing",
            never: None,
        }
        # an archive without a complete turn is "nothing" as well
        del ck.guest.fail["export_context"]
        third = await ck.pk.add_session(4, state="suspended")
        await accept(env, third)
        ck.guest.archives[third] = archive(session_id=third, transcript=b'{"type":"user"}\n')
        await observe(ck, third, state="suspended", at=TS)
        assert await worker.take_all(await due_list(env, ck, [third])) == ["dropped"]
        assert (await context_row(env, third) or {}).get("checkpoint_state") == "nothing"

    run(rig, body)


async def due_list(env: Any, ck: Any, sids: list[str]) -> list[store.DueSession]:
    async with env.pool.connection() as conn:
        found = await store.due_on_vm(conn, (await ck.pk.vm()).id)
    return [due for due in found if due.session_id in sids]


def test_the_checkpoint_a_never_savable_session_would_continue_from(rig: Rig) -> None:
    """HOT-24: the session's own newest checkpoint, else its task's, else none (it starts fresh)."""

    async def body(env: Any) -> None:
        ck, object_id = await checkpointed(env)
        row = await ck.pk.vm()
        own = session_id(1)
        same_task = await ck.pk.add_session(2, state="suspended")
        other_task = await ck.pk.add_session(3, state="suspended", task=session_id(30))
        async with env.pool.connection() as conn:
            cur = await conn.execute("SELECT created_at FROM runner.context_object WHERE object_id = %s", (object_id,))
            found = await cur.fetchone()
        assert found is not None
        assert await vm_retire.fallbacks(env.context, row, [own, same_task, other_task]) == {
            own: found[0],
            same_task: found[0],
            other_task: None,
        }
        async with env.pool.connection() as conn:
            assert await store.fallback_times(conn, []) == {}
            assert await store.checkpoint_holds(conn, []) == {}
        assert await vm_retire.fallbacks(SimpleNamespace(identity=None), row, [own]) == {}

    run(rig, body)


def test_a_never_used_session_whose_vm_was_switched_starts_fresh_on_the_new_vm(rig: Rig) -> None:
    """HOT-24 goal 1: a paused session without checkpoint that moved with its VM takes the existing path of a session
    without checkpoint on the new VM: a fresh conversation that reads the task's messages, reported."""

    async def body(env: Any) -> None:
        ck = await ckit(env)
        sid = await ck.pk.add_session(state="suspended")
        new_vm = (await ck.pk.vm()).id
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.session SET vm_id = 'vm-0000ffff' WHERE id = %s", (sid,))
            await conn.execute(
                "INSERT INTO runner.golden_switch (old_vm_id, new_vm_id, golden_version, switched_at)"
                " VALUES ('vm-0000ffff', %s, 'g2', now())",
                (new_vm,),
            )
        assert await Resumer(ck.pk.kit.session_env).resume(await ck.pk.session(sid)) == "resumed"
        [create] = [value for name, value in ck.guest.calls if name == "create_session"]
        assert "restore" not in create
        assert create["harness"]["context_markdown"].endswith(RESTART_LINE.format(task_id=TASK))
        assert (await ck.pk.session(sid)).vm_id == new_vm
        assert [item["detail"] for item in await ck.audits(FALLBACK)] == [{"reason": "context_missing"}]

    run(rig, body)


def test_a_session_whose_vm_was_switched_hears_of_the_new_version(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck, object_id = await checkpointed(env)
        sid = session_id(1)
        await ck.pk.set_state(sid, "suspended")
        new_vm = (await ck.pk.vm()).id
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.session SET vm_id = 'vm-0000ffff' WHERE id = %s", (sid,))
            await conn.execute(
                "INSERT INTO runner.golden_switch (old_vm_id, new_vm_id, golden_version, switched_at)"
                " VALUES ('vm-0000ffff', %s, 'g2', now())",
                (new_vm,),
            )
        senv = ck.pk.kit.session_env
        senv.desired = {**(senv.desired or {}), "vm_switch": []}
        assert await Resumer(senv).resume(await ck.pk.session(sid)) == "resumed"
        [create] = [value for name, value in ck.guest.calls if name == "create_session"]
        note = create["restore"]["note"]
        assert "on the new version of the workgroup VM" in note
        assert "your files were carried over: your workspace was restored as it was." in note
        assert "work VM" not in note  # work VMs are separate and still there
        await observe(ck, sid, state="agent_starting", at=TS)
        [audit] = await ck.audits(RESTORED)
        assert audit["detail"] == {"object_id": object_id, "workspace": "full", "stale": False, "moved": "golden"}

    run(rig, body)


def test_an_older_hub_gets_no_moved_field(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck, _object_id = await checkpointed(env)
        sid = session_id(1)
        await ck.pk.set_state(sid, "suspended")
        new_vm = (await ck.pk.vm()).id
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.session SET vm_id = 'vm-0000ffff' WHERE id = %s", (sid,))
            await conn.execute(
                "INSERT INTO runner.golden_switch (old_vm_id, new_vm_id, golden_version, switched_at)"
                " VALUES ('vm-0000ffff', %s, 'g2', now())",
                (new_vm,),
            )
        ck.pk.kit.session_env.desired = None
        assert await Resumer(ck.pk.kit.session_env).resume(await ck.pk.session(sid)) == "resumed"
        [create] = [value for name, value in ck.guest.calls if name == "create_session"]
        assert "on the new version of the workgroup VM" in create["restore"]["note"]
        await observe(ck, sid, state="agent_starting", at=TS)
        [audit] = await ck.audits(RESTORED)
        assert "moved" not in audit["detail"]

    run(rig, body)


def test_the_golden_sentence_names_a_stale_checkpoint() -> None:
    context = archive_format.parse_context(json.dumps(context_json()).encode())
    line = restored_line(Restored("o", context, {"ts": TS}), where="golden", stale=True)
    assert line.endswith(STALE_TEXT)


def test_a_resume_on_another_vm_than_the_successor_is_no_golden_move(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck, _object_id = await checkpointed(env)
        sid = session_id(1)
        await ck.pk.set_state(sid, "suspended")
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.session SET vm_id = 'vm-0000ffff' WHERE id = %s", (sid,))
            await conn.execute(
                "INSERT INTO runner.golden_switch (old_vm_id, new_vm_id, golden_version, switched_at)"
                " VALUES ('vm-0000ffff', 'vm-0000eeee', 'g2', now())"
            )
        assert await Resumer(ck.pk.kit.session_env).resume(await ck.pk.session(sid)) == "resumed"
        [create] = [value for name, value in ck.guest.calls if name == "create_session"]
        assert "on another VM of this device" in create["restore"]["note"]

    run(rig, body)
