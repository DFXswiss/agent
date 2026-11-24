"""Copied from src/agent_hub/crypto/runlog.py.

Logs of test runs and work VM jobs (04-crypto.md section 4.12, ADR-080 section 3).

A log is the complete output of one `test.run` or one work VM job, split into pieces of at most `CHUNK_BYTES`
**uncompressed** bytes (a reader fetches any window by `offset // CHUNK_BYTES`) and sealed under a key of the task's
workgroup. The labels are its own, so a log can never pass for a context checkpoint (`agent_hub.crypto.context`) or
the reverse:

- `k_log = HKDF(ikm=WK_e, salt=manifest.salt (32 B), info=P || "run-log\\0" || object_id)`,
- chunk `i` = `nonce(12) || AES-256-GCM(k_log, piece, AAD = P || "run-log-chunk\\0" ||
  canonical({"index": i, "object": object_id}))`,
- manifest (canonical JSON, clear) with exactly the keys `MANIFEST_KEYS`: those of a context manifest plus `kind`
  (always `"log"`), `ref` (the `run_id` `tr-<12 hex>` of the run or the `job_id` `j-<12 hex>` of the job: a reader
  checks it against the encrypted event that names the log, so a hub cannot swap two logs of one session), `bytes`
  (the full output length) and `stored` (the plaintext bytes kept: the last part when the output was longer than the
  device's limit); `size` is the sum of the sealed chunk lengths, `digest` the b64u SHA-256 over the concatenated
  SHA-256 of every sealed chunk in index order,
- `sig = Ed25519_author(P || "run-log\\0" || canonical(manifest))` by the authoring device.

The hub checks manifest, signature, chunk count, size and digest without any key; a reader with `WK_e` opens the
chunks (`open_chunk`, `open_log`). Check order as in `agent_hub.crypto.context`: key set (`header_invalid`), canonical
JSON (`non_ascii`), formats (`header_invalid`), signature (`signature_invalid`), chunks (`header_invalid`), a chunk
larger than `CHUNK_CT_BYTES` (`size_exceeded`), decryption (`decrypt_failed`).
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_runner.crypto import aead, fields
from agent_runner.crypto.context import chunk_hash, digest, digest_of_hashes
from agent_runner.crypto.encoding import P, b64u_decode, b64u_encode, canonical, require_bytes, require_int
from agent_runner.crypto.errors import fail
from agent_runner.crypto.kdf import KEY_BYTES, SALT_BYTES, hkdf
from agent_runner.crypto.keys import ed25519_sign, ed25519_verify_b64u

__all__ = ["chunk_hash", "digest", "digest_of_hashes"]

VERSION = 1
KIND = "log"
RUN_LOG = b"run-log\x00"
"""Info label of `k_log` and signature label of the manifest (without the prefix P)."""
LABEL_CHUNK = b"run-log-chunk\x00"
CHUNK_BYTES = 256 * 1024
"""At most this many output bytes per chunk."""
CHUNK_CT_BYTES = CHUNK_BYTES + aead.NONCE_BYTES + aead.TAG_BYTES
MIN_CT_BYTES = aead.NONCE_BYTES + aead.TAG_BYTES + 1
MAX_CHUNKS = 1_000_000
MAX_EPOCH = 2**31 - 1
MAX_BYTES = 2**53 - 1
MANIFEST_KEYS = frozenset(
    {
        "v",
        "kind",
        "ref",
        "object",
        "org",
        "wg",
        "task",
        "session",
        "author",
        "epoch",
        "salt",
        "chunks",
        "size",
        "digest",
        "bytes",
        "stored",
        "ts",
    }
)
ID_KEYS = ("object", "org", "wg", "task", "session", "author")
REF_RE = re.compile(r"(?:tr|j)-[0-9a-f]{12}")


def log_key(workgroup_key: bytes, salt: bytes, object_id: str) -> bytes:
    """`k_log = HKDF(WK_e, salt, info=P || "run-log\\0" || object_id)`."""
    require_bytes(workgroup_key, KEY_BYTES)
    return hkdf(
        workgroup_key, P + RUN_LOG + fields.uuid(object_id).encode("ascii"), salt=require_bytes(salt, SALT_BYTES)
    )


def chunk_aad(object_id: str, index: int) -> bytes:
    """`P || "run-log-chunk\\0" || canonical({"index": i, "object": object_id})`."""
    return P + LABEL_CHUNK + canonical({"index": require_int(index, 0, MAX_CHUNKS - 1), "object": object_id})


def seal_chunk(key: bytes, object_id: str, index: int, piece: bytes, nonce: bytes | None = None) -> bytes:
    """`nonce || AES-GCM(k_log, piece, AAD)`; an empty or too large piece is `size_exceeded`."""
    if not 1 <= len(piece) <= CHUNK_BYTES:
        fail("size_exceeded")
    return aead.seal_blob(key, piece, chunk_aad(fields.uuid(object_id), index), nonce)


def open_chunk(key: bytes, object_id: str, index: int, sealed: bytes) -> bytes:
    """The output bytes of one chunk (`size_exceeded` above `CHUNK_CT_BYTES`, `decrypt_failed` otherwise)."""
    if len(sealed) > CHUNK_CT_BYTES:
        fail("size_exceeded")
    return aead.open_blob(key, sealed, chunk_aad(fields.uuid(object_id), index))


def check_manifest(value: object) -> dict[str, object]:
    """Exact key set, canonical JSON and field formats (module docstring); returns the manifest."""
    manifest = fields.header_dict(value, MANIFEST_KEYS)
    fields.literal(manifest["v"], VERSION)
    fields.literal(manifest["kind"], KIND)
    if not isinstance(manifest["ref"], str) or REF_RE.fullmatch(manifest["ref"]) is None:
        fail("header_invalid")
    for key in ID_KEYS:
        fields.uuid(manifest[key])
    require_int(manifest["epoch"], 1, MAX_EPOCH)
    fields.b64u_bytes(manifest["salt"], SALT_BYTES)
    chunks = require_int(manifest["chunks"], 1, MAX_CHUNKS)
    require_int(manifest["size"], chunks * MIN_CT_BYTES, chunks * CHUNK_CT_BYTES)
    fields.b64u_bytes(manifest["digest"], 32)
    stored = require_int(manifest["stored"], (chunks - 1) * CHUNK_BYTES + 1, chunks * CHUNK_BYTES)
    require_int(manifest["bytes"], stored, MAX_BYTES)
    fields.time(manifest["ts"])
    return manifest


def manifest_text(manifest: dict[str, object]) -> bytes:
    """`P || "run-log\\0" || canonical(manifest)`, the signed text."""
    return P + RUN_LOG + canonical(manifest)


def sign_manifest(author: Ed25519PrivateKey, manifest: object) -> str:
    """The b64u signature of the authoring device over a checked manifest."""
    return b64u_encode(ed25519_sign(author, manifest_text(check_manifest(manifest))))


def verify_manifest(manifest: object, signature: object, author_sig_public: bytes) -> dict[str, object]:
    """Format, then the author's signature; returns the checked manifest."""
    checked = check_manifest(manifest)
    ed25519_verify_b64u(author_sig_public, signature, manifest_text(checked))
    return checked


