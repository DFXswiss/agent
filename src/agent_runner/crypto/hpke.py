"""Copied from agent-core src/agent_hub/crypto/hpke.py at 07f6d21b261a.

HPKE RFC 9180 base mode, DHKEM(X25519, HKDF-SHA256), HKDF-SHA256, AES-256-GCM, single shot.

Adapted from Spike D `e2ee.py` (`_labeled_extract`, `_labeled_expand`, `_shared_secret`, `_key_schedule`,
`hpke_seal`, `hpke_open`): the scheme is built from pyca primitives because pyca's `hpke.Suite` does not accept a
fixed ephemeral key, which the shared test vectors need. The tests check both directions against `hpke.Suite`.
Output form: `enc(32) || ciphertext`.
"""

from __future__ import annotations

from cryptography.hazmat.primitives import hashes, hmac
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey

from agent_runner.crypto import aead
from agent_runner.crypto.encoding import require_bytes
from agent_runner.crypto.errors import fail

KEM_ID = 0x0020  # DHKEM(X25519, HKDF-SHA256)
KDF_ID = 0x0001  # HKDF-SHA256
AEAD_ID = 0x0002  # AES-256-GCM
KEM_SUITE_ID = b"KEM" + KEM_ID.to_bytes(2, "big")
HPKE_SUITE_ID = b"HPKE" + KEM_ID.to_bytes(2, "big") + KDF_ID.to_bytes(2, "big") + AEAD_ID.to_bytes(2, "big")
ENC_BYTES = 32
_HASH_BYTES = 32
_MODE_BASE = b"\x00"


def _hmac(key: bytes, data: bytes) -> bytes:
    mac = hmac.HMAC(key, hashes.SHA256())
    mac.update(data)
    return mac.finalize()


def _labeled_extract(salt: bytes, label: bytes, ikm: bytes, suite: bytes) -> bytes:
    return _hmac(salt or bytes(_HASH_BYTES), b"HPKE-v1" + suite + label + ikm)


def _labeled_expand(prk: bytes, label: bytes, info: bytes, length: int, suite: bytes) -> bytes:
    labeled_info = length.to_bytes(2, "big") + b"HPKE-v1" + suite + label + info
    out, block, counter = b"", b"", 1
    while len(out) < length:
        block = _hmac(prk, block + labeled_info + bytes([counter]))
        out += block
        counter += 1
    return out[:length]


def _shared_secret(dh: bytes, enc: bytes, pk_rm: bytes) -> bytes:
    eae_prk = _labeled_extract(b"", b"eae_prk", dh, KEM_SUITE_ID)
    return _labeled_expand(eae_prk, b"shared_secret", enc + pk_rm, _HASH_BYTES, KEM_SUITE_ID)


def _key_schedule(shared_secret: bytes, info: bytes) -> tuple[bytes, bytes]:
    psk_id_hash = _labeled_extract(b"", b"psk_id_hash", b"", HPKE_SUITE_ID)
    info_hash = _labeled_extract(b"", b"info_hash", info, HPKE_SUITE_ID)
    context = _MODE_BASE + psk_id_hash + info_hash
    secret = _labeled_extract(shared_secret, b"secret", b"", HPKE_SUITE_ID)
    key = _labeled_expand(secret, b"key", context, aead.KEY_BYTES, HPKE_SUITE_ID)
    base_nonce = _labeled_expand(secret, b"base_nonce", context, aead.NONCE_BYTES, HPKE_SUITE_ID)
    return key, base_nonce


def _exchange(private: X25519PrivateKey, public_raw: bytes) -> bytes:
    shared: bytes | None
    try:
        shared = private.exchange(X25519PublicKey.from_public_bytes(public_raw))
    except ValueError:
        # a small-order point yields the all-zero shared secret, which X25519 refuses (RFC 9180 section 7.1.4)
        shared = None
    if shared is None:
        fail("decrypt_failed")
    return shared


def setup_values(ephemeral: X25519PrivateKey, pk_r: bytes, info: bytes) -> dict[str, bytes]:
    """The intermediate values of the sender setup (`shared_secret`, `key`, `base_nonce`) for test vectors, so that
    another implementation can locate a mismatch."""
    enc = ephemeral.public_key().public_bytes_raw()
    shared_secret = _shared_secret(_exchange(ephemeral, require_bytes(pk_r, ENC_BYTES)), enc, pk_r)
    key, base_nonce = _key_schedule(shared_secret, info)
    return {"shared_secret": shared_secret, "key": key, "base_nonce": base_nonce}


def seal(pk_r: bytes, info: bytes, plaintext: bytes, ephemeral: X25519PrivateKey | None = None) -> bytes:
    """Single-shot seal to the 32-byte X25519 key `pk_r` with empty AAD; returns `enc || ciphertext`.

    `ephemeral` is a parameter only for deterministic test vectors; production callers leave it out.
    """
    require_bytes(pk_r, ENC_BYTES)
    eph = X25519PrivateKey.generate() if ephemeral is None else ephemeral
    enc = eph.public_key().public_bytes_raw()
    key, nonce = _key_schedule(_shared_secret(_exchange(eph, pk_r), enc, pk_r), info)
    return enc + aead.seal(key, nonce, plaintext, b"")


def open_(sk_r: X25519PrivateKey, info: bytes, sealed: bytes) -> bytes:
    """The inverse of `seal`: `hpke_short` below `enc` + tag, `decrypt_failed` for every other failure."""
    if len(sealed) < ENC_BYTES + aead.TAG_BYTES:
        fail("hpke_short")
    enc = sealed[:ENC_BYTES]
    pk_rm = sk_r.public_key().public_bytes_raw()
    key, nonce = _key_schedule(_shared_secret(_exchange(sk_r, enc), enc, pk_rm), info)
    return aead.open_(key, nonce, sealed[ENC_BYTES:], b"")
