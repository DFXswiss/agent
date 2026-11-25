"""Restore of the original conversation into a new sandbox, and the reported emergency path (ADR-072 section 7;
decisions/KOR-33.md, review KOR-33 nos. 1, 3, 6, 10, 16-18, 20 and 22). Replaces the `restart_hint` of RUN-05
(decisions/RUN-05.md no. 20).

`amend(session, request)` is the request amender of every main-session start (and of a resume whose VM changed,
`prompts.resume.Resumer`):

1. **The final checkpoint first** (review no. 1). The session continues a *source*: the hub's `resume_from` (the
   task's previous main session), or the session itself when it is resumed on another VM. When the source ran on
   this device, the restore waits - polling every `LOCAL_STEP_S`, at most `ContextEnv.wait_s` (180 s) - until the
   source has reached a settled state (`idle`, `suspended`, `stopped`, `failed`; for the session itself any state)
   and none of its checkpoints is due any more (taken, identical or given up), or until its VM no longer runs; a
   sealed local object is enough, the upload is not awaited. When the source ran on another device, the restore
   reads `GET /v1/device/state` every `HUB_STEP_S` until the hub names a checkpoint of the source (`context`),
   at most `wait_s`. Either way it then continues with the newest checkpoint it has; when the wait ran out it is
   `stale`, which the harness sentence and the audit say. **After a device failure** (ADR-087 section 3.5) the
   desired state names the session in `failovers {session_id, from_session, cancelled_runs}`: when `from_session` is
   the source, nothing is waited for (the failed device never sends that final checkpoint) and the hub's `context`
   (the checkpoint it pinned at the move) and the local cache are ordered as below. A malformed `failovers` entry is
   ignored (the restore then waits as for any source of another device).
2. **Which checkpoint** (review nos. 6, 10): every cached object of the task (refused ones too: their local copy is
   complete) and the hub's `context {object_id, session_id, created_at, size}`, ordered like the hub orders them
   (`agent_hub.contexts.store`): the session first - the session itself, then the source, then the others by the
   creation time of their session (known for this device's sessions; for another device's session the object's own
   time stands in) -, within a session the newest object (UUIDv7). Without any, a session that continues earlier
   work of the task (`resume_from`, earlier prompts of the task, a resume) takes the emergency path with
   `context_missing`; a first session starts as before.
3. `fetch_verified`: manifest and chunks from the cache, else from the hub (`GET /v1/device/contexts/{object}` and
   `.../chunks/{i}`). A hub read that fails for a transient reason (transport, `unavailable`, `rate_limited`) is
   retried with backoff (`FETCH_BACKOFF_S`, doubling, at most `FETCH_BACKOFF_CAP_S`) until `wait_s` from the start
   of the restore; `not_found` or any other refusal ends it at once (review no. 3; the residual risk - a hub outage
   longer than `wait_s` starts the session fresh, and its later checkpoints supersede the old context - is
   documented in decisions/KOR-33.md). Checks of ADR-072 section 3: the signature with the author device's key from
   the verified membership log - the author must have been a device assigned to the workgroup at the manifest's
   epoch (a log state of that epoch, review no. 17) -, `object`/`org`/`wg`/`task` equal the restoring session's,
   chunk count, total size and digest, every chunk opens (AEAD) with `k_ctx` of the manifest's epoch key; the gzip
   stream decompresses within `archive.archive_bytes` into a 0700 scratch file, passes `archive.inspect` and its
   `context.json` names the task and the manifest's session. Decryption, decompression and the archive checks run in
   worker threads. A cached checkpoint that fails these checks is read again from the hub when the hub holds it
   (review no. 18). A checkpoint whose restore made an agent start fail on this device before
   (`store.restore_failed`) is not used again (`restore_failed`).
4. `compatible`: `agent_kind` must be Claude Code and the target VM's Claude Code at least the highest `version` of
   the transcript lines (spike KOR-33).
5. `POST /v1/contexts/{session}/stage` to the session's VM (its own timeout: the guest does the heavy rewriting there,
   not in the create, review no. 9), then the request gets `restore {claude_session_id, object_id, note}`: `note` is the
   one sentence about what changed (`restored_line`: same VM, another VM of this device, the new version of a VM
   switched to a newer golden (ADR-093) or a VM of another device - review no. 22 -, the checkpoint time, the workspace
   state and what was left out, `stale`, the work VM; after a device failure the failover sentence: the device it ran on
   failed (or, on the same device, it was restarted following a failure), work after the checkpoint may be lost and the
   interrupted instruction is repeated, how many test runs were cancelled there). It is not put into the harness
   context: Claude Code keeps the system prompt of a conversation in its transcript and ignores
   `--append-system-prompt-file` on `--resume`, so a resumed model never reads the harness file. The guest attaches the
   note with the current session facts once to the first hook answer of the resumed conversation
   (`agent_guest.hook_addons.notice`). The audit `session.context_restored {object_id, workspace, stale, moved?}`
   (`workspace` = `full`, `partial`, `omitted` or `empty`; `moved: "golden"` after a switch of the session's VM to a
   newer golden, ADR-093 section 4) is only recorded here and sent by the state observer once the guest reports
   `agent_starting` (review no. 16). A fetched checkpoint stays in the cache (`fetched`).

Any failure takes the emergency path (`fallback`): the earlier `RESTART_LINE` (read the task's messages) and the audit
row `session.context_fallback {reason}` - `context_missing`, `context_invalid`, `context_unavailable` (hub or cache
not reachable, key of the epoch missing), `version_incompatible`, `restore_failed` (the guest refused the stage or an
earlier start from this checkpoint failed). Never silent.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, cast

from agent_runner.contexts import archive, store
from agent_runner.contexts.env import ContextEnv, audit_body
from agent_runner.contexts.store import StoredObject
from agent_runner.crypto import context as crypto
from agent_runner.crypto.encoding import b64u_decode, parse_time
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.kdf import SALT_BYTES
from agent_runner.guestlink.errors import GuestError
from agent_runner.orgs.keys import load_scope_key
from agent_runner.prompts.resume import RESTART_LINE, listed, restarted
from agent_runner.reconcile import wire
from agent_runner.sessions import records
from agent_runner.sessions.failover import Failover, failover_of
from agent_runner.sessions.records import LocalSession
from agent_runner.sync.http import HubRefusal, HubTransportError
from agent_runner.vm import records as vm_records

FETCH_BACKOFF_S = 1.0
FETCH_BACKOFF_CAP_S = 15.0
LOCAL_STEP_S = 1.0
HUB_STEP_S = 5.0
READ_BYTES = 1024 * 1024
TRANSIENT_CODES = frozenset({"unavailable", "rate_limited"})
SETTLED_STATES = frozenset({"idle", "suspended", "stopped", "failed"})
FAILED_START_REASONS = frozenset({"agent_start_failed", "sandbox_failed"})
STARTED_STATES = frozenset({"working", "idle", "suspended"})
RESTORED = "session.context_restored"
FALLBACK = "session.context_fallback"
STATE_TARGET = "/v1/device/state"
REASONS = frozenset(
    {"context_missing", "context_invalid", "context_unavailable", "version_incompatible", "restore_failed"}
)
OMITTED_KINDS = "protected names such as .git or AGENTS.md, links leaving the workspace, special or unreadable files"
STALE_TEXT = (
    " The final checkpoint of the session before was not available in time: its last steps may be missing from this"
    " conversation and from the workspace."
)
_EARLIEST = datetime.min.replace(tzinfo=UTC)
WORK_VM_TEXT = " A work VM you used before does not exist any more: request a new one when you need it."


class RestoreFailed(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Ref:
    """The hub's newest committed checkpoint of the task (desired entry `context`)."""

    object_id: str
    created_at: str
    session_id: str | None = None


