"""Copied from agent-core src/agent_hub/crypto/enroll.py at 3392035c31d1.

Enrollment text of platform devices (04-crypto.md section 3, new; body of `POST /v1/device/enroll`).

Text `P || "enroll\\0" || canonical(body without enroll_sig and inv_sig)`. `enroll_sig` is the Ed25519 signature of
the device identity (`body.pub.sig`), `inv_sig` the signature of `sk_inv`, both over this text. `device_certify`
copies both so that browsers can check them. The body schema itself is the API contract (`contracts/`).
"""

from __future__ import annotations

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_runner.crypto import fields
from agent_runner.crypto.encoding import P, b64u_decode, b64u_encode, canonical
from agent_runner.crypto.errors import fail
from agent_runner.crypto.kdf import LABEL_ENROLL
from agent_runner.crypto.keys import RAW_KEY_BYTES, ed25519_sign, ed25519_verify_b64u

SIGNATURE_FIELDS = frozenset({"enroll_sig", "inv_sig"})
_PUB_KEYS = frozenset({"kem", "sig"})


def _body(body: object) -> dict[str, object]:
    if not isinstance(body, dict):
        fail("header_invalid")
    return body


def _unsigned(body: object) -> dict[str, object]:
    return {key: value for key, value in _body(body).items() if key not in SIGNATURE_FIELDS}


def enrollment_text(body: object) -> bytes:
    """The signed text; the signature fields are left out whether present or not."""
    return P + LABEL_ENROLL + canonical(_unsigned(body))


def _device_sig_public(body: dict[str, object]) -> bytes:
    pub = fields.header_dict(body.get("pub"), _PUB_KEYS)
    fields.b64u_bytes(pub["kem"], RAW_KEY_BYTES)
    return b64u_decode(pub["sig"], RAW_KEY_BYTES)


def sign_enrollment(body: object, device: Ed25519PrivateKey, invite: Ed25519PrivateKey) -> dict[str, object]:
    """The body with `enroll_sig` (device identity) and `inv_sig` (`sk_inv`) over the enrollment text."""
    unsigned = _unsigned(body)
    if _device_sig_public(unsigned) != device.public_key().public_bytes_raw():
        fail("header_invalid")
    text = enrollment_text(unsigned)
    return {
        **unsigned,
        "enroll_sig": b64u_encode(ed25519_sign(device, text)),
        "inv_sig": b64u_encode(ed25519_sign(invite, text)),
    }


def verify_enrollment(body: object, pk_inv: bytes) -> None:
    """Check `enroll_sig` under `body.pub.sig` and `inv_sig` under `pk_inv` (`signature_invalid`)."""
    signed = _body(body)
    unsigned = _unsigned(signed)
    device_public = _device_sig_public(unsigned)
    text = enrollment_text(unsigned)
    ed25519_verify_b64u(device_public, signed.get("enroll_sig"), text)
    ed25519_verify_b64u(pk_inv, signed.get("inv_sig"), text)
