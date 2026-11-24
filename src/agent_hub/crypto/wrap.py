"""Key wrap (04-crypto.md section 4.2), after Spike D `e2ee.wrap_info`/`wrap_scope_key`/`unwrap_scope_key`.

`wrap = b64u(enc || ct)`: HPKE single-shot seal of the 32-byte scope key to the recipient's X25519 key with
`info = P || "wrap\\0" || canonical({"epoch","recipient","scope","scope_id"})` and empty AAD. The wrapper signs
`P || "wrap\\0" || canonical({"epoch","recipient","scope","scope_id","wrap"})` with its Ed25519 key **(new)**.
Who may wrap for whom (W1-W3) is decided by `agent_hub.log`, not here.
"""

from __future__ import annotations

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from agent_hub.crypto import hpke
from agent_hub.crypto.encoding import P, b64u_decode, b64u_encode, canonical, require_bytes, require_int
from agent_hub.crypto.errors import fail
from agent_hub.crypto.kdf import KEY_BYTES, LABEL_WRAP
from agent_hub.crypto.keys import ed25519_sign, ed25519_verify_b64u

SCOPES = frozenset({"org", "wg", "cred", "community"})
"""`community` is the scope of the community key (14-community.md section 3, ADR-069)."""


def _fields(scope: str, scope_id: str, epoch: int, recipient: str) -> dict[str, object]:
    if scope not in SCOPES or not isinstance(scope_id, str) or not isinstance(recipient, str):
        fail("header_invalid")
    if not scope_id or not recipient:
        fail("header_invalid")
    return {"epoch": require_int(epoch, 1), "recipient": recipient, "scope": scope, "scope_id": scope_id}


def wrap_info(scope: str, scope_id: str, epoch: int, recipient: str) -> bytes:
    """The HPKE `info` that binds a wrap to scope, scope ID, epoch and recipient."""
    return P + LABEL_WRAP + canonical(_fields(scope, scope_id, epoch, recipient))


def wrap_key(
    recipient_kem_public: bytes,
    *,
    scope: str,
    scope_id: str,
    epoch: int,
    recipient: str,
    key: bytes,
    ephemeral: X25519PrivateKey | None = None,
) -> str:
    """Wrap a 32-byte scope key for one recipient; `ephemeral` only for test vectors."""
    require_bytes(key, KEY_BYTES)
    info = wrap_info(scope, scope_id, epoch, recipient)
    return b64u_encode(hpke.seal(recipient_kem_public, info, key, ephemeral))


def unwrap_key(
    recipient_kem: X25519PrivateKey, *, scope: str, scope_id: str, epoch: int, recipient: str, wrap: str
) -> bytes:
    """Open a wrap (`hpke_short`, `decrypt_failed`); anything but a 32-byte key is `decrypt_failed`."""
    info = wrap_info(scope, scope_id, epoch, recipient)
    key = hpke.open_(recipient_kem, info, b64u_decode(wrap))
    if len(key) != KEY_BYTES:
        fail("decrypt_failed")
    return key


def wrap_signature_text(scope: str, scope_id: str, epoch: int, recipient: str, wrap: str) -> bytes:
    """`P || "wrap\\0" || canonical({epoch, recipient, scope, scope_id, wrap})`."""
    fields = _fields(scope, scope_id, epoch, recipient)
    b64u_decode(wrap)
    return P + LABEL_WRAP + canonical({**fields, "wrap": wrap})


def sign_wrap(wrapper: Ed25519PrivateKey, *, scope: str, scope_id: str, epoch: int, recipient: str, wrap: str) -> str:
    """The wrapper's b64u Ed25519 signature over `wrap_signature_text`."""
    return b64u_encode(ed25519_sign(wrapper, wrap_signature_text(scope, scope_id, epoch, recipient, wrap)))


def verify_wrap(
    wrapper_sig_public: bytes,
    *,
    scope: str,
    scope_id: str,
    epoch: int,
    recipient: str,
    wrap: str,
    signature: str,
) -> None:
    """Check the wrapper's signature (`signature_invalid`); the hub can do this without any key."""
    ed25519_verify_b64u(wrapper_sig_public, signature, wrap_signature_text(scope, scope_id, epoch, recipient, wrap))