@dataclass(frozen=True)
class Restored:
    object_id: str
    context: archive.Context
    manifest: Mapping[str, Any]


@dataclass(frozen=True)
class Candidate:
    """A checkpoint the restore may use: its order key (see the module docstring) and the cached row, if any."""

    object_id: str
    key: tuple[int, datetime, str, str]
    cached: StoredObject | None


def parse_ref(value: object) -> Ref | None:
    """The desired entry's `context`; None when absent, `wire.ResponseInvalid` when malformed."""
    if value is None:
        return None
    data = wire.obj(value, "sessions")
    created = data.get("created_at")
    if not isinstance(created, str) or not 0 < len(created) <= 64:
        raise wire.ResponseInvalid("sessions")
    session = data.get("session_id")
    return Ref(
        object_id=wire.uuid_text(data.get("object_id"), "sessions"),
        created_at=created,
        session_id=None if session is None else wire.uuid_text(session, "sessions"),
    )


def _moment(text: object) -> datetime:
    try:
        return parse_time(str(text))
    except CryptoError:
        return _EARLIEST


def order_key(
    *, object_id: str, session_id: str | None, own: str, source: str | None, session_time: datetime
) -> tuple[int, datetime, str, str]:
    """The order of the module docstring: own session, source, then by session time; then by object ID."""
    rank = 2 if session_id == own else 1 if source is not None and session_id == source else 0
    return rank, session_time, session_id or "", object_id


