"""Copied from agent-core src/agent_hub/crypto/bundle.py at e051302eeb8e.

Identity bundle (04-crypto.md section 4.1), after Spike D `e2ee.wrap_bundle`/`unwrap_bundle`/`Identity.bundle`.

Plaintext `canonical({"kem","sig","v":1})`, ciphertext `b64u(nonce12 || AES-GCM(KEK, plaintext, AAD))` with
`AAD = P || "bundle\\0" || user_id || "\\0" || slot`. Slots: `passkey:<b64u(credential_id)>` and `recovery`.
"""

from __future__ import annotations

import re

from agent_runner.crypto import aead
from agent_runner.crypto.encoding import (
    P,
    b64u_decode,
    b64u_encode,
    canonical,
    is_printable_ascii,
    parse_json,
    require_bytes,
    require_int,
    require_keys,
)
from agent_runner.crypto.errors import fail
from agent_runner.crypto.kdf import KEY_BYTES, LABEL_BUNDLE
from agent_runner.crypto.keys import RAW_KEY_BYTES, Identity, ed25519_seed, x25519_private_raw

RECOVERY_SLOT = "recovery"
_PASSKEY_SLOT_RE = re.compile(r"passkey:[A-Za-z0-9_-]+")
_BUNDLE_KEYS = frozenset({"kem", "sig", "v"})


def passkey_slot(credential_id: bytes) -> str:
    """The slot name of a passkey: `passkey:<b64u(credential_id)>`."""
    if not credential_id:
        fail("header_invalid")
    return "passkey:" + b64u_encode(credential_id)


def check_slot(slot: object) -> str:
    """`recovery` or a passkey slot with a canonical, non-empty credential ID (`header_invalid` otherwise)."""
    if slot == RECOVERY_SLOT:
        return slot
    if not isinstance(slot, str) or not _PASSKEY_SLOT_RE.fullmatch(slot):
        fail("header_invalid")
    b64u_decode(slot.removeprefix("passkey:"))
    return slot


def bundle_aad(user_id: str, slot: str) -> bytes:
    """`P || "bundle\\0" || user_id || "\\0" || slot`; both parts printable ASCII (`non_ascii`)."""
    if not isinstance(user_id, str) or not user_id:
        fail("header_invalid")
    if not is_printable_ascii(user_id):
        fail("non_ascii")
    return P + LABEL_BUNDLE + user_id.encode("ascii") + b"\x00" + check_slot(slot).encode("ascii")


def bundle_plaintext(identity: Identity) -> bytes:
    """`canonical({"kem": b64u(x25519 private), "sig": b64u(ed25519 seed), "v": 1})`."""
    return canonical(
        {"kem": b64u_encode(x25519_private_raw(identity.kem)), "sig": b64u_encode(ed25519_seed(identity.sig)), "v": 1}
    )


def parse_bundle_plaintext(plaintext: bytes) -> Identity:
    """The identity of a bundle plaintext; exact key set, `v == 1`, canonical bytes (`header_invalid`)."""
    data = require_keys(parse_json(plaintext), _BUNDLE_KEYS)
    require_int(data["v"], 1, 1)
    if canonical(data) != plaintext:
        fail("header_invalid")
    kem = b64u_decode(data["kem"], RAW_KEY_BYTES)
    sig = b64u_decode(data["sig"], RAW_KEY_BYTES)
    return Identity.from_raw(kem, sig)


def seal_bundle(kek: bytes, user_id: str, slot: str, identity: Identity, nonce: bytes | None = None) -> str:
    """Encrypt the identity for one slot; `nonce` only for test vectors."""
    require_bytes(kek, KEY_BYTES)
    return b64u_encode(aead.seal_blob(kek, bundle_plaintext(identity), bundle_aad(user_id, slot), nonce))


def open_bundle(kek: bytes, user_id: str, slot: str, sealed: str) -> Identity:
    """Decrypt a slot (`decrypt_failed` for a wrong KEK, user or slot) and parse the identity."""
    aad = bundle_aad(user_id, slot)
    return parse_bundle_plaintext(aead.open_blob(kek, b64u_decode(sealed), aad))
