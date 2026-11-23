"""Copied from agent-core src/agent_hub/crypto/invite.py at 07f6d21b261a.

Invitation codes and packages (04-crypto.md sections 3 and 4.6), after Spike D `e2ee.invite_secrets`,
`format_code`, `parse_code`, `seal_package` and `open_package`.

Code: 16 random bytes, shown as base32 (RFC 4648, uppercase, no padding) in groups `XXXXX-XXXXX-XXXXX-XXXXX-XXXXXX`.
The parser strips `-` and spaces and uppercases; then a character outside the alphabet or non-zero trailing bits is
`invite_code_invalid` and a wrong length is `invite_code_length` (checked in this order). Recovery codes use the same
text form (`agent_hub.crypto.recovery`).

Package **(changed)**: `b64u(nonce || AES-GCM(package_key, body_json(package), AAD = P || "invite-package\\0" ||
invite_id))`; the plaintext is body JSON (non-ASCII names allowed). A package that does not open is
`invite_code_invalid` (section 5.1 step 2); a package with a wrong structure is `header_invalid`.

Registration proof **(new, KOR-01, ADR-043)**: `inv_sig = Ed25519_{sk_inv}(P || "invite-register\\0" || user_id ||
"\\0" || challenge)` over the raw 32-byte challenge of the registration ceremony (`register/begin`): only the holder of
the code can register through an invitation, even when its `invite_id` is readable (platform log, membership log).
"""

from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_runner.crypto import aead, fields
from agent_runner.crypto.encoding import (
    P,
    b64u_decode,
    b64u_encode,
    body_json,
    is_printable_ascii,
    parse_json,
    require_bytes,
    require_int,
    require_keys,
)
from agent_runner.crypto.errors import CryptoError, fail
from agent_runner.crypto.kdf import (
    CODE_BYTES,
    LABEL_INVITE_PACKAGE,
    LABEL_INVITE_REGISTER,
    invite_id_raw,
    invite_sign_seed,
    package_key,
)
from agent_runner.crypto.keys import (
    RAW_KEY_BYTES,
    ed25519_private,
    ed25519_public_raw,
    ed25519_sign,
    ed25519_verify_b64u,
)

CODE_CHARS = 26
_BASE32_ANY_CASE_RE = re.compile(r"[A-Za-z2-7]*")
_HUB_KEY_ID_RE = re.compile(r"[a-z0-9-]{1,64}")
PACKAGE_VERSION = 1
PACKAGE_KEYS = frozenset({"org", "org_name", "wg_names", "founder", "head", "keys", "hub_key", "v"})
_HEAD_KEYS = frozenset({"seq", "hash"})
_KEY_KEYS = frozenset({"scope", "scope_id", "epoch", "key"})
_HUB_KEY_KEYS = frozenset({"id", "public_key"})
PACKAGE_SCOPES = frozenset({"org", "wg"})
HASH_BYTES = 32
CHALLENGE_BYTES = 32


def generate_code() -> bytes:
    """16 random bytes."""
    return os.urandom(CODE_BYTES)


def format_code(code: bytes) -> str:
    """`XXXXX-XXXXX-XXXXX-XXXXX-XXXXXX` (26 base32 characters)."""
    if len(code) != CODE_BYTES:
        fail("invite_code_length")
    text = base64.b32encode(code).decode("ascii").rstrip("=")
    return "-".join(text[i : i + 5] for i in range(0, 20, 5)) + "-" + text[20:]


def parse_code_text(text: object, length_error: str) -> bytes:
    """The 16 bytes of a code text; `length_error` names the code for a wrong length."""
    if not isinstance(text, str):
        fail("invite_code_invalid")
    clean = text.replace("-", "").replace(" ", "")
    # the alphabet is checked before uppercasing: str.upper() maps some non-ASCII letters to ASCII ones
    if not _BASE32_ANY_CASE_RE.fullmatch(clean):
        fail("invite_code_invalid")
    clean = clean.upper()
    if len(clean) != CODE_CHARS:
        fail(length_error)
    raw = base64.b32decode(clean + "======")
    if base64.b32encode(raw).decode("ascii").rstrip("=") != clean:
        fail("invite_code_invalid")
    return raw


def parse_code(text: object) -> bytes:
    """The 16 bytes of an invitation code text."""
    return parse_code_text(text, "invite_code_length")


