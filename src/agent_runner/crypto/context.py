"""Copied from src/agent_hub/crypto/context.py.

Context checkpoints and pull-request link tags (04-crypto.md sections 3 and 4.11, ADR-072 sections 3 and 8).

A checkpoint is the gzip-compressed context archive of one main session, split into chunks of at most `CHUNK_BYTES`
compressed bytes and sealed under the object key of the task's workgroup:

- `k_ctx = HKDF(ikm=WK_e, salt=manifest.salt (32 B), info=P || "context\\0" || object_id)`,
- chunk `i` = `nonce(12) || AES-256-GCM(k_ctx, chunk, AAD = P || "context-chunk\\0" ||
  canonical({"index": i, "object": object_id}))`,
- manifest (canonical JSON, clear) with exactly the keys `MANIFEST_KEYS`; `size` is the sum of the sealed chunk
  lengths, `digest` the b64u SHA-256 over the concatenated SHA-256 of every sealed chunk in index order,
- `sig = Ed25519_author(P || "context\\0" || canonical(manifest))` by the authoring device.

The hub stores and checks the manifest, the signature, the chunk count, the size and the digest without any key; a
reader with `WK_e` opens the chunks (`open_context`). Pull-request links are opaque:
`tag = b64u(HMAC-SHA256(HKDF(ikm=WK_e, info=P || "pr-link"), canonical({"number": n, "repo": "<owner>/<name>"})))`
with the repository name in lower case. The sealed body of a link (section 4.11a, ADR-084 section 1) names the pull
request for a reader with `WK_e`:

- `k_body = HKDF(ikm=WK_e, info=P || "pr-link-body")`,
- `sealed = b64u(nonce(12) || AES-256-GCM(k_body, pad(canonical({"number": n, "repo": "<owner>/<name>"})), AAD))`
  with the repository as GitHub spells it, padded with zero bytes to `PR_LINK_BODY_BYTES`,
- `AAD = P || "pr-link-body\0" || wg_id || "\0" || epoch || "\0" || tag || "\0" || task_id || "\0" || origin` with
  `origin` = `device:<session_id>` or `user:<linked_by>`.

`open_pr_link` refuses a sealed text of another length (`header_invalid`), a body that does not open
(`decrypt_failed`), a padding or plaintext that is not exactly the sealed form (`header_invalid`) and a body whose
recomputed tag is not the row's tag (`header_invalid`): a hub can move a link to no other task, workgroup, origin or
pull request.

Check order (the same in every implementation): manifest key set (`header_invalid`), canonical JSON (`non_ascii`),
field formats (`header_invalid`); signature (`signature_invalid`); chunk count, size and digest against the chunks
(`header_invalid`); a chunk larger than `CHUNK_CT_BYTES` (`size_exceeded`); decryption (`decrypt_failed`).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence

from cryptography.hazmat.primitives import hashes, hmac
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_runner.crypto import aead, fields
from agent_runner.crypto.encoding import (
    P,
    b64u_decode,
    b64u_encode,
    canonical,
    parse_json,
    require_bytes,
    require_int,
    require_keys,
)
from agent_runner.crypto.errors import fail
from agent_runner.crypto.kdf import KEY_BYTES, SALT_BYTES, hkdf
from agent_runner.crypto.keys import ed25519_sign, ed25519_verify_b64u

VERSION = 1
CONTEXT = b"context\x00"
"""Info label of `k_ctx` and signature label of the manifest (without the prefix P)."""
LABEL_CONTEXT_CHUNK = b"context-chunk\x00"
PR_LINK = b"pr-link"
PR_LINK_BODY = b"pr-link-body"
"""Info label of `k_body` (without the prefix P)."""
LABEL_PR_LINK_BODY = b"pr-link-body\x00"
PR_LINK_BODY_BYTES = 256
"""The padded plaintext of a sealed link body."""
PR_LINK_SEALED_BYTES = aead.NONCE_BYTES + PR_LINK_BODY_BYTES + aead.TAG_BYTES
"""The decoded length of every `sealed` text."""
CHUNK_BYTES = 2 * 1024 * 1024
"""At most this many compressed archive bytes per chunk."""
CHUNK_CT_BYTES = CHUNK_BYTES + aead.NONCE_BYTES + aead.TAG_BYTES
"""The largest sealed chunk."""
MIN_CT_BYTES = aead.NONCE_BYTES + aead.TAG_BYTES + 1
"""The smallest sealed chunk (a chunk holds at least one byte)."""
MAX_CHUNKS = 1_000_000
MAX_EPOCH = 2**31 - 1
MANIFEST_KEYS = frozenset(
    {"v", "object", "org", "wg", "task", "session", "author", "epoch", "salt", "chunks", "size", "digest", "ts"}
)
ID_KEYS = ("object", "org", "wg", "task", "session", "author")
_REPO_RE = re.compile(r"[a-z0-9_.-]{1,100}/[a-z0-9_.-]{1,100}")
_ORIGIN_KINDS = ("device:", "user:")


def context_key(workgroup_key: bytes, salt: bytes, object_id: str) -> bytes:
    """`k_ctx = HKDF(WK_e, salt, info=P || "context\\0" || object_id)`."""
    require_bytes(workgroup_key, KEY_BYTES)
    return hkdf(
        workgroup_key, P + CONTEXT + fields.uuid(object_id).encode("ascii"), salt=require_bytes(salt, SALT_BYTES)
    )


def chunk_aad(object_id: str, index: int) -> bytes:
    """`P || "context-chunk\\0" || canonical({"index": i, "object": object_id})`."""
    return P + LABEL_CONTEXT_CHUNK + canonical({"index": require_int(index, 0, MAX_CHUNKS - 1), "object": object_id})


def seal_chunk(key: bytes, object_id: str, index: int, chunk: bytes, nonce: bytes | None = None) -> bytes:
    """`nonce || AES-GCM(k_ctx, chunk, AAD)`; an empty or too large chunk is `size_exceeded`."""
    if not 1 <= len(chunk) <= CHUNK_BYTES:
        fail("size_exceeded")
    return aead.seal_blob(key, chunk, chunk_aad(fields.uuid(object_id), index), nonce)


def open_chunk(key: bytes, object_id: str, index: int, sealed: bytes) -> bytes:
    """The compressed bytes of one chunk (`size_exceeded` above `CHUNK_CT_BYTES`, `decrypt_failed` otherwise)."""
    if len(sealed) > CHUNK_CT_BYTES:
        fail("size_exceeded")
    return aead.open_blob(key, sealed, chunk_aad(fields.uuid(object_id), index))


def chunk_hash(sealed: bytes) -> bytes:
    """SHA-256 of one sealed chunk (the hub stores it per chunk)."""
    return hashlib.sha256(sealed).digest()


def digest_of_hashes(hashes_in_order: Sequence[bytes]) -> str:
    """b64u SHA-256 over the concatenated chunk hashes in index order."""
    return b64u_encode(hashlib.sha256(b"".join(hashes_in_order)).digest())


def digest(chunks: Sequence[bytes]) -> str:
    """The manifest `digest` of the sealed chunks in index order."""
    return digest_of_hashes([chunk_hash(sealed) for sealed in chunks])


def check_manifest(value: object) -> dict[str, object]:
    """Exact key set, canonical JSON and field formats (module docstring); returns the manifest."""
    manifest = fields.header_dict(value, MANIFEST_KEYS)
    fields.literal(manifest["v"], VERSION)
    for key in ID_KEYS:
        fields.uuid(manifest[key])
    require_int(manifest["epoch"], 1, MAX_EPOCH)
    fields.b64u_bytes(manifest["salt"], SALT_BYTES)
    chunks = require_int(manifest["chunks"], 1, MAX_CHUNKS)
    require_int(manifest["size"], chunks * MIN_CT_BYTES, chunks * CHUNK_CT_BYTES)
    fields.b64u_bytes(manifest["digest"], 32)
    fields.time(manifest["ts"])
    return manifest


def manifest_text(manifest: dict[str, object]) -> bytes:
    """`P || "context\\0" || canonical(manifest)`, the signed text."""
    return P + CONTEXT + canonical(manifest)


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


def split(compressed: bytes) -> list[bytes]:
    """The compressed archive in chunks of at most `CHUNK_BYTES` (at least one chunk; empty input is
    `size_exceeded`)."""
    if not compressed:
        fail("size_exceeded")
    return [compressed[start : start + CHUNK_BYTES] for start in range(0, len(compressed), CHUNK_BYTES)]


def seal_context(
    workgroup_key: bytes,
    ids: dict[str, str],
    *,
    epoch: int,
    ts: str,
    compressed: bytes,
    author: Ed25519PrivateKey,
    salt: bytes,
    nonces: Sequence[bytes] | None = None,
) -> tuple[dict[str, object], str, list[bytes]]:
    """Seal a compressed archive: `(manifest, sig, sealed chunks)`.

    `ids` names `object`, `org`, `wg`, `task`, `session` and `author`; `salt` is 32 random bytes per object (fixed
    together with `nonces` only by the test vectors)."""
    parts = split(compressed)
    if nonces is not None and len(nonces) != len(parts):
        fail("header_invalid")
    key = context_key(workgroup_key, salt, ids["object"])
    sealed = [
        seal_chunk(key, ids["object"], index, part, None if nonces is None else nonces[index])
        for index, part in enumerate(parts)
    ]
    manifest: dict[str, object] = {
        "v": VERSION,
        **{name: ids[name] for name in ID_KEYS},
        "epoch": epoch,
        "salt": b64u_encode(salt),
        "chunks": len(sealed),
        "size": sum(len(item) for item in sealed),
        "digest": digest(sealed),
        "ts": ts,
    }
    return manifest, sign_manifest(author, manifest), sealed


def open_context(
    workgroup_key: bytes, manifest: object, signature: object, author_sig_public: bytes, chunks: Sequence[bytes]
) -> bytes:
    """Verify manifest and chunks (module docstring), decrypt and join: the compressed archive."""
    checked = verify_manifest(manifest, signature, author_sig_public)
    check_chunks(checked, chunks)
    object_id = str(checked["object"])
    key = context_key(workgroup_key, b64u_decode(checked["salt"]), object_id)
    return b"".join(open_chunk(key, object_id, index, sealed) for index, sealed in enumerate(chunks))


def pr_link_key(workgroup_key: bytes) -> bytes:
    """`HKDF(WK_e, info=P || "pr-link")`."""
    return hkdf(require_bytes(workgroup_key, KEY_BYTES), P + PR_LINK)


def pr_link_text(repo: object, number: object) -> bytes:
    """`canonical({"number": n, "repo": repo})` with the repository `<owner>/<name>` in lower case."""
    if not isinstance(repo, str):
        fail("header_invalid")
    lowered = repo.lower()
    text = canonical({"number": require_int(number, 1), "repo": lowered})
    if _REPO_RE.fullmatch(lowered) is None:
        fail("header_invalid")
    return text


def pr_link_tag(link_key: bytes, repo: object, number: object) -> str:
    """The b64u tag of a pull request (the hub never learns the repository or the number)."""
    mac = hmac.HMAC(require_bytes(link_key, KEY_BYTES), hashes.SHA256())
    mac.update(pr_link_text(repo, number))
    return b64u_encode(mac.finalize())


def pr_link_body_key(workgroup_key: bytes) -> bytes:
    """`k_body = HKDF(WK_e, info=P || "pr-link-body")`."""
    return hkdf(require_bytes(workgroup_key, KEY_BYTES), P + PR_LINK_BODY)


def pr_link_origin(origin: object) -> str:
    """`device:<session_id>` or `user:<linked_by>` with a UUIDv7, else `header_invalid`."""
    if not isinstance(origin, str):
        fail("header_invalid")
    kind = next((prefix for prefix in _ORIGIN_KINDS if origin.startswith(prefix)), None)
    if kind is None:
        fail("header_invalid")
    fields.uuid(origin[len(kind) :])
    return origin


def pr_link_body_aad(wg_id: object, epoch: object, tag: object, task_id: object, origin: object) -> bytes:
    """`P || "pr-link-body\0" || wg_id || "\0" || epoch || "\0" || tag || "\0" || task_id || "\0" || origin`."""
    parts = (
        fields.uuid(wg_id),
        str(require_int(epoch, 1, MAX_EPOCH)),
        fields.b64u_bytes(tag, 32),
        fields.uuid(task_id),
        pr_link_origin(origin),
    )
    return P + LABEL_PR_LINK_BODY + b"\x00".join(part.encode("ascii") for part in parts)


def pr_link_body_text(repo: object, number: object) -> bytes:
    """`canonical({"number": n, "repo": repo})` with the repository as given (checked like `pr_link_text`)."""
    pr_link_text(repo, number)
    return canonical({"number": number, "repo": repo})


def seal_pr_link(
    body_key: bytes,
    *,
    wg_id: str,
    epoch: int,
    tag: str,
    task_id: str,
    origin: str,
    repo: str,
    number: int,
    nonce: bytes | None = None,
) -> str:
    """The b64u `sealed` body of a link (module docstring); `nonce` is fixed only by the test vectors."""
    aad = pr_link_body_aad(wg_id, epoch, tag, task_id, origin)
    text = pr_link_body_text(repo, number)
    padded = text + bytes(PR_LINK_BODY_BYTES - len(text))
    return b64u_encode(aead.seal_blob(require_bytes(body_key, KEY_BYTES), padded, aad, nonce))


def open_pr_link(
    body_key: bytes,
    link_key: bytes,
    *,
    wg_id: str,
    epoch: int,
    tag: str,
    task_id: str,
    origin: str,
    sealed: object,
) -> tuple[str, int]:
    """`(repo, number)` of a sealed body whose recomputed tag is `tag`; every refusal as in the module docstring."""
    aad = pr_link_body_aad(wg_id, epoch, tag, task_id, origin)
    blob = b64u_decode(sealed, PR_LINK_SEALED_BYTES)
    padded = aead.open_blob(require_bytes(body_key, KEY_BYTES), blob, aad)
    text = padded.rstrip(b"\x00")
    body = require_keys(parse_json(text), frozenset({"number", "repo"}))
    repo, number = body["repo"], body["number"]
    if pr_link_body_text(repo, number) != text or pr_link_tag(link_key, repo, number) != tag:
        fail("header_invalid")
    return str(repo), require_int(number, 1)
