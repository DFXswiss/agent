"""Context checkpoints and pull-request links at the hub (ADR-072 sections 3, 5, 6 and 8; 03-api.md sections 4.6, 6.1).

**Upload** (`PUT /v1/device/contexts/{object}/chunks/{index}` `{session_id, ct}`, `device:session`): the session must
exist (`404 not_found`), run or have run on the calling device and lie in a workgroup the device is assigned to
(`403 forbidden`, rule E7 of events), and be a main session (`400 request_invalid` at `/session_id`; subagents are
not checkpointed). The first chunk creates the object (`uploading`) for that session and device; another session or
device under the same object ID is `409 id_conflict`. A chunk is stored once: the identical bytes again answer
`replayed: true`, other bytes at a stored index (or any chunk after the commit) `409 id_conflict`. A sealed chunk is
at least 29 bytes (`400 request_invalid` at `/ct`) and at most `CHUNK_CT_BYTES` (`413 size_exceeded`). Quotas
(`413 quota_exceeded` with `detail.quota {scope, limit, used}`): the sealed bytes of one object stay within
`AGENT_HUB_CONTEXT_MAX_OBJECT_BYTES`, those of every checkpoint of the organisation within
`AGENT_HUB_CONTEXT_ORG_BYTES` (checked under a per-organisation advisory lock, so parallel uploads cannot overshoot).
A refusal for a quota is recorded: the route writes the audit row `session.context_refused {reason, scope, limit, used,
requested}` (actor the device, result `denied`) in a transaction of its own, after the refused one was rolled back.

**Commit** (`POST /v1/device/contexts/{object}/commit` `{manifest, sig}`, same rights; exactly ADR-072 section 3): the
object must exist (`404`) and belong to the calling device (`403`); the manifest has the format of
`agent_hub.crypto.context` (`400 header_invalid`/`non_ascii`) and names exactly the object's IDs (`object`, `org`,
`wg`, `task`, `session`, `author` = the device) and an epoch of the workgroup (`400 request_invalid` with the
pointer); the signature verifies with the device's Ed25519 key (`401 signature_invalid`); `chunks` equals the number
of stored chunks with the indexes `0..chunks-1`, `size` their sealed bytes and `digest` the digest over their stored
SHA-256 in index order (`400 request_invalid` at the field). The object becomes `complete`; the same manifest and
signature again answer `replayed: true`, anything else after the commit is `409 id_conflict`. Complete checkpoints of
the task beyond the newest `AGENT_HUB_CONTEXT_KEEP` (never the one just committed) are deleted in the same
transaction, one commit of the task at a time, and the audit row `session.context_saved {chunks, size, pruned}` names
the device.

**Newest first** means: ordered by the creation time of the checkpoint's session at the hub, then by the object ID
(UUIDv7, chosen by the session's own device), both descending - never by a device clock or by the time of the commit.
A checkpoint of an earlier session that a device uploads late (it was offline while the task went on elsewhere) never
supersedes or displaces those of its successor. The same order lists a task's checkpoints, picks the `context` of the
device state and decides what the pruning keeps.

**Download** (`GET /v1/device/contexts/{object}` and `…/chunks/{index}`, `device:wg`): a complete checkpoint of a
workgroup the device is assigned to - a restore may run on any device of the workgroup; anything else (a log too) is
`404`.

**Logs** (KOR-43, ADR-080 section 3): an object whose first chunk names `kind: "log"` is the output of a test run or
of a long work VM job of a session (a subagent's too), sealed like a checkpoint. It is never listed as a checkpoint,
never chosen as the `context` of the device state, and pruned on its own: complete logs of the task beyond the newest
`AGENT_HUB_LOG_KEEP` (by creation time, then ID) go at the commit. A later chunk of another kind than the object's is
`409 id_conflict`. Members with `task:read` read a complete log of the task (`task_log`, `task_log_chunk`); the hub
holds no key.

**Pull-request links** (`POST /v1/device/pr-links` `{session_id, epoch, tag, sealed?}`, `device:session` as above):
the tag is stored for the session's workgroup and an epoch of it (`400 request_invalid` at `/epoch`) with the sealed
body when the runner sends one (04-crypto.md section 4.11a; NULL from an older runner); the same tag for the same task
again is `replayed`, for another task `409 id_conflict` (the first task keeps the link). A tag a person linked before
is **replaced** by the device's link (the creating task is the true origin, ADR-084 section 1): the row takes the
device's task, session and body, loses `linked_by` and gets the new time, audited `pr.linked {epoch, replaced: true}`;
a new device link is audited `session.pr_linked {epoch}` as before.

A person links a pull request by its address (`link_by_user`, `wg:write` checked by the route) with
`{epoch, tag, sealed, task_id}`: the task must lie in the workgroup (`400 request_invalid` at `/task_id`) and the
epoch must be the workgroup's current one (`400 epoch_stale`), so a client never adds a link under a key the
workgroup has rotated away from. A new row answers `created: true` (audit `pr.linked {epoch, replaced: false}`, actor
the user); a tag linked already under that epoch answers `created: false` with the existing row, whoever made it -
an existing link is never moved silently. `unlink` removes a person's link (`pr.unlinked {epoch}`); a link the device
registered is a fact of the task and stays (`409 id_conflict`); an unknown one is `404 not_found`.

Members read (`wg:read`, checked by the route) one tag with `pr_link` and the links of a workgroup or of one of its
tasks with `list_links`: newest first by `created_at`, then epoch and tag descending, pages of at most `LIST_MAX`
rows with an opaque cursor (the position of the last row; a malformed cursor is `400 request_invalid` at `/cursor`).
A row is `pr_link_row`: `{epoch, tag, sealed, task_id, session_id, linked_by, created_at}`, exactly one of
`session_id` and `linked_by` set; nothing about the pull request is in clear.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import psycopg

from agent_hub.audit import recorder
from agent_hub.audit.chain import AuditRow, DetailValue, append_audit
from agent_hub.crypto import context, runlog
from agent_hub.crypto.encoding import b64u_decode, b64u_encode, canonical, format_time
from agent_hub.crypto.errors import CryptoError
from agent_hub.device.access import DeviceView
from agent_hub.errors import HubError

Connection = psycopg.Connection[Any]
STALE_UPLOAD = timedelta(hours=6)
"""An upload without a new chunk for this long is removed by the job `contexts.expire`."""
CONTEXT_LOCK = 742180233
"""First key of the two-key advisory lock that serialises the quota check of one organisation."""
COMMIT_LOCK = 742180234
"""First key of the two-key advisory lock that serialises the commits (and their pruning) of one task."""
_NEWEST_FIRST = (
    " FROM hub.context_object o JOIN hub.session s ON s.id = o.session_id WHERE o.task_id = %s"
    " AND o.state = 'complete' AND o.kind = 'context' ORDER BY s.created_at DESC, s.id DESC, o.id DESC"
)
"""The complete checkpoints of a task, newest first (module docstring)."""
SAVED = "session.context_saved"
PR_LINKED = "session.pr_linked"
LINKED = "pr.linked"
UNLINKED = "pr.unlinked"
LIST_DEFAULT = 100
LIST_MAX = 200
_LINK_COLUMNS = "epoch, tag, sealed, task_id, session_id, linked_by, created_at"
_LINK_ORDER = ' ORDER BY created_at DESC, epoch DESC, tag COLLATE "C" DESC'
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
REFUSED = "session.context_refused"
_OBJECT = (
    "SELECT org_id, wg_id, task_id, session_id, author_id, state, chunks, size, manifest_text, sig, created_at,"
    " completed_at, epoch, digest, kind FROM hub.context_object WHERE id = %s"
)
_LOGS_NEWEST_FIRST = (
    " FROM hub.context_object WHERE task_id = %s AND state = 'complete' AND kind = 'log'"
    " ORDER BY created_at DESC, id DESC"
)
KINDS = frozenset({"context", "log"})


@dataclass(frozen=True)
class Limits:
    """The quota settings (`HubConfig.context_*`)."""

    max_object_bytes: int
    org_bytes: int
    keep: int
    log_org_bytes: int = 0
    """The sealed bytes of every log of the organisation (`AGENT_HUB_LOG_ORG_BYTES`, ADR-080 section 3): logs have a
    quota of their own, so they never crowd out the checkpoints a resume needs."""


@dataclass(frozen=True)
class SessionRow:
    """The scope of a session that runs (or ran) on the calling device."""

    org_id: UUID
    wg_id: UUID
    task_id: UUID
    session_id: UUID


def _invalid(pointer: str) -> HubError:
    return HubError("request_invalid", detail={"pointers": [pointer]})


def own_session(
    connection: Connection, view: DeviceView, device: UUID, session_id: str, *, main: bool = True
) -> SessionRow:
    """The session `session_id` of the calling device in an assigned workgroup, a main session unless `main` is False
    (a log may belong to a subagent; module docstring)."""
    row = connection.execute(
        "SELECT org_id, wg_id, task_id, id, device_id, depth FROM hub.session WHERE id = %s", (session_id,)
    ).fetchone()
    if row is None:
        raise HubError("not_found")
    if row[4] != device or not view.reads_wg(row[1]):
        raise HubError("forbidden")
    if main and row[5] != 0:
        raise _invalid("/session_id")
    return SessionRow(org_id=row[0], wg_id=row[1], task_id=row[2], session_id=row[3])


class QuotaRefused(HubError):
    """`413 quota_exceeded` for a chunk, with what the audit row `session.context_refused` names."""

    def __init__(
        self, session: SessionRow, object_id: UUID, *, scope: str, limit: int, used: int, requested: int
    ) -> None:
        super().__init__("quota_exceeded", detail={"quota": {"scope": scope, "limit": limit, "used": used}})
        self.session = session
        self.object_id = object_id
        self.audit_detail: dict[str, DetailValue] = {
            "reason": "quota_exceeded",
            "scope": scope,
            "limit": limit,
            "used": used,
            "requested": requested,
        }


def record_refusal(connection: Connection, device: UUID, refused: QuotaRefused, now: datetime) -> None:
    """The audit row of a refused chunk (module docstring); its own transaction, the refused one is rolled back."""
    session = refused.session
    append_audit(
        connection,
        AuditRow(
            at=now,
            actor_kind="device",
            actor_id=device,
            action=REFUSED,
            result="denied",
            org_id=session.org_id,
            wg_id=session.wg_id,
            task_id=session.task_id,
            session_id=session.session_id,
            device_id=device,
            target_kind="context",
            target_id=str(refused.object_id),
            detail=refused.audit_detail,
        ),
    )


def put_chunk(
    connection: Connection,
    view: DeviceView,
    device: UUID,
    *,
    object_id: UUID,
    index: int,
    session_id: str,
    ct: bytes,
    now: datetime,
    limits: Limits,
    kind: str = "context",
) -> bool:
    """Store one sealed chunk (module docstring); True when the identical chunk was stored before."""
    if len(ct) < context.MIN_CT_BYTES:
        raise _invalid("/ct")
    if len(ct) > (context.CHUNK_CT_BYTES if kind == "context" else runlog.CHUNK_CT_BYTES):
        raise HubError("size_exceeded", status=413)
    session = own_session(connection, view, device, session_id, main=kind == "context")
    # The no-op update locks an existing row and returns it in the same statement (it cannot vanish in between).
    row = cast(
        tuple[Any, ...],
        connection.execute(
            "INSERT INTO hub.context_object AS o (id, org_id, wg_id, task_id, session_id, author_id, state, created_at,"
            " updated_at, kind) VALUES (%s, %s, %s, %s, %s, %s, 'uploading', %s, %s, %s) ON CONFLICT (id) DO UPDATE"
            " SET updated_at = o.updated_at RETURNING session_id, author_id, state, size, kind",
            (object_id, session.org_id, session.wg_id, session.task_id, session.session_id, device, now, now, kind),
        ).fetchone(),
    )
    if row[0] != session.session_id or row[1] != device or row[4] != kind:
        raise HubError("id_conflict")
    stored = connection.execute(
        "SELECT ct FROM hub.context_chunk WHERE object_id = %s AND chunk_index = %s", (object_id, index)
    ).fetchone()
    if stored is not None:
        if bytes(stored[0]) != ct:
            raise HubError("id_conflict")
        return True
    if row[2] != "uploading":
        raise HubError("id_conflict")
    if row[3] + len(ct) > limits.max_object_bytes:
        raise QuotaRefused(
            session, object_id, scope="object", limit=limits.max_object_bytes, used=int(row[3]), requested=len(ct)
        )
    connection.execute("SELECT pg_catalog.pg_advisory_xact_lock(%s, hashtext(%s))", (CONTEXT_LOCK, str(session.org_id)))
    used = int(
        cast(
            tuple[int],
            connection.execute(
                "SELECT COALESCE(SUM(size), 0) FROM hub.context_object WHERE org_id = %s AND kind = %s",
                (session.org_id, kind),
            ).fetchone(),
        )[0]
    )
    org_limit = limits.org_bytes if kind == "context" else limits.log_org_bytes
    if used + len(ct) > org_limit:
        raise QuotaRefused(session, object_id, scope="org", limit=org_limit, used=used, requested=len(ct))
    connection.execute(
        "INSERT INTO hub.context_chunk (object_id, chunk_index, ct, sha256) VALUES (%s, %s, %s, %s)",
        (object_id, index, ct, context.chunk_hash(ct)),
    )
    connection.execute(
        "UPDATE hub.context_object SET chunks = chunks + 1, size = size + %s, updated_at = %s WHERE id = %s",
        (len(ct), now, object_id),
    )
    return False


def _manifest(value: object, kind: str) -> dict[str, object]:
    try:
        return context.check_manifest(value) if kind == "context" else runlog.check_manifest(value)
    except CryptoError as refused:
        raise HubError(refused.code) from None


def _check_ids(manifest: dict[str, object], object_id: UUID, row: tuple[Any, ...]) -> None:
    expected = {
        "object": object_id,
        "org": row[0],
        "wg": row[1],
        "task": row[2],
        "session": row[3],
        "author": row[4],
    }
    for key, value in expected.items():
        if manifest[key] != str(value):
            raise _invalid(f"/manifest/{key}")


def _check_chunks(connection: Connection, object_id: UUID, manifest: dict[str, object], row: tuple[Any, ...]) -> None:
    hashes = connection.execute(
        "SELECT chunk_index, sha256 FROM hub.context_chunk WHERE object_id = %s ORDER BY chunk_index", (object_id,)
    ).fetchall()
    if manifest["chunks"] != row[6] or [found[0] for found in hashes] != list(range(len(hashes))):
        raise _invalid("/manifest/chunks")
    if manifest["size"] != row[7]:
        raise _invalid("/manifest/size")
    if manifest["digest"] != context.digest_of_hashes([bytes(found[1]) for found in hashes]):
        raise _invalid("/manifest/digest")


def commit(
    connection: Connection,
    view: DeviceView,
    device: UUID,
    *,
    object_id: UUID,
    manifest_value: object,
    sig: str,
    now: datetime,
    keep: int,
    log_keep: int = 0,
) -> dict[str, Any]:
    """Verify and seal an upload (module docstring); `{completed_at, replayed}`. `keep` complete checkpoints and
    `log_keep` complete logs of the task stay."""
    row = connection.execute(_OBJECT + " FOR UPDATE", (object_id,)).fetchone()
    if row is None:
        raise HubError("not_found")
    if row[4] != device or not view.reads_wg(row[1]):
        raise HubError("forbidden")
    manifest = _manifest(manifest_value, row[14])
    if row[5] == "complete":
        if row[8] != canonical(manifest).decode("ascii") or b64u_encode(bytes(row[9])) != sig:
            raise HubError("id_conflict")
        recorder.unchanged()
        return {"completed_at": format_time(row[11]), "replayed": True}
    _check_ids(manifest, object_id, row)
    epoch = connection.execute(
        "SELECT 1 FROM hub.scope_epoch WHERE scope = 'wg' AND scope_id = %s AND epoch = %s", (row[1], manifest["epoch"])
    ).fetchone()
    if epoch is None:
        raise _invalid("/manifest/epoch")
    key = cast(tuple[bytes], connection.execute("SELECT sig_pub FROM hub.device WHERE id = %s", (device,)).fetchone())
    try:
        (context.verify_manifest if row[14] == "context" else runlog.verify_manifest)(manifest, sig, bytes(key[0]))
    except CryptoError as refused:
        raise HubError(refused.code) from None
    _check_chunks(connection, object_id, manifest, row)
    connection.execute(
        "UPDATE hub.context_object SET state = 'complete', epoch = %s, digest = %s, manifest_text = %s, sig = %s,"
        " completed_at = %s, updated_at = %s WHERE id = %s",
        (
            manifest["epoch"],
            manifest["digest"],
            canonical(manifest).decode("ascii"),
            b64u_decode(sig),
            now,
            now,
            object_id,
        ),
    )
    # One commit of the task at a time: each prunes with every other completed object of its kind in view.
    connection.execute("SELECT pg_catalog.pg_advisory_xact_lock(%s, hashtext(%s))", (COMMIT_LOCK, str(row[2])))
    newest_first = "SELECT o.id" + _NEWEST_FIRST if row[14] == "context" else "SELECT id" + _LOGS_NEWEST_FIRST
    pruned = connection.execute(
        "DELETE FROM hub.context_object WHERE id IN (" + newest_first + " OFFSET %s) AND id <> %s RETURNING id",
        (row[2], keep if row[14] == "context" else log_keep, object_id),
    ).fetchall()
    append_audit(
        connection,
        AuditRow(
            at=now,
            actor_kind="device",
            actor_id=device,
            action=SAVED,
            result="ok",
            org_id=row[0],
            wg_id=row[1],
            task_id=row[2],
            session_id=row[3],
            device_id=device,
            target_kind=row[14],
            target_id=str(object_id),
            detail={"chunks": int(row[6]), "size": int(row[7]), "pruned": len(pruned)},
        ),
    )
    return {"completed_at": format_time(now), "replayed": False}


def _complete(connection: Connection, view: DeviceView, object_id: UUID) -> tuple[Any, ...]:
    row = connection.execute(_OBJECT, (object_id,)).fetchone()
    if row is None or row[5] != "complete" or row[14] != "context" or not view.reads_wg(row[1]):
        raise HubError("not_found")
    return cast(tuple[Any, ...], row)


def _described(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "manifest": json.loads(row[8]),
        "sig": b64u_encode(bytes(row[9])),
        "created_at": format_time(row[10]),
        "completed_at": format_time(row[11]),
    }


def get_object(connection: Connection, view: DeviceView, object_id: UUID) -> dict[str, Any]:
    """`{manifest, sig, created_at, completed_at}` of a complete checkpoint of an assigned workgroup."""
    return _described(_complete(connection, view, object_id))


def _chunk(connection: Connection, object_id: UUID, index: int) -> dict[str, Any]:
    found = connection.execute(
        "SELECT ct FROM hub.context_chunk WHERE object_id = %s AND chunk_index = %s", (object_id, index)
    ).fetchone()
    if found is None:
        raise HubError("not_found")
    return {"ct": b64u_encode(bytes(found[0]))}


def get_chunk(connection: Connection, view: DeviceView, object_id: UUID, index: int) -> dict[str, Any]:
    """`{ct}` of one chunk of a complete checkpoint of an assigned workgroup."""
    _complete(connection, view, object_id)
    return _chunk(connection, object_id, index)


def _log(connection: Connection, task_id: UUID, object_id: UUID) -> tuple[Any, ...]:
    row = connection.execute(_OBJECT, (object_id,)).fetchone()
    if row is None or row[5] != "complete" or row[14] != "log" or row[2] != task_id:
        raise HubError("not_found")
    return cast(tuple[Any, ...], row)


def task_log(connection: Connection, task_id: UUID, object_id: UUID) -> dict[str, Any]:
    """`{manifest, sig, created_at, completed_at}` of a complete log of the task (the route checks `task:read`)."""
    return _described(_log(connection, task_id, object_id))


def task_log_chunk(connection: Connection, task_id: UUID, object_id: UUID, index: int) -> dict[str, Any]:
    """`{ct}` of one chunk of a complete log of the task."""
    _log(connection, task_id, object_id)
    return _chunk(connection, object_id, index)


def task_contexts(connection: Connection, task_id: UUID, limit: int) -> dict[str, Any]:
    """The complete checkpoints of a task, newest first (`GET /v1/tasks/{task}/contexts`)."""
    rows = connection.execute(
        "SELECT o.id, o.session_id, o.author_id, o.epoch, o.chunks, o.size, o.digest, o.created_at, o.completed_at"
        + _NEWEST_FIRST
        + " LIMIT %s",
        (task_id, limit),
    ).fetchall()
    return {
        "contexts": [
            {
                "object_id": str(row[0]),
                "session_id": str(row[1]),
                "author_id": str(row[2]),
                "epoch": row[3],
                "chunks": row[4],
                "size": row[5],
                "digest": row[6],
                "created_at": format_time(row[7]),
                "completed_at": format_time(row[8]),
            }
            for row in rows
        ]
    }


def newest(connection: Connection, task_id: str) -> dict[str, Any] | None:
    """`{object_id, session_id, created_at, size}` of the newest complete checkpoint of the task's main sessions."""
    row = connection.execute(
        "SELECT o.id, o.session_id, o.created_at, o.size" + _NEWEST_FIRST + " LIMIT 1", (task_id,)
    ).fetchone()
    if row is None:
        return None
    return {"object_id": str(row[0]), "session_id": str(row[1]), "created_at": format_time(row[2]), "size": row[3]}


