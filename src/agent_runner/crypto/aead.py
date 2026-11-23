"""Copied from agent-core src/agent_hub/crypto/aead.py at 0346281bb510.

AES-256-GCM with a 12-byte nonce and a 16-byte tag (04-crypto.md section 1).

`seal_blob`/`open_blob` produce and read the `nonce(12) || ciphertext` form of identity bundles and invitation
packages (after Spike D `e2ee.wrap_bundle`/`seal_package`).
"""

from __future__ import annotations

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from agent_runner.crypto.encoding import require_bytes
from agent_runner.crypto.errors import fail

KEY_BYTES = 32
NONCE_BYTES = 12
TAG_BYTES = 16


def random_nonce() -> bytes:
    """12 random bytes."""
    return os.urandom(NONCE_BYTES)


def seal(key: bytes, nonce: bytes, plaintext: bytes, aad: bytes) -> bytes:
    """`ciphertext || tag`; key 32 bytes and nonce 12 bytes (`header_invalid` otherwise)."""
    aesgcm = AESGCM(require_bytes(key, KEY_BYTES))
    return aesgcm.encrypt(require_bytes(nonce, NONCE_BYTES), plaintext, aad)


def open_(key: bytes, nonce: bytes, ciphertext: bytes, aad: bytes) -> bytes:
    """The plaintext, or `decrypt_failed` for a wrong key, nonce, AAD or a tampered ciphertext."""
    aesgcm = AESGCM(require_bytes(key, KEY_BYTES))
    require_bytes(nonce, NONCE_BYTES)
    plaintext: bytes | None
    try:
        plaintext = aesgcm.decrypt(nonce, ciphertext, aad)
    except InvalidTag:
        plaintext = None
    if plaintext is None:
        fail("decrypt_failed")
    return plaintext


def seal_blob(key: bytes, plaintext: bytes, aad: bytes, nonce: bytes | None = None) -> bytes:
    """`nonce || AES-GCM(key, plaintext, aad)` with a random nonce unless one is given (test vectors)."""
    used = random_nonce() if nonce is None else nonce
    return used + seal(key, used, plaintext, aad)


def open_blob(key: bytes, blob: bytes, aad: bytes) -> bytes:
    """The inverse of `seal_blob`; anything shorter than nonce and tag is `decrypt_failed`."""
    if len(blob) < NONCE_BYTES + TAG_BYTES:
        fail("decrypt_failed")
    return open_(key, blob[:NONCE_BYTES], blob[NONCE_BYTES:], aad)
