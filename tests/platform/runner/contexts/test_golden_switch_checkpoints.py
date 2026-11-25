"""U-RUN-contexts: whether the newest checkpoint of a paused session is complete, so its VM may move to a newer golden
(`store.checkpoint_holds`, `vm_retire.checkpoints`; ADR-093 section 1, KOR-70)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from agent_runner.contexts import archive as archive_format
from agent_runner.contexts import store, vm_retire
from agent_runner.contexts.checkpoint import Checkpointer
from agent_runner.contexts.restore import RESTORED, STALE_TEXT, Restored, restored_line
from agent_runner.guestlink.errors import GuestRefused
from agent_runner.prompts.resume import Resumer

from ..prompts.kit import run
from ..reconcile.rig import Rig
from ..sessions.kit import TASK, WG, session_id
from .kit import archive, ckit, context_json
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

        # never checkpointed here
        assert await vm_retire.checkpoints(env.context, row, [sid, other]) == {
            sid: "checkpoint_missing",
            other: "checkpoint_missing",
        }
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
        # a request that ended without a checkpoint: the older one does not hold the newest files
        ck.guest.fail["export_context"] = GuestRefused("context_export", 409, "size_exceeded")
        assert await worker.take(await due()) == "dropped"
        assert (await state(), await holds()) == ("dropped", {sid: "checkpoint_missing"})
        del ck.guest.fail["export_context"]
        ck.guest.archives[sid] = archive(work={"notes.txt": b"complete again"})
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
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.session SET vm_id = 'vm-0000ffff' WHERE id = %s", (sid,))
            await conn.execute(
                "INSERT INTO runner.golden_switch (old_vm_id, new_vm_id, golden_version, switched_at)"
                " VALUES ('vm-0000ffff', 'vm-0000eeee', 'g2', now())"
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
