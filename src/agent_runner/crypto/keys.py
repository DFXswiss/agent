"""Copied from agent-core src/agent_hub/crypto/keys.py at 07f6d21b261a.

Raw key formats (04-crypto.md sections 1 and 2): X25519 and Ed25519 as 32 raw bytes, P-256 session keys as the
65-byte uncompressed point with raw `r || s` signatures (the `agent-session-v1` scheme of `agent_core.passkey`).

`Identity` follows Spike D `e2ee.Identity`: the key pairs of a user or device, `kem` (X25519) to receive scope keys
and `sig` (Ed25519) to sign.
"""

from __future__ import annotations

from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from agent_runner.crypto.encoding import b64u_decode, b64u_encode, is_b64u, require_bytes
from agent_runner.crypto.errors import fail

RAW_KEY_BYTES = 32
ED25519_SIGNATURE_BYTES = 64
P256_POINT_BYTES = 65
P256_SIGNATURE_BYTES = 64
_P256_SCALAR_BYTES = 32
P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
ED25519_ORDER = 2**252 + 27742317777372353535851937790883648493
# Small-order Ed25519 public keys (the libsodium blacklist): the canonical encodings of the 8 torsion points plus the
# non-canonical y = p and y = p + 1, compared with the sign bit (top bit of byte 31) cleared. OpenSSL accepts a
# signature `R = key, S = 0` under such a key for every message, so they are refused before the library is called.
ED25519_SMALL_ORDER_KEYS = frozenset(
    bytes.fromhex(text)
    for text in (
        "0000000000000000000000000000000000000000000000000000000000000000",
        "0100000000000000000000000000000000000000000000000000000000000000",
        "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05",
        "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a",
        "ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
        "edffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
        "eeffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
    )
)


def x25519_private(raw: bytes) -> X25519PrivateKey:
    """An X25519 private key from its 32 raw bytes (`header_invalid` otherwise)."""
    return X25519PrivateKey.from_private_bytes(require_bytes(raw, RAW_KEY_BYTES))


def ed25519_private(seed: bytes) -> Ed25519PrivateKey:
    """An Ed25519 private key from its 32-byte seed (`header_invalid` otherwise)."""
    return Ed25519PrivateKey.from_private_bytes(require_bytes(seed, RAW_KEY_BYTES))


def x25519_private_raw(key: X25519PrivateKey) -> bytes:
    """The 32 raw private bytes."""
    return key.private_bytes_raw()


def ed25519_seed(key: Ed25519PrivateKey) -> bytes:
    """The 32-byte seed."""
    return key.private_bytes_raw()


def x25519_public_raw(key: X25519PrivateKey) -> bytes:
    """The 32-byte public key of a private key."""
    return key.public_key().public_bytes_raw()


def ed25519_public_raw(key: Ed25519PrivateKey) -> bytes:
    """The 32-byte public key of a private key."""
    return key.public_key().public_bytes_raw()


def ed25519_sign(key: Ed25519PrivateKey, message: bytes) -> bytes:
    """The 64-byte Ed25519 signature (deterministic)."""
    return key.sign(message)


def ed25519_small_order(public_key: bytes) -> bool:
    """Whether a 32-byte Ed25519 public key encodes a small-order point (either sign bit)."""
    return public_key[:31] + bytes([public_key[31] & 0x7F]) in ED25519_SMALL_ORDER_KEYS


def ed25519_verify(public_key: bytes, signature: bytes, message: bytes) -> None:
    """Verify an Ed25519 signature; a wrong signature, key or length is `signature_invalid`.

    Stricter than the library (ATS-03): a small-order public key and a non-canonical `S >= L` are refused before
    the library is called, identically in the TypeScript core (`ed25519Verify`).
    """
    if len(public_key) != RAW_KEY_BYTES or len(signature) != ED25519_SIGNATURE_BYTES:
        fail("signature_invalid")
    if ed25519_small_order(public_key) or int.from_bytes(signature[RAW_KEY_BYTES:], "little") >= ED25519_ORDER:
        fail("signature_invalid")
    valid = True
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
    except (InvalidSignature, ValueError):
        valid = False
    if not valid:
        fail("signature_invalid")


def ed25519_verify_b64u(public_key: bytes, signature: object, message: bytes) -> None:
    """`ed25519_verify` with the signature as b64u text (a structure field); malformed text is `signature_invalid`."""
    if not isinstance(signature, str) or not is_b64u(signature):
        fail("signature_invalid")
    ed25519_verify(public_key, b64u_decode(signature), message)


@dataclass(frozen=True)
class Identity:
    """The key pairs of a user or a device: X25519 `kem` and Ed25519 `sig`."""

    kem: X25519PrivateKey
    sig: Ed25519PrivateKey

    @staticmethod
    def generate() -> Identity:
        """Fresh random key pairs."""
        return Identity(X25519PrivateKey.generate(), Ed25519PrivateKey.generate())

    @staticmethod
    def from_raw(kem: bytes, sig: bytes) -> Identity:
        """From the 32-byte X25519 private key and the 32-byte Ed25519 seed."""
        return Identity(x25519_private(kem), ed25519_private(sig))

    def kem_public(self) -> bytes:
        """The 32-byte X25519 public key."""
        return x25519_public_raw(self.kem)

    def sig_public(self) -> bytes:
        """The 32-byte Ed25519 public key."""
        return ed25519_public_raw(self.sig)

    def public(self) -> dict[str, str]:
        """`{"kem": b64u, "sig": b64u}` as in log entries (`pub`)."""
        return {"kem": b64u_encode(self.kem_public()), "sig": b64u_encode(self.sig_public())}


def p256_private(scalar: bytes) -> ec.EllipticCurvePrivateKey:
    """A P-256 private key from its 32-byte big-endian scalar (1..n-1, else `header_invalid`)."""
    value = int.from_bytes(require_bytes(scalar, _P256_SCALAR_BYTES), "big")
    if not 1 <= value < P256_ORDER:
        fail("header_invalid")
    return ec.derive_private_key(value, ec.SECP256R1())


def p256_public_raw(key: ec.EllipticCurvePrivateKey) -> bytes:
    """The 65-byte uncompressed public point."""
    return key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)


def p256_sign_raw(key: ec.EllipticCurvePrivateKey, message: bytes, *, deterministic: bool = False) -> bytes:
    """ECDSA P-256/SHA-256 as raw `r || s` (64 bytes); `deterministic` (RFC 6979) only for test vectors."""
    der = key.sign(message, ec.ECDSA(hashes.SHA256(), deterministic_signing=deterministic))
    r, s = decode_dss_signature(der)
    return r.to_bytes(_P256_SCALAR_BYTES, "big") + s.to_bytes(_P256_SCALAR_BYTES, "big")


def p256_verify_raw(public_point: bytes, signature: bytes, message: bytes) -> None:
    """Verify a raw `r || s` signature under a 65-byte uncompressed point; any failure is `signature_invalid`."""
    if len(public_point) != P256_POINT_BYTES or public_point[0] != 0x04 or len(signature) != P256_SIGNATURE_BYTES:
        fail("signature_invalid")
    r = int.from_bytes(signature[:_P256_SCALAR_BYTES], "big")
    s = int.from_bytes(signature[_P256_SCALAR_BYTES:], "big")
    if not (1 <= r < P256_ORDER and 1 <= s < P256_ORDER):
        fail("signature_invalid")
    valid = True
    try:
        key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public_point)
        key.verify(encode_dss_signature(r, s), message, ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError):
        valid = False
    if not valid:
        fail("signature_invalid")
