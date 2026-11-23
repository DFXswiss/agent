"""Runner storage key and local sealing (04-crypto.md sections 2 and 3, 02-data-model.md section 4, 05-runner.md 1.4).

Not a copy of agent-core: only the runner keeps secrets at rest. Outbox bodies (plaintext item JSON) and scope keys
are sealed with the storage key and opened only into memory; cached credential records (already the hub
ciphertext, 02-data-model.md section 4) may be sealed as well so that a row cannot be swapped for an older version.
Decrypted credential material is never stored (05-runner.md section 2.6).

- storage key = `HKDF(ikm=device_sig_seed, info=P||"runner-storage")` (`kdf.storage_key`, shared vector
  `kdf.json` `storage_key`). It is derived from the device's Ed25519 seed in `keys/device.key`, so a store wipe
  or a copied database without the key file reveals nothing.
- `seal_local(key, purpose, context, plaintext)` = `nonce(12) || AES-256-GCM(key, plaintext, AAD)` with a fresh
  random nonce and `AAD = P || "runner-storage\\0" || purpose || "\\0" || context` (decisions/CRY-03.md). The AAD
  binds a ciphertext to its row, so a ciphertext copied into another row or another table does not open.
- `purpose` is one of `PURPOSES`; `context` names the row (printable ASCII without `\\0`, at most 1024
  characters), built by `outbox_context`, `scope_key_context` or `credential_context`.
- One key seals every row for the device's lifetime with random 96-bit nonces; the AES-GCM bound of 2^32 seals per
  key is far above the runner's volume (decisions/CRY-03.md).

A wrong key, purpose, context or a modified or truncated blob is `decrypt_failed`; a key that is not 32 bytes is
`header_invalid` (the codes of `aead`).
"""

from __future__ import annotations

from agent_runner.crypto import aead, kdf
from agent_runner.crypto.encoding import P, is_printable_ascii

LABEL_STORAGE = kdf.RUNNER_STORAGE + b"\0"
PURPOSES = frozenset({"outbox", "scope_key", "credential"})
SCOPES = frozenset({"org", "wg", "cred"})
MAX_CONTEXT = 1024
MAX_BIGINT = 2**63 - 1


def storage_key(device_sig_seed: bytes) -> bytes:
    """The storage key of the device whose Ed25519 seed is `device_sig_seed` (32 bytes, else `header_invalid`)."""
    return kdf.storage_key(device_sig_seed)


def local_aad(purpose: str, context: str) -> bytes:
    """`P || "runner-storage\\0" || purpose || "\\0" || context`; ValueError for an unknown purpose or bad context."""
    if purpose not in PURPOSES:
        raise ValueError(f"unknown storage purpose: {purpose!r}")
    if not context or len(context) > MAX_CONTEXT or not is_printable_ascii(context):
        raise ValueError("storage context must be 1-1024 printable ASCII characters")
    return P + LABEL_STORAGE + purpose.encode("ascii") + b"\0" + context.encode("ascii")


def seal_local(key: bytes, purpose: str, context: str, plaintext: bytes) -> bytes:
    """`nonce || AES-GCM(key, plaintext, local_aad(purpose, context))` with a fresh random nonce."""
    return aead.seal_blob(key, plaintext, local_aad(purpose, context))


def open_local(key: bytes, purpose: str, context: str, blob: bytes) -> bytes:
    """The plaintext of `seal_local`, or `decrypt_failed`."""
    return aead.open_blob(key, blob, local_aad(purpose, context))


def _part(value: str, name: str) -> str:
    if not value or ":" in value:
        raise ValueError(f"{name} must be non-empty and free of ':'")
    return value


def _count(value: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_BIGINT:
        raise ValueError(f"{name} must be an integer from 0 to 2^63-1")
    return value


def outbox_context(origin_seq: int, item_kind: str) -> str:
    """Context of `outbox.body_ct`: `<origin_seq>:<item_kind>` (both columns are authenticated)."""
    return f"{_count(origin_seq, 'origin_seq')}:{_part(item_kind, 'item_kind')}"


def scope_key_context(scope: str, scope_id: str, epoch: int) -> str:
    """Context of `scope_key.key_ct`: `<scope>:<scope_id>:<epoch>`."""
    if scope not in SCOPES:
        raise ValueError(f"unknown scope: {scope!r}")
    return f"{scope}:{_part(scope_id, 'scope_id')}:{_count(epoch, 'epoch')}"


def credential_context(owner_id: str, kind: str, version: int) -> str:
    """Context of a cached credential record (`credential_cache`): `<owner_id>:<kind>:<version>`."""
    return f"{_part(owner_id, 'owner_id')}:{_part(kind, 'kind')}:{_count(version, 'version')}"
