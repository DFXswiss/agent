"""Copied from agent-core src/agent_hub/crypto/request.py at 9d06f916bd4d.

Device requests and responses (04-crypto.md section 4.5, new).

Envelope `{"header": {...}, "ct": b64u(enc || ct), "sig": b64u(...)}`: HPKE single shot to the recipient's X25519 key
(the device for requests, the user identity for responses) with `info = P || "request-wrap\\0" || canonical(header)`
and empty AAD; `sig = Ed25519_sender(P || "request-sig\\0" || canonical(header) || ct_raw)`.
Request header key set exactly `REQUEST_KEYS`, response header key set exactly `RESPONSE_KEYS`. Check order as in
`agent_hub.crypto.signed`, with `hpke_short`/`decrypt_failed` from the HPKE layer last.
"""

from __future__ import annotations

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from agent_runner.crypto import fields, hpke
from agent_runner.crypto.encoding import P, b64u_decode, b64u_encode, canonical, require_int, require_keys
from agent_runner.crypto.errors import fail
from agent_runner.crypto.kdf import LABEL_REQUEST_SIG, LABEL_REQUEST_WRAP
from agent_runner.crypto.keys import ed25519_sign, ed25519_verify_b64u

VERSION = 1
ENC_ALG = "HPKE-X25519-A256GCM"
REQUEST_KEYS = frozenset({"v", "id", "user", "device", "kind", "follows", "ts", "enc_alg"})
RESPONSE_KEYS = frozenset({"v", "id", "user", "device", "kind", "in_reply_to", "seq", "final", "ts", "enc_alg"})
_ENVELOPE_KEYS = frozenset({"header", "ct", "sig"})
MAX_CT_BYTES = 1024 * 1024
"""Bounded by the signed request body limit of 03-api.md section 1."""


def _common(header: dict[str, object]) -> None:
    fields.literal(header["v"], VERSION)
    fields.literal(header["enc_alg"], ENC_ALG)
    fields.uuid(header["id"])
    fields.uuid(header["user"])
    fields.uuid(header["device"])
    fields.kind(header["kind"])
    fields.time(header["ts"])


def check_request_header(value: object) -> dict[str, object]:
    """Exact request key set and value formats (`follows` is a request ID or `""`)."""
    header = fields.header_dict(value, REQUEST_KEYS)
    _common(header)
    fields.uuid_or_empty(header["follows"])
    return header


def check_response_header(value: object) -> dict[str, object]:
    """Exact response key set and value formats (`seq` from 1, `final` boolean)."""
    header = fields.header_dict(value, RESPONSE_KEYS)
    _common(header)
    fields.uuid(header["in_reply_to"])
    require_int(header["seq"], 1)
    if not isinstance(header["final"], bool):
        fail("header_invalid")
    return header


def wrap_info(header: dict[str, object]) -> bytes:
    """The HPKE `info`: `P || "request-wrap\\0" || canonical(header)`."""
    return P + LABEL_REQUEST_WRAP + canonical(header)


def signature_text(header: dict[str, object], ct_raw: bytes) -> bytes:
    """`P || "request-sig\\0" || canonical(header) || ct_raw`."""
    return P + LABEL_REQUEST_SIG + canonical(header) + ct_raw


def _seal(
    header: dict[str, object],
    recipient_kem_public: bytes,
    plaintext: bytes,
    sender: Ed25519PrivateKey,
    ephemeral: X25519PrivateKey | None,
) -> dict[str, object]:
    ct = hpke.seal(recipient_kem_public, wrap_info(header), plaintext, ephemeral)
    if len(ct) > MAX_CT_BYTES:
        fail("size_exceeded")
    return {
        "header": header,
        "ct": b64u_encode(ct),
        "sig": b64u_encode(ed25519_sign(sender, signature_text(header, ct))),
    }


def _verify(envelope: object, response: bool, sender_sig_public: bytes) -> tuple[dict[str, object], bytes]:
    data = require_keys(envelope, _ENVELOPE_KEYS)
    header = check_response_header(data["header"]) if response else check_request_header(data["header"])
    ct = b64u_decode(data["ct"])
    if len(ct) > MAX_CT_BYTES:
        fail("size_exceeded")
    ed25519_verify_b64u(sender_sig_public, data["sig"], signature_text(header, ct))
    return header, ct


def seal_request(
    header: object,
    device_kem_public: bytes,
    plaintext: bytes,
    user: Ed25519PrivateKey,
    ephemeral: X25519PrivateKey | None = None,
) -> dict[str, object]:
    """Encrypt a request body to the device and sign it with the user identity; `ephemeral` for vectors only."""
    return _seal(check_request_header(header), device_kem_public, plaintext, user, ephemeral)


def verify_request(envelope: object, user_sig_public: bytes) -> dict[str, object]:
    """Structure, header, size and the user's signature (what the hub checks); returns the header."""
    return _verify(envelope, False, user_sig_public)[0]


def open_request(device_kem: X25519PrivateKey, envelope: object, user_sig_public: bytes) -> bytes:
    """`verify_request`, then decrypt with the device X25519 key."""
    header, ct = _verify(envelope, False, user_sig_public)
    return hpke.open_(device_kem, wrap_info(header), ct)


def seal_response(
    header: object,
    user_kem_public: bytes,
    plaintext: bytes,
    device: Ed25519PrivateKey,
    ephemeral: X25519PrivateKey | None = None,
) -> dict[str, object]:
    """Encrypt a response body to the user identity and sign it with the device key."""
    return _seal(check_response_header(header), user_kem_public, plaintext, device, ephemeral)


def verify_response(envelope: object, device_sig_public: bytes) -> dict[str, object]:
    """Structure, header, size and the device's signature; returns the header."""
    return _verify(envelope, True, device_sig_public)[0]


def open_response(user_kem: X25519PrivateKey, envelope: object, device_sig_public: bytes) -> bytes:
    """`verify_response`, then decrypt with the user identity X25519 key."""
    header, ct = _verify(envelope, True, device_sig_public)
    return hpke.open_(user_kem, wrap_info(header), ct)