@dataclass(frozen=True)
class InviteSecrets:
    """What a code yields: `invite_id` (the only value the hub sees), `package_key` and `sk_inv`/`pk_inv`."""

    invite_id: str
    package_key: bytes
    signing: Ed25519PrivateKey

    @property
    def pk_inv(self) -> str:
        """b64u of the 32-byte Ed25519 public key of `sk_inv`."""
        return b64u_encode(ed25519_public_raw(self.signing))


def invite_id(code: bytes) -> str:
    """`b64u(HKDF(code, info=P||"invite-id")[:16])`."""
    return b64u_encode(invite_id_raw(code))


def invite_secrets(code: bytes) -> InviteSecrets:
    """All derivations of a 16-byte code (`invite_code_length` otherwise)."""
    return InviteSecrets(invite_id(code), package_key(code), ed25519_private(invite_sign_seed(code)))


def package_aad(invite_id_text: str) -> bytes:
    """`P || "invite-package\\0" || invite_id`."""
    fields.b64u_bytes(invite_id_text, 16)
    return P + LABEL_INVITE_PACKAGE + invite_id_text.encode("ascii")


def check_package(value: object) -> dict[str, object]:
    """Exact key sets and value formats of the package (section 4.6)."""
    package = require_keys(value, PACKAGE_KEYS)
    fields.literal(package["v"], PACKAGE_VERSION)
    fields.uuid(package["org"])
    if not isinstance(package["org_name"], str):
        fail("header_invalid")
    names = package["wg_names"]
    if not isinstance(names, dict) or not all(isinstance(name, str) for name in names.values()):
        fail("header_invalid")
    for wg in names:
        fields.uuid(wg)
    fields.b64u_bytes(package["founder"], RAW_KEY_BYTES)
    head = require_keys(package["head"], _HEAD_KEYS)
    require_int(head["seq"], 0)
    fields.b64u_bytes(head["hash"], HASH_BYTES)
    keys = package["keys"]
    if not isinstance(keys, list) or not keys:
        fail("header_invalid")
    for item in keys:
        entry = require_keys(item, _KEY_KEYS)
        if entry["scope"] not in PACKAGE_SCOPES:
            fail("header_invalid")
        fields.uuid(entry["scope_id"])
        require_int(entry["epoch"], 1)
        fields.b64u_bytes(entry["key"], 32)
    hub_key = require_keys(package["hub_key"], _HUB_KEY_KEYS)
    if not isinstance(hub_key["id"], str) or not _HUB_KEY_ID_RE.fullmatch(hub_key["id"]):
        fail("header_invalid")
    fields.b64u_bytes(hub_key["public_key"], RAW_KEY_BYTES)
    return package


def seal_package(secrets: InviteSecrets, package: object, nonce: bytes | None = None) -> str:
    """Encrypt a checked package under the code's package key; `nonce` only for test vectors."""
    plaintext = body_json(check_package(package))
    return b64u_encode(aead.seal_blob(secrets.package_key, plaintext, package_aad(secrets.invite_id), nonce))


def open_package(secrets: InviteSecrets, sealed: str) -> dict[str, object]:
    """Decrypt (`invite_code_invalid` if the code does not open it) and check the package."""
    blob = b64u_decode(sealed)
    plaintext: bytes | None
    try:
        plaintext = aead.open_blob(secrets.package_key, blob, package_aad(secrets.invite_id))
    except CryptoError:
        plaintext = None
    if plaintext is None:
        fail("invite_code_invalid")
    return check_package(parse_json(plaintext))


def register_text(user_id: str, challenge: bytes) -> bytes:
    """`P || "invite-register\\0" || user_id || "\\0" || challenge` (the registration proof, ADR-043)."""
    if not isinstance(user_id, str) or not user_id:
        fail("header_invalid")
    if not is_printable_ascii(user_id):
        fail("non_ascii")
    return P + LABEL_INVITE_REGISTER + user_id.encode("ascii") + b"\x00" + require_bytes(challenge, CHALLENGE_BYTES)


def sign_register(secrets: InviteSecrets, user_id: str, challenge: bytes) -> str:
    """The b64u `inv_sig` of `register/finish` by `sk_inv` of the code."""
    return b64u_encode(ed25519_sign(secrets.signing, register_text(user_id, challenge)))


def verify_register(pk_inv: bytes, user_id: str, challenge: bytes, inv_sig: object) -> None:
    """Check `inv_sig` under the invitation's `pk_inv` (`signature_invalid`)."""
    ed25519_verify_b64u(pk_inv, inv_sig, register_text(user_id, challenge))
