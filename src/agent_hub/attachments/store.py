"""Attachments of prompts at the hub (ADR-102 section 3; 03-api.md sections 4.6 and 6.1; 04-crypto.md section 4.14).

**Upload** (`PUT /v1/workgroups/{wg}/attachments/{object}/chunks/{index}` `{ct}`, `wg:write` checked by the route):
the first chunk creates the object (`uploading`) for the workgroup and the uploading user; another workgroup or user
under the same object ID is `409 id_conflict`. A chunk is stored once: the identical bytes again answer
`replayed: true`, other bytes at a stored index (or any new chunk after the commit) `409 id_conflict`. A sealed chunk
is at least `MIN_CT_BYTES` (`400 request_invalid` at `/ct`) and at most `CHUNK_CT_BYTES` (`413 size_exceeded`); the
index is below `MAX_CHUNKS` (else the path names nothing, `404 not_found`, checked by the route), so one object never
holds more than the sealed size of a file of `MAX_BYTES`. The sealed bytes of every attachment of the organisation stay
within `AGENT_HUB_ATTACHMENT_ORG_BYTES` (`413 quota_exceeded` with `detail.quota {scope: "org", limit, used}`, checked
under a per-organisation advisory lock so parallel uploads cannot overshoot).

**Commit** (`POST …/attachments/{object}/commit` `{chunks, size, digest}`, same right): the object must exist in the
workgroup (`404 not_found`) and belong to the caller (`403 forbidden`); `chunks` equals the number of stored chunks with
the indexes `0..chunks-1`, `size` their sealed bytes and `digest` the digest over their stored SHA-256 in index order
(`400 request_invalid` at the field). The object becomes `complete`, audited `attachment.stored {chunks, size}` (actor
the user); the same values again answer `replayed: true`, other values after the commit are `409 id_conflict`.

**Download**: members with `wg:read` (`chunk`, the route checks the right) and devices assigned to the workgroup
(`device_chunk`) read one chunk of a complete object of the workgroup; anything else is `404 not_found`. The hub holds
no key and never learns name, type or content of a file.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

import psycopg

from agent_hub.audit import recorder
from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.crypto import attachment
from agent_hub.crypto.encoding import b64u_encode, format_time
from agent_hub.device.access import DeviceView
from agent_hub.errors import HubError

Connection = psycopg.Connection[Any]
STALE_UPLOAD = timedelta(hours=6)
"""An upload without a new chunk for this long is removed by the job `attachments.expire`."""
ATTACHMENT_LOCK = 742180235
"""First key of the two-key advisory lock that serialises the quota check of one organisation."""
MAX_OBJECT_CT_BYTES = attachment.sealed_size(attachment.MAX_BYTES)
"""The sealed bytes of the largest file."""
STORED = "attachment.stored"
_OBJECT = "SELECT org_id, wg_id, author_id, state, chunks, size, digest, completed_at FROM hub.attachment_object"


def _invalid(pointer: str) -> HubError:
    return HubError("request_invalid", detail={"pointers": [pointer]})


def org_of(connection: Connection, wg_id: UUID) -> UUID:
    """The organisation of a workgroup (the route checked that the caller may read or write it)."""
    return cast(tuple[UUID], connection.execute("SELECT org_id FROM hub.wg WHERE id = %s", (wg_id,)).fetchone())[0]


def put_chunk(
    connection: Connection,
    user: UUID,
    wg_id: UUID,
    *,
    object_id: UUID,
    index: int,
    ct: bytes,
    now: datetime,
    org_limit: int,
) -> bool:
    """Store one sealed chunk (module docstring); True when the identical chunk was stored before."""
    if len(ct) < attachment.MIN_CT_BYTES:
        raise _invalid("/ct")
    if len(ct) > attachment.CHUNK_CT_BYTES:
        raise HubError("size_exceeded", status=413)
    org_id = org_of(connection, wg_id)
    # The no-op update locks an existing row and returns it in the same statement (it cannot vanish in between).
    row = cast(
        tuple[Any, ...],
        connection.execute(
            "INSERT INTO hub.attachment_object AS o (id, org_id, wg_id, author_id, state, created_at, updated_at)"
            " VALUES (%s, %s, %s, %s, 'uploading', %s, %s) ON CONFLICT (id) DO UPDATE SET updated_at = o.updated_at"
            " RETURNING wg_id, author_id, state, size",
            (object_id, org_id, wg_id, user, now, now),
        ).fetchone(),
    )
    if row[0] != wg_id or row[1] != user:
        raise HubError("id_conflict")
    stored = connection.execute(
        "SELECT ct FROM hub.attachment_chunk WHERE object_id = %s AND chunk_index = %s", (object_id, index)
    ).fetchone()
    if stored is not None:
        if bytes(stored[0]) != ct:
            raise HubError("id_conflict")
        recorder.unchanged()
        return True
    if row[2] != "uploading":
        raise HubError("id_conflict")
    connection.execute("SELECT pg_catalog.pg_advisory_xact_lock(%s, hashtext(%s))", (ATTACHMENT_LOCK, str(org_id)))
    used = int(
        cast(
            tuple[int],
            connection.execute(
                "SELECT COALESCE(SUM(size), 0) FROM hub.attachment_object WHERE org_id = %s", (org_id,)
            ).fetchone(),
        )[0]
    )
    if used + len(ct) > org_limit:
        raise HubError("quota_exceeded", detail={"quota": {"scope": "org", "limit": org_limit, "used": used}})
    connection.execute(
        "INSERT INTO hub.attachment_chunk (object_id, chunk_index, ct, sha256) VALUES (%s, %s, %s, %s)",
        (object_id, index, ct, attachment.chunk_hash(ct)),
    )
    connection.execute(
        "UPDATE hub.attachment_object SET chunks = chunks + 1, size = size + %s, updated_at = %s WHERE id = %s",
        (len(ct), now, object_id),
    )
    return False


def _check_chunks(connection: Connection, object_id: UUID, values: dict[str, Any], row: tuple[Any, ...]) -> None:
    hashes = connection.execute(
        "SELECT chunk_index, sha256 FROM hub.attachment_chunk WHERE object_id = %s ORDER BY chunk_index",
        (object_id,),
    ).fetchall()
    if values["chunks"] != row[4] or [found[0] for found in hashes] != list(range(len(hashes))):
        raise _invalid("/chunks")
    if values["size"] != row[5]:
        raise _invalid("/size")
    if values["digest"] != attachment.digest_of_hashes([bytes(found[1]) for found in hashes]):
        raise _invalid("/digest")


def commit(
    connection: Connection, user: UUID, wg_id: UUID, *, object_id: UUID, values: dict[str, Any], now: datetime
) -> dict[str, Any]:
    """Seal an upload (module docstring); `{completed_at, replayed}`. `values` holds `chunks`, `size` and `digest`."""
    row = connection.execute(_OBJECT + " WHERE id = %s FOR UPDATE", (object_id,)).fetchone()
    if row is None or row[1] != wg_id:
        raise HubError("not_found")
    if row[2] != user:
        raise HubError("forbidden")
    if row[3] == "complete":
        if (values["chunks"], values["size"], values["digest"]) != (row[4], row[5], row[6]):
            raise HubError("id_conflict")
        recorder.unchanged()
        return {"completed_at": format_time(row[7]), "replayed": True}
    _check_chunks(connection, object_id, values, row)
    connection.execute(
        "UPDATE hub.attachment_object SET state = 'complete', digest = %s, completed_at = %s, updated_at = %s"
        " WHERE id = %s",
        (values["digest"], now, now, object_id),
    )
    append_audit(
        connection,
        AuditRow(
            at=now,
            actor_kind="user",
            actor_id=user,
            action=STORED,
            result="ok",
            org_id=row[0],
            wg_id=wg_id,
            target_kind="attachment",
            target_id=str(object_id),
            detail={"chunks": int(row[4]), "size": int(row[5])},
        ),
    )
    return {"completed_at": format_time(now), "replayed": False}


def _chunk(connection: Connection, wg_id: UUID, object_id: UUID, index: int) -> dict[str, Any]:
    found = connection.execute(
        "SELECT c.ct FROM hub.attachment_chunk c JOIN hub.attachment_object o ON o.id = c.object_id"
        " WHERE o.id = %s AND o.wg_id = %s AND o.state = 'complete' AND c.chunk_index = %s",
        (object_id, wg_id, index),
    ).fetchone()
    if found is None:
        raise HubError("not_found")
    return {"ct": b64u_encode(bytes(found[0]))}


def chunk(connection: Connection, wg_id: UUID, object_id: UUID, index: int) -> dict[str, Any]:
    """`{ct}` of one chunk of a complete attachment of the workgroup (the route checks `wg:read`)."""
    return _chunk(connection, wg_id, object_id, index)


def device_chunk(connection: Connection, view: DeviceView, object_id: UUID, index: int) -> dict[str, Any]:
    """`{ct}` of one chunk of a complete attachment of a workgroup the device is assigned to."""
    row = connection.execute("SELECT wg_id FROM hub.attachment_object WHERE id = %s", (object_id,)).fetchone()
    if row is None or not view.reads_wg(row[0]):
        raise HubError("not_found")
    return _chunk(connection, row[0], object_id, index)


def expire(connection: Connection, now: datetime) -> int:
    """Delete the uploads without a chunk for `STALE_UPLOAD`; the number of objects removed."""
    removed = connection.execute(
        "DELETE FROM hub.attachment_object WHERE state = 'uploading' AND updated_at < %s RETURNING id",
        (now - STALE_UPLOAD,),
    ).fetchall()
    return len(removed)