def _link_row(row: tuple[Any, ...]) -> dict[str, Any]:
    """`pr_link_row` of `_LINK_COLUMNS`."""
    return {
        "epoch": row[0],
        "tag": row[1],
        "sealed": row[2],
        "task_id": str(row[3]),
        "session_id": None if row[4] is None else str(row[4]),
        "linked_by": None if row[5] is None else str(row[5]),
        "created_at": format_time(row[6]),
    }


def _find(connection: Connection, wg_id: UUID, epoch: int, tag: str, *, lock: bool = False) -> tuple[Any, ...] | None:
    return cast(
        tuple[Any, ...] | None,
        connection.execute(
            f"SELECT {_LINK_COLUMNS} FROM hub.pr_link WHERE wg_id = %s AND epoch = %s AND tag = %s"
            + (" FOR UPDATE" if lock else ""),
            (wg_id, epoch, tag),
        ).fetchone(),
    )


def _device_audit(
    connection: Connection,
    session: SessionRow,
    device: UUID,
    *,
    now: datetime,
    action: str,
    tag: str,
    epoch: int,
    **extra: DetailValue,
) -> None:
    append_audit(
        connection,
        AuditRow(
            at=now,
            actor_kind="device",
            actor_id=device,
            action=action,
            result="ok",
            org_id=session.org_id,
            wg_id=session.wg_id,
            task_id=session.task_id,
            session_id=session.session_id,
            device_id=device,
            target_kind="pr_link",
            target_id=tag,
            detail={"epoch": epoch, **extra},
        ),
    )


