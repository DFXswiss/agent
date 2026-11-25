"""U-RUN-contexts: checkpoints of main sessions - due marks, export, sealing, upload with its failures, the cache
(ADR-072 sections 2-4 and 6; review KOR-33 nos. 2, 6, 7, 8, 11, 12)."""

from __future__ import annotations

import random
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from agent_runner.contexts import store, vm_retire
from agent_runner.contexts.checkpoint import MAX_ATTEMPTS, Checkpointer, current_key, retry_delay
from agent_runner.contexts.session_state import OBSERVERS
from agent_runner.crypto import context as crypto
from agent_runner.crypto.encoding import b64u_decode
from agent_runner.guestlink.client import JournalRecord
from agent_runner.guestlink.errors import GuestRefused, GuestUnreachable
from agent_runner.prompts import session_state as prompt_state
from agent_runner.sessions.consume import VmJournal
from agent_runner.sessions.journal import HANDLERS
from agent_runner.sync.http import HubRefusal

from ..prompts.kit import run
from ..reconcile.rig import Rig, scope_key
from ..sessions.kit import TASK, WG, session_id
from .kit import CKit, archive, ckit, transport_error

AT = "2026-11-21T12:00:02.000Z"


async def observe(ck: CKit, sid: str, **body: Any) -> None:
    """One guest `session.state` record through the real handlers with the prompt and context observers."""
    ck.pk.seq += 1
    record = JournalRecord(seq=ck.pk.seq, ts=AT, session_id=sid, kind="session.state", body=dict(body))
    observers = [*prompt_state.OBSERVERS, *OBSERVERS]
    await VmJournal(ck.pk.kit.session_env, await ck.pk.vm(), handlers=HANDLERS, observers=observers).handle([record])


async def due_rows(ck: CKit) -> list[store.DueSession]:
    async with ck.pk.env.pool.connection() as conn:
        return await store.due(conn, ck.contexts.now())


async def objects_due(ck: CKit, vm_id: str) -> list[store.DueSession]:
    """Every due checkpoint of the VM, also those waiting for it."""
    async with ck.pk.env.pool.connection() as conn:
        return await store.due_on_vm(conn, vm_id)


async def objects(ck: CKit) -> list[store.StoredObject]:
    async with ck.pk.env.pool.connection() as conn:
        return await store.cache_rows(conn)


async def taken(ck: CKit, sid: str) -> str:
    """Take a due checkpoint of the session and run the upload loop once."""
    result = await Checkpointer(ck.contexts).take(await due_now(ck, sid))
    await Checkpointer(ck.contexts).upload_round()
    return result


async def prepared(env: Any, *, state: str = "working", data: bytes | None = None, **settings: Any) -> tuple[CKit, str]:
    ck = await ckit(env, **settings)
    sid = await ck.pk.add_session(state=state)
    ck.guest.archives[sid] = data if data is not None else archive()
    return ck, sid