def check_chunks(manifest: dict[str, object], chunks: Sequence[bytes]) -> None:
    """The sealed chunks fit the manifest: count, size and digest (`header_invalid`), each at most `CHUNK_CT_BYTES`
    (`size_exceeded`)."""
    if any(len(sealed) > CHUNK_CT_BYTES for sealed in chunks):
        fail("size_exceeded")
    if (
        len(chunks) != manifest["chunks"]
        or sum(len(sealed) for sealed in chunks) != manifest["size"]
        or digest(chunks) != manifest["digest"]
    ):
        fail("header_invalid")


def split(output: bytes) -> list[bytes]:
    """The output in pieces of at most `CHUNK_BYTES` (at least one; empty output is `size_exceeded`)."""
    if not output:
        fail("size_exceeded")
    return [output[start : start + CHUNK_BYTES] for start in range(0, len(output), CHUNK_BYTES)]


def seal_log(
    workgroup_key: bytes,
    ids: dict[str, str],
    *,
    ref: str,
    epoch: int,
    ts: str,
    output: bytes,
    total: int,
    author: Ed25519PrivateKey,
    salt: bytes,
    nonces: Sequence[bytes] | None = None,
) -> tuple[dict[str, object], str, list[bytes]]:
    """Seal kept output: `(manifest, sig, sealed chunks)`. `total` is the full output length (at least
    `len(output)`); `ids` names `object`, `org`, `wg`, `task`, `session` and `author`; `salt` is 32 random bytes per
    object (fixed together with `nonces` only by the test vectors)."""
    parts = split(output)
    if nonces is not None and len(nonces) != len(parts):
        fail("header_invalid")
    key = log_key(workgroup_key, salt, ids["object"])
    sealed = [
        seal_chunk(key, ids["object"], index, part, None if nonces is None else nonces[index])
        for index, part in enumerate(parts)
    ]
    manifest: dict[str, object] = {
        "v": VERSION,
        "kind": KIND,
        "ref": ref,
        **{name: ids[name] for name in ID_KEYS},
        "epoch": epoch,
        "salt": b64u_encode(salt),
        "chunks": len(sealed),
        "size": sum(len(item) for item in sealed),
        "digest": digest(sealed),
        "bytes": total,
        "stored": len(output),
        "ts": ts,
    }
    return manifest, sign_manifest(author, manifest), sealed


def open_log(
    workgroup_key: bytes, manifest: object, signature: object, author_sig_public: bytes, chunks: Sequence[bytes]
) -> bytes:
    """Verify manifest and chunks (module docstring), decrypt and join: the kept output."""
    checked = verify_manifest(manifest, signature, author_sig_public)
    check_chunks(checked, chunks)
    object_id = str(checked["object"])
    key = log_key(workgroup_key, b64u_decode(checked["salt"]), object_id)
    return b"".join(open_chunk(key, object_id, index, sealed) for index, sealed in enumerate(chunks))