def link_pr(
    connection: Connection,
    view: DeviceView,
    device: UUID,
    *,
    session_id: str,
    epoch: int,
    tag: str,
    sealed: str | None,
    now: datetime,
) -> bool:
    """Store the tag of a pull request the session created (module docstring); True for a replay."""
    session = own_session(connection, view, device, session_id)
    known = connection.execute(
        "SELECT 1 FROM hub.scope_epoch WHERE scope = 'wg' AND scope_id = %s AND epoch = %s", (session.wg_id, epoch)
    ).fetchone()
    if known is None:
        raise _invalid("/epoch")
    stored = connection.execute(
        "INSERT INTO hub.pr_link (wg_id, epoch, tag, task_id, session_id, sealed, created_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (wg_id, epoch, tag) DO NOTHING RETURNING task_id",
        (session.wg_id, epoch, tag, session.task_id, session.session_id, sealed, now),
    ).fetchone()
    if stored is not None:
        _device_audit(connection, session, device, now=now, action=PR_LINKED, tag=tag, epoch=epoch)
        return False
    found = cast(tuple[Any, ...], _find(connection, session.wg_id, epoch, tag, lock=True))
    if found[5] is not None:
        connection.execute(
            "UPDATE hub.pr_link SET task_id = %s, session_id = %s, linked_by = NULL, sealed = %s, created_at = %s"
            " WHERE wg_id = %s AND epoch = %s AND tag = %s",
            (session.task_id, session.session_id, sealed, now, session.wg_id, epoch, tag),
        )
        _device_audit(connection, session, device, now=now, action=LINKED, tag=tag, epoch=epoch, replaced=True)
        return False
    if found[3] != session.task_id:
        raise HubError("id_conflict")
    recorder.unchanged()
    return True