def choose(candidates: list[Candidate]) -> Candidate | None:
    """The newest candidate (see the module docstring); a cached row wins over the hub's reference of the same
    object."""
    best: Candidate | None = None
    for item in candidates:
        if best is None or item.key > best.key or (item.key == best.key and item.cached is not None):
            best = item
    return best


def compatible(context: archive.Context, target_version: str | None) -> bool:
    """The version rule (spike KOR-33, decisions/KOR-33.md): the same agent kind, and the target VM's Claude Code is
    at least the highest version written into the transcript (an older one may not read newer entries; older and
    newer versions otherwise resume each other). An unknown target version or a transcript without versions does
    not block the restore."""
    if context.agent_kind != archive.AGENT_KIND:
        return False
    target = archive.version_key(target_version)
    return context.max_version is None or target is None or target >= context.max_version


def workspace_text(context: archive.Context) -> str:
    state = context.workspace_state
    if state == "partial":
        count = context.workspace_omitted
        noun = "entry" if count == 1 else "entries"
        return f"your workspace was restored, but {count} {noun} of it could not be saved ({OMITTED_KINDS})"
    return {
        "full": "your workspace was restored as it was",
        "omitted": "your workspace was too large to be saved and starts empty",
        "empty": "your workspace was empty",
    }[state]


def cancelled_text(count: int) -> str:
    """The cancelled test runs of the failed device (empty when none)."""
    if count == 0:
        return ""
    if count == 1:
        return " 1 test run that was running there was cancelled: start it again when you need its result."
    return f" {count} test runs that were running there were cancelled: start them again when you need their results."


def restored_line(restored: Restored, *, where: str, stale: bool, failover: Failover | None = None) -> str:
    """The one sentence about what changed (`restore.note` of the create request); `where` is `same_vm`, `other_vm`,
    `golden` (the session's VM was switched to a newer golden, ADR-093) or `other_device`; `failover` gives the
    variant after a device failure (ADR-087 sections 3.5 and 3.7: another device, or this one after its restart;
    nothing was waited for, so it is never `stale`)."""
    if failover is not None:
        moved = (
            "on a VM of another device because the device it ran on failed"
            if where == "other_device"
            else "on this device after it was restarted following a failure"
        )
        return (
            f"This is your own earlier conversation of this task, continued {moved}, from its checkpoint of"
            f" {restored.manifest['ts']};"
            f" {workspace_text(restored.context)}. Work after that checkpoint may be lost: the interrupted instruction"
            f" is repeated next.{cancelled_text(failover.cancelled_runs)}{WORK_VM_TEXT}"
        )
    if where == "golden":
        # the VM was replaced by one of the newest golden while the session was paused (ADR-093 section 4); its work
        # VMs are separate and still there
        return (
            "This is your own earlier conversation of this task, continued from its checkpoint of"
            f" {restored.manifest['ts']} on the new version of the workgroup VM (it was updated while the session was"
            f" paused); your files were carried over: {workspace_text(restored.context)}.{STALE_TEXT if stale else ''}"
        )
    place = {
        "same_vm": "in a new sandbox on the same VM",
        "other_vm": "on another VM of this device",
        "other_device": "on a VM of another device",
    }[where]
    return (
        "This is your own earlier conversation of this task, continued from its checkpoint of"
        f" {restored.manifest['ts']} {place}; {workspace_text(restored.context)}.{WORK_VM_TEXT}"
        f"{STALE_TEXT if stale else ''}"
    )


