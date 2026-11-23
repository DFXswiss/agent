"""Copied from agent-core src/agent_hub/crypto/agent.py at 9d06f916bd4d.

Delegated agent access (11-agent-access.md section 3, ADR-056).

- `fingerprint(kem_pub, sig_pub)`: `b64u(SHA-256(P || "agent-key\\0" || kem_pub || sig_pub)[:16])`, carried in the
  fragment of the pairing link so that the browser can check the keys the hub returns.
- `pop_text`/`sign_pop`/`verify_pop`: the agent's proof of possession of its Ed25519 key,
  `P || "agent-pair\\0" || canonical({"kem", "pairing_id", "sig"})`.
- The handover secret `r` (32 random bytes, drawn by the confirming browser, given to the agent only over the
  loopback) yields the bundle key `k = HKDF(r, P || "agent-bundle-key\\0" || canonical({"agent", "user"}))` and the
  claim `HKDF(r, P || "agent-claim\\0" || canonical({"agent", "user"}))`; the hub stores only
  `claim_hash = SHA-256(P || "agent-claim-hash\\0" || claim)`.
- `seal_identity`/`open_identity`: the user's identity bundle plaintext (04-crypto.md section 4.1), first encrypted
  under `k` (`nonce || AES-GCM(k, plaintext, AAD = info)`), then sealed with HPKE to the agent's X25519 key,
  `info = P || "agent-bundle\\0" || canonical({"agent", "user"})`. Opening needs the agent's private key **and** `r`:
  a hub that swapped the agent's key, or that put its own key into a link, still lacks `r`; and a hub cannot make the
  agent accept an identity of its own choosing.
- `seal_label`/`open_label`: the label `{"host", "name"}` (body JSON) sealed with HPKE to the user's identity X25519
  key, `info = P || "agent-label\\0" || canonical({"agent", "user"})`.

The hub uses only `verify_pop`, `claim_hash` and the format checks; sealing and opening belong to the browser and the
agent tool (this module is the reference for the vectors).
"""

from __future__ import annotations

import hashlib
import unicodedata

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from agent_runner.crypto import aead, hpke
from agent_runner.crypto.bundle import bundle_plaintext, parse_bundle_plaintext
from agent_runner.crypto.encoding import (
    P,
    b64u_decode,
    b64u_encode,
    body_json,
    canonical,
    parse_json,
    require_bytes,
    require_keys,
)
from agent_runner.crypto.errors import fail
from agent_runner.crypto.kdf import hkdf
from agent_runner.crypto.keys import RAW_KEY_BYTES, Identity, ed25519_sign, ed25519_verify_b64u

LABEL_AGENT_KEY = b"agent-key\x00"
LABEL_AGENT_PAIR = b"agent-pair\x00"
LABEL_AGENT_BUNDLE = b"agent-bundle\x00"
LABEL_AGENT_BUNDLE_KEY = b"agent-bundle-key\x00"
LABEL_AGENT_LABEL = b"agent-label\x00"
LABEL_AGENT_CLAIM = b"agent-claim\x00"
LABEL_AGENT_CLAIM_HASH = b"agent-claim-hash\x00"
FINGERPRINT_BYTES = 16
SECRET_BYTES = 32
LABEL_TEXT_MAX = 64
_LABEL_KEYS = frozenset({"host", "name"})
# Unicode categories a label must not contain: controls, format characters (bidi overrides, zero-width), line and
# paragraph separators, surrogates, private use and unassigned code points.
_LABEL_REFUSED_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs", "Co", "Cn"})


def fingerprint(kem_pub: bytes, sig_pub: bytes) -> str:
    """The 22-character fingerprint of an agent's public keys."""
    require_bytes(kem_pub, RAW_KEY_BYTES)
    require_bytes(sig_pub, RAW_KEY_BYTES)
    digest = hashlib.sha256(P + LABEL_AGENT_KEY + kem_pub + sig_pub).digest()
    return b64u_encode(digest[:FINGERPRINT_BYTES])


def pop_text(pairing_id: str, kem_pub: bytes, sig_pub: bytes) -> bytes:
    """The text of the proof of possession."""
    require_bytes(kem_pub, RAW_KEY_BYTES)
    require_bytes(sig_pub, RAW_KEY_BYTES)
    fields = {"kem": b64u_encode(kem_pub), "pairing_id": pairing_id, "sig": b64u_encode(sig_pub)}
    return P + LABEL_AGENT_PAIR + canonical(fields)


def sign_pop(agent: Identity, pairing_id: str) -> str:
    """The agent's b64u Ed25519 signature over `pop_text`."""
    return b64u_encode(ed25519_sign(agent.sig, pop_text(pairing_id, agent.kem_public(), agent.sig_public())))


