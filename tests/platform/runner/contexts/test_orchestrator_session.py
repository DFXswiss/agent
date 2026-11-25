"""U-RUN-contexts: a session of the main orchestrator never becomes due for a context checkpoint (05-runner.md section
17.6, ADR-082 section 6.6): neither from its states, nor periodically, nor when its VM goes away, and a checkpoint that
was due before it became the orchestrator is not taken."""

from __future__ import annotations

from typing import Any

from agent_runner.contexts import store, vm_retire
from agent_runner.contexts.checkpoint import Checkpointer
from agent_runner.orchestrator import store as orchestrator_store

from ..prompts.kit import run
from ..reconcile.rig import Rig
from ..sessions.kit import TASK, WG, session_id
from .test_checkpoint import AT, due_rows, objects, objects_due, observe, prepared


async def make_orchestrator(env: Any, sid: str, at: Any) -> None:
    async with env.pool.connection() as conn:
        await orchestrator_store.register(conn, sid, session_id(9), at)


def test_an_orchestrator_session_is_never_checkpointed(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck, sid = await prepared(env, state="working", interval_s=600.0)
        # due before it became the orchestrator: the due mark stays, but it is never taken
        await observe(ck, sid, state="idle", at=AT)
        assert [row.session_id for row in await due_rows(ck)] == [sid]
        await make_orchestrator(env, sid, ck.contexts.now())
        assert await due_rows(ck) == []
        row = await ck.pk.vm()
        assert await objects_due(ck, row.id) == []
        async with env.pool.connection() as conn:
            await conn.execute("DELETE FROM runner.context_session")
            await store.mark_due(
                conn, session_id=sid, task_id=TASK, org_id=str(ck.pk.kit.guest.org_id), wg_id=WG, at=ck.contexts.now()
            )
            assert await store.mark_vm_due(conn, row.id, ck.contexts.now()) == 0
        # its states, the periodic round and the VM's end make nothing due
        await observe(ck, sid, state="working", at=AT)
        async with env.pool.connection() as conn:
            assert await store.mark_periodic(conn, interval_s=0.0, at=ck.contexts.now()) == 0
        assert await Checkpointer(ck.contexts).round() == 0
        await observe(ck, sid, state="suspended", at=AT)
        await vm_retire.flush(env.context, row)
        assert await objects(ck) == []
        assert await env.rows("SELECT 1 FROM runner.context_session") == []
        assert all(name != "export_context" for name, _value in ck.guest.calls)

    run(rig, body)
