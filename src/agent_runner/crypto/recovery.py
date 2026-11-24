"""Copied from src/agent_hub/crypto/recovery.py.

Recovery codes (04-crypto.md sections 3, 4.1 and 5.5).

`recovery_id = b64u(HKDF(code16, info=P||"recovery-id")[:16])` **(new)**, `KEK_rec` in `agent_hub.crypto.kdf`, and the
proof `Ed25519_identity(P || "recovery-proof\\0" || user_id || "\\0" || challenge)` over the raw 32-byte challenge of
`POST /v1/recovery/begin` **(new)**. The code text has the form of invitation codes; a wrong length is
`recovery_code_length`.
"""

from __future__ import annotations

import os

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_runner.crypto.encoding import P, b64u_encode, is_printable_ascii, require_bytes
from agent_runner.crypto.errors import fail
from agent_runner.crypto.invite import format_code, parse_code_text
from agent_runner.crypto.kdf import CODE_BYTES, LABEL_RECOVERY_PROOF, recovery_id_raw
from agent_runner.crypto.keys import ed25519_sign, ed25519_verify_b64u

CHALLENGE_BYTES = 32


def generate_recovery_code() -> bytes:
    """16 random bytes."""
    return os.urandom(CODE_BYTES)


def format_recovery_code(code: bytes) -> str:
    """The grouped base32 text (`recovery_code_length` unless 16 bytes)."""
    require_bytes(code, CODE_BYTES, "recovery_code_length")
    return format_code(code)


def parse_recovery_code(text: object) -> bytes:
    """The 16 bytes of a recovery code text."""
    return parse_code_text(text, "recovery_code_length")


def recovery_id(code: bytes) -> str:
    """The b64u ID under which the hub finds the recovery slot (it never sees the code)."""
    return b64u_encode(recovery_id_raw(code))


def proof_text(user_id: str, challenge: bytes) -> bytes:
    """`P || "recovery-proof\\0" || user_id || "\\0" || challenge`."""
    if not isinstance(user_id, str) or not user_id:
        fail("header_invalid")
    if not is_printable_ascii(user_id):
        fail("non_ascii")
    return P + LABEL_RECOVERY_PROOF + user_id.encode("ascii") + b"\x00" + require_bytes(challenge, CHALLENGE_BYTES)


def sign_proof(identity_sig: Ed25519PrivateKey, user_id: str, challenge: bytes) -> str:
    """The b64u proof signature of the unlocked identity."""
    return b64u_encode(ed25519_sign(identity_sig, proof_text(user_id, challenge)))


def verify_proof(identity_sig_public: bytes, user_id: str, challenge: bytes, proof: str) -> None:
    """Check the proof against the registered identity (`signature_invalid`)."""
    ed25519_verify_b64u(identity_sig_public, proof, proof_text(user_id, challenge))
