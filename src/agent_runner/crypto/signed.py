"""Copied from agent-core src/agent_hub/crypto/signed.py at 56db2db59b0e.

The shared shape of events (04-crypto.md section 4.3) and credential records (section 4.4).

`{"header": {...}, "ct": b64u(AES-GCM(k, plaintext, AAD = canonical(header))), "sig": b64u(Ed25519_author(P || label
|| canonical(header) || ct_raw))}` where `k = HKDF(scope_key, salt = header.salt, info = P || info_label)` and the
nonce is `header.nonce`. Adapted from Spike D `e2ee.seal_event`/`verify_event_signature`/`open_event`.

`seal` takes the header without `salt` and `nonce` and always draws both, so a caller cannot reuse them.

Check order (the same in every implementation, so that negative vectors yield one code): object key set
(`header_invalid`), header (key set `header_invalid`, then `canonical` = `non_ascii`, then field formats
`header_invalid`), `ct` encoding (`header_invalid`), size (`size_exceeded`), signature (`signature_invalid`),
decryption (`decrypt_failed`).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_runner.crypto import aead
from agent_runner.crypto.encoding import P, b64u_decode, b64u_encode, canonical, require_keys
from agent_runner.crypto.errors import fail
from agent_runner.crypto.kdf import SALT_BYTES
from agent_runner.crypto.keys import ed25519_sign, ed25519_verify_b64u

_OBJECT_KEYS = frozenset({"header", "ct", "sig"})


@dataclass(frozen=True)
class Format:
    """One signed AEAD format: header check, key derivation, signature label and size limit."""

    check_header: Callable[[object], dict[str, object]]
    derive_key: Callable[[bytes, bytes], bytes]
    sig_label: bytes
    max_ct: Callable[[dict[str, object]], int]


RANDOM_FIELDS = frozenset({"salt", "nonce"})
_PLACEHOLDER = {"salt": b64u_encode(bytes(SALT_BYTES)), "nonce": b64u_encode(bytes(aead.NONCE_BYTES))}


def check_fields(fmt: Format, fields: object) -> dict[str, object]:
    """Check a header without `salt` and `nonce` (these two are drawn by `seal`, never by the caller)."""
    if not isinstance(fields, dict) or RANDOM_FIELDS & set(fields):
        fail("header_invalid")
    fmt.check_header({**fields, **_PLACEHOLDER})
    return dict(fields)


def signature_text(fmt: Format, header: dict[str, object], ct_raw: bytes) -> bytes:
    """`P || label || canonical(header) || ct_raw`."""
    return P + fmt.sig_label + canonical(header) + ct_raw


def seal(
    fmt: Format,
    scope_key: bytes,
    fields: object,
    plaintext: bytes,
    author: Ed25519PrivateKey,
    *,
    fixed_salt_nonce: tuple[bytes, bytes] | None = None,
) -> dict[str, object]:
    """Encrypt and sign. `fields` is the header without `salt`/`nonce`: every call draws a fresh salt and nonce,
    so re-sealing the same header (e.g. a retried event ID with an edited body) never reuses key and nonce.
    `fixed_salt_nonce` exists only for deterministic test vectors.
    """
    check_fields(fmt, fields)
    salt, nonce = (os.urandom(SALT_BYTES), aead.random_nonce()) if fixed_salt_nonce is None else fixed_salt_nonce
    header = fmt.check_header(
        {**cast(dict[str, object], fields), "salt": b64u_encode(salt), "nonce": b64u_encode(nonce)}
    )
    key = fmt.derive_key(scope_key, salt)
    ct = aead.seal(key, nonce, plaintext, canonical(header))
    if len(ct) > fmt.max_ct(header):
        fail("size_exceeded")
    sig = ed25519_sign(author, signature_text(fmt, header, ct))
    return {"header": header, "ct": b64u_encode(ct), "sig": b64u_encode(sig)}


def verify(fmt: Format, sealed: object, author_sig_public: bytes) -> tuple[dict[str, object], bytes]:
    """Check structure, header, size and the author's signature without any key; returns header and raw ct."""
    data = require_keys(sealed, _OBJECT_KEYS)
    header = fmt.check_header(data["header"])
    ct = b64u_decode(data["ct"])
    if len(ct) > fmt.max_ct(header):
        fail("size_exceeded")
    ed25519_verify_b64u(author_sig_public, data["sig"], signature_text(fmt, header, ct))
    return header, ct


def open_(fmt: Format, scope_key: bytes, sealed: object, author_sig_public: bytes) -> bytes:
    """`verify`, then decrypt (`decrypt_failed` for a wrong scope key)."""
    header, ct = verify(fmt, sealed, author_sig_public)
    key = fmt.derive_key(scope_key, b64u_decode(header["salt"]))
    return aead.open_(key, b64u_decode(header["nonce"]), ct, canonical(header))