def pr_link(connection: Connection, wg_id: UUID, epoch: int, tag: str) -> dict[str, Any]:
    """`{task_id, session_id, linked_by, sealed, created_at}` of a tag, `404 not_found` for an unknown one."""
    row = _find(connection, wg_id, epoch, tag)
    if row is None:
        raise HubError("not_found")
    found = _link_row(row)
    del found["epoch"], found["tag"]
    return found


def _cursor(at: datetime, epoch: int, tag: str) -> str:
    """The opaque position of a row: its time in microseconds since the Unix epoch, its epoch and its tag."""
    return b64u_encode(canonical({"at": (at - _EPOCH) // timedelta(microseconds=1), "epoch": epoch, "tag": tag}))


def decode_cursor(text: str) -> tuple[datetime, int, str]:
    """`(created_at, epoch, tag)` of a cursor `_cursor` made; anything else is `400 request_invalid` at `/cursor`."""
    found: tuple[datetime, int, str] | None = None
    try:
        data = json.loads(b64u_decode(text))
        at, epoch, tag = _EPOCH + timedelta(microseconds=data["at"]), data["epoch"], data["tag"]
        if type(epoch) is int and type(tag) is str and _cursor(at, epoch, tag) == text:
            found = (at, epoch, tag)
    except (CryptoError, ValueError, TypeError, KeyError, OverflowError):
        found = None
    if found is None:
        raise _invalid("/cursor")
    return found


def list_links(
    connection: Connection, wg_id: UUID, *, task_id: UUID | None, cursor: str | None, limit: int
) -> dict[str, Any]:
    """`{items: [pr_link_row], next_cursor}` of a workgroup or of one of its tasks, newest first (module docstring)."""
    where = "wg_id = %s"
    params: list[object] = [wg_id]
    if task_id is not None:
        where, params = where + " AND task_id = %s", [*params, task_id]
    if cursor is not None:
        at, epoch, tag = decode_cursor(cursor)
        where, params = where + ' AND (created_at, epoch, tag COLLATE "C") < (%s, %s, %s)', [*params, at, epoch, tag]
    rows = connection.execute(
        f"SELECT {_LINK_COLUMNS} FROM hub.pr_link WHERE {where}{_LINK_ORDER} LIMIT %s", (*params, limit + 1)
    ).fetchall()
    last = rows[limit - 1] if len(rows) > limit else None
    return {
        "items": [_link_row(row) for row in rows[:limit]],
        "next_cursor": None if last is None else _cursor(last[6], last[0], last[1]),
    }


def _user_audit(
    connection: Connection,
    user: UUID,
    wg: tuple[UUID, UUID],
    *,
    now: datetime,
    action: str,
    row: tuple[Any, ...],
    **extra: DetailValue,
) -> None:
    append_audit(
        connection,
        AuditRow(
            at=now,
            actor_kind="user",
            actor_id=user,
            action=action,
            result="ok",
            org_id=wg[0],
            wg_id=wg[1],
            task_id=row[3],
            target_kind="pr_link",
            target_id=row[1],
            detail={"epoch": row[0], **extra},
        ),
    )


def _org_of(connection: Connection, wg_id: UUID) -> UUID:
    return cast(tuple[UUID], connection.execute("SELECT org_id FROM hub.wg WHERE id = %s", (wg_id,)).fetchone())[0]


def link_by_user(
    connection: Connection,
    user: UUID,
    wg_id: UUID,
    *,
    epoch: int,
    tag: str,
    sealed: str,
    task_id: str,
    now: datetime,
) -> tuple[bool, dict[str, Any]]:
    """A person links a pull request to a task (module docstring): `(created, pr_link_row)`."""
    task = connection.execute("SELECT wg_id FROM hub.task WHERE id = %s", (task_id,)).fetchone()
    if task is None or task[0] != wg_id:
        raise _invalid("/task_id")
    current = cast(
        tuple[int | None],
        connection.execute(
            "SELECT max(epoch) FROM hub.scope_epoch WHERE scope = 'wg' AND scope_id = %s", (wg_id,)
        ).fetchone(),
    )
    if current[0] != epoch:
        raise HubError("epoch_stale")
    stored = connection.execute(
        f"INSERT INTO hub.pr_link (wg_id, epoch, tag, task_id, linked_by, sealed, created_at)"
        f" VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (wg_id, epoch, tag) DO NOTHING RETURNING {_LINK_COLUMNS}",
        (wg_id, epoch, tag, task_id, user, sealed, now),
    ).fetchone()
    if stored is None:
        recorder.unchanged()
        return False, _link_row(cast(tuple[Any, ...], _find(connection, wg_id, epoch, tag)))
    _user_audit(
        connection, user, (_org_of(connection, wg_id), wg_id), now=now, action=LINKED, row=stored, replaced=False
    )
    return True, _link_row(stored)


def unlink(connection: Connection, user: UUID, wg_id: UUID, *, epoch: int, tag: str, now: datetime) -> None:
    """Remove a person's link (module docstring)."""
    row = _find(connection, wg_id, epoch, tag, lock=True)
    if row is None:
        raise HubError("not_found")
    if row[5] is None:
        raise HubError("id_conflict")
    connection.execute("DELETE FROM hub.pr_link WHERE wg_id = %s AND epoch = %s AND tag = %s", (wg_id, epoch, tag))
    _user_audit(connection, user, (_org_of(connection, wg_id), wg_id), now=now, action=UNLINKED, row=row)


def expire(connection: Connection, now: datetime) -> int:
    """Delete the uploads without a chunk for `STALE_UPLOAD`; the number of objects removed."""
    removed = connection.execute(
        "DELETE FROM hub.context_object WHERE state = 'uploading' AND updated_at < %s RETURNING id",
        (now - STALE_UPLOAD,),
    ).fetchall()
    return len(removed)
