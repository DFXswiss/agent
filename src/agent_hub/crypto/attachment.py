"""Files and images attached to a prompt (04-crypto.md section 4.14, ADR-102).

An attachment is one file of 1 to `MAX_BYTES` bytes, split into pieces of at most `CHUNK_BYTES` and sealed by the
author's browser under a key of the prompt's workgroup:

- `k_att = HKDF(ikm=WK_e, salt=ref.salt (32 B random per object), info=P || "attachment\\0" || object_id)`,
- chunk `i` = `nonce(12) || AES-256-GCM(k_att, piece, AAD = P || "attachment-chunk\\0" ||
  canonical({"index": i, "object": object_id}))`.

There is no manifest and no signature of its own: the prompt body names each attachment with a reference of exactly
the keys `REF_KEYS` (`object`, `name`, `type`, `bytes`, `epoch`, `salt`, `digest`), inside the prompt's ciphertext and
under the author's signature. `digest` is the b64u SHA-256 over the concatenated SHA-256 of every sealed chunk in index
order (as for context checkpoints), so the signed prompt binds the exact ciphertext; the number of chunks
(`chunk_count`) and their sealed length (`sealed_size`) follow from `bytes`.

The hub checks only that the stored chunks match the `{chunks, size, digest}` of the commit, without any key. A reader
with `WK_e` checks the reference (`header_invalid`), the chunks against it - each at most `CHUNK_CT_BYTES`
(`size_exceeded`), then count, size and digest (`header_invalid`) - and decrypts each (`decrypt_failed`).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import cast

from agent_hub.crypto import aead, fields
from agent_hub.crypto.context import chunk_hash, digest, digest_of_hashes
from agent_hub.crypto.encoding import (
    P,
    b64u_decode,
    b64u_encode,
    canonical,
    require_bytes,
    require_int,
    require_keys,
)
from agent_hub.crypto.errors import fail
from agent_hub.crypto.kdf import KEY_BYTES, SALT_BYTES, hkdf

__all__ = ["chunk_hash", "digest", "digest_of_hashes"]

ATTACHMENT = b"attachment\x00"
"""Info label of `k_att` (without the prefix P)."""
LABEL_CHUNK = b"attachment-chunk\x00"
CHUNK_BYTES = 1024 * 1024
"""At most this many file bytes per chunk."""
CHUNK_CT_BYTES = CHUNK_BYTES + aead.NONCE_BYTES + aead.TAG_BYTES
"""The largest sealed chunk."""
MIN_CT_BYTES = aead.NONCE_BYTES + aead.TAG_BYTES + 1
"""The smallest sealed chunk (a chunk holds at least one byte)."""
MAX_BYTES = 32 * CHUNK_BYTES
"""The largest file."""
MAX_CHUNKS = MAX_BYTES // CHUNK_BYTES
MAX_ATTACHMENTS = 10
"""At most this many attachments per prompt."""
MAX_EPOCH = 2**31 - 1
NAME_MAX = 255
TYPE_MAX = 127
REF_KEYS = frozenset({"object", "name", "type", "bytes", "epoch", "salt", "digest"})
TYPE_RE = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*")
"""A media type in lower case without parameters (`image/png`, `application/vnd.ms-excel`)."""
_NAME_FORBIDDEN_RE = re.compile(r"[\x00-\x1f\x7f/\\]")


def attachment_key(workgroup_key: bytes, salt: bytes, object_id: str) -> bytes:
    """`k_att = HKDF(WK_e, salt, info=P || "attachment\\0" || object_id)`."""
    require_bytes(workgroup_key, KEY_BYTES)
    return hkdf(
        workgroup_key, P + ATTACHMENT + fields.uuid(object_id).encode("ascii"), salt=require_bytes(salt, SALT_BYTES)
    )


def chunk_aad(object_id: str, index: int) -> bytes:
    """`P || "attachment-chunk\\0" || canonical({"index": i, "object": object_id})`."""
    return P + LABEL_CHUNK + canonical({"index": require_int(index, 0, MAX_CHUNKS - 1), "object": object_id})


def seal_chunk(key: bytes, object_id: str, index: int, piece: bytes, nonce: bytes | None = None) -> bytes:
    """`nonce || AES-GCM(k_att, piece, AAD)`; an empty or too large piece is `size_exceeded`."""
    if not 1 <= len(piece) <= CHUNK_BYTES:
        fail("size_exceeded")
    return aead.seal_blob(key, piece, chunk_aad(fields.uuid(object_id), index), nonce)


def open_chunk(key: bytes, object_id: str, index: int, sealed: bytes) -> bytes:
    """The file bytes of one chunk (`size_exceeded` above `CHUNK_CT_BYTES`, `decrypt_failed` otherwise)."""
    if len(sealed) > CHUNK_CT_BYTES:
        fail("size_exceeded")
    return aead.open_blob(key, sealed, chunk_aad(fields.uuid(object_id), index))


def chunk_count(size: int) -> int:
    """The number of chunks of a file of `size` bytes."""
    return -(-size // CHUNK_BYTES)


def sealed_size(size: int) -> int:
    """The sealed bytes of all chunks of a file of `size` bytes."""
    return size + chunk_count(size) * (aead.NONCE_BYTES + aead.TAG_BYTES)


def check_name(value: object) -> str:
    """A file name: 1 to `NAME_MAX` characters, no control character, no `/` or `\\`, not `.` or `..`."""
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= NAME_MAX
        or _NAME_FORBIDDEN_RE.search(value) is not None
        or value in (".", "..")
    ):
        fail("header_invalid")
    return value


def check_type(value: object) -> str:
    """A media type of `TYPE_RE`, at most `TYPE_MAX` characters."""
    if not isinstance(value, str) or len(value) > TYPE_MAX or TYPE_RE.fullmatch(value) is None:
        fail("header_invalid")
    return value


def check_ref(value: object) -> dict[str, object]:
    """Exact key set and field formats of one reference of a prompt body (module docstring); returns it."""
    ref = require_keys(value, REF_KEYS)
    fields.uuid(ref["object"])
    check_name(ref["name"])
    check_type(ref["type"])
    require_int(ref["bytes"], 1, MAX_BYTES)
    require_int(ref["epoch"], 1, MAX_EPOCH)
    fields.b64u_bytes(ref["salt"], SALT_BYTES)
    fields.b64u_bytes(ref["digest"], 32)
    return ref


def check_refs(value: object) -> list[dict[str, object]]:
    """The `attachments` of a prompt body: a list of at most `MAX_ATTACHMENTS` references with distinct objects."""
    if not isinstance(value, list) or len(value) > MAX_ATTACHMENTS:
        fail("header_invalid")
    refs = [check_ref(item) for item in value]
    if len({ref["object"] for ref in refs}) != len(refs):
        fail("header_invalid")
    return refs


def check_chunks(ref: dict[str, object], chunks: Sequence[bytes]) -> None:
    """The sealed chunks fit the reference: each at most `CHUNK_CT_BYTES` (`size_exceeded`), then count, size and
    digest (`header_invalid`)."""
    if any(len(sealed) > CHUNK_CT_BYTES for sealed in chunks):
        fail("size_exceeded")
    size = cast(int, ref["bytes"])
    if (
        len(chunks) != chunk_count(size)
        or sum(len(sealed) for sealed in chunks) != sealed_size(size)
        or digest(chunks) != ref["digest"]
    ):
        fail("header_invalid")


def split(data: bytes) -> list[bytes]:
    """The file in pieces of at most `CHUNK_BYTES` (an empty or too large file is `size_exceeded`)."""
    if not 1 <= len(data) <= MAX_BYTES:
        fail("size_exceeded")
    return [data[start : start + CHUNK_BYTES] for start in range(0, len(data), CHUNK_BYTES)]


def seal_attachment(
    workgroup_key: bytes,
    *,
    object_id: str,
    name: str,
    media_type: str,
    epoch: int,
    data: bytes,
    salt: bytes,
    nonces: Sequence[bytes] | None = None,
) -> tuple[dict[str, object], list[bytes]]:
    """Seal a file: `(reference, sealed chunks)`. `salt` is 32 random bytes per object (fixed together with `nonces`
    only by the test vectors)."""
    parts = split(data)
    if nonces is not None and len(nonces) != len(parts):
        fail("header_invalid")
    key = attachment_key(workgroup_key, salt, object_id)
    sealed = [
        seal_chunk(key, object_id, index, part, None if nonces is None else nonces[index])
        for index, part in enumerate(parts)
    ]
    ref: dict[str, object] = {
        "object": object_id,
        "name": name,
        "type": media_type,
        "bytes": len(data),
        "epoch": epoch,
        "salt": b64u_encode(salt),
        "digest": digest(sealed),
    }
    return check_ref(ref), sealed


def open_attachment(workgroup_key: bytes, ref: object, chunks: Sequence[bytes]) -> bytes:
    """Check the reference and the chunks (module docstring), decrypt and join: the file. `workgroup_key` is `WK` of
    the reference's epoch."""
    checked = check_ref(ref)
    check_chunks(checked, chunks)
    object_id = str(checked["object"])
    key = attachment_key(workgroup_key, b64u_decode(checked["salt"]), object_id)
    return b"".join(open_chunk(key, object_id, index, sealed) for index, sealed in enumerate(chunks))