def test_an_idle_main_session_is_checkpointed_uploaded_and_committed(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck, sid = await prepared(env)
        await observe(ck, sid, state="idle", at=AT)
        assert ck.contexts.wake.is_set()
        [due] = await due_rows(ck)
        assert await Checkpointer(ck.contexts).take(due) == "taken"
        assert ck.contexts.upload_wake.is_set()
        [obj] = await objects(ck)
        assert obj.state == "sealed"  # the export does not wait for the upload
        assert await Checkpointer(ck.contexts).upload_round() == 1
        [obj] = await objects(ck)
        assert (obj.state, obj.source, obj.session_id, obj.task_id) == ("committed", "local", sid, TASK)
        assert ck.store.committed[obj.object_id]["sig"] == obj.sig
        manifest = ck.store.committed[obj.object_id]["manifest"]
        assert manifest["author"] == ck.contexts.device_id
        assert manifest["epoch"] == 1
        # the chunks the hub holds open with the workgroup key of the manifest's epoch, only on this device
        key = crypto.context_key(scope_key("wg", WG, 1), b64u_decode(manifest["salt"]), obj.object_id)
        sealed = b64u_decode(ck.store.chunks[(obj.object_id, 0)])
        assert crypto.open_chunk(key, obj.object_id, 0, sealed)[:2] == b"\x1f\x8b"  # gzip
        assert ck.contexts.cache.read_chunk(obj.object_id, 0) == sealed
        export = next(value for name, value in ck.guest.calls if name == "export_context")
        assert export == (sid, ck.contexts.workspace_bytes, ck.contexts.transcript_bytes)
        assert await due_rows(ck) == []
        assert not list(ck.contexts.scratch.iterdir())  # the plaintext is gone
        # the same archive again: not uploaded a second time
        await observe(ck, sid, state="working", at=AT)
        await observe(ck, sid, state="idle", at=AT)
        [again] = await due_rows(ck)
        assert await Checkpointer(ck.contexts).take(again) == "identical"
        assert len(await objects(ck)) == 1
        assert any("checkpoint_identical" in line for line in ck.lines)

    run(rig, body)


def test_only_main_sessions_in_ending_states_become_due(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck = await ckit(env)
        child = await ck.pk.add_session(2, state="working", depth=1)
        main = await ck.pk.add_session(3, state="idle")
        await observe(ck, child, state="idle", at=AT)
        await observe(ck, main, state="working", at=AT)
        assert await due_rows(ck) == []
        for state in ("suspended",):
            await observe(ck, main, state=state, at=AT)
        assert [row.session_id for row in await due_rows(ck)] == [main]

    run(rig, body)


def test_a_large_archive_is_split_into_many_chunks(rig: Rig) -> None:
    async def body(env: Any) -> None:
        noise = random.Random(5).randbytes(5 * crypto.CHUNK_BYTES)
        ck, sid = await prepared(env, data=archive(work={"blob.bin": noise}, workspace_bytes=len(noise)))
        assert await taken(ck, sid) == "taken"
        [obj] = await objects(ck)
        assert obj.manifest["chunks"] == 6
        assert sorted(index for oid, index in ck.store.chunks if oid == obj.object_id) == list(range(6))
        assert obj.bytes == obj.manifest["size"] > 5 * crypto.CHUNK_BYTES

    run(rig, body)


async def due_now(ck: CKit, sid: str) -> store.DueSession:
    async with ck.pk.env.pool.connection() as conn:
        await store.mark_due(
            conn, session_id=sid, task_id=TASK, org_id=str(ck.pk.kit.guest.org_id), wg_id=WG, at=ck.contexts.now()
        )
    [due] = [row for row in await due_rows(ck) if row.session_id == sid]
    return due


def test_export_failures_drop_or_postpone_the_request(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck, sid = await prepared(env)
        worker = Checkpointer(ck.contexts)
        for error in ("not_found", "state_conflict", "size_exceeded"):
            ck.guest.fail["export_context"] = GuestRefused("context_export", 409, error)
            assert await worker.take(await due_now(ck, sid)) == "dropped"
            assert any(f"export_refused error={error}" in line for line in ck.lines)
        ck.guest.fail["export_context"] = GuestUnreachable("context_export")
        assert await worker.take(await due_now(ck, sid)) == "postponed"
        assert await due_rows(ck) == []  # waits for its backoff
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.context_session SET next_at = NULL, attempts = %s", (MAX_ATTEMPTS - 1,))
        [due] = await due_rows(ck)
        assert await worker.take(due) == "dropped"
        assert retry_delay(0) == 5.0
        assert retry_delay(50) == 3600.0

    run(rig, body)


def test_unusable_sessions_and_archives_are_dropped(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck, sid = await prepared(env)
        worker = Checkpointer(ck.contexts)
        # an archive of another session, one that is not a context archive
        for data in (archive(session_id=session_id(9)), archive(task_id=session_id(8)), b"junk" * 300):
            ck.guest.archives[sid] = data
            assert await worker.take(await due_now(ck, sid)) == "dropped"
        assert any("archive_invalid code=archive_invalid" in line for line in ck.lines)
        # KOR-33 E2E: an archive whose conversation holds no complete turn is nothing to save (no object sealed)
        ck.guest.archives[sid] = archive(transcript=b'{"type":"user"}\n')
        assert await worker.take(await due_now(ck, sid)) == "dropped"
        assert any("archive_invalid code=context_incomplete" in line for line in ck.lines)
        assert await objects(ck) == []
        # the session is unknown or a subagent
        assert await worker.take(await due_now(ck, session_id(7))) == "dropped"
        child = await ck.pk.add_session(8, state="idle", depth=1)
        assert await worker.take(await due_now(ck, child)) == "dropped"
        assert all(name != "export_context" or value[0] == sid for name, value in ck.guest.calls)
        # a session without VM, or whose VM was deleted, ends the request
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.session SET vm_id = NULL WHERE id = %s", (sid,))
        assert await worker.take(await due_now(ck, sid)) == "dropped"
        assert any("vm_gone" in line for line in ck.lines)

    run(rig, body)


def test_a_due_checkpoint_waits_while_its_vm_is_stopped_and_ends_when_it_is_deleted(rig: Rig) -> None:
    """Review no. 2: `vm_gone` no longer drops a due checkpoint of a VM that only stopped."""

    async def body(env: Any) -> None:
        ck, sid = await prepared(env)
        worker = Checkpointer(ck.contexts)
        row = await ck.pk.vm()
        await due_now(ck, sid)
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.vm SET state = 'stopped' WHERE id = %s", (row.id,))
        assert await due_rows(ck) == []  # left out while the VM does not run
        assert await worker.round() == 0
        [waiting] = [item for item in await objects_due(ck, row.id) if item.session_id == sid]
        assert await worker.take(waiting) == "waiting"  # e.g. when the VM is flushed: stays due, no attempt
        assert [item.attempts for item in await objects_due(ck, row.id)] == [0]
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.vm SET state = 'running' WHERE id = %s", (row.id,))
        assert await worker.round() == 1
        assert len(await objects(ck)) == 1
        await due_now(ck, sid)
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.vm SET state = 'deleted' WHERE id = %s", (row.id,))
        assert await worker.round() == 1
        assert await objects_due(ck, row.id) == []
        assert any("vm_gone" in line for line in ck.lines)

    run(rig, body)


def test_a_missing_epoch_key_postpones_and_the_newest_epoch_is_used(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck, sid = await prepared(env)
        async with env.pool.connection() as conn:
            found = await current_key(conn, ck.contexts.sessions.storage_key, WG)
            assert found is not None
            assert found[0] == 1
            await conn.execute("DELETE FROM runner.scope_key WHERE scope = 'wg'")
            assert await current_key(conn, ck.contexts.sessions.storage_key, WG) is None
        assert await Checkpointer(ck.contexts).take(await due_now(ck, sid)) == "postponed"
        assert any("key_missing" in line for line in ck.lines)

    run(rig, body)


def test_upload_failures_are_retried_or_refused_and_a_missing_commit_is_resumed(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck, sid = await prepared(env)
        worker = Checkpointer(ck.contexts)
        # the commit does not arrive: the object stays sealed with every chunk confirmed
        ck.store.refuse["commit"] = (503, "unavailable")
        assert await taken(ck, sid) == "taken"
        [obj] = await objects(ck)
        assert (obj.state, obj.uploaded) == ("sealed", 1)
        assert obj.object_id not in ck.store.committed
        # later round: the commit goes through without sending the chunk again
        del ck.store.refuse["commit"]
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.context_object SET next_at = NULL")
        puts = len([t for m, t in ck.hub.targets if m == "PUT"])
        assert await worker.upload_round() == 1
        assert len([t for m, t in ck.hub.targets if m == "PUT"]) == puts
        assert (await objects(ck))[0].state == "committed"

    run(rig, body)


def test_upload_refusals_and_transport_errors(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck, sid = await prepared(env)
        worker = Checkpointer(ck.contexts)
        ck.hub.errors = [transport_error()]
        assert await taken(ck, sid) == "taken"
        [obj] = await objects(ck)
        assert obj.state == "sealed"
        assert any("upload_retry" in line for line in ck.lines)
        ck.store.refuse["put"] = (413, "quota_exceeded")
        assert await worker.upload((await objects(ck))[0]) == "refused"
        assert (await objects(ck))[0].state == "refused"
        assert any("upload_refused code=quota_exceeded" in line for line in ck.lines)
        # the local copy stays; a chunk lost from the cache refuses the upload
        assert ck.contexts.cache.read_chunk(obj.object_id, 0) is not None
        ck.contexts.cache.remove(obj.object_id)
        assert await worker.upload(obj) == "refused"
        assert any("code=chunk_missing" in line for line in ck.lines)

    run(rig, body)


def test_a_hub_without_the_route_gets_no_uploads_and_the_newest_copy_stays_local(rig: Rig) -> None:
    """Review no. 12: `404` on the very first chunk is an older hub (or a session it does not know)."""

    async def body(env: Any) -> None:
        ck, sid = await prepared(env)
        ck.store.refuse["put"] = (404, "not_found")
        assert await taken(ck, sid) == "taken"
        [obj] = await objects(ck)
        assert obj.state == "refused"
        assert any("upload_refused code=not_found" in line for line in ck.lines)
        assert await due_rows(ck) == []  # not taken again
        # a newer checkpoint of the task replaces it in the cache
        ck.guest.archives[sid] = archive(work={"notes.txt": b"newer"})
        assert await taken(ck, sid) == "taken"
        [newest] = await objects(ck)
        assert newest.object_id != obj.object_id
        assert ck.contexts.cache.read_chunk(obj.object_id, 0) is None

    run(rig, body)


def _expire_after_first_chunk(ck: CKit) -> None:
    original = ck.store.route

    def route(method: str, path: str, body: Any) -> Any:
        if method == "PUT" and path.endswith("/chunks/1"):
            return 404, {"error": "not_found"}
        return original(method, path, body)

    ck.store.route = route  # type: ignore[method-assign]


@pytest.mark.parametrize("where", ["chunk", "commit", "commit_chunks"])
def test_an_upload_the_hub_expired_starts_again_with_a_new_object(rig: Rig, where: str) -> None:
    """Review no. 7: an interrupted upload the hub removed after 6 h is never finished; the session's checkpoint is
    due again and goes up as a new object, the old local copy stays until it is superseded."""

    async def body(env: Any) -> None:
        noise = random.Random(7).randbytes(3 * crypto.CHUNK_BYTES)
        ck, sid = await prepared(env, data=archive(work={"blob.bin": noise}, workspace_bytes=len(noise)))
        if where == "chunk":
            _expire_after_first_chunk(ck)
        elif where == "commit":
            ck.store.refuse["commit"] = (404, "not_found")
        else:
            ck.hub.errors = []
            original = ck.store.route

            def route(method: str, path: str, body: Any) -> Any:
                if path.endswith("/commit"):
                    return 400, {"error": "request_invalid", "detail": {"pointers": ["/manifest/chunks"]}}
                return original(method, path, body)

            ck.store.route = route  # type: ignore[method-assign]
        assert await taken(ck, sid) == "taken"
        [old] = await objects(ck)
        assert old.state == "refused"
        assert any("upload_expired" in line for line in ck.lines)
        assert ck.contexts.wake.is_set()
        [again] = await due_rows(ck)
        vars(ck.store).pop("route", None)
        ck.store.refuse.clear()
        # the same conversation again: a new object nevertheless (the hub holds none of it)
        assert await Checkpointer(ck.contexts).take(again) == "taken"
        assert await Checkpointer(ck.contexts).upload_round() == 1
        [new] = await objects(ck)  # the refused older copy went with the committed newer one
        assert new.object_id != old.object_id
        assert new.state == "committed"

    run(rig, body)


def test_a_commit_refused_for_another_reason_is_refused(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck, sid = await prepared(env)
        original = ck.store.route

        def route(method: str, path: str, body: Any) -> Any:
            if path.endswith("/commit"):
                return 400, {"error": "request_invalid", "detail": {"pointers": ["/manifest/epoch"]}}
            return original(method, path, body)

        ck.store.route = route  # type: ignore[method-assign]
        assert await taken(ck, sid) == "taken"
        assert (await objects(ck))[0].state == "refused"
        assert not any("upload_expired" in line for line in ck.lines)
        ck.store.route = original  # type: ignore[method-assign]
        ck.store.refuse["commit"] = (400, "request_invalid")  # without pointers
        ck.guest.archives[sid] = archive(work={"notes.txt": b"other"})
        assert await taken(ck, sid) == "taken"
        assert not any("upload_expired" in line for line in ck.lines)

    run(rig, body)


def test_working_sessions_are_checkpointed_periodically(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck, sid = await prepared(env, interval_s=600.0)
        worker = Checkpointer(ck.contexts)
        await worker.round()  # first sight: counted from now
        assert await objects(ck) == []
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.context_session SET last_at = last_at - interval '601 seconds'")
        assert await worker.round() == 1
        assert await worker.upload_round() == 1
        [obj] = await objects(ck)
        assert obj.state == "committed"
        # a request that arrived while a checkpoint ran stays due
        async with env.pool.connection() as conn:
            await store.mark_due(
                conn,
                session_id=sid,
                task_id=TASK,
                org_id=str(ck.pk.kit.guest.org_id),
                wg_id=WG,
                at=ck.contexts.now() + timedelta(seconds=1),
            )
            await store.taken(
                conn, sid, due_at=ck.contexts.now(), sha256="0" * 64, workspace="full", at=ck.contexts.now()
            )
            cur = await conn.execute(
                "SELECT due_at IS NOT NULL FROM runner.context_session WHERE session_id = %s", (sid,)
            )
            assert (await cur.fetchone()) == (True,)

    run(rig, body)


def test_an_unexpected_error_of_a_checkpoint_postpones_it(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck, sid = await prepared(env)
        await due_now(ck, sid)
        ck.guest.fail["export_context"] = RuntimeError("boom")
        assert await Checkpointer(ck.contexts).round() == 1
        assert any("checkpoint_error" in line and "RuntimeError" in line for line in ck.lines)
        async with env.pool.connection() as conn:
            cur = await conn.execute("SELECT attempts FROM runner.context_session WHERE session_id = %s", (sid,))
            assert (await cur.fetchone()) == (1,)

    run(rig, body)


def test_the_cache_keeps_the_newest_object_per_task_and_its_size_limit_covers_every_object(rig: Rig) -> None:
    """Review no. 12: the limit holds for every object; only the newest of each task is always kept."""

    async def body(env: Any) -> None:
        ck, sid = await prepared(env)
        worker = Checkpointer(ck.contexts)
        first = await taken(ck, sid)
        ck.guest.archives[sid] = archive(work={"notes.txt": b"changed"})
        later = ck.pk.kit.session_env.wall() + 10
        ck.pk.kit.session_env.wall = lambda: later
        second = await taken(ck, sid)
        assert (first, second) == ("taken", "taken")
        [kept] = await objects(ck)  # the older object of the task went with the newer one
        assert ck.contexts.cache.read_chunk(kept.object_id, 0) is not None
        # an older object still being uploaded stays while the cache has room ...
        ck.store.refuse["put"] = (503, "unavailable")
        ck.guest.archives[sid] = archive(work={"notes.txt": b"third"})
        assert await taken(ck, sid) == "taken"
        ck.guest.archives[sid] = archive(work={"notes.txt": b"fourth"})
        ck.pk.kit.session_env.wall = lambda: later + 10
        assert await taken(ck, sid) == "taken"
        states = [row.state for row in await objects(ck)]
        assert states == ["sealed", "sealed"]
        # ... and goes when the cache is over its limit, whatever its state; the newest of the task stays
        ck.contexts.cache_bytes = 0
        newest, older = await objects(ck)
        assert await worker.evict() == [older.object_id]
        assert [row.object_id for row in await objects(ck)] == [newest.object_id]
        assert any("cache_full" in line for line in ck.lines)
        # an error while sealing removes the partial object
        ck.guest.archives[sid] = archive(work={"notes.txt": b"fifth"})
        cache = ck.contexts.cache

        def broken(object_id: str, index: int, data: bytes) -> None:
            raise OSError("disk full")

        cache.write_chunk = broken  # type: ignore[method-assign]
        await due_now(ck, sid)
        assert await worker.round() == 1
        assert len(await objects(ck)) == 1
        assert any("checkpoint_error" in line and "OSError" in line for line in ck.lines)

    run(rig, body)


def test_the_cache_keeps_the_newest_object_of_every_task(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck, sid = await prepared(env)
        other = await ck.pk.add_session(2, state="idle", task=session_id(40))
        ck.guest.archives[other] = archive(session_id=other, task_id=session_id(40))
        assert await taken(ck, sid) == "taken"
        assert await taken(ck, other) == "taken"
        ck.contexts.cache_bytes = 0
        assert await Checkpointer(ck.contexts).evict() == []
        assert len(await objects(ck)) == 2

    run(rig, body)


def test_a_refused_hub_answer_with_a_retry_code_is_retried(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck, sid = await prepared(env)
        ck.hub.errors = [HubRefusal(429, "rate_limited")]
        assert await taken(ck, sid) == "taken"
        assert (await objects(ck))[0].state == "sealed"

    run(rig, body)


def test_a_vm_that_goes_away_has_the_checkpoints_of_its_sessions_taken_first(rig: Rig) -> None:
    """Review no. 2 (the hook of `vm.extensions`): before a VM is stopped or deleted, and a stopped VM with due
    checkpoints is kept instead of replaced."""

    async def body(env: Any) -> None:
        ck, sid = await prepared(env, state="working")
        ended = await ck.pk.add_session(2, state="stopped")
        row = await ck.pk.vm()
        assert not await vm_retire.pending(env.context, row)
        await vm_retire.flush(env.context, row)
        [obj] = await objects(ck)
        assert (obj.session_id, obj.state) == (sid, "sealed")  # the upload follows in its own loop
        assert ck.contexts.upload_wake.is_set()
        assert await objects_due(ck, row.id) == []
        assert all(value[0] != ended for name, value in ck.guest.calls if name == "export_context")
        assert any(f"vm_flushed vm_id={row.id} checkpoints=1" in line for line in ck.lines)
        await due_now(ck, sid)
        assert await vm_retire.pending(env.context, row)
        # a device that is not enrolled has nothing to take
        bare = SimpleNamespace(identity=None)
        await vm_retire.flush(bare, row)
        assert not await vm_retire.pending(bare, row)

    run(rig, body)