class Restorer:
    def __init__(self, env: ContextEnv) -> None:
        self.env = env
        self.deadline = 0.0

    # -- the amender ---------------------------------------------------------------------------------------------

    async def amend(self, session: LocalSession, request: dict[str, Any]) -> str | None:
        """Restore into the request or take the emergency path; returns `restored`, the fallback reason or None (a
        first session without checkpoint)."""
        if session.depth != 0:
            return None
        env = self.env
        self.deadline = env.clock() + env.wait_s
        entry = listed(env.sessions, session.id)
        try:
            ref = parse_ref(None if entry is None else entry.get("context"))
        except wire.ResponseInvalid:
            return await self.fallback(session, request, "context_invalid")
        source = _source(session, entry)
        failover = failover_of(env.sessions.desired, session.id)
        if failover is not None and failover.from_session != source:
            failover = None
        stale = False
        if source is not None and failover is None:
            ref, stale = await self.wait_final(session, source, ref)
        chosen = choose(await self.candidates(session, source, ref))
        if chosen is None:
            earlier = session.state == "resuming" or await restarted(env.sessions, session, request)
            return await self.fallback(session, request, "context_missing") if earlier else None
        object_id = chosen.object_id
        try:
            restored = await self.restore_with_retry(session, chosen, ref)
        except RestoreFailed as failed:
            return await self.fallback(session, request, failed.reason)
        request.pop("resume_claude_session_id", None)
        where = await self._where(session, restored)
        request["restore"] = {
            "claude_session_id": restored.context.claude_session_id,
            "object_id": object_id,
            "note": restored_line(restored, where=where, stale=stale, failover=failover),
        }
        detail: dict[str, Any] = {"object_id": object_id, "workspace": restored.context.workspace_state, "stale": stale}
        if where == "golden" and failover is None and "vm_switch" in (env.sessions.desired or {}):
            detail["moved"] = "golden"  # only to a hub that knows the field (it names `vm_switch`, ADR-093 section 4)
        async with env.sessions.transaction() as conn:
            await store.set_restored(
                conn,
                session_id=session.id,
                task_id=session.task_id,
                org_id=session.org_id,
                wg_id=session.wg_id,
                object_id=object_id,
                audit=detail,
            )
        env.line(
            f"context_restored session_id={session.id} object_id={object_id}"
            f" workspace={detail['workspace']} stale={str(stale).lower()} failover={str(failover is not None).lower()}"
        )
        return "restored"

    async def _where(self, session: LocalSession, restored: Restored) -> str:
        if restored.manifest["author"] != self.env.device_id:
            return "other_device"
        async with self.env.sessions.transaction() as conn:
            origin = await records.get(conn, str(restored.manifest["session"]))
        if origin is not None and origin.vm_id == session.vm_id:
            return "same_vm"
        if origin is not None and origin.vm_id is not None and await vm_records.switched_to(self.env.db, origin.vm_id):
            return "golden"
        return "other_vm"

    @staticmethod
    def _add_line(request: dict[str, Any], line: str) -> None:
        harness = request["harness"]
        harness["context_markdown"] = str(harness["context_markdown"]).rstrip("\n") + "\n" + line

    async def fallback(self, session: LocalSession, request: dict[str, Any], reason: str) -> str:
        """The reported emergency path: a fresh session told to read the task's messages."""
        env = self.env
        self._add_line(request, RESTART_LINE.format(task_id=session.task_id))
        body = audit_body(
            action=FALLBACK, result="failed", task_id=session.task_id, session_id=session.id, detail={"reason": reason}
        )
        async with env.sessions.transaction() as conn:
            await env.sessions.outbox.enqueue(conn, "audit", body)
        env.line(f"context_fallback session_id={session.id} reason={reason}")
        return reason

    # -- waiting for the final checkpoint ------------------------------------------------------------------------

    async def wait_final(self, session: LocalSession, source: str, ref: Ref | None) -> tuple[Ref | None, bool]:
        """Wait for the final checkpoint of `source` (see the module docstring); the hub reference to use and
        whether the wait ran out (`stale`)."""
        env = self.env
        async with env.sessions.transaction() as conn:
            local = await records.get(conn, source)
        if local is None:
            return await self._wait_hub(session, source, ref)
        while not await self._settled(local, own=source == session.id):
            if env.clock() >= self.deadline:
                env.line(f"context_wait_timeout session_id={session.id} source={source}")
                return ref, True
            await env.sleep(LOCAL_STEP_S)
            async with env.sessions.transaction() as conn:
                local = await records.get(conn, source) or local
        return ref, False

    async def _settled(self, source: LocalSession, *, own: bool) -> bool:
        if not own and source.state not in SETTLED_STATES:
            return await self._vm_down(source)
        async with self.env.sessions.transaction() as conn:
            due = await store.pending(conn, source.id)
        return not due or await self._vm_down(source)

    async def _vm_down(self, source: LocalSession) -> bool:
        """The source's VM does not run (a due checkpoint of it cannot be taken now)."""
        vm = None if source.vm_id is None else await vm_records.get(self.env.db, source.vm_id)
        return vm is None or vm.state != "running"

    async def _wait_hub(self, session: LocalSession, source: str, ref: Ref | None) -> tuple[Ref | None, bool]:
        env = self.env
        while ref is None or ref.session_id != source:
            if env.clock() >= self.deadline:
                env.line(f"context_wait_timeout session_id={session.id} source={source}")
                return ref, True
            await env.sleep(HUB_STEP_S)
            try:
                answer = await env.hub.request("GET", STATE_TARGET)
                found = _entry(answer, session.id)
                ref = parse_ref(None if found is None else found.get("context")) or ref
            except (HubRefusal, HubTransportError, wire.ResponseInvalid) as failed:
                env.line(f"context_wait_read_failed session_id={session.id} error={type(failed).__name__}")
        return ref, False

    # -- candidates ----------------------------------------------------------------------------------------------

    async def candidates(self, session: LocalSession, source: str | None, ref: Ref | None) -> list[Candidate]:
        env = self.env
        async with env.sessions.transaction() as conn:
            cached = await store.objects_of_task(conn, session.task_id)
            ref_session = None if ref is None or ref.session_id is None else await records.get(conn, ref.session_id)
        found = [
            Candidate(
                object_id=row.object_id,
                key=order_key(
                    object_id=row.object_id,
                    session_id=row.session_id,
                    own=session.id,
                    source=source,
                    session_time=row.session_created_at or _moment(row.manifest.get("ts")),
                ),
                cached=row,
            )
            for row in cached
        ]
        if ref is not None:
            time = ref_session.created_at if ref_session is not None else _moment(ref.created_at)
            key = order_key(
                object_id=ref.object_id, session_id=ref.session_id, own=session.id, source=source, session_time=time
            )
            found.append(Candidate(object_id=ref.object_id, key=key, cached=None))
        return found

    # -- restore -------------------------------------------------------------------------------------------------

    async def restore_with_retry(self, session: LocalSession, chosen: Candidate, ref: Ref | None) -> Restored:
        """`restore`; a cached copy that fails its checks is read from the hub once more when the hub holds it."""
        try:
            return await self.restore(session, chosen.object_id, chosen.cached)
        except RestoreFailed as failed:
            cached = chosen.cached
            at_hub = cached is not None and (
                cached.state == "committed" or (ref is not None and ref.object_id == cached.object_id)
            )
            if not at_hub or failed.reason not in ("context_invalid", "context_unavailable"):
                raise
            self.env.line(f"cache_unusable object_id={chosen.object_id} reason={failed.reason}")
            return await self.restore(session, chosen.object_id, None)

    async def restore(self, session: LocalSession, object_id: str, cached: StoredObject | None) -> Restored:
        env = self.env
        async with env.sessions.transaction() as conn:
            if await store.restore_failed(conn, session.task_id, object_id):
                raise RestoreFailed("restore_failed")
        scratch = Path(tempfile.mkdtemp(dir=env.scratch, prefix="ctx-"))
        try:
            tar = scratch / "context.tar"
            manifest, fetched = await self.fetch_verified(session, object_id, cached, tar)
            try:
                context = await asyncio.to_thread(_inspect, tar, self._limits())
            except archive.ArchiveInvalid:
                raise RestoreFailed("context_invalid") from None
            if context.task_id != session.task_id or context.session_id != manifest["session"]:
                raise RestoreFailed("context_invalid")
            async with env.sessions.transaction() as conn:
                target = await records.agent_version(conn, session.vm_id)
            if not compatible(context, target):
                raise RestoreFailed("version_incompatible")
            await self._stage(session, tar)
            if fetched is not None:
                async with env.sessions.transaction() as conn:
                    await store.insert_object(
                        conn,
                        manifest=manifest,
                        sig=fetched,
                        source="fetched",
                        state="committed",
                        size=cast(int, manifest["size"]),
                        at=env.now(),
                    )
        except BaseException:
            if cached is None:
                async with env.sessions.transaction() as conn:
                    known = await store.get_object(conn, object_id)
                if known is None:
                    env.cache.remove(object_id)
            raise
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        return Restored(object_id=object_id, context=context, manifest=manifest)

    def _limits(self) -> Any:
        return archive.limits_for(self.env.transcript_bytes, self.env.workspace_bytes)

    async def _stage(self, session: LocalSession, tar: Path) -> None:
        env = self.env
        vm = None if session.vm_id is None else await vm_records.get(env.db, session.vm_id)
        if vm is None:
            raise RestoreFailed("restore_failed")
        size = tar.stat().st_size

        async def body() -> AsyncIterator[bytes]:
            with tar.open("rb") as source:
                while data := await asyncio.to_thread(source.read, READ_BYTES):
                    yield data

        try:
            async with await env.sessions.client(vm) as guest:
                await guest.stage_context(session.id, body(), size)
        except GuestError as refused:
            env.line(f"stage_refused session_id={session.id} error={refused.code}")
            raise RestoreFailed("restore_failed") from None

    async def _hub(self, target: str) -> dict[str, object]:
        """A hub read, retried while the failure is transient and `wait_s` has not passed (see the module
        docstring); `context_unavailable` when it does not succeed."""
        env = self.env
        delay = FETCH_BACKOFF_S
        while True:
            try:
                return await env.hub.request("GET", target)
            except HubRefusal as refused:
                env.line(f"fetch_refused target={target} code={refused.code}")
                if refused.code not in TRANSIENT_CODES:
                    break
            except HubTransportError as failed:
                env.line(f"fetch_failed target={target} {failed.describe()}")
            if env.clock() + delay > self.deadline:
                break
            await env.sleep(delay)
            delay = min(FETCH_BACKOFF_CAP_S, delay * 2)
        raise RestoreFailed("context_unavailable")

    def _author_key(self, session: LocalSession, author: object, epoch: object) -> bytes:
        """The signing key of the author device, which must have been assigned to the workgroup in a log state of the
        manifest's epoch (review no. 17); an epoch newer than the verified log is `context_unavailable` (the log may
        lag behind), anything else `context_invalid`."""
        book = self.env.sessions.book
        log = book.log(session.org_id)
        if log is None or not isinstance(author, str) or author in book.excluded(session.org_id):
            raise RestoreFailed("context_invalid")
        if isinstance(epoch, int) and epoch > log.states[-1].epoch("wg", session.wg_id):
            raise RestoreFailed("context_unavailable")  # an epoch this device's log does not know yet
        of_epoch = (state for state in reversed(log.states) if state.epoch("wg", session.wg_id) == epoch)
        for state in of_epoch:
            principal = state.principals.get(author)
            if principal is not None and principal.kind == "device" and state.assigned(author, session.wg_id):
                return b64u_decode(principal.sig, 32)
        raise RestoreFailed("context_invalid")

    async def fetch_verified(
        self, session: LocalSession, object_id: str, cached: StoredObject | None, tar: Path
    ) -> tuple[dict[str, Any], str | None]:
        """Download (or read) and verify the checkpoint into `tar`; the manifest and - for a download - its
        signature (the caller caches the object once it was used)."""
        env = self.env
        if cached is not None:
            raw_manifest: object = cached.manifest
            sig: object = cached.sig
        else:
            answer = await self._hub(f"/v1/device/contexts/{object_id}")
            raw_manifest, sig = answer.get("manifest"), answer.get("sig")
        try:
            author, epoch = _field(raw_manifest, "author"), _field(raw_manifest, "epoch")
            manifest = crypto.verify_manifest(raw_manifest, sig, self._author_key(session, author, epoch))
        except CryptoError:
            raise RestoreFailed("context_invalid") from None
        ids = (manifest["object"], manifest["org"], manifest["wg"], manifest["task"])
        if ids != (object_id, session.org_id, session.wg_id, session.task_id):
            raise RestoreFailed("context_invalid")
        epoch_number = cast(int, manifest["epoch"])
        async with env.sessions.transaction() as conn:
            key = await load_scope_key(conn, env.sessions.storage_key, "wg", session.wg_id, epoch_number)
        if key is None:
            raise RestoreFailed("context_unavailable")
        k_ctx = crypto.context_key(key, b64u_decode(manifest["salt"], SALT_BYTES), object_id)
        hashes: list[bytes] = []
        size = 0
        with tar.open("wb") as out:
            inflater = archive.Inflater(out, archive.archive_bytes(self._limits()))
            for index in range(cast(int, manifest["chunks"])):
                chunk = await self._chunk(object_id, index, cached is None)
                hashes.append(crypto.chunk_hash(chunk))
                size += len(chunk)
                if size > cast(int, manifest["size"]):
                    raise RestoreFailed("context_invalid")
                try:
                    await asyncio.to_thread(_open_into, inflater, k_ctx, object_id, index, chunk)
                except (CryptoError, archive.ArchiveInvalid):
                    raise RestoreFailed("context_invalid") from None
            if size != manifest["size"] or crypto.digest_of_hashes(hashes) != manifest["digest"]:
                raise RestoreFailed("context_invalid")
            try:
                inflater.finish()
            except archive.ArchiveInvalid:
                raise RestoreFailed("context_invalid") from None
        return manifest, None if cached is not None else str(sig)

    async def _chunk(self, object_id: str, index: int, download: bool) -> bytes:
        env = self.env
        found = None if download else await asyncio.to_thread(env.cache.read_chunk, object_id, index)
        if found is not None:
            return found
        answer = await self._hub(f"/v1/device/contexts/{object_id}/chunks/{index}")
        try:
            data = b64u_decode(answer.get("ct"))
        except CryptoError:
            raise RestoreFailed("context_invalid") from None
        if download:
            await asyncio.to_thread(env.cache.write_chunk, object_id, index, data)
        return data


