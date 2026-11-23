"""Signed checkpoints of the audit head (02-data-model.md section 2.9).

A checkpoint is the text

    agent-audit-head-v1\\n<hub key id>\\n<seq>\\n<hash>\\n<published_at>

(ASCII, `published_at` as `YYYY-MM-DDTHH:MM:SS.mmmZ`, the time format of the audit API) signed with the hub's Ed25519
key; the 64 signature bytes are stored raw and exposed as b64u. This is the format of
`agent_core.record.checkpoint` with its own label and the audit head. Which hour a checkpoint is written in is
decided by the hourly job (HUB-16); `write_checkpoint` writes one for the current head unless the newest checkpoint
of the key already covers it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

import psycopg

from agent_core.signing import HubSigner, b64url_decode
from agent_hub.audit.chain import to_millis
from agent_hub.crypto.encoding import format_time
from agent_hub.crypto.errors import CryptoError
from agent_hub.crypto.keys import ed25519_verify

CHECKPOINT_LABEL = "agent-audit-head-v1"
SIGNATURE_BYTES = 64
PUBLIC_KEY_BYTES = 32

_KEY_ID_RE = re.compile(r"[a-z0-9-]{1,64}")
_HASH_RE = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class AuditCheckpoint:
    """One row of `hub.audit_checkpoint`."""

    hub_key_id: str
    seq: int
    hash: str
    published_at: datetime
    signature: bytes = field(repr=False)


def checkpoint_text(hub_key_id: str, seq: int, head_hash: str, published_at: datetime) -> bytes:
    """The signed text; ValueError for a part outside its form."""
    if _KEY_ID_RE.fullmatch(hub_key_id) is None:
        raise ValueError("the hub key id breaks [a-z0-9-]{1,64}")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise ValueError("seq is an integer of at least 1")
    if _HASH_RE.fullmatch(head_hash) is None:
        raise ValueError("the hash is 64 lower-case hex characters")
    published = format_time(to_millis(published_at))
    return "\n".join([CHECKPOINT_LABEL, hub_key_id, str(seq), head_hash, published]).encode("ascii")


def sign_checkpoint(signer: HubSigner, seq: int, head_hash: str, published_at: datetime) -> AuditCheckpoint:
    """A checkpoint of the head (`seq`, `head_hash`) signed with the hub key."""
    published = to_millis(published_at)
    # HubSigner.sign returns the canonical b64url text of the 64 signature bytes.
    signature = cast(bytes, b64url_decode(signer.sign(checkpoint_text(signer.key_id, seq, head_hash, published))))
    return AuditCheckpoint(signer.key_id, seq, head_hash, published, signature)


def checkpoint_signature_valid(public_key: bytes, checkpoint: AuditCheckpoint) -> bool:
    """Whether the signature verifies under the hub's raw Ed25519 public key; False for any malformed part."""
    if len(public_key) != PUBLIC_KEY_BYTES or len(checkpoint.signature) != SIGNATURE_BYTES:
        return False
    try:
        text = checkpoint_text(checkpoint.hub_key_id, checkpoint.seq, checkpoint.hash, checkpoint.published_at)
        ed25519_verify(public_key, checkpoint.signature, text)
    except (ValueError, CryptoError):
        return False
    return True


def _checkpoint(record: tuple[Any, ...]) -> AuditCheckpoint:
    return AuditCheckpoint(str(record[0]), int(record[1]), str(record[2]), record[3], bytes(record[4]))


def newest_checkpoint(connection: psycopg.Connection[Any], hub_key_id: str) -> AuditCheckpoint | None:
    """The checkpoint with the highest `seq` of this hub key, None when there is none."""
    record = connection.execute(
        "SELECT hub_key_id, seq, hash, published_at, signature FROM hub.audit_checkpoint"
        " WHERE hub_key_id = %s ORDER BY seq DESC LIMIT 1",
        (hub_key_id,),
    ).fetchone()
    return None if record is None else _checkpoint(record)


def write_checkpoint(connection: psycopg.Connection[Any], signer: HubSigner, now: datetime) -> AuditCheckpoint | None:
    """Sign and insert a checkpoint of the current audit head.

    None when the audit is empty, when the newest checkpoint of the key already has the head's `seq`, or when a
    concurrent writer inserted the same checkpoint first.
    """
    with connection.transaction():
        head = connection.execute("SELECT seq, hash FROM hub.audit ORDER BY seq DESC LIMIT 1").fetchone()
        if head is None:
            return None
        newest = newest_checkpoint(connection, signer.key_id)
        if newest is not None and newest.seq >= int(head[0]):
            return None
        checkpoint = sign_checkpoint(signer, int(head[0]), str(head[1]), now)
        inserted = connection.execute(
            "INSERT INTO hub.audit_checkpoint (hub_key_id, seq, hash, published_at, signature)"
            " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (hub_key_id, seq) DO NOTHING",
            (checkpoint.hub_key_id, checkpoint.seq, checkpoint.hash, checkpoint.published_at, checkpoint.signature),
        )
        return checkpoint if inserted.rowcount == 1 else None


def checkpoint_valid(connection: psycopg.Connection[Any], checkpoint: AuditCheckpoint, public_key: bytes) -> bool:
    """The checkpoint's signature verifies and its hash equals the stored audit hash at its `seq`."""
    record = connection.execute("SELECT hash FROM hub.audit WHERE seq = %s", (checkpoint.seq,)).fetchone()
    return record is not None and record[0] == checkpoint.hash and checkpoint_signature_valid(public_key, checkpoint)
