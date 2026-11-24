"""Logs of test runs and work VM jobs as encrypted objects (KOR-43, ADR-080 section 3; 04-crypto.md section 4.12).

`store_log(env, owner, ref=..., total=..., pieces=...)` seals the kept output of one run or job into the local cache
of the context objects as it arrives: pieces of at most `runlog.CHUNK_BYTES` output bytes, each sealed under `k_log`
of a new object (UUIDv7, random salt) and the **current** epoch key of the workgroup, written to the cache; then the
manifest (`ref` = the run or job ID, `bytes` = the full output length `total`, `stored` = the bytes kept) is signed
with the device key and the object recorded `sealed` with kind `log`, and the upload loop is woken. It is uploaded
like a checkpoint (`checkpoint.Checkpoints.upload`) and leaves the cache once the hub holds it.

Returns the `log_ref` of the contracts (`{object_id, bytes, stored_bytes, truncated}`), or None when nothing is kept
(no output, no key of the workgroup on this device yet - logged as `log_key_missing`). Plaintext never reaches the
disk.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from agent_runner.contexts import store
from agent_runner.contexts.checkpoint import current_key
from agent_runner.contexts.env import ContextEnv
from agent_runner.crypto import runlog
from agent_runner.crypto.encoding import b64u_encode
from agent_runner.crypto.ids import uuid7
from agent_runner.crypto.kdf import SALT_BYTES


@dataclass(frozen=True)
class Owner:
    """Where a log belongs."""

    org_id: str
    wg_id: str
    task_id: str
    session_id: str


async def store_log(
    env: ContextEnv, owner: Owner, *, ref: str, total: int, pieces: AsyncIterator[bytes]
) -> dict[str, Any] | None:
    """Seal and cache the pieces (module docstring); `total` is the full output length."""
    async with env.sessions.transaction() as conn:
        key = await current_key(conn, env.sessions.storage_key, owner.wg_id)
    if key is None:
        env.line(f"log_key_missing session_id={owner.session_id} ref={ref}")
        return None
    epoch, workgroup_key = key
    object_id = uuid7()
    salt = os.urandom(SALT_BYTES)
    k_log = runlog.log_key(workgroup_key, salt, object_id)
    hashes: list[bytes] = []
    size = stored = 0
    buffer = b""
    try:
        async for data in pieces:
            buffer += data
            while len(buffer) >= runlog.CHUNK_BYTES:
                piece, buffer = buffer[: runlog.CHUNK_BYTES], buffer[runlog.CHUNK_BYTES :]
                size += _seal(env, k_log, object_id, piece=piece, hashes=hashes)
                stored += len(piece)
        if buffer:
            size += _seal(env, k_log, object_id, piece=buffer, hashes=hashes)
            stored += len(buffer)
    except BaseException:
        env.cache.remove(object_id)
        raise
    if not hashes:
        return None
    manifest = runlog.check_manifest(
        {
            "v": runlog.VERSION,
            "kind": runlog.KIND,
            "ref": ref,
            "object": object_id,
            "org": owner.org_id,
            "wg": owner.wg_id,
            "task": owner.task_id,
            "session": owner.session_id,
            "author": env.device_id,
            "epoch": epoch,
            "salt": b64u_encode(salt),
            "chunks": len(hashes),
            "size": size,
            "digest": runlog.digest_of_hashes(hashes),
            "bytes": max(total, stored),
            "stored": stored,
            "ts": env.sessions.at(),
        }
    )
    async with env.sessions.transaction() as conn:
        await store.insert_object(
            conn,
            manifest=manifest,
            sig=runlog.sign_manifest(env.signing_key, manifest),
            source="local",
            state="sealed",
            size=size,
            at=env.now(),
            kind="log",
        )
    env.upload_wake.set()
    env.line(f"log_sealed session_id={owner.session_id} ref={ref} object_id={object_id} chunks={len(hashes)}")
    full = max(total, stored)
    return {"object_id": object_id, "bytes": full, "stored_bytes": stored, "truncated": stored < full}


def _seal(env: ContextEnv, key: bytes, object_id: str, *, piece: bytes, hashes: list[bytes]) -> int:
    index = len(hashes)
    chunk = runlog.seal_chunk(key, object_id, index, piece)
    env.cache.write_chunk(object_id, index, chunk)
    hashes.append(runlog.chunk_hash(chunk))
    return len(chunk)