def _inspect(path: Path, limits: Any) -> archive.Context:
    with path.open("rb") as source:
        return archive.inspect(cast(BinaryIO, source), limits)


def _open_into(inflater: archive.Inflater, k_ctx: bytes, object_id: str, index: int, chunk: bytes) -> None:
    inflater.feed(crypto.open_chunk(k_ctx, object_id, index, chunk))


def _field(manifest: object, name: str) -> object:
    return manifest.get(name) if isinstance(manifest, Mapping) else None


def _entry(answer: Mapping[str, object], session_id: str) -> Mapping[str, Any] | None:
    sessions = answer.get("sessions")
    for item in sessions if isinstance(sessions, list) else ():
        if isinstance(item, dict) and item.get("session_id") == session_id:
            return item
    return None


def _source(session: LocalSession, entry: Mapping[str, Any] | None) -> str | None:
    """The session whose conversation this one continues (see the module docstring)."""
    if session.state == "resuming":
        return session.id
    found = None if entry is None else entry.get("resume_from")
    return found if isinstance(found, str) else None


async def observe(conn: Any, outbox: Any, session: LocalSession, body: Mapping[str, Any]) -> str | None:
    """The restore part of a guest `session.state` record of a main session (`session_state.py`): `audited` when the
    restored agent starts (its `session.context_restored` row is sent), `started` once it ran, `failed` when it
    failed inside its start window (that checkpoint is not tried again on this device); None otherwise."""
    state = body.get("state")
    if state == "agent_starting":
        detail = await store.take_restore_audit(conn, session.id)
        if detail is None:
            return None
        audit = audit_body(action=RESTORED, result="ok", task_id=session.task_id, session_id=session.id, detail=detail)
        await outbox.enqueue(conn, "audit", audit)
        return "audited"
    if state in STARTED_STATES:
        await store.mark_restore_started(conn, session.id)
        return "started"
    if state == "failed" and body.get("reason") in FAILED_START_REASONS:
        return "failed" if await store.mark_restore_failed(conn, session.id) else None
    return None