def verify_pop(pairing_id: str, kem_pub: bytes, sig_pub: bytes, pop: object) -> None:
    """`signature_invalid` unless `pop` is the signature of `sig_pub` over `pop_text`."""
    ed25519_verify_b64u(sig_pub, pop, pop_text(pairing_id, kem_pub, sig_pub))


def _ids(agent_id: str, user_id: str) -> bytes:
    return canonical({"agent": agent_id, "user": user_id})


def identity_info(agent_id: str, user_id: str) -> bytes:
    """The HPKE `info` (and inner AAD) of the identity sealed to the agent."""
    return P + LABEL_AGENT_BUNDLE + _ids(agent_id, user_id)


def label_info(agent_id: str, user_id: str) -> bytes:
    """The HPKE `info` of the label sealed to the user."""
    return P + LABEL_AGENT_LABEL + _ids(agent_id, user_id)


def bundle_key(secret: bytes, agent_id: str, user_id: str) -> bytes:
    """`k = HKDF(r, P || "agent-bundle-key\\0" || canonical({"agent", "user"}))`."""
    require_bytes(secret, SECRET_BYTES)
    return hkdf(secret, P + LABEL_AGENT_BUNDLE_KEY + _ids(agent_id, user_id))


def claim(secret: bytes, agent_id: str, user_id: str) -> bytes:
    """The claim the agent sends: `HKDF(r, P || "agent-claim\\0" || canonical({"agent", "user"}))`."""
    require_bytes(secret, SECRET_BYTES)
    return hkdf(secret, P + LABEL_AGENT_CLAIM + _ids(agent_id, user_id))


def claim_hash(claim_raw: bytes) -> bytes:
    """`SHA-256(P || "agent-claim-hash\\0" || claim)` of a 32-byte claim (what the hub stores)."""
    require_bytes(claim_raw, SECRET_BYTES)
    return hashlib.sha256(P + LABEL_AGENT_CLAIM_HASH + claim_raw).digest()


def seal_identity(
    agent_kem_pub: bytes,
    secret: bytes,
    agent_id: str,
    user_id: str,
    identity: Identity,
    *,
    nonce: bytes | None = None,
    ephemeral: X25519PrivateKey | None = None,
) -> str:
    """The user's identity under `k` and sealed to the agent (`nonce`, `ephemeral` only for test vectors)."""
    info = identity_info(agent_id, user_id)
    inner = aead.seal_blob(bundle_key(secret, agent_id, user_id), bundle_plaintext(identity), info, nonce)
    return b64u_encode(hpke.seal(agent_kem_pub, info, inner, ephemeral))


def open_identity(agent_kem: X25519PrivateKey, secret: bytes, agent_id: str, user_id: str, sealed: str) -> Identity:
    """The inverse of `seal_identity` (`hpke_short`, `decrypt_failed`, `header_invalid`)."""
    info = identity_info(agent_id, user_id)
    inner = hpke.open_(agent_kem, info, b64u_decode(sealed))
    return parse_bundle_plaintext(aead.open_blob(bundle_key(secret, agent_id, user_id), inner, info))


def _label_text_valid(text: object) -> bool:
    return (
        isinstance(text, str)
        and 1 <= len(text) <= LABEL_TEXT_MAX
        and text == text.strip()
        and not any(unicodedata.category(char) in _LABEL_REFUSED_CATEGORIES for char in text)
    )


def check_label(value: object) -> dict[str, str]:
    """Exactly `{"host", "name"}`, each 1-64 characters, no leading or trailing space, no control, format, separator,
    private-use or unassigned characters (`header_invalid`)."""
    label = require_keys(value, _LABEL_KEYS)
    if not all(_label_text_valid(label[key]) for key in _LABEL_KEYS):
        fail("header_invalid")
    return {"host": str(label["host"]), "name": str(label["name"])}


def seal_label(
    user_kem_pub: bytes,
    agent_id: str,
    user_id: str,
    label: object,
    ephemeral: X25519PrivateKey | None = None,
) -> str:
    """The label sealed to the user's identity (`ephemeral` only for test vectors)."""
    info = label_info(agent_id, user_id)
    return b64u_encode(hpke.seal(user_kem_pub, info, body_json(check_label(label)), ephemeral))


def open_label(user_kem: X25519PrivateKey, agent_id: str, user_id: str, sealed: str) -> dict[str, str]:
    """The inverse of `seal_label`."""
    plaintext = hpke.open_(user_kem, label_info(agent_id, user_id), b64u_decode(sealed))
    label = check_label(parse_json(plaintext))
    if body_json(label) != plaintext:
        fail("header_invalid")
    return label


def new_agent() -> Identity:
    """Fresh agent key pairs (X25519 and Ed25519)."""
    return Identity(X25519PrivateKey.generate(), Ed25519PrivateKey.generate())
