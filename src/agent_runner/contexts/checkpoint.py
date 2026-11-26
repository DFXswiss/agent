"""Context checkpoints of main sessions (ADR-072 sections 2-4 and 6; decisions/KOR-33.md, review KOR-33 nos. 2, 7,
8, 11 and 12).

A checkpoint becomes due (`runner.context_session.due_at`) when the guest reports `idle`, `suspended`, `stopped` or
`failed` for a main session (`session_state.py`), every `interval_s` while it is `working` (`mark_periodic`), and for
every main session of a VM that is about to be stopped, deleted or replaced (`flush_vm`, the VM extension point of
`vm_retire.py`). Export and upload are separate:

**Export** (`take`, `round`): up to `EXPORTS_AT_ONCE` due checkpoints are taken at the same time, each under its
session's lock (`ContextEnv.session_lock`) - one checkpoint per session at a time, requests that arrive meanwhile are
coalesced:

1. the session's VM must run. A VM that exists but does not run (stopped, stopping) keeps the request due without
   counting an attempt until it runs again or is deleted (`store.due` leaves such requests out); a session that is
   unknown here, a subagent, or whose VM is deleted ends the request (`dropped`);
2. the guest streams the archive (`POST /v1/sessions/{id}/context/export` with the workspace and transcript limits)
   into a scratch file of the runner's `tmp/` (0700, removed afterwards);
3. `archive.inspect` checks it with the untrusted-archive rules - in a worker thread, like every CPU or disk bound
   step here (review no. 8) - and its `context.json` must name this session and task; an archive whose transcript
   and workspace are unchanged (`Context.content_sha256`, review no. 11) ends here (`identical`);
4. gzip, 2 MiB pieces, each sealed with `k_ctx` of a new object (UUIDv7, random salt) under the **current** epoch key
   of the workgroup (`runner.scope_key`), written to the cache; the manifest is signed with the device key and the
   object recorded `sealed` together with the content hash (one transaction); the upload task is woken.

Export failures: a guest refusal (`not_found`: no such session - or a guest without the export route, which answers
the same, HOT-24; `state_conflict`: no conversation yet; `size_exceeded`: transcript over its limit) drops this request
with a log line; an unreachable guest or a missing epoch key postpones it with backoff, at most `MAX_ATTEMPTS` times.
A refused archive (`ArchiveInvalid`) is dropped - among them `context_incomplete`, a conversation without a complete
turn: nothing to save yet (the guest's export waits for a quiet transcript and refuses such a conversation itself,
`state_conflict`; this check also holds against an older guest). A guest refusal `unavailable` (a passing fault of
the guest) is postponed like an unreachable guest. What a drop means is stored (`store.drop_due`, HOT-24, ADR-093
addendum): `nothing` for `state_conflict`/`context_incomplete` of a session that never had a checkpoint here (nothing
to save: it holds no VM); `impossible` with the reason (`export_refused:not_found` - also a guest without the export
route -, `export_refused:size_exceeded`) for a refusal that stays as long as the guest stays - the session is not made
due again until it worked again; `dropped` for everything else (the session or its VM is gone, the attempts ran out,
another refusal), which a later request may still end.

**Upload** (`upload`, `upload_round`, its own loop in the daemon task so a long upload never delays an export):
`PUT /v1/device/contexts/{object}/chunks/{i}` from the first chunk the hub has not confirmed, then `POST .../commit`;
`committed` afterwards. A transport failure, `unavailable` or `rate_limited` is retried with backoff (`retry_delay`).
`not_found` for the very first chunk means a hub without the route (or without the session): the object is
`refused` and stays local - such a hub gets no uploads, the device keeps the newest checkpoint of each task (review
no. 12). `not_found` after the hub had accepted chunks of the object, or a commit refused for missing chunks, means
the hub expired the interrupted upload (6 h): the object is `refused` (the local copy stays) and the session's
checkpoint becomes due again, so a new object with a new ID is uploaded (review no. 7). Any other refusal
(`quota_exceeded`, `size_exceeded`, `forbidden`, ...) ends the object `refused` and keeps the local copy (log line
`upload_refused`; a restore on this device still uses it, review no. 6). A missing commit never makes a checkpoint
visible: the hub lists only committed objects.

**Cache** (`evict`, review no. 12): the newest object of every task is always kept; an older object of a task is
removed once it is no longer being uploaded (committed, refused or fetched); then, while the cache is over
`cache_bytes`, the oldest remaining objects that are not the newest of their task go, whatever their state. A cache
that is still over the limit with only the newest objects is logged (`cache_full`).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from psycopg import AsyncConnection

from agent_runner.contexts import archive, store
from agent_runner.contexts.env import ContextEnv
from agent_runner.contexts.store import DueSession, StoredObject
from agent_runner.crypto import context as crypto
from agent_runner.crypto.encoding import b64u_encode
from agent_runner.crypto.ids import uuid7
from agent_runner.crypto.kdf import SALT_BYTES
from agent_runner.guestlink.errors import GuestError, GuestRefused
from agent_runner.orgs.keys import load_scope_key
from agent_runner.sessions import records
from agent_runner.sync.http import HubRefusal, HubTransportError
from agent_runner.vm import records as vm_records

MAX_ATTEMPTS = 8
BACKOFF_BASE_S = 5.0
BACKOFF_CAP_S = 3600.0
EXPORTS_AT_ONCE = 4
RETRY_CODES = frozenset({"not_found", "unavailable", "rate_limited"})
"""Hub refusals a pull-request registration retries (`prlinks`)."""
UPLOAD_RETRY_CODES = frozenset({"unavailable", "rate_limited"})
EXPIRED_POINTERS = frozenset({"/manifest/chunks", "/manifest/size", "/manifest/digest"})
"""Commit refusals (`request_invalid` at these pointers) that mean the hub no longer holds every chunk."""


def retry_delay(attempts: int) -> float:
    """5 s doubling per failed attempt, at most one hour."""
    return float(min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** min(attempts, 20)))


async def current_key(conn: AsyncConnection[Any], storage_key: bytes, wg_id: str) -> tuple[int, bytes] | None:
    """The newest epoch of the workgroup whose key the device holds, with the opened key."""
    cur = await conn.execute("SELECT max(epoch) FROM runner.scope_key WHERE scope = 'wg' AND scope_id = %s", (wg_id,))
    row = await cur.fetchone()
    epoch = None if row is None else row[0]
    if epoch is None:
        return None
    key = await load_scope_key(conn, storage_key, "wg", wg_id, int(epoch))
    return None if key is None else (int(epoch), key)


def inspect_file(path: Path, limits: Any) -> archive.Context:
    with path.open("rb") as source:
        return archive.inspect(source, limits)


class _Stop(Exception):
    """No checkpoint now: the request is dropped (with its outcome and the reason an `impossible` one is stored with,
    `store.drop_due`), postponed with backoff, or waits for its VM."""

    def __init__(
        self,
        why: str,
        *,
        postpone: bool = False,
        wait: bool = False,
        outcome: str = "dropped",
        reason: str | None = None,
    ) -> None:
        super().__init__(why)
        self.why = why
        self.postpone = postpone
        self.wait = wait
        self.outcome = outcome
        self.reason = reason


NOTHING_REFUSALS = frozenset({"state_conflict"})
"""Guest refusals of an export that mean: no complete turn to save yet."""
NOTHING_ARCHIVES = frozenset({"context_incomplete"})
"""Archive refusals that mean the same."""
IMPOSSIBLE_REFUSALS = frozenset({"not_found", "size_exceeded"})
"""Guest refusals that stay as long as the guest stays: the guest does not know the session or has no export route
(both `not_found`), or the transcript is over its limit (it only grows). HOT-24."""
RETRY_REFUSALS = frozenset({"unavailable"})
"""Guest refusals of a passing fault (a file that changed while it was read, an error of the handler): postponed."""


def _ended(code: str, why: str, *, nothing: frozenset[str], first: bool) -> _Stop:
    """A request that ends for good: `nothing` to save (no complete turn, and the session never had a checkpoint,
    `first`), `impossible` on this VM for a refusal in `IMPOSSIBLE_REFUSALS`, else `dropped` (HOT-24, ADR-093
    addendum)."""
    if code in nothing and first:
        return _Stop(why, outcome="nothing")
    if code in IMPOSSIBLE_REFUSALS:
        return _Stop(why, outcome="impossible", reason=f"export_refused:{code}")
    return _Stop(why)


@dataclass
class _Sealed:
    hashes: list[bytes] = field(default_factory=list)
    size: int = 0


def _expired(refused: HubRefusal, *, commit: bool, started: bool) -> bool:
    """Whether a refusal means the hub expired an interrupted upload (see the module docstring)."""
    if refused.code == "not_found":
        return commit or started
    pointers = refused.detail.get("pointers")
    return (
        commit
        and refused.code == "request_invalid"
        and isinstance(pointers, list)
        and any(pointer in EXPIRED_POINTERS for pointer in pointers)
    )


class Checkpointer:
    def __init__(self, env: ContextEnv) -> None:
        self.env = env

    # -- one checkpoint ------------------------------------------------------------------------------------------

    async def take(self, due: DueSession) -> str:
        """`taken`, `identical`, `postponed`, `dropped` or `waiting` (see the module docstring)."""
        async with self.env.session_lock(due.session_id):
            try:
                stored = await self._checkpoint(due)
            except _Stop as stop:
                if stop.wait:
                    return "waiting"
                if stop.postpone:
                    return await self._postpone(due, stop.why)
                return await self._drop(due, stop.why, stop.outcome, stop.reason)
        if stored is None:
            return "identical"
        self.env.upload_wake.set()
        return "taken"

    async def _checkpoint(self, due: DueSession) -> StoredObject | None:
        """The new object, None for an unchanged archive; `_Stop` when no checkpoint is taken now."""
        env = self.env
        sid = due.session_id
        async with env.sessions.transaction() as conn:
            session = await records.get(conn, sid)
        if session is None or session.depth != 0:
            raise _Stop("session_gone")
        vm = None if session.vm_id is None else await vm_records.get(env.db, session.vm_id)
        if vm is None or vm.state == "deleted":
            raise _Stop("vm_gone")
        if vm.state != "running":
            raise _Stop("vm_not_running", wait=True)
        scratch = Path(tempfile.mkdtemp(dir=env.scratch, prefix="ctx-"))
        try:
            tar = scratch / "context.tar"
            await self._export(due, vm, tar)
            limits = archive.limits_for(env.transcript_bytes, env.workspace_bytes)
            try:
                found = await asyncio.to_thread(inspect_file, tar, limits)
            except archive.ArchiveInvalid as invalid:
                why = f"archive_invalid code={invalid.code}"
                first = await self._first(due)
                raise _ended(invalid.code, why, nothing=NOTHING_ARCHIVES, first=first) from None
            if found.session_id != sid or found.task_id != session.task_id:
                raise _Stop("archive_invalid code=context_invalid")
            if found.content_sha256 == due.archive_sha256:
                async with env.sessions.transaction() as conn:
                    await store.taken(
                        conn,
                        sid,
                        due_at=due.due_at,
                        sha256=found.content_sha256,
                        workspace=found.workspace_state,
                        at=env.now(),
                    )
                env.line(f"checkpoint_identical session_id={sid}")
                return None
            async with env.sessions.transaction() as conn:
                key = await current_key(conn, env.sessions.storage_key, session.wg_id)
            if key is None:
                raise _Stop("key_missing", postpone=True)
            stored = await asyncio.to_thread(self._seal, session, key, tar)
            async with env.sessions.transaction() as conn:
                await store.insert_object(
                    conn,
                    manifest=stored.manifest,
                    sig=stored.sig,
                    source="local",
                    state="sealed",
                    size=stored.bytes,
                    at=env.now(),
                )
                await store.taken(
                    conn,
                    sid,
                    due_at=due.due_at,
                    sha256=found.content_sha256,
                    workspace=found.workspace_state,
                    at=env.now(),
                )
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        env.line(
            f"checkpoint_taken session_id={sid} object_id={stored.object_id} chunks={stored.manifest['chunks']}"
            f" bytes={stored.bytes} workspace={found.workspace_state} omitted={found.workspace_omitted}"
        )
        return stored

    async def _export(self, due: DueSession, vm: Any, tar: Path) -> None:
        """Stream the guest's archive of the session into `tar`; `_Stop` when the guest refuses or fails."""
        env = self.env
        try:
            with tar.open("wb") as out:
                async with await env.sessions.client(vm) as guest:
                    await guest.export_context(
                        due.session_id,
                        out.write,
                        workspace_max_bytes=env.workspace_bytes,
                        transcript_max_bytes=env.transcript_bytes,
                    )
        except GuestRefused as refused:
            why = f"export_refused error={refused.error}"
            if refused.error in RETRY_REFUSALS:
                raise _Stop(why, postpone=True) from None
            first = await self._first(due)
            raise _ended(refused.error, why, nothing=NOTHING_REFUSALS, first=first) from None
        except GuestError as failed:
            raise _Stop(f"export_failed error={failed.code}", postpone=True) from None

    def _seal(self, session: records.LocalSession, key: tuple[int, bytes], tar: Path) -> StoredObject:
        """Compress, seal and cache the archive (runs in a worker thread)."""
        env = self.env
        epoch, workgroup_key = key
        object_id = uuid7()
        salt = os.urandom(SALT_BYTES)
        k_ctx = crypto.context_key(workgroup_key, salt, object_id)
        sealed = _Sealed()

        def emit(index: int, piece: bytes) -> None:
            chunk = crypto.seal_chunk(k_ctx, object_id, index, piece)
            env.cache.write_chunk(object_id, index, chunk)
            sealed.hashes.append(crypto.chunk_hash(chunk))
            sealed.size += len(chunk)

        try:
            with tar.open("rb") as source:
                chunks = archive.compress_chunks(source, emit)
        except BaseException:
            env.cache.remove(object_id)
            raise
        manifest = crypto.check_manifest(
            {
                "v": crypto.VERSION,
                "object": object_id,
                "org": session.org_id,
                "wg": session.wg_id,
                "task": session.task_id,
                "session": session.id,
                "author": env.device_id,
                "epoch": epoch,
                "salt": b64u_encode(salt),
                "chunks": chunks,
                "size": sealed.size,
                "digest": crypto.digest_of_hashes(sealed.hashes),
                "ts": env.sessions.at(),
            }
        )
        return StoredObject(
            object_id=object_id,
            task_id=session.task_id,
            session_id=session.id,
            org_id=session.org_id,
            wg_id=session.wg_id,
            source="local",
            state="sealed",
            manifest=manifest,
            sig=crypto.sign_manifest(env.signing_key, manifest),
            bytes=sealed.size,
            uploaded=0,
            attempts=0,
            created_at=env.now(),
        )

    async def _drop(self, due: DueSession, why: str, outcome: str = "dropped", reason: str | None = None) -> str:
        async with self.env.sessions.transaction() as conn:
            await store.drop_due(
                conn, due.session_id, due_at=due.due_at, at=self.env.now(), outcome=outcome, reason=reason
            )
        self.env.line(f"checkpoint_dropped session_id={due.session_id} {why} outcome={outcome}")
        return "dropped"

    async def _first(self, due: DueSession) -> bool:
        """Whether the session never had a checkpoint here (no archive hash and no cached object of it)."""
        if due.archive_sha256 is not None:
            return False
        async with self.env.sessions.transaction() as conn:
            return not await store.has_object(conn, due.session_id)

    async def _postpone(self, due: DueSession, why: str) -> str:
        if due.attempts + 1 >= MAX_ATTEMPTS:
            return await self._drop(due, f"{why} attempts={due.attempts + 1}")
        next_at = self.env.now() + timedelta(seconds=retry_delay(due.attempts))
        async with self.env.sessions.transaction() as conn:
            await store.postpone(conn, due.session_id, next_at=next_at)
        self.env.line(f"checkpoint_postponed session_id={due.session_id} {why}")
        return "postponed"

    async def _take_safely(self, item: DueSession) -> str:
        try:
            return await self.take(item)
        except Exception as exc:
            self.env.line(f"checkpoint_error session_id={item.session_id} error={type(exc).__name__}")
            with contextlib.suppress(Exception):
                await self._postpone(item, "error")
            return "error"

    async def take_all(self, dues: list[DueSession]) -> list[str]:
        """Take the checkpoints, at most `EXPORTS_AT_ONCE` at the same time; their results in order."""
        gate = asyncio.Semaphore(EXPORTS_AT_ONCE)

        async def one(item: DueSession) -> str:
            async with gate:
                return await self._take_safely(item)

        return list(await asyncio.gather(*(one(item) for item in dues)))

    # -- before a VM goes away -----------------------------------------------------------------------------------

    async def flush_vm(self, vm_id: str) -> list[str]:
        """Take the checkpoints of the VM's main sessions now (before it is stopped, deleted or replaced; the caller
        bounds the time); returns their results. The uploads follow in the upload loop."""
        env = self.env
        async with env.sessions.transaction() as conn:
            await store.mark_vm_due(conn, vm_id, env.now())
            dues = await store.due_on_vm(conn, vm_id)
        results = await self.take_all(dues)
        env.line(f"vm_flushed vm_id={vm_id} checkpoints={len(dues)}")
        return results

    async def vm_pending(self, vm_id: str) -> bool:
        """Whether a checkpoint of a session on the VM that worked is still due (a stopped VM is then not replaced; a
        session that never worked has nothing to save, HOT-24)."""
        async with self.env.sessions.transaction() as conn:
            return bool(await store.due_on_vm(conn, vm_id, worked=True))

    # -- upload --------------------------------------------------------------------------------------------------

    async def upload(self, obj: StoredObject) -> str:
        """`committed`, `retry`, `refused` or `expired` (see the module docstring)."""
        env = self.env
        oid = obj.object_id
        chunks = int(obj.manifest["chunks"])
        commit = False
        try:
            for index in range(obj.uploaded, chunks):
                data = await asyncio.to_thread(env.cache.read_chunk, oid, index)
                if data is None:
                    return await self._finish(obj, "refused", "upload_refused code=chunk_missing")
                body: dict[str, str] = {"session_id": obj.session_id, "ct": b64u_encode(data)}
                if obj.kind == "log":
                    body["kind"] = "log"
                await env.hub.request("PUT", f"/v1/device/contexts/{oid}/chunks/{index}", body)
                async with env.sessions.transaction() as conn:
                    await store.uploaded(conn, oid, index + 1)
            commit = True
            await env.hub.request(
                "POST", f"/v1/device/contexts/{oid}/commit", {"manifest": obj.manifest, "sig": obj.sig}
            )
        except HubTransportError as failed:
            return await self._retry(obj, failed.describe())
        except HubRefusal as refused:
            if refused.code in UPLOAD_RETRY_CODES:
                return await self._retry(obj, f"code={refused.code}")
            async with env.sessions.transaction() as conn:
                current = await store.get_object(conn, oid)
            started = (current.uploaded if current is not None else obj.uploaded) > 0
            if _expired(refused, commit=commit, started=started):
                return await self._expired(obj, refused.code)
            return await self._finish(obj, "refused", f"upload_refused code={refused.code}")
        return await self._finish(obj, "committed", "checkpoint_committed")

    async def _expired(self, obj: StoredObject, code: str) -> str:
        """The hub dropped the interrupted upload: the object stays local, the session's checkpoint is due again; a log
        (it cannot be taken again) is uploaded again from its first chunk under the same ID (its manifest is still
        valid)."""
        env = self.env
        if obj.kind == "log":
            async with env.sessions.transaction() as conn:
                await store.restart_upload(
                    conn, obj.object_id, env.now() + timedelta(seconds=retry_delay(obj.attempts))
                )
            env.line(f"upload_expired code={code} object_id={obj.object_id} session_id={obj.session_id} again=1")
            return "expired"
        async with env.sessions.transaction() as conn:
            await store.finish_object(conn, obj.object_id, "refused", env.now())
            await store.mark_due(
                conn,
                session_id=obj.session_id,
                task_id=obj.task_id,
                org_id=obj.org_id,
                wg_id=obj.wg_id,
                at=env.now(),
                forget=True,
            )
        env.wake.set()
        env.line(f"upload_expired code={code} object_id={obj.object_id} session_id={obj.session_id}")
        return "expired"

    async def _finish(self, obj: StoredObject, state: str, line: str) -> str:
        async with self.env.sessions.transaction() as conn:
            await store.finish_object(conn, obj.object_id, state, self.env.now())
        self.env.line(f"{line} object_id={obj.object_id} session_id={obj.session_id}")
        return state

    async def _retry(self, obj: StoredObject, why: str) -> str:
        next_at = self.env.now() + timedelta(seconds=retry_delay(obj.attempts))
        async with self.env.sessions.transaction() as conn:
            await store.retry_upload(conn, obj.object_id, next_at)
        self.env.line(f"upload_retry object_id={obj.object_id} {why}")
        return "retry"

    async def upload_round(self) -> int:
        """Upload every pending object whose backoff passed; returns their number."""
        env = self.env
        async with env.sessions.transaction() as conn:
            pending = await store.uploads(conn, env.now())
        for obj in pending:
            await self.upload(obj)
        if pending:
            await self.evict()
        return len(pending)

    # -- cache ---------------------------------------------------------------------------------------------------

    async def evict(self) -> list[str]:
        """Remove cached objects (see the module docstring); returns their IDs."""
        env = self.env
        async with env.sessions.transaction() as conn:
            rows = await store.cache_rows(conn)
        newest: set[str] = set()
        seen: set[str] = set()
        removed: list[str] = []
        kept: list[StoredObject] = []
        for row in rows:  # newest first
            if row.kind == "log":  # a log leaves the cache once the hub holds it or refused it for good
                if row.state != "sealed":
                    removed.append(row.object_id)
                else:
                    newest.add(row.object_id)  # never evicted before the hub has it
                    kept.append(row)
                continue
            if row.task_id not in seen:
                seen.add(row.task_id)
                newest.add(row.object_id)
            elif row.state != "sealed":
                removed.append(row.object_id)
                continue
            kept.append(row)
        total = sum(row.bytes for row in kept)
        for row in reversed(kept):  # oldest first
            if total <= env.cache_bytes:
                break
            if row.object_id in newest:
                continue
            removed.append(row.object_id)
            total -= row.bytes
        if total > env.cache_bytes:
            env.line(f"cache_full bytes={total} limit={env.cache_bytes}")
        for object_id in removed:
            async with env.sessions.transaction() as conn:
                await store.delete_object(conn, object_id)
            await asyncio.to_thread(env.cache.remove, object_id)
        return removed

    # -- loop ----------------------------------------------------------------------------------------------------

    async def round(self) -> int:
        """One export pass: periodic dues, then the due checkpoints. Returns their number."""
        env = self.env
        async with env.sessions.transaction() as conn:
            await store.mark_periodic(conn, interval_s=env.interval_s, at=env.now())
            dues = await store.due(conn, env.now())
        await self.take_all(dues)
        if dues:
            await self.evict()
        return len(dues)
